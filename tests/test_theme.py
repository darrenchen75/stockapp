import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pyqtgraph as pg
import pytest
from PySide6.QtWidgets import QApplication

from app import prefs, theme


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def restore(qapp):
    yield
    theme.apply(qapp, "system")


def test_prefs_roundtrip_and_corrupt(tmp_path):
    prefs.save(tmp_path, theme="dark"); prefs.save(tmp_path, group_by="sector")
    assert prefs.load(tmp_path) == {"theme": "dark", "group_by": "sector"}
    (tmp_path / "display.json").write_text("{nope")
    assert prefs.load(tmp_path) == {}


def test_fallback_sets_fusion_palette_when_override_ignored(qapp, monkeypatch):
    monkeypatch.setattr(theme, "_override_took", lambda requested: False)
    theme.apply(qapp, "dark")
    assert qapp.style().name().lower() == "fusion" and theme.colors() == theme.DARK
    theme.apply(qapp, "system")
    assert qapp.style().name().lower() == theme.ORIGINAL_STYLE.lower()


def test_watcher_fires_on_palette_change_and_new_window_starts_in_new_colours(qapp, monkeypatch):
    monkeypatch.setattr(theme, "_override_took", lambda requested: False)
    seen = []; theme.watcher().changed.connect(lambda: seen.append(1))
    theme.apply(qapp, "dark"); qapp.processEvents()
    assert seen
    plot = pg.PlotWidget(); theme.style_plot(plot)
    assert plot.backgroundBrush().color().name() == theme.DARK.background
    theme.apply(qapp, "system")


def test_compare_window_follows_theme(qapp, monkeypatch, tmp_path):
    from app.compare_ui import CompareWindow
    monkeypatch.setattr(theme, "_override_took", lambda requested: False)
    theme.apply(qapp, "dark")
    w = CompareWindow(str(tmp_path / "bars.db"), tmp_path, lambda: (None, None), "1")
    assert w.plot.backgroundBrush().color().name() == theme.DARK.background
    theme.apply(qapp, "light"); qapp.processEvents()
    assert w.plot.backgroundBrush().color().name() == theme.LIGHT.background
    theme.apply(qapp, "system")


def test_native_override_leaves_style_and_palette_alone(qapp, monkeypatch):
    theme.apply(qapp, "system")
    style = qapp.style().name()
    calls = []
    monkeypatch.setattr(theme, "_override_took", lambda requested: True)
    monkeypatch.setattr(qapp, "setPalette", lambda *a: calls.append(a))
    monkeypatch.setattr(qapp, "setStyle", lambda *a: calls.append(a))
    theme.apply(qapp, "dark")
    assert calls == [] and qapp.style().name() == style


def test_system_unpins_palette(qapp, monkeypatch):
    monkeypatch.setattr(theme, "_override_took", lambda requested: False)
    theme.apply(qapp, "dark")
    theme.apply(qapp, "system")
    assert qapp.palette() == type(qapp.palette())()


def test_paper_window_and_dialog_restyle_on_theme_change(qapp, monkeypatch, tmp_path):
    from app.compare_ui import NewRunDialog
    from app.paper_ui import PaperWindow
    monkeypatch.setattr(theme, "_override_took", lambda requested: False)
    theme.apply(qapp, "light")
    w = PaperWindow(str(tmp_path / "bars.db"), tmp_path / "paper", None, lambda: (), "0.1.4")
    d = NewRunDialog(w)
    assert "#c62828" in w.cash_label.styleSheet() and "#c62828" in d.error.styleSheet()
    theme.apply(qapp, "dark"); qapp.processEvents()
    assert "#ef9a9a" in w.cash_label.styleSheet() and "#ef9a9a" in d.error.styleSheet()
