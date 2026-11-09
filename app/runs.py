"""Saved backtest runs: one plain-file folder per run under the app's data folder. No Qt."""
import json
import math
import os
import re
import secrets
import shutil
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import pandas as pd
from platformdirs import user_data_dir

from app.backtest import RESULTS_FORMAT, SOURCE, Cancelled, Result, write_results


@dataclass(frozen=True)
class Settings:
    start: str
    end: str
    capital: float
    cost_bps: float
    dividends: bool


@dataclass(frozen=True)
class RunRecord:
    id: str
    folder: Path
    name: str
    settings: Settings | None
    strategy_path: str
    ran_at: str
    data_through: str | None
    app_version: str
    format: int | None
    readable: bool


def runs_dir() -> Path:
    path = Path(user_data_dir("stockapp", appauthor=False)) / "runs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_run(root: Path, result: Result, settings: Settings, strategy_path: str, strategy_bytes: bytes,
             data_through: str | None, app_version: str, now: datetime | None = None,
             cancelled: Callable[[], bool] | None = None) -> RunRecord:
    now = now or datetime.now(timezone.utc)
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    run_id = now.strftime("%Y%m%d-%H%M%S")
    if (root / run_id).exists():
        run_id += "-" + secrets.token_hex(2)
    staging, final = root / f".{run_id}.tmp", root / run_id
    meta = dict(name=re.split(r"[\\/]", strategy_path)[-1], settings=asdict(settings), strategy_path=strategy_path,
                ran_at=now.isoformat(timespec="seconds"), data_through=data_through, app_version=app_version,
                format=RESULTS_FORMAT)
    try:
        staging.mkdir()
        (staging / "strategy.py").write_bytes(strategy_bytes)
        write_results(result, str(staging), SOURCE)
        (staging / "run.json").write_text(json.dumps(meta, indent=2))
        if cancelled and cancelled():
            raise Cancelled()
        os.rename(staging, final)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return _record(final)


def _read_csvs(folder: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    portfolio = pd.read_csv(folder / "portfolio.csv", comment="#")
    positions = pd.read_csv(folder / "positions.csv", comment="#")
    if not {"date", "value"} <= set(portfolio.columns) or portfolio.empty:
        raise ValueError("portfolio.csv has no usable rows")
    if not {"decided_on", "fill_price", "warning"} <= set(positions.columns):
        raise ValueError("positions.csv is missing columns")
    portfolio["date"] = pd.to_datetime(portfolio["date"])  # raises on bad dates
    if portfolio["date"].isna().any() or not portfolio["value"].map(lambda v: math.isfinite(v)).all():
        raise ValueError("portfolio.csv has bad dates or values")
    for col in ("decided_on", "date"):
        if col in positions.columns:
            positions[col] = pd.to_datetime(positions[col])
    return portfolio, positions


def _record(folder: Path) -> RunRecord:
    meta = json.loads((folder / "run.json").read_text())
    try:
        _read_csvs(folder)
        readable = True
    except Exception:
        readable = False
    return RunRecord(id=folder.name, folder=folder, name=meta["name"], settings=Settings(**meta["settings"]),
                     strategy_path=meta["strategy_path"], ran_at=meta["ran_at"], data_through=meta.get("data_through"),
                     app_version=meta.get("app_version", ""), format=meta.get("format"), readable=readable)


def list_runs(root: Path) -> list[RunRecord]:
    root = Path(root)
    if not root.is_dir():
        return []
    out = []
    for folder in sorted(root.iterdir(), reverse=True):  # ids start with the time, so this is newest first
        if not folder.is_dir() or folder.name.startswith("."):
            continue
        try:
            out.append(_record(folder))
        except Exception:  # one bad run never hides the others
            out.append(RunRecord(folder.name, folder, folder.name, None, "", "", None, "", None, False))
    return out


def load_result(record: RunRecord) -> Result:
    if record.settings is None:
        raise ValueError(f"run {record.id} has no readable settings")
    portfolio, positions = _read_csvs(record.folder)
    return Result(portfolio, positions, record.settings.dividends)


def rename_run(record: RunRecord, name: str) -> RunRecord:
    path = record.folder / "run.json"
    meta = json.loads(path.read_text())
    meta["name"] = name
    tmp = path.with_name(".run.json.tmp")
    tmp.write_text(json.dumps(meta, indent=2))
    os.replace(tmp, path)
    return replace(record, name=name)


def delete_run(record: RunRecord) -> None:
    shutil.rmtree(record.folder)


def export_run(record: RunRecord, out_dir: str) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for name in ("portfolio.csv", "positions.csv"):
        shutil.copyfile(record.folder / name, out / name)
