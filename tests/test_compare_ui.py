import os
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QObject, Qt
from PySide6.QtWidgets import QApplication

from app import backtest, settings
from app.backtest import main
from app.compare_ui import MAX_TICKED, CompareWindow, RunWorker
from app.runs import RunRecord, Settings, list_runs, save_run
from app.store import DailyBar, Store
from tests.backtest_cases import D, all_in, plain
from tests.test_daily import FakeActions, FakeClient, fake_day
from tests.test_feed import HTTPError

STRATEGY = "tests/fixtures/buy_spy.py"
SETTINGS = Settings(start=D[0], end=D[3], capital=100_000.0, cost_bps=0.0, dividends=True)


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


class Sink(QObject):
    """Receives the worker's signals through bound methods, so Qt queues them onto this (the main) thread."""

    def __init__(self, worker, join=True):
        super().__init__()
        self.join = join
        self.progress, self.finished, self.failed, self.stopped, self.threads = [], [], [], [], set()
        self.on_progress = None
        self.worker, self.at_end = worker, []  # at_end: what a terminal slot sees when it runs
        worker.progressed.connect(self.got_progress)
        worker.finished.connect(self.got_finished)
        worker.failed.connect(self.got_failed)
        worker.stopped.connect(self.got_stopped)

    def got_progress(self, stage, done, total):
        self.threads.add(threading.current_thread())
        self.progress.append((stage, done, total))
        if self.on_progress:
            self.on_progress(stage, done, total)

    def snapshot(self):
        if self.join:
            self.worker._thread.join(timeout=1)  # the terminal signal is the thread's last statement
        root = self.worker.root
        self.at_end.append((self.worker._thread.is_alive(), self.worker.busy,
                            sorted(p.name for p in root.iterdir()) if root.exists() else []))

    def got_finished(self, record):
        self.snapshot()
        self.threads.add(threading.current_thread())
        self.finished.append(record)

    def got_failed(self, text):
        self.snapshot()
        self.failed.append(text)

    def got_stopped(self):
        self.snapshot()
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


def worker_for(fakes, strategy=None, settings_=SETTINGS, join=True):
    w = RunWorker(str(strategy or fakes.strategy), settings_, fakes.make_clients, fakes.db_path, fakes.root, "0.1.4")
    return w, Sink(w, join)


def wait(condition, timeout=10.0):
    end = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < end, "timed out waiting for the worker"
        QApplication.processEvents()
        time.sleep(0.005)


def run_to_end(worker, sink):
    worker.start()
    wait(lambda: sink.finished or sink.failed or sink.stopped)
    wait(lambda: not worker.busy)  # still needed: the thread may be a moment from exiting when the slot has run
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
    assert str(fakes.strategy) in sink.failed[0] and ".strategy-" not in sink.failed[0]  # the user's file, not the scratch copy


@pytest.mark.parametrize("how", ["finished", "failed", "stopped"])
def test_terminal_signal_comes_after_cleanup(qapp, tmp_path, fakes, how):
    if how == "failed":
        fakes.strategy.write_text("x = 1\n")
    w, sink = worker_for(fakes)
    if how == "stopped":
        fakes.stock.hook = w.cancel
    run_to_end(w, sink)
    assert bool(sink.finished) == (how == "finished") and bool(sink.failed) == (how == "failed")
    assert bool(sink.stopped) == (how == "stopped")
    [(alive, busy, left)] = sink.at_end
    assert not alive and not busy  # the thread had ended by the time the slot could join it
    assert not [n for n in left if n.startswith(".")]  # no scratch folder (a saved run folder may be there)


def test_download_failure_saves_nothing(qapp, tmp_path, fakes, monkeypatch):
    bad = FakeClient(errors=[HTTPError(403)])
    fakes.make_clients = lambda: (bad, FakeActions())
    w, sink = worker_for(fakes)
    run_to_end(w, sink)
    [text] = sink.failed
    assert text.startswith("Could not download daily data")
    assert list_runs(fakes.root) == [] and not sink.finished


def test_slow_cleanup_finishes_before_the_terminal_signal(qapp, tmp_path, fakes, monkeypatch):
    from app import compare_ui
    real = compare_ui.shutil.rmtree

    def slow_rmtree(*args, **kwargs):
        time.sleep(0.3)  # an early terminal signal would reach its slot during this wait
        real(*args, **kwargs)

    monkeypatch.setattr(compare_ui.shutil, "rmtree", slow_rmtree)
    w, sink = worker_for(fakes, join=False)  # a joining sink slot would block this thread past the cleanup
    seen = []

    class Probe(QObject):
        def look(self, *args):
            seen.append(sorted(p.name for p in fakes.root.iterdir() if p.name.startswith(".")))

    probe = Probe()
    w.finished.connect(probe.look)
    run_to_end(w, sink)
    assert seen == [[]]


# --- CompareWindow ---
NOW = datetime(2026, 11, 8, 12, 0, 0, tzinfo=timezone.utc)


def save_one(root, i, capital=100_000.0, cost_bps=0.0, start=D[0], end=D[3]):
    result = backtest.run(all_in("A"), plain, start, end, capital, cost_bps)
    settings_ = Settings(start, end, capital, cost_bps, True)
    return save_run(root, result, settings_, f"/x/s{i}.py", b"x = 1\n", D[3], "0.1.4", now=NOW + timedelta(seconds=i))


@pytest.fixture
def make_window(qapp, tmp_path):
    made = []

    def make(runs=3, spy=True, spy_growth=1.1, **save_kw):
        root = tmp_path / "runs"
        for i in range(runs):
            save_one(root, i, cost_bps=i * 100.0, **save_kw)
        store = Store(str(tmp_path / "bars.db"))
        if spy:
            store.replace_daily("SPY", [DailyBar("SPY", d, c, c, c, c, 1.0, 1.0, 1.0)
                                        for d, c in zip(D, (100 * spy_growth ** i for i in range(4)))])
            store.set_daily_through(D[3])
        w = CompareWindow(str(tmp_path / "bars.db"), root, lambda: None, "0.1.4")
        made.append(w)
        return w

    yield make
    for w in made:
        wait(lambda: not w.workers)
        w.close()


def lines(w):
    return [(i.opts.get("name"), *i.getData()) for i in w.plot.plotItem.listDataItems()]


def spy_ready(w):
    seen = []
    w.benchmark_ready.connect(lambda: seen.append(1))
    wait(lambda: seen)


def test_seventh_tick_is_refused(make_window):
    w = make_window(runs=7)
    for i in range(7):
        w.tick(i)
    assert len(w.ticked()) == MAX_TICKED == 6 and "6" in w.message()


def test_chart_has_one_line_per_ticked_run_plus_spy(make_window):
    w = make_window()
    assert lines(w) == [] and w.plot.plotItem.titleLabel.text == "Tick a run to compare"
    w.tick(0)
    w.tick(1)
    assert len(lines(w)) == 2 and w.spy_note.text() == "SPY: loading…"
    spy_ready(w)
    assert len(lines(w)) == 3 and lines(w)[-1][0] == f"SPY (data to {D[3]})" and w.spy_note.text() == ""


def test_tick_change_returns_before_the_benchmark_is_computed(make_window, monkeypatch):
    from app import compare_ui
    real, gate = compare_ui.run, threading.Event()
    monkeypatch.setattr(compare_ui, "run", lambda *a, **k: (gate.wait(10), real(*a, **k))[1])
    w = make_window()
    w.tick(0)  # returns while the benchmark blocks
    assert len(lines(w)) == 1 and w.spy_note.text() == "SPY: loading…" and w.table.columnCount() == 1
    gate.set()
    spy_ready(w)
    assert len(lines(w)) == 2 and w.table.columnCount() == 2


def test_stale_benchmark_result_is_dropped(make_window, monkeypatch):
    from app import compare_ui
    real = compare_ui.run
    gates = {}

    def gated(strategy, bars, start, end, *args, **kwargs):
        gates.setdefault((start, end), threading.Event()).wait(10)
        return real(strategy, bars, start, end, *args, **kwargs)

    monkeypatch.setattr(compare_ui, "run", gated)
    w = make_window(runs=0)
    save_one(w.root, 0, end=D[2])  # A: D0..D2
    save_one(w.root, 1, start=D[1])  # B: D1..D3
    w.reload()
    seen = []
    w.benchmark_ready.connect(lambda: seen.append(1))
    w.tick(1)  # list is newest first: row 0 is B, row 1 is A
    w.tick(0)
    wait(lambda: len(gates) == 2)
    gates[(D[0], D[3])].set()  # the A+B result arrives first
    wait(lambda: seen)
    gates[(D[0], D[2])].set()  # the A-only result arrives last
    wait(lambda: len(w.workers) == 0)
    QApplication.processEvents()
    assert len(seen) == 1 and len(lines(w)[-1][1]) == 4  # still the 4-day A+B line


def test_single_day_run_shows_dash_for_growth(make_window):
    w = make_window(runs=1, start=D[0], end=D[0])
    w.tick(0)
    assert w.table.item(1, 0).text() == "—" and w.table.item(0, 0).text() == "+0.0%"


def test_lines_start_at_100k_whatever_the_capital(make_window):
    w = make_window(runs=0)
    save_one(w.root, 0, capital=50_000.0)
    save_one(w.root, 1, capital=200_000.0)
    w.reload()
    w.tick(0)
    w.tick(1)
    spy_ready(w)
    assert len(lines(w)) == 3 and all(ys[0] == pytest.approx(100_000.0) for _, _, ys in lines(w))


def test_table_bolds_best_but_never_spy(make_window):
    w = make_window(spy_growth=2.0)  # SPY beats every run
    for i in range(3):
        w.tick(i)
    spy_ready(w)
    bold = lambda r, c: w.table.item(r, c).font().bold()
    assert w.table.horizontalHeaderItem(3).text() == "SPY" and w.table.rowCount() == 5
    # row 0 is the newest run (cost 200 bps), so the cheapest run is column 2
    assert [bold(0, c) for c in range(4)] == [False, False, True, False]
    assert not any(bold(r, 3) for r in range(5)) and not any(bold(r, c) for r in (3, 4) for c in range(4))


def test_spy_disabled_without_daily_data(make_window):
    w = make_window(spy=False)
    w.tick(0)
    assert not w.spy_item.flags() & Qt.ItemIsEnabled and w.spy_item.toolTip() and not w.workers
    assert len(lines(w)) == 1 and w.table.columnCount() == 1


def test_unreadable_run_is_listed_but_cannot_be_ticked(make_window):
    w = make_window(runs=2)
    (w.records[1].folder / "portfolio.csv").write_text("garbage")
    w.reload()
    assert len(w.records) == 2 and not w.records[1].readable and "damaged" in w.list.item(1).text()
    assert not w.list.item(1).flags() & Qt.ItemIsUserCheckable
    w.tick(1)
    assert w.ticked() == []


def test_drawdown_mode_plots_drawdown(make_window):
    w = make_window()
    w.tick(0)
    spy_ready(w)
    w.set_mode("drawdown")
    assert len(lines(w)) == 2 and all(max(ys) <= 0 for _, _, ys in lines(w))


def test_rename_rejects_a_blank_name_and_renames(make_window):
    w = make_window(runs=1)
    assert not w.rename_row(0, "   ") and "name" in w.message() and w.records[0].name == "s0.py"
    assert w.rename_row(0, " mine ") and w.records[0].name == "mine" and "mine" in w.list.item(0).text()


def test_delete_removes_the_run_and_its_tick(make_window):
    w = make_window(runs=2)
    w.tick(0)
    assert w.delete_row(0) and len(w.records) == 1 and w.ticked() == []
