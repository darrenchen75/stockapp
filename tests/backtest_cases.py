import pandas as pd


def bars(rows):
    """rows: (symbol, date, o, c, v, factor_all, factor_split); h and l equal the larger and smaller of o and c."""
    return pd.DataFrame([dict(symbol=s, date=d, o=o, h=max(o, c), l=min(o, c), c=c, v=v,
                              factor_all=fa, factor_split=fs) for s, d, o, c, v, fa, fs in rows])


D = ["2020-01-02", "2020-01-03", "2020-01-06", "2020-01-07"]
SPLIT = bars([("A", D[0], 100, 100, 10, .5, .5), ("A", D[1], 100, 100, 10, .5, .5),
              ("A", D[2], 50, 50, 20, 1, 1), ("A", D[3], 50, 60, 20, 1, 1)])
DIVIDEND = bars([("A", D[0], 100, 100, 10, .98, 1), ("A", D[1], 100, 100, 10, .98, 1),
                 ("A", D[2], 98, 98, 10, 1, 1)])
# A: d1 o10 c10; d2 o10 c11; d3 o12 c12; d4 o12 c15, no corporate actions
plain = bars([("A", D[0], 10, 10, 1, 1, 1), ("A", D[1], 10, 11, 1, 1, 1),
              ("A", D[2], 12, 12, 1, 1, 1), ("A", D[3], 12, 15, 1, 1, 1)])


def all_in(symbol):
    return lambda history, as_of: {symbol: 1.0}
