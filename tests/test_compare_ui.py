import os
import threading
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QObject
from PySide6.QtWidgets import QApplication

from app import backtest, settings
from app.backtest import main
from app.compare_ui import RunWorker
from app.runs import RunRecord, Settings, list_runs
from app.store import Store
from tests.backtest_cases import D
from tests.test_daily import FakeActions, FakeClient, fake_day
from tests.test_feed import HTTPError

STRATEGY = "tests/fixtures/buy_spy.py"
SETTINGS = Settings(start=D[0], end=D[3], capital=100_000.0, cost_bps=0.0, dividends=True)


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


class Sink(QObject):
    """Receives the worker's signals through bound methods, so Qt queues them onto this (the main) thread."""

    def __init__(self, worker):
        super().__init__()
        self.progress, self.finished, self.failed, self.stopped, self.threads = [], [], [], [], set()
        self.on_progress = None
        worker.progressed.connect(self.got_progress)
        worker.finished.connect(self.got_finished)
        worker.failed.connect(self.got_failed)
        worker.stopped.connect(self.got_stopped)

    def got_progress(self, stage, done, total):
        self.threads.add(threading.current_thread())
        self.progress.append((stage, done, total))
        if self.on_progress:
            self.on_progress(stage, done, total)

    def got_finished(self, record):
        self.threads.add(threading.current_thread())
        self.finished.append(record)

    def got_failed(self, text):
        self.failed.append(text)

    def got_stopped(self):
        self.stopped.append(True)


class HookedClient(FakeClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.hook = None

    def get_stock_bars(self, req):
        if self.hook:
            self.hook()
        return super().get_stock_bars(req)


@pytest.fixture
def fakes(tmp_path, monkeypatch):
    """Fake clients and a real sqlite file; patches what main() reads so both paths see the same fakes."""
    monkeypatch.setattr(settings, "load_keys", lambda: ("k", "s"))
    monkeypatch.setattr(backtest, "default_db_path", lambda: str(tmp_path / "bars.db"))
    monkeypatch.setattr(backtest, "universe", lambda: ["SPY"])
    clients = HookedClient({"SPY": [fake_day(d) for d in D]}), FakeActions()
    monkeypatch.setattr(backtest, "CorporateActionsClient", lambda *keys: clients[1])
    monkeypatch.setattr(backtest, "StockHistoricalDataClient", lambda *keys: clients[0])
    strategy = tmp_path / "strategy_under_test.py"
    strategy.write_bytes(open(STRATEGY, "rb").read())
    return SimpleNamespace(clients=clients, stock=clients[0], db_path=str(tmp_path / "bars.db"), root=tmp_path / "runs",
                           strategy=strategy, make_clients=lambda: clients)


def worker_for(fakes, strategy=None, settings_=SETTINGS):
    w = RunWorker(str(strategy or fakes.strategy), settings_, fakes.make_clients, fakes.db_path, fakes.root, "0.1.4")
    return w, Sink(w)


def wait(condition, timeout=10.0):
    end = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < end, "timed out waiting for the worker"
        QApplication.processEvents()
        time.sleep(0.005)


def run_to_end(worker, sink):
    worker.start()
    wait(lambda: sink.finished or sink.failed or sink.stopped)
    wait(lambda: not worker.busy)
    QApplication.processEvents()


def test_worker_saves_a_successful_run(qapp, tmp_path, fakes):
    w, sink = worker_for(fakes)
    run_to_end(w, sink)
    [record] = sink.finished
    assert isinstance(record, RunRecord) and record.readable and record.name == "strategy_under_test.py"
    assert [r.id for r in list_runs(fakes.root)] == [record.id]
    assert sink.threads == {threading.main_thread()}  # signals reached the receiver on its own thread
    assert ("run", 4, 4) in sink.progress and any(s == "download" for s, _, _ in sink.progress)
    assert not sink.failed and not sink.stopped


def test_worker_result_matches_command(qapp, tmp_path, fakes):
    assert main([STRATEGY, "--start", D[0], "--end", D[3], "--out", str(tmp_path / "cli")]) == 0
    w, sink = worker_for(fakes)
    run_to_end(w, sink)
    [record] = sink.finished
    for name in ("portfolio.csv", "positions.csv"):
        assert (record.folder / name).read_bytes() == (tmp_path / "cli" / name).read_bytes()


def test_worker_uses_a_real_sqlite_db_from_its_own_thread(qapp, tmp_path, fakes):
    main_store = Store(fakes.db_path)  # made on this thread; the worker must open its own
    w, sink = worker_for(fakes)
    run_to_end(w, sink)
    assert len(sink.finished) == 1 and not sink.failed
    assert os.path.getsize(fakes.db_path) > 0
    assert sink.finished[0].data_through == main_store.daily_through() is not None


def cancel_when(when, w, sink, fakes, monkeypatch):
    if when == "download":
        fakes.stock.hook = w.cancel
    elif when == "last day":
        # the worker thread itself cancels, so the flag is set before the end-of-run check
        real = backtest.run

        def run_then_cancel(*args, progress=None, **kwargs):
            def at_last(done, total):
                progress(done, total)
                if done == total:
                    w.cancel()
            return real(*args, progress=at_last, **kwargs)

        monkeypatch.setattr(backtest, "run", run_then_cancel)
    else:
        from app import compare_ui
        real_save = compare_ui.save_run

        def cancel_then_save(*args, **kwargs):
            w.cancel()
            return real_save(*args, **kwargs)

        monkeypatch.setattr(compare_ui, "save_run", cancel_then_save)


@pytest.mark.parametrize("when", ["download", "last day", "save"])
def test_cancel_saves_nothing(qapp, tmp_path, fakes, monkeypatch, when):
    w, sink = worker_for(fakes)
    cancel_when(when, w, sink, fakes, monkeypatch)
    w.start()
    wait(lambda: sink.stopped or sink.finished or sink.failed)
    wait(lambda: not w.busy)
    assert sink.stopped and not sink.finished and not sink.failed
    assert list_runs(fakes.root) == []
    assert not fakes.root.exists() or list(fakes.root.iterdir()) == []  # no folders, no temp strategy file


def test_saved_copy_is_the_code_that_ran(qapp, tmp_path, fakes):
    original = fakes.strategy.read_bytes()
    w, sink = worker_for(fakes)

    def edit_while_downloading():
        fakes.strategy.write_text("def target_weights(history, as_of):\n    return {}\n")
        fakes.stock.hook = None

    fakes.stock.hook = edit_while_downloading  # runs on the worker thread, after it read the file
    run_to_end(w, sink)
    [record] = sink.finished
    assert fakes.strategy.read_bytes() != original
    assert (record.folder / "strategy.py").read_bytes() == original
    # and the code that ran was the original: buy SPY, not cash
    assert "SPY" in (record.folder / "positions.csv").read_text()


def test_original_deleted_mid_run_still_saves(qapp, tmp_path, fakes):
    w, sink = worker_for(fakes)
    fakes.stock.hook = lambda: fakes.strategy.unlink()
    run_to_end(w, sink)
    assert len(sink.finished) == 1 and len(list_runs(fakes.root)) == 1


def test_strategy_error_names_the_date_and_saves_nothing(qapp, tmp_path, fakes):
    fakes.strategy.write_text("def target_weights(history, as_of):\n    raise RuntimeError('boom')\n")
    w, sink = worker_for(fakes)
    run_to_end(w, sink)
    [text] = sink.failed
    assert D[0] in text and "boom" in text and "\n" not in text
    assert list_runs(fakes.root) == [] and not sink.finished


def test_unloadable_strategy_fails_with_its_own_text(qapp, tmp_path, fakes):
    fakes.strategy.write_text("x = 1\n")
    w, sink = worker_for(fakes)
    run_to_end(w, sink)
    assert "target_weights" in sink.failed[0] and list_runs(fakes.root) == []


def test_download_failure_saves_nothing(qapp, tmp_path, fakes, monkeypatch):
    bad = FakeClient(errors=[HTTPError(403)])
    fakes.make_clients = lambda: (bad, FakeActions())
    w, sink = worker_for(fakes)
    run_to_end(w, sink)
    [text] = sink.failed
    assert text.startswith("Could not download daily data")
    assert list_runs(fakes.root) == [] and not sink.finished
