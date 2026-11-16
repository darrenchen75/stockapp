import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pyqtgraph as pg
import pytest
from PySide6.QtWidgets import QApplication

from app import prefs, theme


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


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
    assert qapp.style().name().lower() != "fusion" or theme.ORIGINAL_STYLE == "fusion"


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
