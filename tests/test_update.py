import json

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
