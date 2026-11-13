"""On-disk state for one paper account: settings, halt/loss lock, journal, event log, one-writer lock."""
import copy
import json
import logging
import os
import sys
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path

from platformdirs import user_data_dir

from app.paper_plan import Limits

log = logging.getLogger(__name__)

_SETTINGS = {"limits": Limits(), "strategy_path": None, "dividends": False}
_WRITE = threading.Lock()  # one read-modify-write at a time, so a kill's halt is never written over
_STATE = {"halted": False, "loss_lock": None, "loss_checked_through": None, "cleared": [], "killed_at": None}


class Busy(Exception):
    """Another window holds the account lock."""


def paper_dir() -> Path:
    return Path(user_data_dir("stockapp", appauthor=False)) / "paper"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_json(path: Path, data: dict) -> None:
    # a temp name of its own, so two writers of one file (the paper window and the compare screen) never collide
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(f.name, path)


def _append(path: Path, event: dict) -> None:
    line = json.dumps({**event, "at": _now()})
    with open(path, "a+b") as f:
        if f.tell() and (f.seek(-1, os.SEEK_END), f.read(1))[1] != b"\n":
            f.write(b"\n")  # a crash left a torn line; don't glue onto it
        f.write(line.encode() + b"\n")
        f.flush()
        os.fsync(f.fileno())


def _read_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out, lines = [], path.read_text().splitlines()
    for i, line in enumerate(lines):
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            log.warning("skipping truncated line %d in %s", i + 1, path)
    return out


class Journal:
    def __init__(self, folder: Path):
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True)

    def write_plan(self, plan: dict) -> None:
        path = self.folder / "plan.json"
        if path.exists():
            raise FileExistsError(path)
        _write_json(path, plan)

    def plan(self) -> dict:
        return json.loads((self.folder / "plan.json").read_text())

    def mark(self, client_order_id: str, status: str, detail: dict | None = None) -> None:
        _append(self.folder / "events.jsonl", {"id": client_order_id, "status": status, "detail": detail})

    def events(self) -> list[dict]:
        return _read_events(self.folder / "events.jsonl")

    def order_states(self) -> dict[str, str]:
        return {e["id"]: e["status"] for e in self.events() if "id" in e}

    def close(self, why: str) -> None:
        _append(self.folder / "events.jsonl", {"close": why})

    def closed(self) -> str | None:
        return next((e["close"] for e in reversed(self.events()) if "close" in e), None)


class AccountStore:
    def __init__(self, root: Path, account_key: str):
        self.dir = Path(root) / account_key
        self.dir.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def lock(self):
        f = open(self.dir / "lock", "a+b")
        try:
            if sys.platform == "win32":
                import msvcrt
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    raise Busy(str(self.dir)) from None
            else:
                import fcntl
                try:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    raise Busy(str(self.dir)) from None
            yield
        finally:
            f.close()  # closing drops the OS lock

    def _log(self, kind: str, old: dict, new: dict) -> None:
        _append(self.dir / "events.jsonl", {"kind": kind, "old": old, "new": new})

    def _load(self, name: str, defaults: dict) -> dict:
        path = self.dir / name
        data = json.loads(path.read_text()) if path.exists() else {}
        return {**copy.deepcopy(defaults), **data}

    def settings(self) -> dict:
        s = self._load("settings.json", {**_SETTINGS, "limits": asdict(_SETTINGS["limits"])})
        s["limits"] = Limits(**s["limits"])
        return s

    def save_settings(self, **changes) -> None:
        with _WRITE:
            old = self.settings()
            new = {**old, **changes}
            enc = lambda s: {**s, "limits": asdict(s["limits"])}
            _write_json(self.dir / "settings.json", enc(new))
            self._log("settings", enc(old), enc(new))

    def state(self) -> dict:
        return self._load("state.json", _STATE)

    def save_state(self, **changes) -> None:
        with _WRITE:
            old = self.state()
            _write_json(self.dir / "state.json", {**old, **changes})
            self._log("state", {k: old[k] for k in changes}, changes)

    def attempts(self, day: date) -> list[int]:
        folder = self.dir / "days" / day.isoformat()
        return sorted(int(p.name[1:]) for p in folder.glob("a*")) if folder.exists() else []

    def journal(self, day: date, attempt: int) -> Journal:
        return Journal(self.dir / "days" / day.isoformat() / f"a{attempt}")

    def entries(self) -> list[Journal]:
        keys = sorted(((d.name, int(a.name[1:])) for d in (self.dir / "days").glob("*") for a in d.glob("a*")), reverse=True)
        return [self.journal(date.fromisoformat(d), n) for d, n in keys]

    def unresolved(self) -> list[tuple[Journal, str]]:
        return [(j, cid) for j in self.entries() for cid, st in j.order_states().items() if st == "sending"]
