import hashlib
import io
import json
import os
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
