import logging

import keyring
from keyring.errors import KeyringError
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFormLayout, QLabel,
                               QLineEdit, QMessageBox)

log = logging.getLogger(__name__)
SERVICE = "stockapp"


def load_keys() -> tuple[str, str] | None:
    try:
        key = keyring.get_password(SERVICE, "key_id")
        secret = keyring.get_password(SERVICE, "secret")
    except KeyringError as e:
        log.warning("keychain unavailable: %s", e)
        return None
    return (key, secret) if key and secret else None


def save_keys(key: str, secret: str) -> bool:
    try:
        keyring.set_password(SERVICE, "key_id", key)
        keyring.set_password(SERVICE, "secret", secret)
    except KeyringError as e:
        log.warning("keychain unavailable, keys kept for this session only: %s", e)
        return False
    return True


def remember(parent, keys: tuple[str, str]) -> None:
    if not save_keys(*keys):
        QMessageBox.warning(parent, "No keychain",
                            "No system keychain was found, so your key is used for this "
                            "session only and you'll be asked again next launch.")


def ask_keys(parent=None, message: str = "") -> tuple[str, str] | None:
    dialog = QDialog(parent)
    dialog.setWindowTitle("Alpaca API key")
    form = QFormLayout(dialog)
    info = QLabel((message + "<br><br>" if message else "") +
                  'Paste a free API key from <a href="https://app.alpaca.markets">'
                  "app.alpaca.markets</a> (a paper-trading key works).")
    info.setOpenExternalLinks(True)
    info.setWordWrap(True)
    form.addRow(info)
    key = QLineEdit()
    secret = QLineEdit(echoMode=QLineEdit.EchoMode.Password)
    form.addRow("Key ID", key)
    form.addRow("Secret", secret)
    buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    form.addRow(buttons)
    if dialog.exec() != QDialog.Accepted:
        return None
    k, s = key.text().strip(), secret.text().strip()
    return (k, s) if k and s else None
