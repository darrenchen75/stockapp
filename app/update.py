import json
import logging
import re
import sys
import urllib.request
from dataclasses import dataclass

log = logging.getLogger(__name__)

API = "https://api.github.com/repos/darrenchen75/stockapp/releases/latest"
RELEASES_PAGE = "https://github.com/darrenchen75/stockapp/releases"
ASSETS = {
    "linux": "StockBars-linux.AppImage",
    "win32": "StockBars-windows.zip",
    "darwin": "StockBars-macos.zip",
}


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
