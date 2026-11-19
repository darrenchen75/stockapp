"""Group-by views of the viewer list. ETFs are never members; they back sector and index groups."""
from collections import defaultdict
from dataclasses import dataclass

from app.listing import ETFS, Listing

BY = ("none", "sector", "exchange", "index")


@dataclass(frozen=True)
class Group:
    name: str
    members: list[str]
    avg_pct: float | None
    etf: str | None


def build(by: str, listings: dict[str, Listing], exchanges: dict[str, str],
          pct: dict[str, float | None]) -> list[Group]:
    if by == "none":
        return []
    members = defaultdict(list)
    for s, x in sorted(listings.items()):
        if x.etf_for is None:
            for name in {"sector": [x.sector], "exchange": [exchanges.get(s, "Unknown")], "index": x.indexes}[by]:
                members[name].append(s)
    etf_of = {} if by == "exchange" else {group: s for s, (_, group) in ETFS.items()}
    out = []
    for name in sorted(members):
        vals = [pct[s] for s in members[name] if pct.get(s) is not None]
        out.append(Group(name, members[name], sum(vals) / len(vals) if vals else None, etf_of.get(name)))
    return out
