import hashlib
import http.client
import json
import logging
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

API = "https://api.github.com/repos/darrenchen75/stockapp/releases/latest"
RELEASES_PAGE = "https://github.com/darrenchen75/stockapp/releases"
ASSETS = {
    "linux": "StockBars-linux.AppImage",
    "win32": "StockBars-windows.zip",
    "darwin": "StockBars-macos.zip",
}
CONFIRM_FLAG = "--confirm-update"


class UpdateError(Exception):
    pass


@dataclass(frozen=True)
class Release:
    version: str
    url: str
    digest: str  # bare hex


def parse_version(tag: str) -> tuple[int, int, int] | None:
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", tag)
    return tuple(map(int, m.groups())) if m else None


def latest(current: str, platform: str = sys.platform, opener=urllib.request.urlopen) -> Release | None:
    have = parse_version(current)
    if have is None or platform not in ASSETS:
        return None
    # GitHub rejects requests without a User-Agent
    req = urllib.request.Request(API, headers={"User-Agent": "StockBars", "Accept": "application/vnd.github+json"})
    try:
        with opener(req, timeout=10) as resp:
            data = json.load(resp)
        tag = data["tag_name"]
        want = parse_version(tag)
        if want is None or want <= have:
            return None
        asset = next((a for a in data["assets"] if a["name"] == ASSETS[platform]), None)
        digest = (asset or {}).get("digest") or ""
        if not digest.startswith("sha256:"):
            return None
        return Release(tag.lstrip("v"), asset["browser_download_url"], digest.removeprefix("sha256:"))
    except (OSError, ValueError, KeyError, TypeError) as e:
        log.warning("update check failed: %s", e)
        return None


def target(platform: str, executable: str, environ: Mapping[str, str], *, frozen: bool = getattr(sys, "frozen", False)) -> Path | None:
    if platform == "linux":
        return Path(environ["APPIMAGE"]) if environ.get("APPIMAGE") else None
    if platform == "win32":
        return Path(executable) if frozen and executable.lower().endswith(".exe") else None
    if platform == "darwin":
        return next((p for p in Path(executable).parents if p.suffix == ".app"), None)
    return None


def download(release: Release, dest: Path, opener=urllib.request.urlopen) -> None:
    req = urllib.request.Request(release.url, headers={"User-Agent": "StockBars"})
    h = hashlib.sha256()
    with opener(req, timeout=60) as resp, open(dest, "wb") as f:
        while chunk := resp.read(1 << 16):
            h.update(chunk)
            f.write(chunk)
    if not release.digest or h.hexdigest() != release.digest.lower():
        dest.unlink(missing_ok=True)
        raise UpdateError("download did not match its checksum")


def prepare(release: Release, target: Path, platform: str, opener=urllib.request.urlopen) -> Path:
    parent = target.parent
    if platform == "linux":
        out = parent / (target.name + ".new")
        temps = [out]
    elif platform == "win32":
        out = parent / "StockBars.new.exe"
        temps = [out, parent / "StockBars.new.zip"]
    else:
        out = parent / "StockBars.new.app"
        temps = [out, parent / "StockBars.new.zip"]
    tmpdir = None
    try:
        if platform == "linux":
            download(release, out, opener)
            out.chmod(0o755)
        else:
            archive = temps[1]
            download(release, archive, opener)
            if platform == "win32":
                with zipfile.ZipFile(archive) as z, z.open("StockBars.exe") as src, open(out, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            else:
                tmpdir = Path(tempfile.mkdtemp(dir=parent))
                subprocess.run(["ditto", "-x", "-k", str(archive), str(tmpdir)], check=True, timeout=300)
                # exits nonzero when there is no quarantine attribute, the normal case here
                xa = subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(tmpdir / "StockBars.app")], timeout=300)
                if xa.returncode:
                    log.debug("xattr exited %s", xa.returncode)
                shutil.rmtree(out, ignore_errors=True)
                (tmpdir / "StockBars.app").rename(out)
            archive.unlink()
        return out
    except (OSError, http.client.HTTPException, zipfile.BadZipFile, KeyError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        for t in temps:
            shutil.rmtree(t, ignore_errors=True) if t.is_dir() else t.unlink(missing_ok=True)
        raise UpdateError(str(e)) from e
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


def _rm(p: Path) -> None:
    shutil.rmtree(p) if p.is_dir() else p.unlink(missing_ok=True)


def aside_path(target: Path, platform: str) -> Path:
    return target.with_name({"win32": "StockBars.old.exe", "darwin": "StockBars.old.app"}.get(platform, target.name + ".old"))


def swap(target: Path, new: Path, platform: str) -> Path:
    aside = aside_path(target, platform)
    try:
        _rm(aside)
        if platform == "linux":
            os.link(target, aside)  # hard link keeps the old build; replace keeps target always present
            os.replace(new, target)
            return aside
        os.rename(target, aside)
    except OSError as e:
        raise UpdateError(str(e)) from e
    try:
        os.rename(new, target)
    except OSError as e:
        try:
            os.rename(aside, target)
        except OSError:
            raise UpdateError(f"{e}; the previous version is at {aside}") from e
        raise UpdateError(str(e)) from e
    return aside


def restore(target: Path, aside: Path) -> None:
    if not aside.exists():
        raise UpdateError("the previous version is missing")
    # never delete target first: a failed replace must leave something installed
    keep = target.with_name(target.name + ".restoring") if target.is_dir() else None
    try:
        if keep:
            _rm(keep)
            os.rename(target, keep)
        os.replace(aside, target)
    except OSError as e:
        if keep and keep.exists() and not target.exists():
            os.rename(keep, target)
        raise UpdateError(str(e)) from e
    if keep:
        shutil.rmtree(keep, ignore_errors=True)


def cleanup(target: Path | None, platform: str) -> None:
    if target is None:
        return
    try:
        _rm(aside_path(target, platform))
    except OSError as e:
        log.debug("could not remove old copy: %s", e)


def new_confirm_path(data_dir: Path) -> Path:
    return data_dir / f"update-{secrets.token_hex(8)}.ok"


def launch(target: Path, platform: str, confirm: Path) -> subprocess.Popen:
    exe = target / "Contents/MacOS/StockBars" if platform == "darwin" else target
    kw = {"env": {**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"}} if platform == "win32" else {"start_new_session": True}
    try:
        return subprocess.Popen([str(exe), CONFIRM_FLAG, str(confirm)], **kw)
    except OSError as e:
        raise UpdateError(str(e)) from e


def _group_gone(proc: subprocess.Popen, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        proc.poll()  # reap the leader: a zombie keeps its group alive
        try:
            os.killpg(proc.pid, 0)
        except ProcessLookupError:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def stop_process(proc: subprocess.Popen, platform: str, timeout: float = 5) -> bool:
    if platform == "win32":  # the onefile bootloader's child survives a plain terminate
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=timeout)
            proc.wait(timeout)
        except subprocess.TimeoutExpired:
            return False
        return True
    # child leads its own session, so pgid == pid; a descendant may outlive the leader
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass
        if _group_gone(proc, timeout):
            return True
    return False


def confirm_from_argv(argv: list[str]) -> None:
    if CONFIRM_FLAG in argv and argv.index(CONFIRM_FLAG) + 1 < len(argv):
        try:
            Path(argv[argv.index(CONFIRM_FLAG) + 1]).touch()
        except OSError as e:
            log.debug("could not write confirmation: %s", e)


def wait_confirmed(path: Path, timeout: float = 30, poll: float = 0.2, sleep=time.sleep, clock=time.monotonic) -> bool:
    deadline = clock() + timeout
    while not path.exists():
        if clock() >= deadline:
            return False
        sleep(poll)
    return True


def clean_confirmations(data_dir: Path, now: float | None = None, max_age: float = 60) -> None:
    now = time.time() if now is None else now
    for p in data_dir.glob("update-*.ok"):
        try:
            if now - p.stat().st_mtime > max_age:
                p.unlink()
        except OSError:
            pass
