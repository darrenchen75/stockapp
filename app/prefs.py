import json
import os
import tempfile
from pathlib import Path


def load(data_dir: Path) -> dict:
    try:
        data = json.loads((Path(data_dir) / "display.json").read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(data_dir: Path, **changes) -> None:
    path = Path(data_dir) / "display.json"
    data = {**load(data_dir), **changes}
    f = tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".display.", suffix=".tmp", delete=False)
    try:
        with f:
            json.dump(data, f, indent=2)
        os.replace(f.name, path)
    except BaseException:
        Path(f.name).unlink(missing_ok=True)
        raise
