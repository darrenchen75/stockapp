# User strategy files run inside the frozen app and may import the standard
# library, pandas and numpy. PyInstaller only bundles what it can see imported,
# so list them. The hook is keyed on app.compare_ui because app/ui.py imports
# that module lazily.
import sys
from importlib.util import find_spec

from PyInstaller.utils.hooks import collect_submodules

_SKIP = {"tkinter", "_tkinter", "turtle", "idlelib", "test", "lib2to3", "ensurepip", "venv"}


def _spec(name):
    # find_spec does not execute the module: importing would run side effects
    # (`antigravity` opens a browser, `this` prints). A missing module is None.
    try:
        return find_spec(name)
    except (ImportError, ValueError):
        return None


def _no_tests(name):
    return ".test" not in name and ".idle_test" not in name


_stdlib = {n: s for n in sys.stdlib_module_names - _SKIP if (s := _spec(n)) is not None}
hiddenimports = sorted(_stdlib)
# a package's submodules (email.mime.text, xml.dom.minidom) are not found from its top-level name
for _name, _s in sorted(_stdlib.items()):
    if _s.submodule_search_locations is not None:
        hiddenimports += collect_submodules(_name, filter=_no_tests)
hiddenimports += collect_submodules("pandas", filter=lambda m: ".tests" not in m)
hiddenimports += collect_submodules("numpy", filter=lambda m: ".tests" not in m)
