import logging
from datetime import date, datetime, timedelta

from alpaca.trading.requests import GetCalendarRequest

from app.sessions import Session
from app.store import ET, Store

log = logging.getLogger(__name__)
RETRY = timedelta(hours=1)
_failed: set[str] = set()  # first failure logs a traceback, later ones one line


def fetch_sessions(trading, start: date, end: date) -> list[Session]:
    rows = trading.get_calendar(GetCalendarRequest(start=start, end=end))
    # alpaca-py gives naive New York datetimes
    return [Session(r.date, int(r.open.replace(tzinfo=ET).timestamp()),
                    int(r.close.replace(tzinfo=ET).timestamp())) for r in rows]


def fetch_exchanges(trading) -> dict[str, str]:
    # raw rows: alpaca-py's Exchange enum rejects new codes such as TXSE
    assets = trading.get("/assets", {"asset_class": "us_equity"})
    if not assets:  # an empty answer would wipe the cache and look fresh for a week
        raise ValueError("no assets returned")
    return {a["symbol"]: a["exchange"] for a in assets if a.get("symbol") and a.get("exchange")}


def refresh(trading, store: Store, now: datetime) -> None:
    """Re-download the calendar daily and the exchanges weekly, after a failure at most hourly.
    Failures are logged and independent of each other: bars never depend on either."""
    def calendar():
        today = now.astimezone(ET).date()
        start, end = today - timedelta(days=35), today + timedelta(days=10)
        store.save_sessions(fetch_sessions(trading, start, end))
        lo, hi = store.meta("calendar_from"), store.meta("calendar_to")
        store.set_meta("calendar_from", min(lo, start.isoformat()) if lo else start.isoformat())
        store.set_meta("calendar_to", max(hi, end.isoformat()) if hi else end.isoformat())

    _refresh("calendar", timedelta(days=1), calendar, store, now)
    _refresh("exchanges", timedelta(days=7), lambda: store.save_exchanges(fetch_exchanges(trading)), store, now)


def _refresh(name: str, every: timedelta, work, store: Store, now: datetime) -> None:
    try:
        failed = store.meta(f"{name}_failed_at")
        if failed and now - datetime.fromisoformat(failed) < RETRY:
            return
        at = store.meta(f"{name}_at")
        if at and now - datetime.fromisoformat(at) < every:
            return
        work()
        store.set_meta(f"{name}_at", now.isoformat())
        _failed.discard(name)
    except Exception as e:
        if name in _failed:
            log.warning("market %s refresh failed: %s", name, e)
        else:
            log.exception("market %s refresh failed", name)
            _failed.add(name)
        try:
            store.set_meta(f"{name}_failed_at", now.isoformat())
        except Exception:
            pass
