import hashlib
import http.client
import io
import json
import os
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

from app import update


def fake(payload=None, exc=None):
    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *a):
            return json.dumps(payload).encode()

    calls = []

    def opener(req, timeout=None):
        calls.append(req)
        if exc:
            raise exc
        return Resp()

    opener.calls = calls
    return opener


def release(tag, assets=None):
    if assets is None:
        assets = [{"name": "StockBars-linux.AppImage", "browser_download_url": "https://x/a", "digest": "sha256:ab"}]
    return {"tag_name": tag, "assets": assets}


def test_parse_version():
    assert update.parse_version("v0.1.3") == (0, 1, 3)
    assert update.parse_version("0.10.0") == (0, 10, 0)
    assert update.parse_version("dev") is None
    assert update.parse_version("v1.2") is None


def test_latest_offers_newer_release():
    r = update.latest("0.1.2", "linux", opener=fake(release("v0.1.3")))
    assert r == update.Release("0.1.3", "https://x/a", "ab")


def test_latest_ignores_same_or_older():
    assert update.latest("0.1.2", "linux", opener=fake(release("v0.1.2"))) is None
    assert update.latest("0.1.2", "linux", opener=fake(release("v0.1.1"))) is None


def test_latest_never_checks_from_source():
    opener = fake(release("v9.9.9"))
    assert update.latest("dev", "linux", opener=opener) is None
    assert opener.calls == []


def test_latest_ignores_release_without_this_os_asset():
    assert update.latest("0.1.2", "darwin", opener=fake(release("v0.1.3"))) is None


def test_latest_ignores_asset_without_digest():
    a = {"name": "StockBars-linux.AppImage", "browser_download_url": "https://x/a"}
    assert update.latest("0.1.2", "linux", opener=fake(release("v0.1.3", [a]))) is None
    assert update.latest("0.1.2", "linux", opener=fake(release("v0.1.3", [{**a, "digest": None}]))) is None


def test_latest_returns_none_on_network_error():
    assert update.latest("0.1.2", "linux", opener=fake(exc=OSError("down"))) is None


def fake_bytes(data):
    def opener(req, timeout=None):
        return io.BytesIO(data)

    return opener


def rel(data):
    return update.Release("0.1.3", "https://x/a", hashlib.sha256(data).hexdigest())


def test_download_writes_verified_file(tmp_path):
    update.download(rel(b"hello"), tmp_path / "f", opener=fake_bytes(b"hello"))
    assert (tmp_path / "f").read_bytes() == b"hello"


def test_download_rejects_digest_mismatch(tmp_path):
    with pytest.raises(update.UpdateError):
        update.download(update.Release("0.1.3", "https://x/a", "00" * 32), tmp_path / "f", opener=fake_bytes(b"x"))
    assert not (tmp_path / "f").exists()


def test_download_rejects_empty_digest(tmp_path):
    with pytest.raises(update.UpdateError):
        update.download(update.Release("0.1.3", "https://x/a", ""), tmp_path / "f", opener=fake_bytes(b""))


def test_target_per_platform():
    assert update.target("linux", "/x", {"APPIMAGE": "/a/B.AppImage"}) == Path("/a/B.AppImage")
    assert update.target("linux", "/x", {}) is None
    assert update.target("darwin", "/A/StockBars.app/Contents/MacOS/StockBars", {}) == Path("/A/StockBars.app")
    assert update.target("win32", "C:/S/StockBars.exe", {}, frozen=True) == Path("C:/S/StockBars.exe")
    assert update.target("win32", "C:/S/StockBars.exe", {}, frozen=False) is None


def test_prepare_linux_is_executable(tmp_path):
    t = tmp_path / "B.AppImage"
    out = update.prepare(rel(b"elf"), t, "linux", opener=fake_bytes(b"elf"))
    assert out.stat().st_mode & 0o111
    assert out.parent == t.parent
    assert out.read_bytes() == b"elf"


def test_prepare_windows_extracts_exe(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("StockBars.exe", b"MZ")
    data = buf.getvalue()
    out = update.prepare(rel(data), tmp_path / "StockBars.exe", "win32", opener=fake_bytes(data))
    assert out == tmp_path / "StockBars.new.exe"
    assert out.read_bytes() == b"MZ"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["StockBars.new.exe"]


def test_prepare_windows_bad_zip_cleans_up(tmp_path):
    with pytest.raises(update.UpdateError):
        update.prepare(rel(b"nope"), tmp_path / "StockBars.exe", "win32", opener=fake_bytes(b"nope"))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")
def test_prepare_fails_cleanly_on_readonly_dir(tmp_path):
    d = tmp_path / "ro"
    d.mkdir()
    d.chmod(0o555)
    try:
        with pytest.raises(update.UpdateError):
            update.prepare(rel(b"x"), d / "B.AppImage", "linux", opener=fake_bytes(b"x"))
        assert list(d.iterdir()) == []
    finally:
        d.chmod(0o755)


def test_prepare_cleans_up_when_download_is_cut_off(tmp_path):
    class Cut(io.BytesIO):
        def read(self, n=-1):
            if self.tell():
                raise http.client.IncompleteRead(b"")
            return super().read(2)

    with pytest.raises(update.UpdateError):
        update.prepare(rel(b"abcdef"), tmp_path / "B.AppImage", "linux", opener=lambda req, timeout=None: Cut(b"abcdef"))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_swap_then_restore_files(tmp_path, platform):
    t = tmp_path / ("StockBars.exe" if platform == "win32" else "B.AppImage")
    new = tmp_path / "new"
    t.write_bytes(b"old")
    new.write_bytes(b"new")
    aside = update.swap(t, new, platform)
    assert aside == update.aside_path(t, platform)
    assert t.read_bytes() == b"new" and aside.read_bytes() == b"old" and not new.exists()
    update.restore(t, aside)
    assert t.read_bytes() == b"old" and not aside.exists()


def test_swap_bundle_directory(tmp_path):
    t, new = tmp_path / "StockBars.app", tmp_path / "StockBars.new.app"
    for d, v in ((t, b"old"), (new, b"new")):
        d.mkdir()
        (d / "marker").write_bytes(v)
    stale = tmp_path / "StockBars.old.app"
    stale.mkdir()
    (stale / "junk").write_bytes(b"x")
    aside = update.swap(t, new, "darwin")
    assert aside == stale and (t / "marker").read_bytes() == b"new"
    assert (aside / "marker").read_bytes() == b"old" and not (aside / "junk").exists()
    update.restore(t, aside)
    assert (t / "marker").read_bytes() == b"old" and not aside.exists()


def test_swap_linux_target_never_missing(tmp_path, monkeypatch):
    t, new = tmp_path / "B.AppImage", tmp_path / "new"
    t.write_bytes(b"old")
    new.write_bytes(b"new")
    real = os.replace
    seen = []

    def replace(a, b):
        seen.append(Path(b).exists())
        real(a, b)

    monkeypatch.setattr(os, "replace", replace)
    update.swap(t, new, "linux")
    assert seen == [True]


def test_swap_rolls_back_when_second_rename_fails(tmp_path, monkeypatch):
    t, new = tmp_path / "StockBars.exe", tmp_path / "StockBars.new.exe"
    t.write_bytes(b"old")
    new.write_bytes(b"new")
    real = os.rename
    calls = []

    def rename(a, b):
        calls.append(1)
        if len(calls) == 2:
            raise PermissionError("locked")
        real(a, b)

    monkeypatch.setattr(os, "rename", rename)
    with pytest.raises(update.UpdateError):
        update.swap(t, new, "win32")
    assert t.read_bytes() == b"old" and not update.aside_path(t, "win32").exists()


def test_restore_keeps_target_when_aside_missing(tmp_path):
    t = tmp_path / "B.AppImage"
    t.write_bytes(b"new")
    with pytest.raises(update.UpdateError):
        update.restore(t, tmp_path / "B.AppImage.old")
    assert t.read_bytes() == b"new"


def test_cleanup_removes_aside_and_ignores_errors(tmp_path, monkeypatch):
    t = tmp_path / "B.AppImage"
    aside = update.aside_path(t, "linux")
    aside.write_bytes(b"old")
    update.cleanup(t, "linux")
    assert not aside.exists()
    update.cleanup(t, "linux")  # already gone
    update.cleanup(None, "linux")
    d = update.aside_path(tmp_path / "StockBars.app", "darwin")
    d.mkdir()
    update.cleanup(tmp_path / "StockBars.app", "darwin")
    assert not d.exists()
    aside.write_bytes(b"old")
    monkeypatch.setattr(Path, "unlink", lambda self, *a, **k: (_ for _ in ()).throw(PermissionError()))
    update.cleanup(t, "linux")


def test_swap_reports_aside_when_rollback_fails(tmp_path, monkeypatch):
    t, new = tmp_path / "StockBars.exe", tmp_path / "StockBars.new.exe"
    t.write_bytes(b"old")
    new.write_bytes(b"new")
    real = os.rename
    calls = []

    def rename(a, b):
        calls.append(1)
        if len(calls) >= 2:
            raise PermissionError("locked")
        real(a, b)

    monkeypatch.setattr(os, "rename", rename)
    aside = update.aside_path(t, "win32")
    with pytest.raises(update.UpdateError, match=str(aside).replace("\\", "\\\\")):
        update.swap(t, new, "win32")
    assert aside.read_bytes() == b"old"


def test_swap_linux_link_failure_keeps_target(tmp_path, monkeypatch):
    t, new = tmp_path / "B.AppImage", tmp_path / "new"
    t.write_bytes(b"old")
    new.write_bytes(b"new")
    monkeypatch.setattr(os, "link", lambda *a: (_ for _ in ()).throw(OSError("nope")))
    with pytest.raises(update.UpdateError):
        update.swap(t, new, "linux")
    assert t.read_bytes() == b"old"


def test_restore_file_never_leaves_target_missing(tmp_path, monkeypatch):
    t, aside = tmp_path / "B.AppImage", tmp_path / "B.AppImage.old"
    t.write_bytes(b"new")
    aside.write_bytes(b"old")
    real = os.replace
    seen = []

    def replace(a, b):
        seen.append(Path(b).exists())
        real(a, b)

    monkeypatch.setattr(os, "replace", replace)
    update.restore(t, aside)
    assert seen == [True] and t.read_bytes() == b"old"


def test_restore_bundle_keeps_target_when_replace_fails(tmp_path, monkeypatch):
    t, aside = tmp_path / "StockBars.app", tmp_path / "StockBars.old.app"
    for d, v in ((t, b"new"), (aside, b"old")):
        d.mkdir()
        (d / "marker").write_bytes(v)
    monkeypatch.setattr(os, "replace", lambda a, b: (_ for _ in ()).throw(OSError("busy")))
    with pytest.raises(update.UpdateError):
        update.restore(t, aside)
    assert (t / "marker").read_bytes() == b"new" and (aside / "marker").read_bytes() == b"old"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["StockBars.app", "StockBars.old.app"]


def test_confirm_writes_only_the_named_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    update.confirm_from_argv(["app"])
    assert list(tmp_path.iterdir()) == []
    ok = tmp_path / "update-ab.ok"
    update.confirm_from_argv(["app", update.CONFIRM_FLAG, str(ok)])
    assert [p.name for p in tmp_path.iterdir()] == ["update-ab.ok"]
    update.confirm_from_argv([update.CONFIRM_FLAG, str(tmp_path / "no" / "x.ok")])  # OSError ignored


def test_new_confirm_path_is_unique_and_absent(tmp_path):
    a, b = update.new_confirm_path(tmp_path), update.new_confirm_path(tmp_path)
    assert a != b and a.parent == tmp_path and not a.exists()
    assert a.name.startswith("update-") and a.suffix == ".ok"


def test_wait_confirmed_true_when_file_appears(tmp_path):
    ok, calls = tmp_path / "update-a.ok", []

    def sleep(s):
        calls.append(s)
        if len(calls) == 2:
            ok.write_text("")

    assert update.wait_confirmed(ok, sleep=sleep) is True
    assert len(calls) == 2


def test_wait_confirmed_times_out(tmp_path):
    t, calls = [0.0], []

    def sleep(s):
        calls.append(s)
        t[0] += 1

    assert update.wait_confirmed(tmp_path / "update-a.ok", poll=1, sleep=sleep, clock=lambda: t[0]) is False
    assert len(calls) == 30


def test_wait_ignores_other_files(tmp_path):
    (tmp_path / "update-other.ok").write_text("")
    t = [0.0]

    def sleep(s):
        t[0] += 10

    assert update.wait_confirmed(tmp_path / "update-a.ok", sleep=sleep, clock=lambda: t[0]) is False


def test_clean_confirmations_keeps_fresh_files(tmp_path):
    old, new, other = tmp_path / "update-old.ok", tmp_path / "update-new.ok", tmp_path / "keep.txt"
    for p in (old, new, other):
        p.write_text("")
    now = 1_000_000.0
    os.utime(old, (now - 120, now - 120))
    os.utime(new, (now - 10, now - 10))
    os.utime(other, (now - 120, now - 120))
    update.clean_confirmations(tmp_path, now=now)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["keep.txt", "update-new.ok"]


@pytest.mark.parametrize("platform", ["linux", "win32", "darwin"])
def test_launch_command(tmp_path, monkeypatch, platform):
    seen = {}

    def popen(cmd, **kw):
        seen.update(cmd=cmd, **kw)
        return "proc"

    monkeypatch.setattr(update.subprocess, "Popen", popen)
    ok = tmp_path / "update-a.ok"
    assert update.launch(tmp_path / "StockBars", platform, ok) == "proc"
    exe = tmp_path / "StockBars" / "Contents/MacOS/StockBars" if platform == "darwin" else tmp_path / "StockBars"
    assert seen["cmd"] == [str(exe), "--confirm-update", str(ok)]
    assert seen.get("start_new_session", False) is (platform != "win32")
    assert (seen.get("env") or {}).get("PYINSTALLER_RESET_ENVIRONMENT") == ("1" if platform == "win32" else None)


def test_launch_failure_is_update_error(tmp_path):
    f = tmp_path / "StockBars"
    f.write_text("x")
    with pytest.raises(update.UpdateError):
        update.launch(f, "linux", tmp_path / "a.ok")


@pytest.mark.skipif(sys.platform != "linux", reason="posix process groups")
def test_stop_process_kills_tree(tmp_path):
    pidfile = tmp_path / "pid"
    proc = subprocess.Popen(["sh", "-c", f"sleep 60 & echo $! > {pidfile}; wait"], start_new_session=True)
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text().strip():
            break
        time.sleep(0.05)
    grand = int(pidfile.read_text())
    os.kill(grand, 0)
    assert update.stop_process(proc, "linux") is True
    for _ in range(100):
        try:
            os.kill(grand, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    pytest.fail("grandchild survived")


@pytest.mark.skipif(sys.platform != "linux", reason="posix process groups")
def test_stop_process_kills_term_ignoring_descendant(tmp_path):
    pidfile = tmp_path / "pid"
    script = f"sh -c \"trap '' TERM; sleep 60\" & echo $! > {pidfile}; exit 0"
    proc = subprocess.Popen(["sh", "-c", script], start_new_session=True)
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text().strip():
            break
        time.sleep(0.05)
    grand = int(pidfile.read_text())
    proc.wait()  # leader already gone; only the TERM-ignoring descendant remains
    os.kill(grand, 0)
    assert update.stop_process(proc, "linux", timeout=1) is True
    for _ in range(100):
        try:
            os.kill(grand, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    pytest.fail("descendant survived")


def setup_apply(tmp_path, monkeypatch, *, halt=True, launch_exc=None, confirmed=True, stopped=True, launched=None):
    t = tmp_path / "B.AppImage"
    t.write_bytes(b"old")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    calls, seen = [], {}

    def fake_launch(target, platform, confirm):
        calls.append("launch")
        seen["confirm"] = confirm
        if launch_exc:
            raise launch_exc
        if confirmed:
            confirm.touch()
        return "proc"

    def fake_stop(proc, platform):
        calls.append("stop")
        seen["confirm"].touch()  # the new copy confirms too late
        if isinstance(stopped, Exception):
            raise stopped
        return stopped

    def fake_halt():
        calls.append("halt")
        if isinstance(halt, Exception):
            raise halt
        return halt

    def fake_launched():
        calls.append("launched")
        if launched:
            raise launched

    monkeypatch.setattr(update, "launch", fake_launch)
    monkeypatch.setattr(update, "wait_confirmed", lambda p, timeout=30: p.exists())
    monkeypatch.setattr(update, "stop_process", fake_stop)

    def go(data=b"new"):
        update.apply(
            rel(data), t, "linux", data_dir,
            halt_poller=fake_halt,
            resume_poller=lambda: calls.append("resume"),
            on_launched=fake_launched,
            opener=fake_bytes(b"new"),
        )

    return t, data_dir, calls, go


def test_apply_confirmed(tmp_path, monkeypatch):
    t, data_dir, calls, go = setup_apply(tmp_path, monkeypatch)
    go()
    assert t.read_bytes() == b"new"
    assert update.aside_path(t, "linux").read_bytes() == b"old"
    assert calls == ["halt", "launch", "launched"]
    assert list(data_dir.iterdir()) == []


def test_apply_rolls_back_when_launch_raises(tmp_path, monkeypatch):
    t, data_dir, calls, go = setup_apply(tmp_path, monkeypatch, launch_exc=update.UpdateError("no exec"))
    with pytest.raises(update.UpdateError) as e:
        go()
    assert e.value.started is False
    assert t.read_bytes() == b"old"
    assert calls == ["halt", "launch", "resume"]


def test_apply_rolls_back_on_timeout(tmp_path, monkeypatch):
    t, data_dir, calls, go = setup_apply(tmp_path, monkeypatch, confirmed=False)
    with pytest.raises(update.UpdateError) as e:
        go()
    assert e.value.started is True
    assert t.read_bytes() == b"old"
    assert not update.aside_path(t, "linux").exists()
    assert calls == ["halt", "launch", "launched", "stop", "resume"]
    assert list(data_dir.iterdir()) == []


def test_apply_leaves_new_copy_if_it_wont_die(tmp_path, monkeypatch):
    t, data_dir, calls, go = setup_apply(tmp_path, monkeypatch, confirmed=False, stopped=False)
    with pytest.raises(update.UpdateError) as e:
        go()
    assert e.value.started is True
    assert "still running" in str(e.value)
    assert t.read_bytes() == b"new"
    assert update.aside_path(t, "linux").read_bytes() == b"old"
    assert "resume" not in calls
    assert list(data_dir.iterdir()) == []


def test_apply_aborts_when_poller_wont_stop(tmp_path, monkeypatch):
    t, data_dir, calls, go = setup_apply(tmp_path, monkeypatch, halt=False)
    with pytest.raises(update.UpdateError) as e:
        go()
    assert e.value.started is False
    assert t.read_bytes() == b"old"
    assert calls == ["halt", "resume"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["B.AppImage", "data"]


def test_apply_prepare_failure_touches_nothing(tmp_path, monkeypatch):
    t, data_dir, calls, go = setup_apply(tmp_path, monkeypatch)
    with pytest.raises(update.UpdateError):
        go(data=b"other")  # digest of other bytes than the download
    assert calls == []
    assert t.read_bytes() == b"old"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["B.AppImage", "data"]


def test_apply_swap_failure_deletes_new_copy(tmp_path, monkeypatch):
    t, data_dir, calls, go = setup_apply(tmp_path, monkeypatch)

    def no_link(*a):
        raise OSError("no links here")

    monkeypatch.setattr(update.os, "link", no_link)
    with pytest.raises(update.UpdateError):
        go()
    assert t.read_bytes() == b"old"
    assert calls == ["halt", "resume"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["B.AppImage", "data"]


def test_apply_restore_failure_still_resumes_and_names_old_copy(tmp_path, monkeypatch):
    t, data_dir, calls, go = setup_apply(tmp_path, monkeypatch, confirmed=False)

    def bad_restore(target, aside):
        raise update.UpdateError("disk full")

    monkeypatch.setattr(update, "restore", bad_restore)
    with pytest.raises(update.UpdateError) as e:
        go()
    assert "disk full" in str(e.value) and str(update.aside_path(t, "linux")) in str(e.value)
    assert e.value.started is True
    assert calls[-1] == "resume"


def test_apply_survives_on_launched_error_when_confirmed(tmp_path, monkeypatch):
    t, data_dir, calls, go = setup_apply(tmp_path, monkeypatch, launched=RuntimeError("ui"))
    go()
    assert t.read_bytes() == b"new"
    assert calls == ["halt", "launch", "launched"]


def test_apply_on_launched_error_then_timeout_rolls_back(tmp_path, monkeypatch):
    t, data_dir, calls, go = setup_apply(tmp_path, monkeypatch, confirmed=False, launched=RuntimeError("ui"))
    with pytest.raises(update.UpdateError) as e:
        go()
    assert e.value.started is True
    assert t.read_bytes() == b"old"
    assert calls == ["halt", "launch", "launched", "stop", "resume"]


def test_apply_halt_error_discards_and_resumes(tmp_path, monkeypatch):
    t, data_dir, calls, go = setup_apply(tmp_path, monkeypatch, halt=RuntimeError("stuck"))
    with pytest.raises(update.UpdateError) as e:
        go()
    assert isinstance(e.value.__cause__, RuntimeError)
    assert t.read_bytes() == b"old"
    assert calls == ["halt", "resume"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["B.AppImage", "data"]


def test_apply_rolls_back_on_any_launch_error(tmp_path, monkeypatch):
    t, data_dir, calls, go = setup_apply(tmp_path, monkeypatch, launch_exc=ValueError("bad arg"))
    with pytest.raises(update.UpdateError) as e:
        go()
    assert isinstance(e.value.__cause__, ValueError) and e.value.started is False
    assert t.read_bytes() == b"old"
    assert calls == ["halt", "launch", "resume"]


def test_apply_stop_error_leaves_new_copy(tmp_path, monkeypatch):
    t, data_dir, calls, go = setup_apply(tmp_path, monkeypatch, confirmed=False, stopped=OSError("denied"))
    with pytest.raises(update.UpdateError) as e:
        go()
    assert e.value.started is True and "still running" in str(e.value)
    assert isinstance(e.value.__cause__, OSError)
    assert t.read_bytes() == b"new"
    assert "resume" not in calls
    assert list(data_dir.iterdir()) == []
