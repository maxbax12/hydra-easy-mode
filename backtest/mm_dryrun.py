"""
Dry run of the REAL MarketMakerStrategy against a stub exchange.

The stub behaves like the node where it matters: per-asset channel capacity is
reserved by resting orders and enforced on placement, fills move capacity
between the sending and receiving side, a BUY's remainder is reported in quote
units (as OrderTracker does), and several markets share one balance sheet.
No node, no funds, no orders.

    python3 backtest/mm_dryrun.py        # from the repo root, project venv
"""
import asyncio, glob, json, math, os, sys, types, tempfile, logging, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
logging.basicConfig(level=logging.WARNING, format="      %(levelname)s %(message)s")

from connectors.base_exchange import TradingPair, OrderSide
from strategies.market_maker import MarketMakerStrategy

RESULTS = []
def check(label, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))

D = lambda x: types.SimpleNamespace(value=repr(float(x)))
CUR = {
    "ETH": types.SimpleNamespace(protocol=2, network_id="1", asset_id="0x" + "0" * 40),
    "BTC": types.SimpleNamespace(protocol=1, network_id="f9beb4d9", asset_id="0x" + "0" * 64),
    "USDC.arb": types.SimpleNamespace(protocol=2, network_id="42161", asset_id="erc20:0xaf88"),
    "USDC.eth": types.SimpleNamespace(protocol=2, network_id="1", asset_id="erc20:0xa0b8"),
}
MARKETS = {  # symbol: (base, quote, base_prec, quote_prec, min_base, min_quote)
    "ETH/BTC": ("ETH", "BTC", 6, 7, 0.0001, 0.00001),
    "BTC/USDC.arb": ("BTC", "USDC.arb", 7, 2, 0.00001, 1.0),
    "USDC.arb/USDC.eth": ("USDC.arb", "USDC.eth", 2, 5, 1.0, 0.001),
}
key = lambda a: (CUR[a].network_id, CUR[a].asset_id)


class StubOrder:
    n = 0
    def __init__(self, sym, side, price, amount):
        StubOrder.n += 1
        self.id = f"o{StubOrder.n:05d}"
        self.sym, self.side, self.price, self.amount = sym, side, price, amount
        self.filled, self.remaining = 0.0, amount   # as returned by place_order: base units
        self.open_base = amount                      # the stub's own truth
        self.status = types.SimpleNamespace(value="open")


class StubClient:
    def __init__(self, ex): self.ex = ex
    def get_public_key(self): return bytes.fromhex("ab" * 32)          # our node identity
    def get_market_info(self, base, quote):
        for sym, (b, q, bp, qp, mb, mq) in MARKETS.items():
            if CUR[b] is base and CUR[q] is quote:
                return types.SimpleNamespace(min_base_amount=D(mb), min_quote_amount=D(mq))
    def get_orderbook_balances(self):
        snapshot = self._balances_view()
        for o, amt in self.ex.pending_release:        # visible from the NEXT read on
            self.ex._release(o, amt)
        self.ex.pending_release.clear()
        return snapshot

    def _balances_view(self):
        return [types.SimpleNamespace(currency=types.SimpleNamespace(network_id=k[0], asset_id=k[1]),
                                      balance=types.SimpleNamespace(sending=D(v["send"]), receiving=D(v["recv"])))
                for k, v in self.ex.bal.items()]


class StubExchange:
    name = "hydra"
    def __init__(self, capacity):
        # capacity: asset -> (send, recv) FREE amounts
        self.bal = {key(a): {"send": s, "recv": r} for a, (s, r) in capacity.items()}
        self.orders, self.comp, self.rejected, self.crossed = [], {}, [], []
        self.reject_all = False
        self.terms, self.refuse_next, self.last_refusal = [], None, {}
        self.pending_release = []   # the balance API lags a cancel by one read
        self._market_cache = {}
        self.client = StubClient(self)
        self.cancels = 0
    def pair(self, sym):
        b, q, *_ = MARKETS[sym]
        return TradingPair(base=b, quote=q, symbol=sym)
    async def get_trading_pairs(self): return [self.pair(s) for s in MARKETS]
    async def ensure_market_initialized(self, pair):
        b, q, bp, qp, *_ = MARKETS[pair.symbol]
        self._market_cache[pair.symbol] = {"base_currency": CUR[b], "quote_currency": CUR[q],
                                           "base_precision": bp, "quote_precision": qp}
        return True
    def _get_currencies_for_pair(self, pair):
        b, q, *_ = MARKETS[pair.symbol]
        return CUR[b], CUR[q]
    def legs(self, sym, side, amount, price):
        b, q, *_ = MARKETS[sym]
        return ((q, amount * price), (b, amount)) if side == OrderSide.BUY else ((b, amount), (q, amount * price))
    async def get_orderbook(self, pair, depth=1):
        bids, asks = {}, {}
        for o in self.orders:
            if o.sym == pair.symbol:
                book = bids if o.side == OrderSide.BUY else asks
                book[o.price] = book.get(o.price, 0) + o.amount
        cb, ca = self.comp.get(pair.symbol, ([], []))
        for p, v in cb: bids[p] = bids.get(p, 0) + v
        for p, v in ca: asks[p] = asks.get(p, 0) + v
        return types.SimpleNamespace(bids=sorted(bids.items(), reverse=True), asks=sorted(asks.items()))
    async def place_order(self, pair, side, type, amount, price, params=None):
        self.terms.append(dict(params or {}))
        if self.refuse_next:                          # a refusal by prefix (e.g. post_only_would_cross)
            self.last_refusal = {(pair.symbol, side): self.refuse_next}
            self.refuse_next = None
            self.rejected.append((pair.symbol, side, price, amount)); return None
        for o, amt in self.pending_release:          # the node itself has already freed it
            self._release(o, amt)
        self.pending_release.clear()
        cb, ca = self.comp.get(pair.symbol, ([], []))
        if (side == OrderSide.BUY and ca and price >= min(p for p, _ in ca)) or \
           (side == OrderSide.SELL and cb and price <= max(p for p, _ in cb)):
            self.crossed.append((pair.symbol, side, price))
        (sa, samt), (ra, ramt) = self.legs(pair.symbol, side, amount, price)
        # like the node: it accepts ~0.7% less receiving than the balance API
        # reports, and reserves ~0.2% on top of the order size
        if self.reject_all or self.bal[key(sa)]["send"] < samt - 1e-12 or \
           self.bal[key(ra)]["recv"] * 0.993 < ramt * 1.002 - 1e-12:
            self.rejected.append((pair.symbol, side, price, amount)); return None
        self.bal[key(sa)]["send"] -= samt; self.bal[key(ra)]["recv"] -= ramt
        o = StubOrder(pair.symbol, side, price, amount); self.orders.append(o); return o
    async def cancel_order(self, order_id, pair=None):
        o = next((x for x in self.orders if x.id == order_id), None)
        if not o: return False
        self.orders.remove(o); self.cancels += 1
        self.pending_release.append((o, self._open_base(o)))   # node frees it, API shows it later
        return True
    def _open_base(self, o):
        return o.open_base
    def _release(self, o, base_amt):
        (sa, samt), (ra, ramt) = self.legs(o.sym, o.side, base_amt, o.price)
        self.bal[key(sa)]["send"] += samt; self.bal[key(ra)]["recv"] += ramt
    def fill(self, o, base_amt=None):
        """Fill (part of) a resting order: reserved capacity is spent, the
        opposite side of each channel grows."""
        open_base = self._open_base(o)
        amt = open_base if base_amt is None else min(base_amt, open_base)
        (sa, samt), (ra, ramt) = self.legs(o.sym, o.side, amt, o.price)
        self.bal[key(sa)]["recv"] += samt        # what we sent can come back
        self.bal[key(ra)]["send"] += ramt        # what we received we can send
        o.filled += amt
        rem_base = open_base - amt
        o.open_base = rem_base
        # OrderTracker quirk: after a fill update a BUY's remainder is in quote units
        o.remaining = rem_base if o.side == OrderSide.SELL else rem_base * o.price
        if rem_base <= 1e-12:
            self.orders.remove(o)
        return amt
    def mine(self, sym, side=None):
        return [o for o in self.orders if o.sym == sym and (side is None or (o.side == OrderSide.BUY) == (side == "buy"))]


class StubOracle:
    def __init__(self, prices): self.prices = dict(prices)
    def get_market_pair_price(self, pair): return self.prices.get(pair)
    get_pair_price = get_market_pair_price


class StubTracker:
    def __init__(self):
        self.tracked, self.stop_on_swap_failure, self.swap_failure_callbacks = [], True, []
        self.event_callbacks = []
    def track_order(self, o): self.tracked.append(o.id)
    def add_event_callback(self, cb): self.event_callbacks.append(cb)


FAIR = {"ETH/BTC": 0.0318, "BTC/USDC.arb": 84000.0}
PARAMS = {
    "ETH/BTC": dict(levels=6, level_size=0.03, half_spread_pct=0.15, level_step_pct=0.15,
                    max_position=0.18, skew_pct_at_max=0.3, size_skew=0.5, reprice_threshold_pct=0.05),
    "BTC/USDC.arb": dict(levels=6, level_size=0.001, half_spread_pct=0.15, level_step_pct=0.15,
                         max_position=0.006, skew_pct_at_max=0.3, size_skew=0.5, reprice_threshold_pct=0.05),
    "USDC.arb/USDC.eth": dict(levels=5, level_size=100, half_spread_pct=0.10, level_step_pct=0.10,
                              max_position=500, skew_pct_at_max=0.2, size_skew=0.5,
                              reprice_threshold_pct=0.02, fair_value=1.0),
}
BIG = {"ETH": (1.0, 1.0), "BTC": (0.1, 0.1), "USDC.arb": (5000, 5000), "USDC.eth": (5000, 5000)}


async def mm(sym, ex=None, oracle=None, tracker=None, state_dir=None, prepare=None, **over):
    """A started MarketMakerStrategy on a stub node. prepare(s), if given, runs on the
    strategy right before initialize() (to inject extra stubs)."""
    ex = ex or StubExchange(BIG)
    params = {**PARAMS[sym], "refresh_interval": 999, "ref_refresh": 0,
              "state_dir": state_dir or tempfile.mkdtemp(), "status_interval": 1e9, **over}
    s = MarketMakerStrategy(f"mm_{sym}", {"pair": sym, "exchange": "hydra", "params": params})
    s.exchange, s.order_tracker = ex, tracker or StubTracker()
    s.price_oracle = oracle or StubOracle(FAIR)
    if prepare:
        prepare(s)
    assert await s.initialize(), "initialize failed"
    await s.run_pass()
    return s, ex


async def settle():
    await asyncio.sleep(0.05)


def prices(ex, sym, side):
    ps = sorted(o.price for o in ex.mine(sym, side))
    return ps[::-1] if side == "buy" else ps


async def main():
    print("\n1. quoting")
    s, ex = await mm("ETH/BTC")
    b, a = prices(ex, "ETH/BTC", "buy"), prices(ex, "ETH/BTC", "sell")
    check("6 bids and 6 asks", len(b) == 6 and len(a) == 6, f"{len(b)} / {len(a)}")
    check("bids below fair, asks above", max(b) < FAIR["ETH/BTC"] < min(a), f"{max(b)} < {FAIR['ETH/BTC']} < {min(a)}")
    check("ladder strictly ordered", b == sorted(set(b), reverse=True) and a == sorted(set(a)))
    check("innermost at fair -/+ 0.15%",
          abs(max(b) - math.floor(0.0318 * 0.9985 * 1e7 + 1e-9) / 1e7) < 1e-12 and
          abs(min(a) - math.ceil(0.0318 * 1.0015 * 1e7 - 1e-9) / 1e7) < 1e-12, f"{max(b)} / {min(a)}")
    check("prices on the 1e-7 tick", all(abs(p * 1e7 - round(p * 1e7)) < 1e-6 for p in b + a))
    check("every order registered with the tracker", len(s.order_tracker.tracked) == 12)
    check("tracker told not to kill the bot on swap failure", s.order_tracker.stop_on_swap_failure is False)
    s.prepare_for_shutdown()

    print("\n2. per-market precision")
    s, ex = await mm("BTC/USDC.arb")
    ps = [o.price for o in ex.mine("BTC/USDC.arb")]
    check("BTC/USDC prices on the $0.01 tick", all(abs(p * 100 - round(p * 100)) < 1e-6 for p in ps), f"{sorted(ps)[:2]}")
    check("sizes on 7 decimals", all(abs(o.amount * 1e7 - round(o.amount * 1e7)) < 1e-6 for o in ex.mine("BTC/USDC.arb")))
    s.prepare_for_shutdown()
    s, ex = await mm("USDC.arb/USDC.eth")
    b, a = prices(ex, "USDC.arb/USDC.eth", "buy"), prices(ex, "USDC.arb/USDC.eth", "sell")
    check("USDC/USDC around fixed 1.0: 0.999 / 1.001", b[0] == 0.999 and a[0] == 1.001, f"{b[0]} / {a[0]}")
    check("sizes 100.00 USDC", all(o.amount == 100.0 for o in ex.mine("USDC.arb/USDC.eth")))
    s.prepare_for_shutdown()

    print("\n3. no churn on small moves")
    oracle = StubOracle(FAIR)
    s, ex = await mm("ETH/BTC", oracle=oracle)
    before = ex.cancels
    for f in (0.03181, 0.03179, 0.031805, 0.031795):     # +-0.03%, under the 0.05% threshold
        oracle.prices["ETH/BTC"] = f
        await s.run_pass()
    check("zero cancels while fair wobbles 0.03%", ex.cancels == before, f"{ex.cancels - before} cancels")
    oracle.prices["ETH/BTC"] = 0.0318 * 1.003                # +0.3%
    await s.run_pass()
    b, a = prices(ex, "ETH/BTC", "buy"), prices(ex, "ETH/BTC", "sell")
    check("0.3% move: book follows", min(a) > 0.0318 * 1.003 and max(b) < 0.0318 * 1.003, f"{max(b)} / {min(a)}")
    check("still 12 quotes, no duplicates", len(ex.mine("ETH/BTC")) == 12 and len(s.orders) == 12)
    s.prepare_for_shutdown()

    print("\n4. fills move inventory and skew the book")
    s, ex = await mm("ETH/BTC")
    top_bid = prices(ex, "ETH/BTC", "buy")[0]
    for o in list(ex.mine("ETH/BTC", "buy"))[:3]:            # three bids filled
        ex.fill(o); s.on_order_filled(o.id)
    await settle(); await s.run_pass()
    check("position +0.09 ETH", abs(s.position - 0.09) < 1e-9, f"{s.position}")
    b, a = prices(ex, "ETH/BTC", "buy"), prices(ex, "ETH/BTC", "sell")
    check("long => bids lowered", b[0] < top_bid, f"{top_bid} -> {b[0]}")
    bs = [o.amount for o in ex.mine("ETH/BTC", "buy")]; as_ = [o.amount for o in ex.mine("ETH/BTC", "sell")]
    check("long => bids smaller, asks bigger", max(bs) < 0.03 < min(as_), f"bids {max(bs)}, asks {min(as_)}")
    check("asks never below fair", min(a) >= 0.0318)
    s.prepare_for_shutdown()

    print("\n5. max position (soft limits)")
    s, ex = await mm("ETH/BTC")
    flat_bid = prices(ex, "ETH/BTC", "buy")[0]; s.prepare_for_shutdown()
    s, ex = await mm("ETH/BTC", inventory_offset=0.18)
    bs = [o.amount for o in ex.mine("ETH/BTC", "buy")]; b = prices(ex, "ETH/BTC", "buy")
    check("at +max: bids still quoted, at 25% size", len(bs) == 6 and max(bs) <= 0.03 * 0.25 + 1e-9, f"{len(bs)} bids, {bs[:2]}")
    check("at +max: bids pushed wider (>= fair*(1 - 0.3% skew - 0.15% - 0.5% extra))",
          b[0] <= 0.0318 * (1 - 0.003) * (1 - 0.0015 - 0.005) + 1e-7, f"{flat_bid} flat -> {b[0]}")
    check("at +max: asks full (reducing side untouched)", len(ex.mine("ETH/BTC", "sell")) == 6)
    s.prepare_for_shutdown()
    s, ex = await mm("ETH/BTC", inventory_offset=-0.18)
    as_ = [o.amount for o in ex.mine("ETH/BTC", "sell")]; a = prices(ex, "ETH/BTC", "sell")
    check("at -max: asks still quoted, small and wide", len(as_) == 6 and max(as_) <= 0.0075 + 1e-9
          and a[0] >= 0.0318 * 1.003 * (1 + 0.0015 + 0.005) - 1e-7, f"{as_[:2]} @ {a[0]}")
    check("at -max: bids full", len(ex.mine("ETH/BTC", "buy")) == 6)
    s.prepare_for_shutdown()
    s, ex = await mm("ETH/BTC", inventory_offset=-0.18 * 1.5)
    check("beyond 1.5x max: asks pulled, bids kept", not ex.mine("ETH/BTC", "sell") and len(ex.mine("ETH/BTC", "buy")) == 6)
    s.prepare_for_shutdown()
    s, ex = await mm("ETH/BTC", inventory_offset=-0.18 * 1.2)
    a2 = prices(ex, "ETH/BTC", "sell")
    check("between max and hard limit: wider still (extra grows with position^2)", a2 and a2[0] > a[0], f"{a[0]} -> {a2[0] if a2 else None}")
    s.prepare_for_shutdown()
    s, ex = await mm("ETH/BTC", inventory_offset=-0.18, hard_limit_mult=1.0)
    check("hard_limit_mult=1.0 restores the old hard cut-off", not ex.mine("ETH/BTC", "sell"))
    s.prepare_for_shutdown()

    print("\n6. partial fills are booked when a quote is replaced")
    oracle = StubOracle(FAIR)
    s, ex = await mm("ETH/BTC", oracle=oracle)
    sell = min(ex.mine("ETH/BTC", "sell"), key=lambda o: o.price)
    buy = max(ex.mine("ETH/BTC", "buy"), key=lambda o: o.price)
    ex.fill(sell, 0.012); ex.fill(buy, 0.007)
    oracle.prices["ETH/BTC"] = 0.0318 * 1.004                # forces a requote
    await s.run_pass(); await settle()
    check("sell partial 0.012 and buy partial 0.007 booked (net -0.005)",
          abs(s.position - (-0.012 + 0.007)) < 1e-9, f"position {s.position}")
    s.prepare_for_shutdown()

    print("\n7. stale reference")
    oracle = StubOracle(FAIR)
    s, ex = await mm("ETH/BTC", oracle=oracle, ref_max_age=0.2)
    oracle.prices["ETH/BTC"] = None
    await asyncio.sleep(0.3); await s.run_pass()
    check("quotes pulled when the reference is stale", not ex.mine("ETH/BTC"))
    oracle.prices["ETH/BTC"] = 0.0318
    await s.run_pass()
    check("quotes back when it is fresh", len(ex.mine("ETH/BTC")) == 12)
    s.prepare_for_shutdown()

    print("\n8. swap failure pauses only that market")
    tr = StubTracker(); ex = StubExchange(BIG)
    s1, _ = await mm("ETH/BTC", ex=ex, tracker=tr)
    s2, _ = await mm("BTC/USDC.arb", ex=ex, tracker=tr, oracle=StubOracle(FAIR))
    victim = ex.mine("ETH/BTC")[0].id
    for cb in tr.swap_failure_callbacks: cb(victim)
    await settle(); await s1.run_pass(); await s2.run_pass()
    check("ETH/BTC paused and pulled", not ex.mine("ETH/BTC"))
    check("BTC/USDC keeps quoting", len(ex.mine("BTC/USDC.arb")) == 12)
    s1.prepare_for_shutdown(); s2.prepare_for_shutdown()

    print("\n9. capacity-aware sizing")
    ex = StubExchange({**BIG, "BTC": (0.1, 0.0020)})         # BTC inbound only 0.002
    s, _ = await mm("ETH/BTC", ex=ex, fail_backoff=0.01)
    for _ in range(12):
        await s.run_pass(); await asyncio.sleep(0.02)
    sold_btc = sum(o.amount * o.price for o in ex.mine("ETH/BTC", "sell"))
    check("sells sized to the 0.002 BTC inbound", sold_btc <= 0.0020 + 1e-9, f"{sold_btc:.6f} BTC reserved")
    check("margin learns: at most 2 rejections, then placed", len(ex.rejected) <= 2 and
          not s._blocked, f"{len(ex.rejected)} rejected, margin {0.5 + s._haircut}%")
    check("uses >= 90% of the inbound (dust floor leaves <20% of a level)", sold_btc >= 0.0020 * 0.90,
          f"{sold_btc / 0.002:.1%}")
    n = len(ex.rejected)
    for _ in range(5): await s.run_pass()
    check("no further rejections once converged", len(ex.rejected) == n)
    check("bids unaffected", len(ex.mine("ETH/BTC", "buy")) == 6)
    s.prepare_for_shutdown()

    print("\n10. two markets share BTC capacity")
    ex = StubExchange({**BIG, "BTC": (0.008, 0.005)})
    tr = StubTracker()
    s1, _ = await mm("ETH/BTC", ex=ex, tracker=tr, fail_backoff=0.01)
    s2, _ = await mm("BTC/USDC.arb", ex=ex, tracker=tr, fail_backoff=0.01)
    for _ in range(12):
        await s1.run_pass(); await s2.run_pass(); await asyncio.sleep(0.02)
    btc_send = sum(o.amount * o.price for o in ex.mine("ETH/BTC", "buy")) + sum(o.amount for o in ex.mine("BTC/USDC.arb", "sell"))
    btc_recv = sum(o.amount * o.price for o in ex.mine("ETH/BTC", "sell")) + sum(o.amount for o in ex.mine("BTC/USDC.arb", "buy"))
    check("combined BTC sending within 0.008", btc_send <= 0.008 + 1e-9, f"{btc_send:.6f}")
    check("combined BTC receiving within 0.005", btc_recv <= 0.005 + 1e-9, f"{btc_recv:.6f}")
    n = len(ex.rejected)
    for _ in range(5): await s1.run_pass(); await s2.run_pass()
    check("converged: no further rejections", len(ex.rejected) == n, f"{n} early rejections")
    s1.prepare_for_shutdown(); s2.prepare_for_shutdown()

    print("\n11. node rejects everything")
    ex = StubExchange(BIG); ex.reject_all = True
    s, _ = await mm("ETH/BTC", ex=ex, fail_backoff=60)
    first = len(ex.rejected)
    for _ in range(10): await s.run_pass()
    check("backs off instead of retrying every pass", len(ex.rejected) == first, f"{first} then {len(ex.rejected)}")
    s.prepare_for_shutdown()

    print("\n12. never take liquidity (maker only)")
    ex = StubExchange(BIG)
    ex.comp["ETH/BTC"] = ([], [(0.0317, 0.05)])              # stale ask BELOW our would-be bid
    s, _ = await mm("ETH/BTC", ex=ex)
    check("no order placed through the book", not ex.crossed, f"{ex.crossed[:2]}")
    check("top bid one tick under their ask", prices(ex, "ETH/BTC", "buy")[0] <= 0.0317 - 1e-7 + 1e-12)
    b = prices(ex, "ETH/BTC", "buy")
    gaps = [b[i] / b[i + 1] - 1 for i in range(len(b) - 1)]
    check("ladder under their ask keeps its 0.15% spacing (not 1-tick steps)",
          len(b) == 6 and min(gaps) > 0.0014, f"gaps {[round(g * 100, 3) for g in gaps]}%")
    s.prepare_for_shutdown()

    print("\n13. external cancel is re-quoted")
    s, ex = await mm("ETH/BTC")
    o = ex.mine("ETH/BTC")[0]
    await ex.cancel_order(o.id); s.on_order_cancelled(o.id)
    await settle(); await s.run_pass()
    check("back to 12 quotes", len(ex.mine("ETH/BTC")) == 12)
    s.prepare_for_shutdown()

    print("\n14. shutdown")
    s, ex = await mm("ETH/BTC")
    await s.stop()
    check("stop() cancels all own quotes", not ex.mine("ETH/BTC"))

    print("\n15. state survives a restart")
    d = tempfile.mkdtemp()
    s, ex = await mm("ETH/BTC", state_dir=d)
    for o in list(ex.mine("ETH/BTC", "sell"))[:2]:
        ex.fill(o); s.on_order_filled(o.id)
    await settle(); pos = s.position; s.prepare_for_shutdown()
    s2, _ = await mm("ETH/BTC", state_dir=d)
    check("position restored", abs(s2.position - pos) < 1e-12 and s2.fills == s.fills, f"{pos} -> {s2.position}")
    s2.prepare_for_shutdown()

    print("\n16. capacity-limited book is stable (the 16:14 churn)")
    ex = StubExchange({**BIG, "ETH": (1.0, 0.100)})          # 3 full bids + a ~30% edge level
    s, _ = await mm("ETH/BTC", ex=ex, fail_backoff=0.01)
    for _ in range(15):
        await s.run_pass(); await asyncio.sleep(0.02)
    c0, p0 = ex.cancels, StubOrder.n
    for _ in range(20):
        await s.run_pass()
    check("steady state: zero cancels and zero new orders over 20 passes",
          ex.cancels == c0 and StubOrder.n == p0, f"{ex.cancels - c0} cancels, {StubOrder.n - p0} placed")
    bids = sorted(ex.mine("ETH/BTC", "buy"), key=lambda o: -o.price)
    sizes = [round(o.amount, 6) for o in bids]
    check("inner levels full, capacity not stranded outside", all(x >= 0.03 * 0.95 for x in sizes[:3])
          and all(sz >= 0.2 * 0.03 for sz in sizes), f"bid sizes {sizes}")
    check("asks untouched by the ETH shortage", len(ex.mine("ETH/BTC", "sell")) == 6)
    s.prepare_for_shutdown()

    print("\n17. inner level blocked, outer took the capacity: inner reclaims it")
    ex = StubExchange({**BIG, "ETH": (1.0, 0.117)})
    s, _ = await mm("ETH/BTC", ex=ex, fail_backoff=0.01)
    for _ in range(10):
        await s.run_pass(); await asyncio.sleep(0.02)
    top3 = sorted(ex.mine("ETH/BTC", "buy"), key=lambda o: -o.price)[:3]
    victim = top3[1]                                         # knock out L1 ...
    await ex.cancel_order(victim.id); s.on_order_cancelled(victim.id); await settle()
    ex.bal[key("ETH")]["recv"] += 0.0                        # ... capacity it held is free again
    for _ in range(10):
        await s.run_pass(); await asyncio.sleep(0.02)
    sizes = [round(o.amount, 6) for o in sorted(ex.mine("ETH/BTC", "buy"), key=lambda o: -o.price)]
    check("L0-L2 back within size tolerance after an external cancel",
          all(x >= 0.03 * 0.75 for x in sizes[:3]), f"{sizes}")
    s.prepare_for_shutdown()

    print("\n18. asymmetric book: bigger ask side")
    s, ex = await mm("BTC/USDC.arb", bid_size=0.0005, ask_size=0.0015)
    bs = sorted(o.amount for o in ex.mine("BTC/USDC.arb", "buy"))
    as_ = sorted(o.amount for o in ex.mine("BTC/USDC.arb", "sell"))
    check("bids 0.0005, asks 0.0015 per level", set(bs) == {0.0005} and set(as_) == {0.0015}, f"{bs[:2]} / {as_[:2]}")
    for o in list(ex.mine("BTC/USDC.arb", "sell"))[:2]:
        ex.fill(o); s.on_order_filled(o.id)
    await settle(); await s.run_pass()
    bs2 = [o.amount for o in ex.mine("BTC/USDC.arb", "buy")]
    check("inventory skew still applies on top (short => bids grow)", min(bs2) > 0.0005, f"{min(bs2)}")
    s.prepare_for_shutdown()

    print("\n19. taking mispriced orders (take_edge_pct)")
    TAKE = dict(take_edge_pct=0.3, taker_fee_pct=0.4, take_max_size=0.02, take_cooldown=30)
    ex = StubExchange(BIG)
    ex.comp["ETH/BTC"] = ([], [(0.0315, 0.05)])              # ask 0.94% below fair 0.0318
    s, _ = await mm("ETH/BTC", ex=ex, **TAKE)
    takes = [o for o in ex.mine("ETH/BTC", "buy") if o.price == 0.0315]
    check("cheap ask taken: one buy at their price, capped at take_max_size",
          len(takes) == 1 and takes[0].amount == 0.02, f"{[(o.price, o.amount) for o in takes]}")
    check("only the take crosses the book; quotes stay maker",
          ex.crossed == [("ETH/BTC", OrderSide.BUY, 0.0315)], f"{ex.crossed}")
    await s.run_pass()
    check("no second take while the first is live", len([o for o in ex.mine("ETH/BTC", "buy") if o.price == 0.0315]) <= 1
          and len(ex.crossed) == 1, f"{len(ex.crossed)} crossings")
    # it filled: position and P&L book it with the TAKER fee
    s2, ex2 = await mm("ETH/BTC", ex=StubExchange(BIG), **TAKE)
    ex2.comp["ETH/BTC"] = ([], [(0.0315, 0.05)])
    await s2.run_pass()
    t = next(o for o in ex2.mine("ETH/BTC", "buy") if o.price == 0.0315)
    ex2.fill(t); ex2.comp["ETH/BTC"] = ([], [(0.0315, 0.03)]); s2.on_order_filled(t.id)
    await settle()
    check("fill books +0.02 position, taker fee charged",
          abs(s2.position - 0.02) < 1e-12 and s2.rebate_est < 0, f"{s2.position} / fee {s2.rebate_est:.3g}")
    await s2.run_pass()
    check("cooldown: no immediate re-take", len(ex2.crossed) == 1, f"{len(ex2.crossed)} crossings")
    s2._last_take["buy"] = 0
    await s2.run_pass()
    check("after the cooldown it takes again", len(ex2.crossed) == 2, f"{len(ex2.crossed)} crossings")
    s.prepare_for_shutdown(); s2.prepare_for_shutdown()
    # an unfilled take is cancelled on the next pass
    ex3 = StubExchange(BIG); ex3.comp["ETH/BTC"] = ([], [(0.0315, 0.05)])
    s3, _ = await mm("ETH/BTC", ex=ex3, **TAKE)
    ex3.comp["ETH/BTC"] = ([], [])                           # their order vanished: ours did not match
    await s3.run_pass()
    check("a take is left alone while its swap may settle (take_settle)",
          len([o for o in ex3.mine("ETH/BTC") if o.price == 0.0315]) == 1)
    s3.cfg.take_settle = 0
    await s3.run_pass()
    check("unfilled take cancelled once take_settle has passed", not [o for o in ex3.mine("ETH/BTC") if o.price == 0.0315])
    s3.prepare_for_shutdown()
    # a fill confirmed AFTER our cancel went through (cancel raced the settling swap)
    s8, ex8 = await mm("ETH/BTC")
    o8 = ex8.mine("ETH/BTC", "sell")[0]
    pos0, fills0 = s8.position, s8.fills
    await s8._cancel(o8.id, "test")                          # we cancel, it books 0 filled
    s8._on_event(o8.id, "filled")                            # ...then the node says it filled
    check("late fill after a cancel is booked", abs(s8.position - (pos0 - o8.amount)) < 1e-12
          and s8.fills == fills0 + 1, f"{pos0} -> {s8.position}")
    s8._on_event(o8.id, "filled")
    check("...exactly once", abs(s8.position - (pos0 - o8.amount)) < 1e-12 and s8.fills == fills0 + 1)
    s8.prepare_for_shutdown()
    # not mispriced enough: 0.3% below fair < fee 0.4% + edge 0.3%
    ex4 = StubExchange(BIG); ex4.comp["ETH/BTC"] = ([], [(0.0317, 0.05)])
    s4, _ = await mm("ETH/BTC", ex=ex4, **TAKE)
    check("ask only 0.3% below fair is NOT taken", not ex4.crossed, f"{ex4.crossed}")
    s4.prepare_for_shutdown()
    # rich bid on USDC/USDC: sell into it
    ex5 = StubExchange(BIG); ex5.comp["USDC.arb/USDC.eth"] = ([(1.01, 30.0)], [])
    s5, _ = await mm("USDC.arb/USDC.eth", ex=ex5, take_edge_pct=0.1, taker_fee_pct=0.1, take_max_size=100)
    check("USDC/USDC: bid at 1.01 sold into (all 30)",
          ex5.crossed == [("USDC.arb/USDC.eth", OrderSide.SELL, 1.01)] and
          any(o.price == 1.01 and o.amount == 30.0 for o in ex5.mine("USDC.arb/USDC.eth", "sell")), f"{ex5.crossed}")
    s5.prepare_for_shutdown()
    # capacity held by our own bids: the take frees it from the outermost bid
    tight = {**BIG, "ETH": (1.0, 0.19)}                      # ETH inbound ~ just the 6 bids
    ex7 = StubExchange(tight)
    s7, _ = await mm("ETH/BTC", ex=ex7)
    s7.cfg.take_edge_pct, s7.cfg.taker_fee_pct, s7.cfg.take_max_size = 0.3, 0.4, 0.02
    n_bids = len(ex7.mine("ETH/BTC", "buy"))
    ex7.comp["ETH/BTC"] = ([], [(0.0315, 0.05)])
    await settle(); await s7.run_pass()
    t7 = [o for o in ex7.mine("ETH/BTC", "buy") if o.price == 0.0315]
    check("take gets capacity from our outermost bid when inbound is full",
          len(t7) == 1 and t7[0].amount > 0.015, f"{n_bids} bids before; take {[o.amount for o in t7]}")
    s7.prepare_for_shutdown()
    # off by default
    ex6 = StubExchange(BIG); ex6.comp["ETH/BTC"] = ([], [(0.0300, 0.05)])
    s6, _ = await mm("ETH/BTC", ex=ex6)
    check("off unless take_edge_pct is set", not ex6.crossed)
    s6.prepare_for_shutdown()

    print("\n20. fees follow the node (volume tiers)")
    class FeeClient(StubClient):
        taker = "0.003"
        def get_market_info(self, base, quote):
            mi = super().get_market_info(base, quote)
            mi.taker_base_fee, mi.maker_base_fee = D(self.taker), D("-0.001")
            return mi
    exf = StubExchange(BIG); exf.client = FeeClient(exf)
    s, _ = await mm("BTC/USDC.arb", ex=exf, taker_fee_pct=0.3, take_edge_pct=0.05, take_max_size=0.001)
    check("taker fee read from the node (0.3%)", abs(s._taker_fee_pct - 0.3) < 1e-9, f"{s._taker_fee_pct}")
    FeeClient.taker = "0.001"; s._fees_at = 0
    exf.comp["BTC/USDC.arb"] = ([(84000 * 1.002, 0.0005)], [])       # bid +0.2%: below 0.35%, above 0.15%
    await s.run_pass()
    check("tier cut to 0.1% is picked up and makes a +0.2% bid worth taking",
          abs(s._taker_fee_pct - 0.1) < 1e-9 and ("BTC/USDC.arb", OrderSide.SELL, 84000 * 1.002) in exf.crossed,
          f"fee {s._taker_fee_pct}, crossed {exf.crossed}")
    s.prepare_for_shutdown()

    print("\n21. stuck swap (timeout) vs node-reported failure")
    import order_tracker as OT
    tr = OT.OrderTracker.__new__(OT.OrderTracker)
    tr.logger = logging.getLogger("tracker"); tr.running = True; tr.stop_on_swap_failure = False
    tr.swap_timeout = 90.0; tr.pending_swaps = {"swapX": time.time() - 120}; tr._swap_orders = {"swapX": "orderX"}
    called = []; tr.swap_failure_callbacks = [called.append]
    real_sleep = OT.time.sleep
    def one_round(_):
        OT.time.sleep = lambda _s: setattr(tr, "running", False)
    OT.time.sleep = one_round
    try:
        tr._swap_watchdog()
    finally:
        OT.time.sleep = real_sleep
    check("stuck swap >90s: logged, NOT reported as a failure (market keeps quoting)",
          not called and "swapX" not in tr.pending_swaps, f"callbacks {called}")
    tr._notify_swap_failure("orderY")
    check("a node-reported failure still reaches the strategy", called == ["orderY"])

    print("\n22. hot reload: new params swapped in between passes")
    def cfg_of(s, **over):
        return MarketMakerStrategy.build_config(s.name, {"pair": s.cfg.pair, "exchange": "hydra",
                                                         "params": {**s.cfg.params, **over}})
    s, ex = await mm("USDC.arb/USDC.eth")
    ids0, c0 = {o.id for o in ex.mine("USDC.arb/USDC.eth")}, ex.cancels
    ch = s.queue_config(cfg_of(s, max_position=800))
    check("queued, not applied mid-pass", ch == {"max_position": (500, 800)} and s.cfg.max_position == 500, f"{ch}")
    await s.run_pass()
    check("applied on the next pass", s.cfg.max_position == 800 and s.config is s.cfg)
    check("flat position: max_position change moves no quote", ex.cancels == c0 and
          {o.id for o in ex.mine("USDC.arb/USDC.eth")} == ids0, f"{ex.cancels - c0} cancels")
    s.queue_config(cfg_of(s, max_position=800, half_spread_pct=0.2)); await s.run_pass()
    b, a = prices(ex, "USDC.arb/USDC.eth", "buy"), prices(ex, "USDC.arb/USDC.eth", "sell")
    check("spread 0.10 -> 0.20: book re-laddered at 0.998 / 1.002", b[0] == 0.998 and a[0] == 1.002, f"{b[0]} / {a[0]}")
    s.queue_config(cfg_of(s, max_position=800, half_spread_pct=0.2, levels=3)); await s.run_pass()
    check("levels 5 -> 3: outer levels cancelled", len(ex.mine("USDC.arb/USDC.eth", "buy")) == 3 and
          len(ex.mine("USDC.arb/USDC.eth", "sell")) == 3 and len(s.orders) == 6)
    ch = s.queue_config(cfg_of(s, max_position=800, half_spread_pct=0.2, levels=3))
    check("identical config: no change queued", ch == {} and s._pending_cfg is None)
    try:
        s.queue_config(MarketMakerStrategy.build_config(s.name, {"pair": "ETH/BTC", "params": s.cfg.params}))
        check("pair change refused in place (needs a rebuild)", False)
    except ValueError:
        check("pair change refused in place (needs a rebuild)", True)
    for bad in ({"max_position": -1}, {"levels": 0}, {"level_size": "abc"}, {"hard_limit_mult": 0.5}):
        try:
            cfg_of(s, **bad); ok = False
        except ValueError:
            ok = True
        check(f"invalid {bad} rejected", ok)
    check("unknown params reported, not fatal",
          MarketMakerStrategy.unknown_params({"params": {"levels": 3, "max_positon": 9}}) == ["max_positon"])
    s.prepare_for_shutdown()

    print("\n23. live rebalance booking (state/adjust_<strategy>.json)")
    s, ex = await mm("USDC.arb/USDC.eth")
    s.position, s.quote_flow = 417.4, -417.4 * 0.9985          # bought 417.4 at 0.9985
    await s.run_pass()
    top0 = prices(ex, "USDC.arb/USDC.eth", "buy")[0]
    pnl0 = s.pnl()["total"]
    path = s.adjust_path()
    def drop(body, age=5):
        with open(path, "w") as f: json.dump(body, f)
        t = time.time() - age; os.utime(path, (t, t))
    drop({"set": 0, "price": 1.0, "cost": 1.5}, age=0)
    await s.run_pass()
    check("a file younger than 1s is left for the next pass", os.path.exists(path) and s.position == 417.4)
    t = time.time() - 5; os.utime(path, (t, t))
    await s.run_pass()
    check("booked: position +417.4 -> 0", s.position == 0 and not os.path.exists(path)
          and glob.glob(path + ".done-*"), f"{s.position}")
    check("P&L unchanged apart from the 1.5 cost", abs(s.pnl()["total"] - (pnl0 - 1.5)) < 1e-9,
          f"{pnl0:.6f} -> {s.pnl()['total']:.6f}")
    top1 = prices(ex, "USDC.arb/USDC.eth", "buy")[0]
    check("skew gone: top bid back to 0.999 in the same pass", top1 == 0.999 and top0 < 0.999, f"{top0} -> {top1}")
    st = json.load(open(s._state_path()))
    check("persisted (position 0, cost 1.5, 1 rebalance)", st["position"] == 0 and st["rebalance_cost"] == 1.5
          and st["rebalances"] == 1, f"{st}")
    drop({"delta": -100})
    await s.run_pass()
    check("delta without price: booked at the current fair (1.0)", s.position == -100 and
          abs(s.pnl()["total"] - (pnl0 - 1.5)) < 1e-9)
    for body, why in (({"set": 0, "delta": 5}, "set and delta"), ({"cost": 1}, "neither"),
                      ({"set": 5000}, "beyond hard limit"), ({"set": "x"}, "not a number"),
                      ({"set": 0, "cost": -1}, "negative cost")):
        pos = s.position
        drop(body)
        await s.run_pass()
        check(f"rejected ({why}): position unchanged, file archived", s.position == pos and not os.path.exists(path)
              and glob.glob(path + ".rejected-*"))
    with open(path, "w") as f: f.write("{not json")
    t = time.time() - 5; os.utime(path, (t, t)); await s.run_pass()
    check("unreadable file rejected, bot keeps running", not os.path.exists(path) and s.position == -100)
    s2 = MarketMakerStrategy(s.name, {"pair": s.cfg.pair, "params": s.cfg.params}); s2._load_state()
    check("restart restores the booked position and cost", s2.position == -100 and s2.rebalance_cost == 1.5)
    s.prepare_for_shutdown()

    print("\n24. CLI reload: all-or-nothing, per strategy")
    import yaml
    import trading_bot_cli as TB
    from strategies.base.base_strategy import StrategyManager
    tmp = tempfile.mkdtemp()
    def entry(name, sym, enabled=True, **over):
        return {"name": name, "type": "market_maker", "exchange": "hydra", "pair": sym, "enabled": enabled,
                "params": {**PARAMS[sym], "refresh_interval": 999, "ref_refresh": 0, "state_dir": tmp,
                           "status_interval": 1e9, **over}}
    def write(*entries):
        with open(os.path.join(tmp, "bot.yaml"), "w") as f:
            yaml.safe_dump({"strategies": list(entries)}, f)
    ex = StubExchange(BIG); tr = StubTracker()
    bot = TB.TradingBotCLI.__new__(TB.TradingBotCLI)
    bot.logger = logging.getLogger("cli"); bot.exchanges = {"hydra": ex}; bot.order_trackers = {"hydra": tr}
    bot.price_oracle = StubOracle(FAIR); bot.strategy_manager = StrategyManager()
    bot._strategy_entries, bot._removed, bot._reload_lock, bot._reload_task, bot._closing = {}, {}, None, None, False
    bot.config_path = os.path.join(tmp, "bot.yaml")
    E, U = entry("mm_eb", "ETH/BTC"), entry("mm_uu", "USDC.arb/USDC.eth")
    write(E, U)
    bot.config = TB.BotConfig(strategies=[E, U])
    await bot._load_strategies()
    for n in ("mm_eb", "mm_uu"):
        await bot.strategy_manager.start_strategy(n)
    for st_ in bot.strategy_manager.strategies.values():
        st_._wake.set()
    await settle()
    eb_ids = {o.id for o in ex.mine("ETH/BTC")}
    check("two markets quoting", len(eb_ids) == 12 and len(ex.mine("USDC.arb/USDC.eth")) == 10)
    U2 = entry("mm_uu", "USDC.arb/USDC.eth", half_spread_pct=0.2)
    write(E, U2)
    ok = await bot.reload_config("test"); await settle()
    check("reload: USDC/USDC re-laddered live", ok and prices(ex, "USDC.arb/USDC.eth", "buy")[0] == 0.998)
    check("...ETH/BTC untouched (same orders)", {o.id for o in ex.mine("ETH/BTC")} == eb_ids)
    write(E, entry("mm_uu", "USDC.arb/USDC.eth", half_spread_pct=0.3, levels=0))
    ok = await bot.reload_config("test"); await settle()
    check("one bad strategy rejects the whole reload", not ok and
          bot.strategy_manager.strategies["mm_uu"].cfg.half_spread_pct == 0.2 and
          prices(ex, "USDC.arb/USDC.eth", "buy")[0] == 0.998)
    with open(bot.config_path, "w") as f: f.write("strategies: [unclosed")
    check("broken YAML rejected", not await bot.reload_config("test"))
    old_eb = bot.strategy_manager.strategies["mm_eb"]
    write(entry("mm_eb", "ETH/BTC", enabled=False), U2)
    await bot.reload_config("test"); await settle()
    check("disabled: ETH/BTC stopped and its orders cancelled",
          "mm_eb" not in bot.strategy_manager.strategies and not ex.mine("ETH/BTC")
          and len(ex.mine("USDC.arb/USDC.eth")) == 10)
    write(E, U2)
    await bot.reload_config("test")
    bot.strategy_manager.strategies["mm_eb"]._wake.set(); await settle()
    check("re-enabled: ETH/BTC quoting again", len(ex.mine("ETH/BTC")) == 12)
    check("old instance detached (no double booking)", old_eb._detached and
          old_eb._tracker_callback not in tr.event_callbacks and len(tr.event_callbacks) == 2,
          f"{len(tr.event_callbacks)} callbacks")
    old_uu = bot.strategy_manager.strategies["mm_uu"]
    write(E, entry("mm_uu", "BTC/USDC.arb"))
    await bot.reload_config("test")
    new_uu = bot.strategy_manager.strategies["mm_uu"]; new_uu._wake.set(); await settle()
    check("pair change: that strategy rebuilt on the new market", new_uu is not old_uu and
          not ex.mine("USDC.arb/USDC.eth") and len(ex.mine("BTC/USDC.arb")) == 12)
    bot.RELOAD_TRIGGER = TB.Path(os.path.join(tmp, "reload"))
    bot.start_reload_watch()
    write(entry("mm_eb", "ETH/BTC", levels=3), entry("mm_uu", "BTC/USDC.arb"))
    open(bot.RELOAD_TRIGGER, "w").close()
    await asyncio.sleep(2.5)
    check("touch trigger file -> reloaded (ETH/BTC down to 3 levels)", not os.path.exists(bot.RELOAD_TRIGGER)
          and len(ex.mine("ETH/BTC")) == 6, f"{len(ex.mine('ETH/BTC'))} orders")
    bot._closing = True; bot._reload_task.cancel()
    for st_ in bot.strategy_manager.strategies.values():
        st_.prepare_for_shutdown()

    print("\n25. order terms: post-only quotes, IOC takes, client_order_id, refusals by reason")
    s, ex = await mm("ETH/BTC")
    quotes = [t for t in ex.terms]
    ids = [t.get("client_order_id") for t in quotes]
    check("every quote is POST_ONLY", quotes and all(t.get("time_in_force") == "post_only" for t in quotes))
    check("every order has its own client_order_id (<= 64 chars)", len(set(ids)) == len(ids) and all(i and len(i) <= 64 for i in ids))
    s.prepare_for_shutdown()
    ex2 = StubExchange(BIG); ex2.comp["ETH/BTC"] = ([], [(0.0315, 0.01)])
    s2, _ = await mm("ETH/BTC", ex=ex2, take_edge_pct=0.1, take_max_size=0.02, taker_fee_pct=0.4)
    take = [t for t in ex2.terms if "-bt-" in t.get("client_order_id", "")]
    check("a take is IOC with self-trade prevention cancel_taker", take and take[0].get("time_in_force") == "ioc"
          and take[0].get("self_trade_prevention") == "cancel_taker", f"{take[:1]}")
    s2.prepare_for_shutdown()
    s3, ex3 = await mm("ETH/BTC")
    h0 = s3._haircut
    ex3.refuse_next = "post_only_would_cross"          # the re-placement after the cancel gets refused
    o = ex3.mine("ETH/BTC", "buy")[0]; await ex3.cancel_order(o.id); s3.on_order_cancelled(o.id); await settle()
    await s3.run_pass()
    check("post_only_would_cross: refused once, no capacity-margin bump, short block (requote next pass)",
          ex3.refuse_next is None and ex3.rejected and s3._haircut == h0 and s3._blocked and
          all(d == 0.0 for _, d in s3._blocked.values()), f"haircut {s3._haircut} blocked {s3._blocked}")
    s3._blocked.clear(); await s3.run_pass()
    check("...and placed again on the next pass", len(ex3.mine("ETH/BTC", "buy")) == 6)
    s3.prepare_for_shutdown()
    s4, ex4 = await mm("ETH/BTC", post_only=False, take_tif="gtc")
    check("post_only: false -> GTC (field unset) for an older node", all("time_in_force" not in t for t in ex4.terms))
    s4.prepare_for_shutdown()
    try:
        MarketMakerStrategy.build_config("x", {"pair": "ETH/BTC", "params": {**PARAMS["ETH/BTC"], "take_tif": "day"}}); ok = False
    except ValueError:
        ok = True
    check("invalid take_tif rejected", ok)

    print("\n26. the gRPC request: terms on the wire, a lost answer retried under the same id")
    import grpc
    from lib.grpc_client import HydraGRPCClient
    from lib.hydra_pb import orderbook_pb2 as OB, currency_pb2 as CU, primitives_pb2 as PR
    class Lost(grpc.RpcError):
        def code(self): return grpc.StatusCode.DEADLINE_EXCEEDED
        def details(self): return "deadline"
    class Refused(grpc.RpcError):
        def code(self): return grpc.StatusCode.FAILED_PRECONDITION
        def details(self): return "post_only_would_cross: price=0.0318, best=0.0317, market=ETH/BTC"
    class FakeOB:
        def __init__(self, fail): self.reqs, self.fail = [], list(fail)
        def CreateOrder(self, req, timeout=None):
            self.reqs.append(req)
            if self.fail: raise self.fail.pop(0)
            return OB.CreateOrderResponse(order_id="ord-1")
    cl = HydraGRPCClient.__new__(HydraGRPCClient); cl._log = lambda *a, **k: None
    cur = CU.OrderbookCurrency(protocol=2, network_id="1", asset_id="0x" + "0" * 40)
    kw = dict(base_currency=cur, quote_currency=cur, base_amount=PR.DecimalString(value="0.03"),
              quote_amount=PR.DecimalString(), min_buy_price=PR.DecimalString(value="0.0318"),
              mid_price=PR.DecimalString(value="0.0318"), max_sell_price=PR.DecimalString(value="0.0318"))
    cl.orderbook_stub = FakeOB([Lost()])
    oid = cl.place_limit_order(**kw, time_in_force="post_only", client_order_id="mm_eth_btc-s0-abc", self_trade_prevention="cancel_taker")
    r0, r1 = cl.orderbook_stub.reqs
    check("CreateOrderRequest carries POST_ONLY, the client_order_id and CANCEL_TAKER",
          r0.order_variant.limit_order.time_in_force == OB.TIME_IN_FORCE_POST_ONLY and r0.client_order_id == "mm_eth_btc-s0-abc"
          and r0.self_trade_prevention == OB.SELF_TRADE_PREVENTION_CANCEL_TAKER)
    check("DEADLINE_EXCEEDED: resent once with the SAME id (the hub returns that order, never a second one)",
          oid == "ord-1" and r1 == r0)
    cl.orderbook_stub = FakeOB([Refused()])
    oid, why = cl.place_limit_order_ex(**kw, time_in_force="post_only", client_order_id="x")
    check("a refusal is read by its prefix", oid is None and why == "post_only_would_cross", f"{why}")
    cl.orderbook_stub = FakeOB([])
    cl.place_limit_order(**kw)
    r = cl.orderbook_stub.reqs[0]
    check("no terms given: fields left unset (GTC, no prevention, no id) — safe with an older hub",
          not r.order_variant.limit_order.time_in_force and not r.HasField("client_order_id") and not r.self_trade_prevention)
    print("\n27. pause switch (state/pause, state/pause_<strategy>)")
    s, ex = await mm("USDC.arb/USDC.eth")
    s2, _ = await mm("BTC/USDC.arb", ex=ex, state_dir=s.cfg.state_dir)
    open(os.path.join(s.cfg.state_dir, f"pause_{s.name.replace('/', '_').replace('.', '_')}"), "w").close()
    await s.run_pass(); await s2.run_pass()
    check("pause_<strategy>: that market pulls its quotes, the other keeps quoting",
          not ex.mine("USDC.arb/USDC.eth") and len(ex.mine("BTC/USDC.arb")) == 12)
    open(os.path.join(s.cfg.state_dir, "pause"), "w").close()
    await s2.run_pass()
    check("state/pause: every market pulled", not ex.mine("BTC/USDC.arb"))
    for f in glob.glob(os.path.join(s.cfg.state_dir, "pause*")):
        os.remove(f)
    await s.run_pass(); await s2.run_pass()
    check("files removed: both quote again", len(ex.mine("USDC.arb/USDC.eth")) == 10 and len(ex.mine("BTC/USDC.arb")) == 12)
    s.prepare_for_shutdown(); s2.prepare_for_shutdown()

    print("\n28. partial fills booked right away (a parity pair's quotes are never repriced)")
    s, ex = await mm("USDC.arb/USDC.eth")
    o = next(x for x in ex.mine("USDC.arb/USDC.eth", "buy") if x.price == 0.999)
    ex.fill(o, 40.0); s.on_order_partial(o.id); await settle(); await s.run_pass()
    check("with skew: partial booked at once; the requote that follows does not book it again",
          abs(s.position - 40) < 1e-9 and o.id not in s.orders and s.fills == 1, f"position {s.position}, fills {s.fills}")
    s.prepare_for_shutdown()
    s, ex = await mm("USDC.arb/USDC.eth", skew_pct_at_max=0, size_skew=0)    # quotes stay put
    o = next(x for x in ex.mine("USDC.arb/USDC.eth", "buy") if x.price == 0.999)
    ex.fill(o, 40.0); s.on_order_partial(o.id); await settle()
    check("first partial (40 of 100) booked at once, the quote stays", abs(s.position - 40) < 1e-9 and o.id in s.orders,
          f"position {s.position}")
    ex.fill(o, 25.0); s.on_order_partial(o.id); await settle()
    check("second partial books only the new 25", abs(s.position - 65) < 1e-9, f"position {s.position}")
    s.on_order_partial(o.id); await settle()
    check("a repeated event books nothing", abs(s.position - 65) < 1e-9)
    ex.fill(o); s.on_order_filled(o.id); await settle()
    check("the final fill books the remaining 35 — total = the order's 100, nothing twice",
          abs(s.position - 100) < 1e-9 and s.fills == 3, f"position {s.position}, fills {s.fills}")
    await s.run_pass()
    o2 = sorted(ex.mine("USDC.arb/USDC.eth", "buy"), key=lambda x: -x.price)[1]     # an untouched level
    ex.fill(o2, 30.0); s.on_order_partial(o2.id); await settle()
    await s._cancel(o2.id, "test"); await settle()
    check("partial then cancelled: 30 booked once (not again at the cancel)", abs(s.position - 130) < 1e-9,
          f"position {s.position}")
    s.prepare_for_shutdown()

    print("\n29. swap failures: pause for our own / unclear ones, keep quoting on the hub's or a counterparty's")
    s, ex = await mm("BTC/USDC.arb")
    oid = next(iter(s.orders))
    them, us = "cd" * 32, "ab" * 32
    cases = [("await_send_settlement: counterparty never revealed preimage", "counterparty", False),
             (f"Swap failed due to client {them}: Swap expired because of unpaid invoice", "counterparty", False),
             (f"orderbook SwapFailed before our inbound locked: Swap failed due to client {us}: Swap expired", "ours", True),
             ("SwapFailed: the hub's fault, no strike (hub restart)", "hub", False),
             ("initiate_send: channel coordinator error: coordinator error: Hydra error", "unknown", True),
             (None, "unknown", True)]
    for err, want, pauses in cases:
        s._paused_until = 0.0
        s._pause_after_failure(oid, err)
        got, paused = s.failure_blame(err), s._paused_until > time.time()
        check(f"{want:<12} -> {'pause' if pauses else 'keep quoting'}: {str(err)[:60]}", got == want and paused == pauses,
              f"blame {got}, paused {paused}")
    s.prepare_for_shutdown()
    s, ex = await mm("BTC/USDC.arb", pause_on_foreign_failure=True)
    s._pause_after_failure(next(iter(s.orders)), "await_send_settlement: counterparty never revealed preimage")
    check("pause_on_foreign_failure: true -> pauses on every failure (the old behaviour)", s._paused_until > time.time())
    s.prepare_for_shutdown()

    print("\n30. partial fills booked right away (a parity pair's quotes are never repriced)")
    s, ex = await mm("USDC.arb/USDC.eth")
    o = next(x for x in ex.mine("USDC.arb/USDC.eth", "buy") if x.price == 0.999)
    ex.fill(o, 40.0); s.on_order_partial(o.id); await settle(); await s.run_pass()
    check("with skew: partial booked at once; the requote that follows does not book it again",
          abs(s.position - 40) < 1e-9 and o.id not in s.orders and s.fills == 1, f"position {s.position}, fills {s.fills}")
    s.prepare_for_shutdown()
    s, ex = await mm("USDC.arb/USDC.eth", skew_pct_at_max=0, size_skew=0)    # quotes stay put
    o = next(x for x in ex.mine("USDC.arb/USDC.eth", "buy") if x.price == 0.999)
    ex.fill(o, 40.0); s.on_order_partial(o.id); await settle()
    check("first partial (40 of 100) booked at once, the quote stays", abs(s.position - 40) < 1e-9 and o.id in s.orders,
          f"position {s.position}")
    ex.fill(o, 25.0); s.on_order_partial(o.id); await settle()
    check("second partial books only the new 25", abs(s.position - 65) < 1e-9, f"position {s.position}")
    s.on_order_partial(o.id); await settle()
    check("a repeated event books nothing", abs(s.position - 65) < 1e-9)
    ex.fill(o); s.on_order_filled(o.id); await settle()
    check("the final fill books the remaining 35 — total = the order's 100, nothing twice",
          abs(s.position - 100) < 1e-9 and s.fills == 3, f"position {s.position}, fills {s.fills}")
    await s.run_pass()
    o2 = sorted(ex.mine("USDC.arb/USDC.eth", "buy"), key=lambda x: -x.price)[1]     # an untouched level
    ex.fill(o2, 30.0); s.on_order_partial(o2.id); await settle()
    await s._cancel(o2.id, "test"); await settle()
    check("partial then cancelled: 30 booked once (not again at the cancel)", abs(s.position - 130) < 1e-9,
          f"position {s.position}")
    s.prepare_for_shutdown()

    print("\n31. late fills: a swap that settles hours later is still booked")
    s, ex = await mm("BTC/USDC.arb")
    o = ex.mine("BTC/USDC.arb", "sell")[0]
    pos0 = s.position
    await s._cancel(o.id, "test"); await settle()             # the bot gave up on the quote …
    k = next(iter(s._recent)); q, booked, at = s._recent[k]
    s._recent[k] = (q, booked, at - 4 * 3600)                 # … 4 hours ago
    s._on_event(o.id, "filled")                                # … and its swap settles now
    check("fill confirmed 4 h after the cancel is still booked (memory 24 h)", abs(s.position - (pos0 - o.amount)) < 1e-12,
          f"{pos0} -> {s.position}")
    s.prepare_for_shutdown()

    passed = sum(RESULTS)
    print(f"\n{passed}/{len(RESULTS)} checks passed")
    return passed == len(RESULTS)


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)
