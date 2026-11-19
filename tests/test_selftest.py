from app.backtest import selftest


def test_selftest_runs_the_smoke_strategy():
    assert selftest("packaging/smoke_strategy.py") == 0


def test_selftest_fails_on_a_strategy_that_cannot_import(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("import not_a_module\n")
    assert selftest(str(bad)) == 1
