import hashlib
import http.client
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
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
        os.rename(aside, target)
        raise UpdateError(str(e)) from e
    return aside


def restore(target: Path, aside: Path) -> None:
    if not aside.exists():
        raise UpdateError("the previous version is missing")
    try:
        _rm(target)
        os.replace(aside, target)
    except OSError as e:
        raise UpdateError(str(e)) from e


def cleanup(target: Path | None, platform: str) -> None:
    if target is None:
        return
    try:
        _rm(aside_path(target, platform))
    except OSError as e:
        log.debug("could not remove old copy: %s", e)
