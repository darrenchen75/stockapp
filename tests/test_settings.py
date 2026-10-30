import keyring
from keyring.errors import NoKeyringError

from app import settings


def test_round_trip(monkeypatch):
    vault = {}
    monkeypatch.setattr(keyring, "set_password", lambda svc, user, pw: vault.__setitem__((svc, user), pw))
    monkeypatch.setattr(keyring, "get_password", lambda svc, user: vault.get((svc, user)))
    assert settings.load_keys() is None
    assert settings.save_keys("KEY", "SECRET") is True
    assert settings.load_keys() == ("KEY", "SECRET")


def test_save_without_keychain_returns_false(monkeypatch):
    def no_keychain(*args):
        raise NoKeyringError("no backend")

    monkeypatch.setattr(keyring, "set_password", no_keychain)
    monkeypatch.setattr(keyring, "get_password", no_keychain)
    assert settings.save_keys("KEY", "SECRET") is False
    assert settings.load_keys() is None
