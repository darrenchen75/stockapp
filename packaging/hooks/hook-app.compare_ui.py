# User strategy files run inside the frozen app and may import the standard
# library, pandas and numpy. PyInstaller only bundles what it can see imported,
# so list them. The hook is keyed on app.compare_ui because app/ui.py imports
# that module lazily.
import sys
from importlib.util import find_spec

from PyInstaller.utils.hooks import collect_submodules

_SKIP = {"tkinter", "_tkinter", "turtle", "idlelib", "test", "lib2to3", "ensurepip", "venv"}


def _importable(name):
    # find_spec does not execute the module: importing would run side effects
    # (`antigravity` opens a browser, `this` prints). A missing module is None.
    try:
        return find_spec(name) is not None
    except (ImportError, ValueError):
        return False


hiddenimports = sorted(n for n in sys.stdlib_module_names - _SKIP if _importable(n))
hiddenimports += collect_submodules("pandas")
hiddenimports += collect_submodules("numpy")
