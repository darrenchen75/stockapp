import json
import logging
import subprocess
import sys
from datetime import date

import pytest

from app import settings as app_settings
from app.paper_plan import Limits
from app.paper_state import AccountStore, Busy

DAY = date(2026, 11, 11)


def test_state_survives_reopen(tmp_path):
    AccountStore(tmp_path, "paper-A").save_state(halted=True)
    assert AccountStore(tmp_path, "paper-A").state()["halted"] is True


def test_defaults(tmp_path):
    s = AccountStore(tmp_path, "paper-A")
    assert s.state() == {"halted": False, "loss_lock": None, "loss_checked_through": None, "cleared": [],
                         "killed_at": None}
    assert s.settings() == {"limits": Limits(), "strategy_path": None, "dividends": False}


def test_accounts_do_not_share_state(tmp_path):
    AccountStore(tmp_path, "paper-A").save_state(halted=True)
    assert AccountStore(tmp_path, "paper-B").state()["halted"] is False


def test_plan_is_written_once(tmp_path):
    j = AccountStore(tmp_path, "paper-A").journal(DAY, 1)
    j.write_plan({"sha256": "abc"})
    assert j.plan() == {"sha256": "abc"}
    with pytest.raises(FileExistsError):
        j.write_plan({"sha256": "def"})


def test_closed_survives_reopen(tmp_path):
    j = AccountStore(tmp_path, "paper-A").journal(DAY, 1)
    assert j.closed() is None
    j.close("sent")
    assert AccountStore(tmp_path, "paper-A").journal(DAY, 1).closed() == "sent"


def test_unresolved_lists_sending_without_result(tmp_path):
    s = AccountStore(tmp_path, "paper-A")
    j = s.journal(DAY, 1)
    j.mark("x", "sending")
    j.mark("y", "sending")
    j.mark("y", "accepted")
    assert j.order_states() == {"x": "sending", "y": "accepted"}
    assert [(u.folder, cid) for u, cid in s.unresolved()] == [(j.folder, "x")]


def test_unresolved_scans_all_days(tmp_path):
    s = AccountStore(tmp_path, "paper-A")
    s.journal(date(2026, 11, 9), 1).mark("old", "sending")
    s.journal(DAY, 2).mark("new", "sending")
    assert {cid for _, cid in s.unresolved()} == {"old", "new"}
    assert s.attempts(DAY) == [2]
    assert [j.folder.parent.name + j.folder.name for j in s.entries()] == ["2026-11-11a2", "2026-11-09a1"]


def test_truncated_last_line_is_skipped(tmp_path, caplog):
    j = AccountStore(tmp_path, "paper-A").journal(DAY, 1)
    j.mark("a", "sending")
    with open(j.folder / "events.jsonl", "a") as f:
        f.write('{"id": "x", "sta')
    with caplog.at_level(logging.WARNING):
        events = j.events()
    assert [e["id"] for e in events] == ["a"]
    assert "truncated" in caplog.text
    j.mark("b", "sending")  # the next append must not glue onto the torn line
    assert [e["id"] for e in j.events()] == ["a", "b"]


def test_second_lock_raises_busy(tmp_path):
    s = AccountStore(tmp_path, "paper-A")
    code = ("import sys; from pathlib import Path; from app.paper_state import AccountStore, Busy\n"
            "try:\n    with AccountStore(Path(sys.argv[1]), 'paper-A').lock(): print('got')\n"
            "except Busy: print('busy')")
    run = lambda: subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True).stdout.strip()
    with s.lock():
        assert run() == "busy"
    assert run() == "got"


def test_settings_edit_is_logged(tmp_path):
    s = AccountStore(tmp_path, "paper-A")
    s.save_settings(limits=Limits(max_weight=.2))
    assert s.settings()["limits"] == Limits(max_weight=.2)
    ev = [json.loads(l) for l in (tmp_path / "paper-A" / "events.jsonl").read_text().splitlines()][-1]
    assert ev["kind"] == "settings" and "at" in ev
    assert ev["old"]["limits"]["max_weight"] == .25 and ev["new"]["limits"]["max_weight"] == .2


def test_state_edit_is_logged(tmp_path):
    s = AccountStore(tmp_path, "paper-A")
    s.save_state(halted=True)
    ev = json.loads((tmp_path / "paper-A" / "events.jsonl").read_text().splitlines()[-1])
    assert ev["kind"] == "state" and ev["old"] == {"halted": False} and ev["new"] == {"halted": True}


def test_no_key_written(tmp_path, monkeypatch):
    monkeypatch.setattr(app_settings, "load_keys", lambda: ("KEYID123", "SECRET456"))
    s = AccountStore(tmp_path, "paper-A")
    with s.lock():
        s.save_settings(limits=Limits(max_weight=.2), strategy_path="s.py", dividends=True)
        s.save_state(halted=True, cleared=["2026-11-10"])
        j = s.journal(DAY, 1)
        j.write_plan({"sha256": "abc"})
        j.mark("x", "sending", {"note": "n"})
        j.close("sent")
    files = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert files
    for p in files:
        text = p.read_text()
        assert "KEYID123" not in text and "SECRET456" not in text


def test_state_defaults_are_not_shared(tmp_path):
    AccountStore(tmp_path, "paper-A").state()["cleared"].append("2026-11-10")
    assert AccountStore(tmp_path, "paper-B").state()["cleared"] == []
    assert AccountStore(tmp_path, "paper-A").state()["cleared"] == []
