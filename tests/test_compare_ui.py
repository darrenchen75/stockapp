import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QObject, Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from app import backtest, settings
from app.backtest import main
from app.compare_ui import MAX_TICKED, CompareWindow, RunDetail, RunWorker
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


def count_benchmark_starts(monkeypatch):
    from app import compare_ui
    started, real = [], compare_ui.BenchmarkWorker.start_thread
    monkeypatch.setattr(compare_ui.BenchmarkWorker, "start_thread", lambda self: (started.append(1), real(self)))
    return started


def test_same_dates_start_one_benchmark_worker(make_window, monkeypatch):
    from app import compare_ui
    real, gate = compare_ui.run, threading.Event()
    monkeypatch.setattr(compare_ui, "run", lambda *a, **k: (gate.wait(10), real(*a, **k))[1])
    started = count_benchmark_starts(monkeypatch)
    w = make_window()  # every run spans D0..D3
    w.tick(0)
    w.tick(1)
    assert len(started) == 1 and w.spy_note.text() == "SPY: loading…"
    gate.set()
    spy_ready(w)
    assert len(started) == 1 and len(lines(w)) == 3


def test_new_data_date_starts_a_worker_on_redraw(make_window, monkeypatch):
    w = make_window()
    w.tick(0)
    spy_ready(w)
    started = count_benchmark_starts(monkeypatch)
    w.store.set_daily_through("2026-11-06")  # a download finished meanwhile
    w.set_mode("drawdown")
    assert len(started) == 1
    spy_ready(w)
    assert lines(w)[-1][0] == "SPY (data to 2026-11-06)" and w.spy_note.text() == ""


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


def test_failed_benchmark_says_why_and_is_retried(make_window, monkeypatch):
    from app import compare_ui
    real = compare_ui.run

    def boom(*args, **kwargs):
        raise RuntimeError("disk is locked")

    monkeypatch.setattr(compare_ui, "run", boom)
    w = make_window()
    w.tick(0)
    wait(lambda: not w.workers)
    QApplication.processEvents()
    assert "could not compute" in w.spy_note.text() and "RuntimeError: disk is locked" in w.spy_note.text()
    assert len(lines(w)) == 1 and not w.benchmarks
    monkeypatch.setattr(compare_ui, "run", real)
    w.tick(1)  # a later refresh retries
    spy_ready(w)
    assert len(lines(w)) == 3 and w.spy_note.text() == ""


def test_spy_without_rows_says_no_data(make_window):
    w = make_window()
    w.store.db.execute("DELETE FROM daily_bars")
    w.store.db.commit()
    w.tick(0)
    wait(lambda: not w.workers)
    QApplication.processEvents()
    assert w.spy_note.text() == "SPY: no data" and len(lines(w)) == 1


# --- new run, detail, export ---
CUSTOM = Settings(start=D[0], end=D[3], capital=50_000.0, cost_bps=7.5, dividends=False)


@pytest.fixture
def window_with_runs(qapp, tmp_path, fakes):
    """A window over one saved run whose strategy file still exists, with the fake clients wired in."""
    result = backtest.run(all_in("A"), plain, CUSTOM.start, CUSTOM.end, CUSTOM.capital, CUSTOM.cost_bps, False)
    result.positions.loc[0, "warning"] = "no bar today: target not filled"
    save_run(fakes.root, result, CUSTOM, str(fakes.strategy), fakes.strategy.read_bytes(), D[3], "0.1.4", now=NOW)
    w = CompareWindow(fakes.db_path, fakes.root, fakes.make_clients, "0.1.4")
    w.fakes = fakes
    yield w
    wait(lambda: not w.busy)
    w.close()


def cells(table):
    return [[table.item(r, c).text() for c in range(table.columnCount())] for r in range(table.rowCount())]


def test_rerun_prefills_settings(window_with_runs):
    w = window_with_runs
    dialog = w.rerun(w.records[0].id)
    assert dialog.values() == (str(w.fakes.strategy), CUSTOM) and dialog.note() == ""


def test_new_run_defaults(window_with_runs):
    path, s = window_with_runs.new_run().values()
    assert path == "" and (s.start, s.capital, s.cost_bps, s.dividends) == ("2016-01-04", 100_000.0, 0.0, True)
    assert s.end >= "2026-01-01"  # the last final date, which is never in the future


def test_rerun_uses_saved_copy_when_original_is_gone(window_with_runs):
    w = window_with_runs
    os.remove(w.fakes.strategy)
    dialog = w.rerun(w.records[0].id)
    assert dialog.values()[0] == str(w.records[0].folder / "strategy.py")
    assert "Original file not found; using the copy saved with this run" in dialog.note()
    dialog.run_button.click()
    wait(lambda: not w.busy)
    new = w.records[0]  # newest first
    assert len(w.records) == 2 and new.id != w.records[1].id
    assert new.name == "strategy_under_test.py" and new.strategy_path == str(w.fakes.strategy)


def test_new_and_rerun_disabled_while_busy(window_with_runs):
    w = window_with_runs
    gate = threading.Event()
    w.fakes.stock.hook = lambda: gate.wait(10)  # holds the worker in its download
    w.list.setCurrentRow(0)
    assert w.new_button.isEnabled() and w.rerun_button.isEnabled()
    dialog = w.rerun(w.records[0].id)
    dialog.run_button.click()
    assert w.busy and not w.new_button.isEnabled() and not w.rerun_button.isEnabled()
    assert w.new_run() is None and w.rerun(w.records[0].id) is None
    gate.set()
    wait(lambda: not w.busy)
    assert w.new_button.isEnabled() and w.rerun_button.isEnabled()
    assert len(w.records) == 2 and not dialog.isVisible()  # the new run appears in the list


def test_cancel_run_stops_and_saves_nothing(window_with_runs):
    w = window_with_runs
    gate = threading.Event()
    w.fakes.stock.hook = lambda: gate.wait(10)
    dialog = w.rerun(w.records[0].id)
    dialog.run_button.click()
    dialog.cancel_button.click()
    assert w.busy  # still busy until the worker has stopped
    gate.set()
    wait(lambda: not w.busy)
    assert len(w.records) == 1 and dialog.isVisible() and "Cancelled" in dialog.error.text()


def test_failed_run_shows_one_message_in_the_dialog(window_with_runs):
    w = window_with_runs
    dialog = w.rerun(w.records[0].id)
    dialog.path.setText(str(w.fakes.strategy.parent / "missing.py"))
    dialog.run_button.click()
    wait(lambda: not w.busy)
    assert "missing.py" in dialog.error.text() and dialog.isVisible() and len(w.records) == 1


def test_detail_shows_positions_and_warnings(window_with_runs):
    record = window_with_runs.records[0]
    d = RunDetail(window_with_runs, record)
    assert [d.tabs.tabText(i) for i in range(d.tabs.count())] == ["Positions", "Warnings"]
    assert d.picker.count() == 4  # one rebalance decision per day
    first = cells(d.positions)
    assert first and first[0][0] == "A" and first[0][-1] == "no bar today: target not filled"
    d.picker.setCurrentIndex(1)
    assert cells(d.positions) != first
    assert any("no bar today: target not filled" in row for row in cells(d.warnings))
    with open(record.folder / "portfolio.csv") as f:
        assert d.note.text() == f.readline().removeprefix("# ").strip()


def test_double_click_opens_detail(window_with_runs):
    w = window_with_runs
    w.list.itemDoubleClicked.emit(w.list.item(0))
    assert isinstance(w.detail, RunDetail) and w.detail.isVisible()


def test_export_copies_both_csvs(window_with_runs, tmp_path, monkeypatch):
    w = window_with_runs
    record = w.records[0]
    out = tmp_path / "out"
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(out)))
    w.list.setCurrentRow(0)
    w.export_button.click()
    for name in ("portfolio.csv", "positions.csv"):
        assert (out / name).read_bytes() == (record.folder / name).read_bytes()
    out2 = tmp_path / "out2"
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(out2)))
    RunDetail(w, record).export_button.click()
    assert (out2 / "portfolio.csv").exists() and (out2 / "positions.csv").exists()


def test_export_cancelled_writes_nothing(window_with_runs, monkeypatch):
    from app import compare_ui
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: ""))
    monkeypatch.setattr(compare_ui, "export_run", lambda *a: pytest.fail("exported after the user backed out"))
    window_with_runs.export(window_with_runs.records[0].id)


# --- entry from the main window, and its guards ---
@pytest.fixture
def main_window(qapp, tmp_path, monkeypatch):
    from app import ui
    monkeypatch.setattr(ui.MainWindow, "start_poller", lambda self, keys: None)
    monkeypatch.setattr(ui.MainWindow, "check_for_update", lambda self: None)
    w = ui.MainWindow(str(tmp_path / "bars.db"), [], ("k", "s"), tmp_path)
    yield w
    w.compare = None
    w.close()


@pytest.fixture
def main_window_with_busy_compare(main_window):
    main_window.compare = SimpleNamespace(busy=True, cancel_run=Mock(), hide=Mock())
    return main_window


def ask(monkeypatch, answer):
    asked = []
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda parent, title, text, *a: asked.append(text) or answer))
    return asked


def test_viewer_still_does_not_import_backtest_modules():
    text = Path("app/ui.py").read_text()
    assert "import app.compare_ui" not in text.split("def open_backtests")[0]  # lazy import only
    assert "from app.compare_ui" not in text.split("def open_backtests")[0]
    assert "from app.runs" not in text.split("def open_backtests")[0]


def test_backtests_menu_opens_one_compare_window(main_window, tmp_path, monkeypatch):
    from app import runs
    monkeypatch.setattr(runs, "runs_dir", lambda: tmp_path / "runs")
    assert main_window.compare is None
    action = [a for top in main_window.menuBar().actions() if top.text().replace("&", "") == "Tools"
              for a in top.menu().actions() if a.text() == "Backtests…"]
    assert len(action) == 1
    action[0].trigger()
    first = main_window.compare
    assert isinstance(first, CompareWindow) and first.isVisible()
    action[0].trigger()
    assert main_window.compare is first
    first.close()


@pytest.mark.parametrize("answer, closes", [(QMessageBox.Yes, True), (QMessageBox.No, False)])
def test_close_while_running_asks_and_cancels(main_window_with_busy_compare, monkeypatch, answer, closes):
    w = main_window_with_busy_compare
    asked = ask(monkeypatch, answer)
    event = QCloseEvent()
    w.closeEvent(event)
    assert asked == ["A backtest is running. Stop it and quit?"]
    assert event.isAccepted() is closes
    assert w.compare.cancel_run.called is closes


def test_updating_guard_runs_before_the_backtest_question(main_window_with_busy_compare, monkeypatch):
    w = main_window_with_busy_compare
    w.updating = True
    asked = ask(monkeypatch, QMessageBox.Yes)
    event = QCloseEvent()
    w.closeEvent(event)
    assert not event.isAccepted() and asked == [] and not w.compare.cancel_run.called
    w.updating = False


def test_update_offer_waits_while_running(main_window_with_busy_compare, monkeypatch):
    w = main_window_with_busy_compare
    monkeypatch.setattr(QMessageBox, "exec", lambda self: pytest.fail("offered an update mid-run"))
    w.offer_update(SimpleNamespace(version="9.9.9"))
    assert w.lock.tryLock(0)  # the offer did not take the update lock
    w.lock.unlock()
    assert "9.9.9" not in w.declined


@pytest.mark.parametrize("answer, closes", [(QMessageBox.Yes, True), (QMessageBox.No, False)])
def test_compare_window_close_while_running_asks_and_cancels(window_with_runs, monkeypatch, answer, closes):
    w = window_with_runs
    w.run_worker = SimpleNamespace(cancel=Mock())
    asked = ask(monkeypatch, answer)
    event = QCloseEvent()
    w.closeEvent(event)
    assert asked == ["A backtest is running. Stop it and close?"]
    assert event.isAccepted() is closes
    assert w.run_worker.cancel.called is closes
    w.run_worker = None
