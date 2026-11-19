# CI runs this inside each packaged app (StockBars --selftest <this file>) to prove a
# user strategy can import the standard library, pandas and numpy from the frozen build.
import json
import statistics
from email.mime.text import MIMEText  # a stdlib submodule, not just a top-level module

import numpy as np
import pandas as pd


def target_weights(history, as_of):
    closes = pd.Series(history["c"]["A"].to_numpy())
    assert abs(statistics.fmean(closes) - float(np.mean(closes))) < 1e-9
    assert json.loads(json.dumps({"ok": True}))["ok"] and MIMEText("x")
    return {"A": 1.0}
