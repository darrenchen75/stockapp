"""The paper trading window. It pulls in Qt and the paper code, so only the lazy Tools menu entry imports it."""
import json
import threading
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from math import nan
from pathlib import Path
from typing import Callable

import pandas as pd
from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QLabel, QListWidget, QMessageBox, QPushButton, QSplitter,
                               QTableWidget, QTabWidget, QVBoxLayout, QWidget)

from app import theme
from app.compare_ui import RunDetail, fixed, one_line
from app.daily import ET
from app.paper import (FILL_COLUMNS, Broker, Plan, Refusal, account_key, cost_summary, export_fills, kill,
                       missed_sessions, negative_cash_days, plan_sell_all, prepare_plan, recover, release, replay,
                       send_plan, send_sell_all, track_fills, unresolved_cancels)
from app.paper_plan import Limits, run_window
from app.paper_state import AccountStore, Busy, Journal, _write_atomic, _write_json
from app.store import Store

LIVE_OFF = "Live trading is off: the app has no code signing, so a tampered update could trade real money."
SIMULATOR_NOTE = ("Alpaca's paper fills are simulated against quotes, so the gap to the official open includes the "
                  "simulator's behaviour, not just real trading costs.")
def red() -> str:
    return f"color: {theme.shade('#c62828')}"
PAPER_ENDPOINT = "paper-api.alpaca.markets"
NOT_PAPER = ("These keys don't reach an Alpaca paper account. Nothing will be sent or cancelled from here. To stop "
             "orders on the paper account, use Alpaca's dashboard.")
WENT_OUT = "Orders already went out — use the kill switch to stop them."
BUSY_JOBS = {"send", "kill", "sell", "sell_send"}  # closing or updating mid-way asks first
fill = RunDetail.fill


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Job(QObject):
    """Runs one function on a thread. Emits (name, result, error): the result, or None and a one-line error."""
    done = Signal(str, object, str)

    def __init__(self, name: str, fn: Callable):
        super().__init__()
        self.name, self.fn = name, fn

    def start(self) -> None:
        threading.Thread(target=self._work, daemon=True).start()

    def _work(self) -> None:
        result, error = None, ""
        try:
            result = self.fn()
        except Busy:
            error = "Another window is using this account. Close it and try again."
        except Exception as e:  # every broker or disk error ends here, shown and never written
            error = one_line(f"{type(e).__name__}: {e}")
        self.done.emit(self.name, result, error)


def _with_store(db_path: str, fn: Callable[[Store], object]):
    store = Store(db_path)  # sqlite3 connections belong to the thread that made them
    try:
        return fn(store)
    finally:
        store.db.close()


class EntryDetail(QDialog):
    """One log entry: its plan, events and fills, and Replay."""

    def __init__(self, window: "PaperWindow", journal: Journal):
        super().__init__(window)
        self.journal = journal
        self.setWindowTitle(f"{journal.folder.parent.name} attempt {journal.folder.name[1:]} · "
                            f"{journal.closed() or 'open'}")
        plan, states = {}, journal.order_states()
        with suppress(FileNotFoundError):
            plan = journal.plan()
        orders = QTableWidget()
        fill(orders, ["Symbol", "Side", "Shares", "Order ID", "State"],
             [[o["symbol"], o["side"], str(o["qty"]), o["client_order_id"],
               states.get(o["client_order_id"]) or "unsent"] for o in plan.get("orders", [])], 2)
        events = QTableWidget()
        fill(events, ["At", "Order", "Status"],
             [[e.get("at", ""), e.get("id", ""), e.get("status") or f"closed: {e.get('close')}"]
              for e in journal.events()])
        rows = []
        with suppress(FileNotFoundError):
            rows = json.loads((journal.folder / "fills.json").read_text())["rows"]
        fills = QTableWidget()
        fill(fills, FILL_COLUMNS, [[_cell(r[c]) for c in FILL_COLUMNS] for r in rows], 3)
        tabs = QTabWidget()
        for widget, name in ((orders, "Orders"), (events, "Events"), (fills, "Fills")):
            tabs.addTab(widget, name)
        for widget in (orders, events, fills):
            widget.setEditTriggers(QAbstractItemView.NoEditTriggers)
        account = plan.get("account") or {}
        summary = QLabel(wordWrap=True)
        summary.setText("\n".join(
            [f"Equity ${account.get('equity', 0):,.2f} · cash ${account.get('cash', 0):,.2f} · targets "
             + (", ".join(f"{s} {w:.1%}" for s, w in plan.get("targets", {}).items()) or "none"),
             f"Strategy {plan.get('strategy_name', '')} sha256 {plan.get('strategy_sha256', '')[:12]} · input "
             f"{plan.get('input_hash', '')[:12]} · data through {plan.get('data_date', '')}"]
            + [n["text"] for n in plan.get("notes", [])]))
        self.replay_button = QPushButton("Replay")
        self.replay_button.clicked.connect(lambda: window.replay_entry(journal))
        self.replay_result = QLabel(wordWrap=True)
        footer = QHBoxLayout()
        footer.addWidget(self.replay_result, 1)
        footer.addWidget(self.replay_button)
        column = QVBoxLayout(self)
        column.addWidget(summary)
        column.addWidget(tabs)
        column.addLayout(footer)
        self.resize(820, 520)

    def show_replay(self, result: dict) -> None:
        if result.get("sell_all"):
            text = "A sell-everything entry ran no strategy, so there is nothing to replay."
        elif not result["hash_matches"]:
            text = "The stored daily data has changed since this run, so it can't be replayed on the same input."
        elif result["same_targets"]:
            text = "Replay matches: the strategy returned the same targets from the same input."
        else:
            text = ("Replay differs: the strategy did not return the same targets from the same input, so it depends "
                    "on more than history and as_of.")
        self.replay_result.setText(text)


def _cell(value) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f}" if isinstance(value, float) else str(value)


class PaperWindow(QWidget):
    def __init__(self, db_path: str, root: Path, make_broker: Callable[[], Broker] | None,
                 make_data_clients: Callable[[], tuple], app_version: str):
        super().__init__()
        self.setWindowTitle("Paper trading")
        self.db_path, self.root, self.make_broker = db_path, Path(root), make_broker
        self.make_data_clients, self.app_version = make_data_clients, app_version
        self.broker: Broker | None = None
        self.acct: AccountStore | None = None  # the confirmed account
        self.status: dict | None = None  # the last status job's answer for the confirmed account
        self.recovered: set[str] = set()  # account keys whose crashed sends recover() has settled
        self.plan: Plan | None = None  # run in this session, not yet sent or cancelled
        self.jobs: dict[str, Job] = {}  # name -> running job, referenced until it reports
        self.run_killed_at: str | None = None  # the kill time when the running Run started
        self.stale = False  # a refresh was asked for while a status job ran; it runs again after
        self.entries: list[Journal] = []
        self.fills = pd.DataFrame(columns=FILL_COLUMNS)
        self.detail: EntryDetail | None = None

        self.account_label = QLabel("Account: checking…")
        self.badge = QLabel("Paper")
        self.badge.setStyleSheet("background: #2e7d32; color: white; padding: 1px 6px; border-radius: 3px")
        self.live = QCheckBox("Live trading", enabled=False, toolTip=LIVE_OFF)
        self.strategy_label = QLabel(wordWrap=True)
        self.strategy_button = QPushButton("Change…")
        self.strategy_button.clicked.connect(self.pick_strategy)
        self.limits_label = QLabel()
        self.limits_button = QPushButton("Edit…")
        self.limits_button.clicked.connect(self.edit_limits)
        self.last_label = QLabel()
        self.missed_label = QLabel(wordWrap=True)
        self.missed_label.setStyleSheet(f"color: {theme.shade('#e65100')}")
        self.cash_label = QLabel(wordWrap=True)
        self.cash_label.setStyleSheet(red())

        self.banner = QWidget()
        self.banner.setStyleSheet("background: #c62828; color: white")
        self.banner_text = QLabel(wordWrap=True)
        self.clear_button = QPushButton("Clear")
        self.clear_button.clicked.connect(self.clear_lock)
        row = QHBoxLayout(self.banner)
        row.addWidget(self.banner_text, 1)
        row.addWidget(self.clear_button)
        self.banner.hide()

        self.run_button, self.send_button, self.cancel_button = (
            QPushButton(t) for t in ("Run today", "Send", "Cancel"))
        self.run_button.clicked.connect(self.run_today)
        self.send_button.clicked.connect(self.send)
        self.cancel_button.clicked.connect(self.cancel)
        self.plan_label = QLabel(wordWrap=True)
        self.orders = QTableWidget()
        self.orders.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.notes = QLabel(wordWrap=True)
        self.message = QLabel(wordWrap=True)
        self.kill_button, self.release_button, self.sell_button = (
            QPushButton(t) for t in ("Kill switch", "Release halt", "Sell everything"))
        self.kill_button.setStyleSheet("font-weight: bold")
        self.kill_button.clicked.connect(self.kill)
        self.release_button.clicked.connect(self.release)
        self.sell_button.clicked.connect(self.sell_everything)
        self.unresolved = QLabel(wordWrap=True)
        self.unresolved.setStyleSheet(red())
        self.unresolved.hide()

        self.log = QListWidget()
        self.log.itemDoubleClicked.connect(lambda item: self.open_entry(self.log.row(item)))
        self.fills_table = QTableWidget()
        self.fills_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.costs = QLabel(wordWrap=True)
        self.simulator_note = QLabel(SIMULATOR_NOTE, wordWrap=True)
        self.export_button = QPushButton("Export CSV…")
        self.export_button.clicked.connect(self.export)

        top = QHBoxLayout()
        top.addWidget(self.account_label)
        top.addWidget(self.badge)
        top.addStretch()
        top.addWidget(self.live)
        form = QFormLayout()
        for label, widget, button in (("Strategy", self.strategy_label, self.strategy_button),
                                      ("Safety", self.limits_label, self.limits_button)):
            line = QHBoxLayout()
            line.addWidget(widget, 1)
            line.addWidget(button)
            form.addRow(label, line)
        actions = QHBoxLayout()
        for b in (self.run_button, self.send_button, self.cancel_button):
            actions.addWidget(b)
        actions.addStretch()
        safety = QHBoxLayout()
        for b in (self.kill_button, self.release_button, self.sell_button):
            safety.addWidget(b)
        safety.addStretch()
        left = QWidget()
        column = QVBoxLayout(left)
        column.addLayout(top)
        column.addLayout(form)
        for w in (self.last_label, self.missed_label, self.cash_label, self.banner):
            column.addWidget(w)
        column.addLayout(actions)
        for w in (self.plan_label, self.orders, self.notes, self.message):
            column.addWidget(w)
        column.addLayout(safety)
        column.addWidget(self.unresolved)
        fills_page = QWidget()
        page = QVBoxLayout(fills_page)
        page.addWidget(self.fills_table)
        page.addWidget(self.costs)
        page.addWidget(self.simulator_note)
        page.addWidget(self.export_button, 0, Qt.AlignRight)
        tabs = QTabWidget()
        tabs.addTab(self.log, "Log")
        tabs.addTab(fills_page, "Fills")
        split = QSplitter()
        split.addWidget(left)
        split.addWidget(tabs)
        split.setSizes([620, 520])
        outer = QHBoxLayout(self)
        outer.addWidget(split)
        self.resize(1150, 720)
        self.clock = QTimer(self, interval=30_000)  # the run window opens and closes by the clock
        self.clock.timeout.connect(self.update_buttons)
        self.clock.start()
        if make_broker is None:
            self.say("No Alpaca keys are stored, so paper trading is off. Add keys in the main window.", error=True)
        self.update_buttons()

    # --- jobs ---
    @property
    def busy(self) -> bool:
        return bool(self.jobs.keys() & BUSY_JOBS)

    def start(self, name: str, fn: Callable) -> None:
        if name in self.jobs:
            return
        job = Job(name, fn)
        job.done.connect(self.on_done)
        self.jobs[name] = job
        self.update_buttons()
        job.start()

    def on_done(self, name: str, result, error: str) -> None:
        self.jobs.pop(name, None)
        if error:
            self.say(error, error=True)
        else:
            getattr(self, f"on_{name}")(result)
        if name == "status" and self.stale:
            self.stale = False
            self.refresh()
        elif name not in ("status", "replay"):
            self.refresh()  # halt, loss lock and journals may all have changed
        self.update_buttons()

    def say(self, text: str, error: bool = False) -> None:
        self.message.setStyleSheet(red() if error else "")
        self.message.setText(one_line(text))

    # --- account and status ---
    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()  # also notices keys that now point to another account

    def refresh(self) -> None:
        if "status" in self.jobs:
            self.stale = True  # that job may have read the account before the change that asked for this
        elif self.make_broker is not None:
            now, recover_now = _now(), not self.busy  # a send, kill or sale settles its own orders
            self.start("status", lambda: self._status(now, recover_now))

    def _status(self, now: datetime, recover_now: bool) -> dict:
        broker = self.make_broker()
        account = broker.account()
        if PAPER_ENDPOINT not in account.endpoint:  # no other call, no folder: these keys may reach real money
            return dict(refused=True)
        acct = AccountStore(self.root, account_key(account))
        errors = []

        def extra(name: str, fn: Callable, default):
            """fn's answer, or default: a failed side check is shown, and never keeps the kill switch off."""
            try:
                return fn()
            except Busy:  # another window holds the account; the next refresh tries again
                return default
            except Exception as e:
                errors.append(one_line(f"{name}: {type(e).__name__}: {e}"))
                return default
        # on every refresh, so an order lost in transit is settled once Alpaca knows it; None when it didn't run
        recovered = extra("Checking unfinished orders", lambda: recover(broker, acct), None) if recover_now else None
        today = now.astimezone(ET).date()
        calendar = extra("Calendar", lambda: broker.calendar(today - timedelta(days=366), today + timedelta(days=10)),
                         [])
        return dict(broker=broker, account=account, acct=acct, recovered=recovered, calendar=calendar,
                    unresolved=extra("Open orders", lambda: unresolved_cancels(broker, acct)
                                     if acct.state()["halted"] else [], []),
                    missed=extra("Missed days", lambda: missed_sessions(acct, calendar, now), []),
                    negative=extra("Cash", lambda: negative_cash_days(acct, broker), []),
                    fills=extra("Fills", lambda: _with_store(
                        self.db_path, lambda store: track_fills(broker, acct, store)), self.fills),
                    errors=errors)

    def on_status(self, s: dict) -> None:
        if s.get("refused"):  # checked on every refresh: every control goes off, the kill switch too
            self.broker = self.acct = self.status = self.plan = None  # the plan's journal is left as it is
            self.show_plan()
            self.say(NOT_PAPER, error=True)
            self.show_status()
            return
        self.broker, key = s["broker"], s["acct"].dir.name
        if s["recovered"] is not None:
            self.recovered.add(key)
        if s["recovered"]:
            self.say("Checked orders left unfinished last time: " + "; ".join(s["recovered"]))
        if s["errors"]:
            self.say("; ".join(s["errors"]), error=True)
        if (self.acct is None or self.acct.dir.name != key) and (why := self.confirm(s)):
            self.acct = self.status = None
            self.say(why, error=True)
        else:
            self.status = s
        self.show_status()

    def confirm(self, s: dict) -> str | None:
        """The account check: a new or different account is confirmed by the user before anything runs. Returns
        why it isn't, or None."""
        self.drop_plan()  # the keys now point elsewhere: an unsent plan is discarded
        account, acct = s["account"], s["acct"]
        current = self.root / "current.json"
        last = json.loads(current.read_text())["account_key"] if current.exists() else None
        if last != acct.dir.name:
            text = f"Use Alpaca paper account {account.number} ({account.endpoint}) for paper trading?"
            if last:
                text += f" The last confirmed account was {last.removeprefix('paper-')}."
            if QMessageBox.question(self, "Paper trading", text) != QMessageBox.Yes:
                return "The account is not confirmed. Close and reopen this window to confirm it."
            self.root.mkdir(parents=True, exist_ok=True)
            _write_json(current, {"account_key": acct.dir.name})
        self.acct = acct
        if not acct.settings()["strategy_path"]:
            self.pick_strategy()
        return None

    def drop_plan(self) -> None:
        if self.plan is not None:
            j = self.acct.journal(self.plan.day, self.plan.attempt)
            if not j.closed() and not j.order_states():
                j.close("discarded")
            self.plan = None
            self.show_plan()

    def show_status(self) -> None:
        s, acct = self.status, self.acct
        self.entries = acct.entries() if acct else []
        self.log.clear()
        self.log.addItems([f"{j.folder.parent.name} {j.folder.name} · {j.closed() or 'open'}" for j in self.entries])
        if s is None:
            self.account_label.setText("Account: not confirmed")
            self.update_buttons()
            return
        a, settings, state = s["account"], acct.settings(), acct.state()
        self.account_label.setText(f"Account {a.number} · equity ${a.equity:,.2f}")
        self.strategy_label.setText(settings["strategy_path"] or "No strategy file chosen")
        lim = settings["limits"]
        self.limits_label.setText(f"Max position {lim.max_weight:.0%} · daily loss cutoff {lim.loss_cutoff:.0%} · "
                                  f"cash buffer {lim.cash_buffer:.0%}")
        last = self.entries[0] if self.entries else None
        self.last_label.setText(f"Last run: {last.folder.parent.name}, attempt {last.folder.name[1:]}, "
                                f"{last.closed() or 'open'}" if last else "No runs yet")
        missed = s["missed"]
        self.missed_label.setText(f"{len(missed)} trading day(s) missed: " + ", ".join(map(str, missed))
                                  if missed else "")
        self.cash_label.setText("Negative cash: " + ", ".join(f"{d} (${c:,.2f})" for d, c in s["negative"])
                                if s["negative"] else "")
        lock = state["loss_lock"]
        self.banner.setVisible(lock is not None)
        if lock:
            self.banner_text.setText(
                (f"Daily loss cutoff hit: {lock['loss']:.1%} on {lock['session']}." if lock["loss"] is not None else
                 f"The loss check failed for {lock['session']}: {one_line(lock['problem'] or '')}")
                + " No buys go out until a person clears this.")
        self.unresolved.setVisible(bool(s["unresolved"]))
        self.unresolved.setText("Not yet confirmed cancelled, filled or rejected: " + ", ".join(s["unresolved"]))
        self.fills = s["fills"]
        fill(self.fills_table, FILL_COLUMNS, [[_cell(None if pd.isna(v) else v) for v in r]
                                              for r in self.fills.itertuples(index=False)], 3)
        costs = cost_summary(self.fills)
        self.costs.setText(f"Average cost, weighted by traded value: {fixed(costs['bps_vs_open'], '+.1f')} bps against "
                           f"the official open, {fixed(costs['bps_vs_close'], '+.1f')} bps against day T's close.")
        self.update_buttons()

    def update_buttons(self) -> None:
        ready = self.acct is not None and self.status is not None and self.broker is not None
        working = bool(self.jobs.keys() - {"replay"})
        halted = ready and self.acct.state()["halted"]
        why = ("No paper account is confirmed." if not ready else
               "The account is halted. Release the halt before running." if halted else
               run_window(self.status["calendar"], _now())[2])
        self.run_button.setEnabled(not why and not working)
        self.run_button.setToolTip(why or "")
        self.send_button.setEnabled(ready and self.plan is not None and not halted and not working
                                    and self.acct.dir.name in self.recovered)
        went_out = ready and self.plan is not None and self.went_out()  # the attempt stays open until settled
        self.cancel_button.setEnabled(ready and self.plan is not None and not working and not went_out)
        self.cancel_button.setToolTip(WENT_OUT if went_out else "")
        self.kill_button.setEnabled(self.acct is not None and self.broker is not None)  # always, even mid-send
        self.release_button.setEnabled(bool(halted) and not self.status["unresolved"] and not working)
        for b in (self.sell_button, self.clear_button, self.strategy_button, self.limits_button):
            b.setEnabled(ready and not working)

    # --- run, send, cancel ---
    def run_today(self) -> None:
        broker, acct, now = self.broker, self.acct, _now()
        self.run_killed_at = acct.state()["killed_at"]

        def work():
            return self._stamp(acct, _with_store(
                self.db_path, lambda store: prepare_plan(broker, acct, store, *self.make_data_clients(), now)))
        self.start("run", work)

    def on_run(self, result) -> None:
        if isinstance(result, Refusal):
            self.say(result.reason, error=True)
            return
        state = self.acct.state()
        if state["halted"] or state["killed_at"] != self.run_killed_at:  # a kill landed while the plan was made
            j = self.acct.journal(result.day, result.attempt)
            if not j.closed() and not j.order_states():
                j.close("discarded")
            self.say("The kill switch was used while this plan was being made, so it was dropped. Run again once "
                     "the halt is released.", error=True)
            return
        self.plan = result
        self.say("")
        self.show_plan()

    def _stamp(self, acct: AccountStore, plan):
        """The app version beside a new plan, for the log."""
        if isinstance(plan, Plan) and not (path := acct.journal(plan.day, plan.attempt).folder
                                           / "app_version.txt").exists():
            _write_atomic(path, self.app_version.encode())
        return plan

    def show_plan(self) -> None:
        p = self.plan
        rows = []
        if p:
            for o in p.orders:
                price = o.ref_price if o.ref_price == o.ref_price else p.last_bars.get(o.symbol, {}).get("c", nan)
                rows.append([o.symbol, o.side, str(o.qty), fixed(o.qty * price, ",.2f"),
                             fixed(p.positions.get(o.symbol, 0.0) * price / p.account.equity, ".1%"),
                             fixed(p.targets.get(o.symbol, 0.0), ".1%")])
        fill(self.orders, ["Symbol", "Side", "Shares", "Approx. value", "Current weight", "Target weight"], rows, 2)
        self.plan_label.setText(f"Plan for {p.day}, attempt {p.attempt}: sent at the open on {p.next_day} once you "
                                "press Send." if p else "")
        self.notes.setText("\n".join(n.text for n in p.notes) if p else "")
        self.update_buttons()

    def send(self) -> None:
        broker, acct, plan, now = self.broker, self.acct, self.plan, _now()
        self.start("send", lambda: send_plan(broker, acct, plan, now))

    def on_send(self, result) -> None:
        self.plan = None
        self.show_plan()
        self.show_sent(result)

    def show_sent(self, result) -> None:
        if result.discarded:
            self.say(f"Nothing sent: {result.discarded}", error=True)
            return
        text = f"Sent {len(result.sent)} order(s)."
        if result.failed:
            text += " Not sent: " + "; ".join(f"{cid}: {err}" for cid, err in result.failed)
        self.say(text, error=bool(result.failed))

    def went_out(self) -> bool:
        return bool(self.acct.journal(self.plan.day, self.plan.attempt).order_states())

    def cancel(self) -> None:
        if self.went_out():
            return
        j = self.acct.journal(self.plan.day, self.plan.attempt)
        if not j.closed():
            j.close("cancelled")
        self.plan = None
        self.show_plan()
        self.say("Cancelled. Nothing was sent.")
        self.show_status()

    # --- loss lock, kill, release, sell everything ---
    def clear_lock(self) -> None:
        state = self.acct.state()
        if state["loss_lock"]:
            self.acct.save_state(loss_lock=None, cleared=[*state["cleared"], state["loss_lock"]["session"]])
        self.show_status()

    def kill(self) -> None:
        broker, acct = self.broker, self.acct
        self.start("kill", lambda: kill(broker, acct))

    def on_kill(self, result) -> None:
        self.plan = None
        self.show_plan()
        self.say(f"Halted. Checked {len(result)} order(s); nothing new goes out until the halt is released.")

    def release(self) -> None:
        broker, acct = self.broker, self.acct
        self.start("release", lambda: release(broker, acct))

    def on_release(self, why) -> None:
        self.say(why or "The halt is released.", error=bool(why))

    def sell_everything(self) -> None:
        if QMessageBox.question(self, "Sell everything", "Sell everything? This halts the account and cancels every "
                                "open order, then lists the sales for you to confirm.") != QMessageBox.Yes:
            return
        broker, acct, now = self.broker, self.acct, _now()
        self.start("sell", lambda: (kill(broker, acct), self._stamp(acct, plan_sell_all(broker, acct, now)))[1])

    def on_sell(self, plan) -> None:
        if isinstance(plan, Refusal):
            self.say(plan.reason, error=True)
            return
        j = self.acct.journal(plan.day, plan.attempt)
        sales = "\n".join([f"{o.symbol} {o.qty}" for o in plan.orders] + [n.text for n in plan.notes])
        if not plan.orders or QMessageBox.question(
                self, "Sell everything", f"Sell these whole shares?\n\n{sales}") != QMessageBox.Yes:
            if not j.closed() and not j.order_states():
                j.close("cancelled")
            self.say("Nothing was sold. The account stays halted." if plan.orders else "No whole shares to sell.")
            return
        broker, acct, now = self.broker, self.acct, _now()
        self.start("sell_send", lambda: send_sell_all(broker, acct, plan, now))

    def on_sell_send(self, result) -> None:
        self.show_sent(result)

    # --- settings ---
    def pick_strategy(self) -> None:
        name, _ = QFileDialog.getOpenFileName(self, "Strategy file", self.acct.settings()["strategy_path"] or "",
                                              "Python files (*.py)")
        if name:
            self.acct.save_settings(strategy_path=name)
            if self.status:
                self.show_status()

    def edit_limits(self) -> None:
        settings = self.acct.settings()
        old = settings["limits"]
        dialog = QDialog(self)
        dialog.setWindowTitle("Safety settings")
        form = QFormLayout(dialog)
        boxes = {}
        for name, label in (("max_weight", "Maximum position"), ("loss_cutoff", "Daily loss cutoff"),
                            ("cash_buffer", "Cash buffer")):
            boxes[name] = QDoubleSpinBox(minimum=0, maximum=100, decimals=1, suffix=" %",
                                         value=getattr(old, name) * 100)
            form.addRow(label, boxes[name])
        dividends = QCheckBox("Include dividends in the strategy's input", checked=settings["dividends"])
        form.addRow("", dividends)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() == QDialog.Accepted:
            new = Limits(**{k: round(b.value() / 100, 4) for k, b in boxes.items()})
            changes = {k: v for k, v, was in (("limits", new, old),
                                              ("dividends", dividends.isChecked(), settings["dividends"])) if v != was}
            if changes:
                self.acct.save_settings(**changes)  # logged by save_settings
            self.show_status()

    # --- log, fills, replay ---
    def open_entry(self, row: int) -> EntryDetail:
        if self.detail is not None:
            self.detail.deleteLater()
        self.detail = EntryDetail(self, self.entries[row])
        self.detail.show()
        return self.detail

    def replay_entry(self, journal: Journal) -> None:
        acct = self.acct
        self.start("replay", lambda: _with_store(self.db_path, lambda store: replay(acct, journal, store)))

    def on_replay(self, result: dict) -> None:
        if self.detail is not None:
            self.detail.show_replay(result)

    def export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export fills", "fills.csv", "CSV files (*.csv)")
        if not path:
            return
        try:
            export_fills(self.fills, path)
        except Exception as e:
            self.say(f"Could not export: {e}", error=True)
            return
        self.say(f"Exported to {path}")

    def closeEvent(self, event):
        if self.busy and QMessageBox.question(
                self, "Paper trading", "Orders are being sent or cancelled. Close the window anyway? The work goes "
                                       "on in the background.") != QMessageBox.Yes:
            event.ignore()
            return
        super().closeEvent(event)
