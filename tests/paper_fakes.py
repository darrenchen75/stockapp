"""In-memory stand-ins for the Alpaca broker, shared by the paper tests."""
from dataclasses import replace

from app.paper import Account, BrokerOrder

FINISHED = {"filled", "canceled", "expired", "rejected", "replaced"}
DUPLICATE = '{"code":42210000,"message":"client_order_id must be unique"}'  # Alpaca's real 422 body


class FakeBroker:
    """Same methods as app.paper.Broker. Tests set the public fields directly."""

    def __init__(self, number="PA0000001", equity=100_000.0, sessions=(), positions=None, history=(),
                 splits=None, orders=()):
        self.acct = Account(number, "https://paper-api.alpaca.markets", equity, equity, 2 * equity)
        self.sessions = list(sessions)  # dates
        self.held = dict(positions or {})  # symbol -> qty
        self.history = list(history)  # (date, equity)
        self.split_dates = dict(splits or {})  # symbol -> ex date
        self.split_error = None  # an exception splits() raises
        self.orders = list(orders)  # BrokerOrder, oldest first
        self.is_open = False
        self.submitted = []  # (client_order_id, tif)
        self.calls = 0  # submit() calls, including refused ones
        self.on_submit = None  # called with the call number as each submit starts; may raise or change state

    def account(self):
        return self.acct

    def calendar(self, start, end):
        return [d for d in self.sessions if start <= d <= end]

    def positions(self):
        return dict(self.held)

    def orders_since(self, after):  # shortcut: ignores `after`; tests put in only the orders they mean
        return list(self.orders)

    def open_orders(self):
        return [o for o in self.orders if o.status not in FINISHED]

    def order(self, client_order_id):
        return next((o for o in self.orders if o.client_order_id == client_order_id), None)

    def submit(self, order, tif="opg"):
        self.calls += 1
        if self.on_submit:
            self.on_submit(self.calls)
        if self.order(order.client_order_id):
            raise Exception(DUPLICATE)
        out = BrokerOrder(order.client_order_id, f"id-{len(self.orders)}", order.symbol, order.side,
                          float(order.qty), 0.0, None, "accepted")
        self.orders.append(out)
        self.submitted.append((order.client_order_id, tif))
        return out

    def cancel_all(self):
        out = []
        for i, o in enumerate(self.orders):
            if o.status not in FINISHED:
                self.orders[i] = replace(o, status="canceled")
                out.append((o.id, True))
        return out

    def equity_history(self):
        return list(self.history)

    def splits(self, symbols, start, end):
        if self.split_error:
            raise self.split_error
        return {s: d for s, d in self.split_dates.items() if s in symbols and start <= d <= end}

    def market_open(self):
        return self.is_open
