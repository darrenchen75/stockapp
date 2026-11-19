"""System / light / dark display. Qt 6.8+ can force a colour scheme; where the platform style
ignores that, fall back to Fusion with a hand-made palette."""
from dataclasses import dataclass

import pyqtgraph as pg
from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

CHOICES = ("system", "light", "dark")


@dataclass(frozen=True)
class Colors:
    background: str
    foreground: str
    line: str
    up: str
    down: str


# line, up and down keep at least 3:1 contrast against the background
LIGHT = Colors("#ffffff", "#202020", "#1a56db", "#1b7f3a", "#c62828")
DARK = Colors("#1e1e1e", "#e0e0e0", "#6f94ff", "#66bb6a", "#ef9a9a")
SHADES = {"#7b3fb0": "#b388ff", "#3a6bff": "#6f94ff", "#c62828": "#ef9a9a"}

ORIGINAL_STYLE: str | None = None
_watcher: "Watcher | None" = None


def _override_took(requested: Qt.ColorScheme) -> bool:
    return QApplication.styleHints().colorScheme() == requested


def _palette(c: Colors) -> QPalette:
    p = QPalette()
    bg, fg = QColor(c.background), QColor(c.foreground)
    alt = bg.lighter(115) if bg.lightness() < 128 else bg.darker(105)
    for role, color in ((QPalette.Window, alt), (QPalette.Base, bg), (QPalette.AlternateBase, alt),
                        (QPalette.Button, alt), (QPalette.WindowText, fg), (QPalette.Text, fg),
                        (QPalette.ButtonText, fg), (QPalette.ToolTipBase, bg), (QPalette.ToolTipText, fg),
                        (QPalette.Highlight, QColor(c.line)), (QPalette.HighlightedText, bg),
                        (QPalette.PlaceholderText, fg.darker(150) if bg.lightness() < 128 else fg.lighter(150))):
        p.setColor(role, color)
    off = QColor((fg.red() + bg.red()) // 2, (fg.green() + bg.green()) // 2, (fg.blue() + bg.blue()) // 2)
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, off)  # halfway to the background, so disabled controls look it
    return p


def apply(app: QApplication, choice: str) -> None:
    global ORIGINAL_STYLE
    if ORIGINAL_STYLE is None:
        ORIGINAL_STYLE = app.style().name()
    watcher()  # installed before the first change, so every window hears it
    scheme = {"light": Qt.ColorScheme.Light, "dark": Qt.ColorScheme.Dark}.get(choice, Qt.ColorScheme.Unknown)
    app.styleHints().setColorScheme(scheme)
    if scheme != Qt.ColorScheme.Unknown and not _override_took(scheme):
        app.setStyle("Fusion")
        app.setPalette(_palette(DARK if choice == "dark" else LIGHT))
    elif app.style().name().lower() != ORIGINAL_STYLE.lower():  # undo an earlier Fusion fallback
        app.setStyle(ORIGINAL_STYLE)
        app.setPalette(QPalette())
    elif scheme == Qt.ColorScheme.Unknown:
        app.setPalette(QPalette())  # unpinned, so Qt follows the OS again


def colors() -> Colors:
    return DARK if QApplication.palette().color(QPalette.Window).lightness() < 128 else LIGHT


def shade(hex_color: str) -> str:
    return SHADES.get(hex_color, hex_color) if colors() is DARK else hex_color


class Watcher(QObject):
    changed = Signal()

    def eventFilter(self, obj, event):
        if event.type() == QEvent.ApplicationPaletteChange and obj is QApplication.instance():
            self.changed.emit()
        return False


def watcher() -> Watcher:
    global _watcher
    if _watcher is None:
        _watcher = Watcher()
        QApplication.instance().installEventFilter(_watcher)
    return _watcher


def style_plot(plot: pg.PlotWidget) -> None:
    c = colors()
    plot.setBackground(c.background)
    for name in ("left", "bottom"):
        axis = plot.getAxis(name)
        axis.setPen(pg.mkPen(c.foreground))
        axis.setTextPen(pg.mkPen(c.foreground))
    plot.getPlotItem().titleLabel.setAttr("color", c.foreground)
