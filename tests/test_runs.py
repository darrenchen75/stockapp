import json
import shutil
from datetime import datetime, timedelta, timezone

import pytest

from app.backtest import RESULTS_FORMAT, Cancelled, run
from app.runs import Settings, delete_run, export_run, list_runs, load_result, rename_run, save_run
from tests.backtest_cases import D, all_in, plain

SETTINGS = Settings(start=D[0], end=D[3], capital=100_000.0, cost_bps=0.0, dividends=True)
NOW = datetime(2026, 11, 8, 12, 0, 0, tzinfo=timezone.utc)


def result_of(bars):
    return run(all_in("A"), bars, D[0], D[3])


def save(root, **kw):
    return save_run(root, result_of(plain), SETTINGS, "/gone/s.py", b"x = 1\n", "2026-11-05", "0.1.4", **kw)


def edit_json(path, **changes):
    data = json.loads(path.read_text())
    data.update(changes)
    path.write_text(json.dumps(data))


def test_save_and_list_round_trip(tmp_path):
    save(tmp_path)
    [listed] = list_runs(tmp_path)
    assert listed.name == "s.py" and listed.settings == SETTINGS and listed.readable and listed.format == RESULTS_FORMAT
    assert listed.strategy_path == "/gone/s.py" and listed.data_through == "2026-11-05" and listed.app_version == "0.1.4"
    assert (listed.folder / "strategy.py").read_bytes() == b"x = 1\n"
    loaded = load_result(listed)
    assert loaded.portfolio["value"].tolist() == pytest.approx(result_of(plain).portfolio["value"].tolist())
    assert loaded.portfolio["date"].dtype.kind == "M" and loaded.dividends is True


def test_list_is_newest_first(tmp_path):
    older = save(tmp_path, now=NOW)
    newer = save(tmp_path, now=NOW + timedelta(seconds=1))
    assert [r.id for r in list_runs(tmp_path)] == [newer.id, older.id]


def test_same_second_gets_a_suffix(tmp_path):
    a, b = save(tmp_path, now=NOW), save(tmp_path, now=NOW)
    assert a.id == "20261108-120000" and b.id.startswith(a.id + "-") and len(b.id) == len(a.id) + 5
    assert [r.id for r in list_runs(tmp_path)] == [b.id, a.id]


def test_same_second_leaves_another_saves_staging_alone(tmp_path):
    other = tmp_path / ".20261108-120000.tmp"  # another save of the same second, still writing
    other.mkdir()
    (other / "strategy.py").write_bytes(b"y = 2\n")
    rec = save(tmp_path, now=NOW)
    assert rec.id.startswith("20261108-120000-") and (other / "strategy.py").read_bytes() == b"y = 2\n"


def test_failed_save_leaves_no_run(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr("app.runs.write_results", boom)
    with pytest.raises(OSError):
        save(tmp_path)
    assert list_runs(tmp_path) == [] and not any(tmp_path.iterdir())


def test_cancel_at_the_save_boundary_leaves_no_run(tmp_path):
    with pytest.raises(Cancelled):
        save(tmp_path, cancelled=lambda: True)
    assert list_runs(tmp_path) == [] and not any(tmp_path.iterdir())


def test_truncated_current_format_csv_is_unreadable(tmp_path):
    rec = save(tmp_path)
    (rec.folder / "portfolio.csv").write_text("# note\ndate,val")  # header cut short, no rows
    [listed] = list_runs(tmp_path)
    assert listed.readable is False


@pytest.mark.parametrize("name, text", [
    ("portfolio.csv", "# note\ndate,value\n"),  # no rows
    ("portfolio.csv", "# note\ndate,value\nnot a date,1\n"),
    ("portfolio.csv", "# note\ndate,value\n2020-01-02,inf\n"),
    ("positions.csv", "# note\ndecided_on,fill_price\n"),  # no warning column
])
def test_other_damage_is_unreadable(tmp_path, name, text):
    rec = save(tmp_path)
    (rec.folder / name).write_text(text)
    assert list_runs(tmp_path)[0].readable is False


def test_older_or_damaged_runs_are_listed_not_fatal(tmp_path):
    good = save(tmp_path)
    (tmp_path / "20200101-000000").mkdir()
    (tmp_path / "20200101-000000" / "run.json").write_text("{not json")
    (tmp_path / "20200103-000000").mkdir()  # no run.json at all
    old = tmp_path / "20200102-000000"
    shutil.copytree(good.folder, old)
    edit_json(old / "run.json", format=0)
    runs = {r.id: r for r in list_runs(tmp_path)}
    assert runs["20200101-000000"].readable is False and runs["20200101-000000"].settings is None
    assert runs["20200103-000000"].readable is False
    assert runs["20200102-000000"].format == 0 and runs["20200102-000000"].readable
    assert runs[good.id].readable


def test_rename_delete_export(tmp_path):
    root = tmp_path / "runs"
    rec = save(root)
    renamed = rename_run(rec, "my run")
    assert renamed.name == "my run" and list_runs(root)[0].name == "my run"
    out = tmp_path / "out" / "x"
    export_run(renamed, str(out))
    for f in ("portfolio.csv", "positions.csv"):
        assert (out / f).read_bytes() == (rec.folder / f).read_bytes()
    delete_run(renamed)
    assert list_runs(root) == [] and not rec.folder.exists()


def test_runs_dir_is_under_the_data_folder(tmp_path, monkeypatch):
    from app import runs
    monkeypatch.setattr(runs, "user_data_dir", lambda *a, **k: str(tmp_path))
    assert runs.runs_dir() == tmp_path / "runs" and (tmp_path / "runs").is_dir()
