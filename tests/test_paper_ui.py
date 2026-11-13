import json
import os
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from app import paper, paper_ui
from app.paper_state import AccountStore
from app.paper_ui import LIVE_OFF, PaperWindow
from app.store import ET, DailyBar, Store
from tests import paper_fakes
from tests.paper_fakes import FakeBroker
from tests.test_compare_ui import wait
from tests.test_daily import FakeActions, FakeClient

T = date(2026, 11, 12)
NOW = datetime(2026, 11, 12, 21, 0, tzinfo=ET)
SESSIONS = [date(2026, 11, d) for d in (2, 3, 4, 5, 6, 9, 10, 11, 12, 13, 16, 17, 18, 19, 20)]
SYMBOLS = ["AAPL", "SPY"]
CLOSES = {"SPY": 100.0, "AAPL": 50.0}
NUMBER = "PA0000001"
BUY_SPY = str(Path(__file__).parent / "fixtures" / "buy_spy.py")
LOCK = {"session": "2026-11-12", "loss": -0.05, "problem": None}


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def env(qapp, tmp_path, monkeypatch):
    store = Store(str(tmp_path / "bars.db"))
    for s in SYMBOLS:
        c = CLOSES[s]
        store.replace_daily(s, [DailyBar(s, d.isoformat(), c, c, c, c, 1000.0, 1.0, 1.0) for d in SESSIONS if d <= T])
    store.set_daily_symbols(SYMBOLS)
    store.set_daily_through(T.isoformat())
    monkeypatch.setattr(paper, "universe", lambda: list(SYMBOLS))
    monkeypatch.setattr(paper_ui, "_now", lambda: NOW)
    monkeypatch.setattr(paper_ui, "kill", lambda b, a: paper.kill(b, a, timeout=0.01, sleep=lambda s: None))
    asked = []
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda parent, title, text, *a: asked.append(text) or QMessageBox.Yes))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (BUY_SPY, "")))
    broker = FakeBroker(NUMBER, sessions=SESSIONS, history=[(date(2026, 11, 11), 1e5), (T, 1e5)])
    root = tmp_path / "paper"
    windows = []

    def open_window():
        w = PaperWindow(str(tmp_path / "bars.db"), root, lambda: broker, lambda: (FakeClient(), FakeActions()), "0.1.4")
        windows.append(w)
        w.show()
        settle(w)
        return w

    yield SimpleNamespace(broker=broker, root=root, asked=asked, open=open_window,
                          acct=lambda n=NUMBER: AccountStore(root, f"paper-{n}"))
    for w in windows:
        w.jobs.clear()  # nothing left running: settle() waited for every job
        w.hide()


def settle(w):
    wait(lambda: not w.jobs)
    QApplication.processEvents()


def click(w, button):
    assert button.isEnabled(), button.toolTip()
    button.click()
    settle(w)


def test_open_confirms_the_account_once_and_picks_a_strategy(env):
    w = env.open()
    assert len(env.asked) == 1 and NUMBER in env.asked[0]
    assert json.loads((env.root / "current.json").read_text()) == {"account_key": f"paper-{NUMBER}"}
    assert env.acct().settings()["strategy_path"] == BUY_SPY
    assert NUMBER in w.account_label.text() and w.badge.text() == "Paper"
    w.hide()
    w.show()
    settle(w)
    assert len(env.asked) == 1
    env.open()  # a new window for the same account
    assert len(env.asked) == 1


def test_open_recovers_a_crashed_send_before_anything_else(env):
    w = env.open()
    click(w, w.run_button)
    j = env.acct().journal(T, 1)
    cid = f"sb-{NUMBER}-20261112-a1-SPY"
    j.mark(cid, "sending")  # a crash mid-send
    w2 = env.open()
    assert j.order_states()[cid] == "not_found"
    assert not w2.send_button.isEnabled()  # no plan of this session


def test_no_keys_disables_everything(qapp, tmp_path):
    w = PaperWindow(str(tmp_path / "bars.db"), tmp_path / "paper", None, lambda: (), "0.1.4")
    w.show()
    assert "No Alpaca keys" in w.message.text()
    for b in (w.run_button, w.send_button, w.cancel_button, w.kill_button, w.release_button, w.sell_button):
        assert not b.isEnabled()
    assert not w.jobs
    w.hide()


def test_run_disabled_outside_window_with_reason(env, monkeypatch):
    monkeypatch.setattr(paper_ui, "_now", lambda: datetime(2026, 11, 13, 9, 28, tzinfo=ET))
    w = env.open()
    assert not w.run_button.isEnabled()
    assert "09:28" in w.run_button.toolTip()
    monkeypatch.setattr(paper_ui, "_now", lambda: NOW)
    w.refresh()
    settle(w)
    assert w.run_button.isEnabled() and w.run_button.toolTip() == ""


def test_send_needs_shown_plan(env):
    w = env.open()
    assert not w.send_button.isEnabled() and not w.cancel_button.isEnabled()
    click(w, w.run_button)
    assert w.plan is not None and w.orders.rowCount() == len(w.plan.orders) > 0
    assert (env.acct().journal(T, 1).folder / "app_version.txt").read_text() == "0.1.4"
    click(w, w.cancel_button)
    assert env.acct().journal(T, 1).closed() == "cancelled"
    assert w.plan is None and not w.send_button.isEnabled() and env.broker.submitted == []
    click(w, w.run_button)
    click(w, w.send_button)
    assert [tif for _, tif in env.broker.submitted] == ["opg"]
    assert w.plan is None and not w.send_button.isEnabled()
    assert "Sent 1" in w.message.text()


def test_refusal_is_shown_and_not_written(env):
    w = env.open()
    env.acct().save_settings(strategy_path="/no/such/strategy.py")
    click(w, w.run_button)
    assert "Strategy file not found" in w.message.text()
    written = "".join(p.read_text() for p in env.root.rglob("*") if p.is_file() and p.name != "lock")
    assert "not found" not in written


def test_banner_and_clear(env):
    env.acct().save_state(loss_lock=LOCK)
    w = env.open()
    assert not w.banner.isHidden() and "2026-11-12" in w.banner_text.text()
    click(w, w.clear_button)
    state = env.acct().state()
    assert state["loss_lock"] is None and state["cleared"] == ["2026-11-12"]
    assert w.banner.isHidden()
    events = [json.loads(line) for line in (env.acct().dir / "events.jsonl").read_text().splitlines()]
    assert events[-1]["kind"] == "state" and events[-1]["new"]["cleared"] == ["2026-11-12"]


def test_live_switch_always_disabled(env):
    w = env.open()
    assert not w.live.isEnabled() and not w.live.isChecked()
    assert w.live.toolTip() == LIVE_OFF == ("Live trading is off: the app has no code signing, so a tampered update "
                                            "could trade real money.")
    click(w, w.run_button)
    assert not w.live.isEnabled()


def test_simulator_note_is_shown(env):
    w = env.open()
    assert "simulated against quotes" in w.simulator_note.text()


def test_kill_switch_halts_and_disables_run(env):
    w = env.open()
    click(w, w.run_button)
    click(w, w.send_button)
    assert env.broker.open_orders()
    click(w, w.kill_button)
    assert env.acct().state()["halted"] and not env.broker.open_orders()
    assert not w.run_button.isEnabled() and "halted" in w.run_button.toolTip()
    assert w.kill_button.isEnabled() and w.release_button.isEnabled()
    assert w.unresolved.isHidden()
    click(w, w.release_button)
    assert not env.acct().state()["halted"] and w.run_button.isEnabled()


def test_unconfirmed_cancels_listed_in_red_and_block_release(env):
    w = env.open()
    click(w, w.run_button)
    click(w, w.send_button)
    env.broker.cancel_to = "pending_cancel"
    click(w, w.kill_button)
    cid = f"sb-{NUMBER}-20261112-a1-SPY"
    assert not w.unresolved.isHidden() and cid in w.unresolved.text() and "c62828" in w.unresolved.styleSheet()
    assert not w.release_button.isEnabled()


def test_sell_everything_has_its_own_confirmation(env, monkeypatch):
    env.broker.held = {"SPY": 10.0, "AAPL": 4.5}
    w = env.open()
    env.asked.clear()
    answers = iter([QMessageBox.Yes, QMessageBox.No])
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda p, t, text, *a: env.asked.append(text) or next(answers)))
    click(w, w.sell_button)
    assert len(env.asked) == 2 and "AAPL 4" in env.asked[1] and "SPY 10" in env.asked[1]
    assert env.broker.submitted == []  # declined the list of sales
    assert env.acct().state()["halted"]
    answers = iter([QMessageBox.Yes, QMessageBox.Yes])
    click(w, w.sell_button)
    assert sorted(env.broker.submitted) == [(f"sb-{NUMBER}-20261112-a2-AAPL", "opg"),
                                            (f"sb-{NUMBER}-20261112-a2-SPY", "opg")]


def test_account_change_discards_plan(env):
    w = env.open()
    click(w, w.run_button)
    assert w.send_button.isEnabled()
    env.broker.acct = paper.Account("PA0000002", env.broker.acct.endpoint, 1e5, 1e5, 2e5)
    env.asked.clear()
    w.refresh()
    settle(w)
    assert w.plan is None and not w.send_button.isEnabled() and w.orders.rowCount() == 0
    assert env.acct().journal(T, 1).closed() == "discarded"
    assert len(env.asked) == 1 and "PA0000002" in env.asked[0] and NUMBER in env.asked[0]
    assert json.loads((env.root / "current.json").read_text()) == {"account_key": "paper-PA0000002"}


def test_declining_the_account_leaves_everything_off(env, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a: QMessageBox.No))
    w = env.open()
    assert not (env.root / "current.json").exists()
    assert not w.run_button.isEnabled() and not w.kill_button.isEnabled()


def test_log_lists_newest_first_and_replays(env):
    w = env.open()
    click(w, w.run_button)
    click(w, w.cancel_button)
    click(w, w.run_button)
    assert [w.log.item(i).text().split(" ")[1] for i in range(w.log.count())] == ["a2", "a1"]
    detail = w.open_entry(1)
    assert "cancelled" in detail.windowTitle()
    detail.replay_button.click()
    settle(w)
    assert "same targets" in detail.replay_result.text()
    detail.close()


def test_close_while_sending_asks(env, monkeypatch):
    w = env.open()
    w.jobs["send"] = SimpleNamespace()
    asked = []
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda p, t, text, *a: asked.append(text) or QMessageBox.No))
    event = QCloseEvent()
    w.closeEvent(event)
    assert not event.isAccepted() and asked and w.busy
    del w.jobs["send"]


# --- entry from the main window ---
@pytest.fixture
def main_window(qapp, tmp_path, monkeypatch):
    from app import paper_state, ui
    monkeypatch.setattr(ui.MainWindow, "start_poller", lambda self, keys: None)
    monkeypatch.setattr(ui.MainWindow, "check_for_update", lambda self: None)
    monkeypatch.setattr(paper_state, "paper_dir", lambda: tmp_path / "paper")
    w = ui.MainWindow(str(tmp_path / "bars.db"), [], ("k", "s"), tmp_path)
    yield w
    w.compare = w.paper = None
    w.close()


def test_tools_menu_opens_one_paper_window(main_window, monkeypatch):
    monkeypatch.setattr(paper, "make_broker", lambda keys: FakeBroker(NUMBER, sessions=SESSIONS))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a: QMessageBox.No))
    assert main_window.paper is None
    action = [a for top in main_window.menuBar().actions() if top.text().replace("&", "") == "Tools"
              for a in top.menu().actions() if a.text() == "Paper trading…"]
    assert len(action) == 1
    action[0].trigger()
    first = main_window.paper
    assert isinstance(first, PaperWindow) and first.isVisible()
    settle(first)
    action[0].trigger()
    assert main_window.paper is first
    settle(first)
    first.hide()


def test_update_waits_while_paper_window_is_busy(main_window, monkeypatch):
    main_window.paper = SimpleNamespace(busy=True)
    monkeypatch.setattr(main_window.lock, "tryLock", Mock(side_effect=AssertionError("offered mid-send")))
    main_window.offer_update(SimpleNamespace(version="9.9.9"))


def test_backtests_get_paper_lines_once_an_account_is_confirmed(main_window, tmp_path, monkeypatch):
    from app import compare_ui, runs
    monkeypatch.setattr(runs, "runs_dir", lambda: tmp_path / "runs")
    seen = []
    monkeypatch.setattr(compare_ui, "CompareWindow", lambda *a, paper_source=None: seen.append(paper_source) or Mock())
    main_window.open_backtests()
    assert seen == [None]  # no account confirmed yet

    (tmp_path / "paper").mkdir()
    (tmp_path / "paper" / "current.json").write_text(json.dumps({"account_key": f"paper-{NUMBER}"}))
    broker = FakeBroker(NUMBER, sessions=SESSIONS, history=[(T, 1e5)])
    monkeypatch.setattr(paper, "make_broker", lambda keys: broker)
    tracked = []
    monkeypatch.setattr(paper, "track_fills", lambda b, a, s: tracked.append((b, a.dir.name)))
    main_window.compare = None
    main_window.open_backtests()
    assert seen[1]() == [] and tracked == [(broker, f"paper-{NUMBER}")]
    broker.acct = paper.Account("PA0000002", broker.acct.endpoint, 1e5, 1e5, 2e5)
    with pytest.raises(ValueError, match="PA0000002"):
        seen[1]()


def test_viewer_still_does_not_import_paper_modules():
    text = Path("app/ui.py").read_text()
    head = text.split("def open_backtests")[0]
    assert "app.paper" not in head and "paper_ui" not in head
    lazy = ('app.paper', 'app.compare_ui', 'app.backtest', 'app.runs')
    code = f"import sys, app.main; print(sorted(m for m in sys.modules if m.startswith({lazy})))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                         env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
    assert out.stdout.strip() == "[]"


def test_refresh_asked_mid_status_runs_again(env):
    w = env.open()
    w.refresh()
    w.refresh()  # while the first status job still runs
    assert w.stale
    settle(w)
    assert not w.stale


@pytest.mark.parametrize("answer, closes", [(QMessageBox.Yes, True), (QMessageBox.No, False)])
def test_quit_while_paper_is_busy_asks(main_window, monkeypatch, answer, closes):
    main_window.paper = SimpleNamespace(busy=True, hide=Mock())
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a: answer))
    event = QCloseEvent()
    main_window.closeEvent(event)
    assert event.isAccepted() is closes and main_window.paper.hide.called is closes


def test_cancel_is_off_once_an_order_went_out(env, tmp_path):
    w = env.open()
    two = tmp_path / "two.py"
    two.write_text("def target_weights(history, as_of):\n    return {'SPY': 0.2, 'AAPL': 0.2}\n")
    env.acct().save_settings(strategy_path=str(two))
    cid = f"sb-{NUMBER}-20261112-a1-SPY"

    def lost(call):  # the second order's answer is lost and Alpaca doesn't know it: left unsent
        if call == 2:
            raise Exception(paper_fakes.DUPLICATE)
    env.broker.on_submit = lost
    click(w, w.run_button)
    click(w, w.send_button)
    env.broker.on_submit = None
    click(w, w.run_button)  # offers the unsent rest of attempt 1
    assert [o.client_order_id for o in w.plan.orders] == [cid]
    assert not w.cancel_button.isEnabled()
    assert w.cancel_button.toolTip() == "Orders already went out — use the kill switch to stop them."
    assert w.send_button.isEnabled() and w.kill_button.isEnabled()
    w.cancel()
    assert env.acct().journal(T, 1).closed() is None


def test_safety_settings_edit_is_saved_and_logged(env, monkeypatch):
    from PySide6.QtWidgets import QDialog, QDoubleSpinBox
    w = env.open()
    monkeypatch.setattr(QDialog, "exec",
                        lambda d: (d.findChildren(QDoubleSpinBox)[0].setValue(30), QDialog.Accepted)[1])
    click(w, w.limits_button)
    assert env.acct().settings()["limits"].max_weight == 0.3
    event = json.loads((env.acct().dir / "events.jsonl").read_text().splitlines()[-1])
    assert event["kind"] == "settings"
    assert (event["old"]["limits"]["max_weight"], event["new"]["limits"]["max_weight"]) == (0.25, 0.3)
    assert "30%" in w.limits_label.text()


def test_change_strategy(env, monkeypatch, tmp_path):
    w = env.open()
    other = str(tmp_path / "other.py")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (other, "")))
    click(w, w.strategy_button)
    assert env.acct().settings()["strategy_path"] == other and w.strategy_label.text() == other
