import logging
from datetime import date, datetime, timedelta

from alpaca.trading.requests import GetCalendarRequest

from app.sessions import Session
from app.store import ET, Store

log = logging.getLogger(__name__)
RETRY = timedelta(hours=1)
_failed = False  # first failure logs a traceback, later ones one line


def fetch_sessions(trading, start: date, end: date) -> list[Session]:
    rows = trading.get_calendar(GetCalendarRequest(start=start, end=end))
    # alpaca-py gives naive New York datetimes
    return [Session(r.date, int(r.open.replace(tzinfo=ET).timestamp()),
                    int(r.close.replace(tzinfo=ET).timestamp())) for r in rows]


def refresh(trading, store: Store, now: datetime) -> None:
    """Re-download the calendar at most daily, after a failure at most hourly. Failures are logged:
    bars never depend on it."""
    global _failed
    try:
        failed = store.meta("calendar_failed_at")
        if failed and now - datetime.fromisoformat(failed) < RETRY:
            return
        at = store.meta("calendar_at")
        if at and now - datetime.fromisoformat(at) < timedelta(days=1):
            return
        today = now.astimezone(ET).date()
        start, end = today - timedelta(days=35), today + timedelta(days=10)
        store.save_sessions(fetch_sessions(trading, start, end))
        lo, hi = store.meta("calendar_from"), store.meta("calendar_to")
        store.set_meta("calendar_from", min(lo, start.isoformat()) if lo else start.isoformat())
        store.set_meta("calendar_to", max(hi, end.isoformat()) if hi else end.isoformat())
        store.set_meta("calendar_at", now.isoformat())
        _failed = False
    except Exception as e:
        if _failed:
            log.warning("market calendar refresh failed: %s", e)
        else:
            log.exception("market calendar refresh failed")
            _failed = True
        try:
            store.set_meta("calendar_failed_at", now.isoformat())
        except Exception:
            pass
