#!/usr/bin/env python3
"""
Market maker
============

Two-sided quoting around an outside fair value, with inventory skew.

Each pass:
  1. fair value     — the outside market (Binance/Kraken median via PriceOracle),
                      or a fixed value (e.g. 1.0 for USDC/USDC)
  2. reservation    — fair shifted against the inventory: long base => lower
  3. quotes         — `levels` per side, laddered out from the reservation price,
                      sized per side by inventory (the side that reduces the
                      position gets bigger), never on the wrong side of fair and
                      never through the book (maker only)
  4. capacity       — sizes trimmed to the channel capacity that is actually free
  5. reconcile      — keep every live quote that is still close enough to its
                      target; cancel/place only the difference

Optionally (take_edge_pct), an order of someone else priced beyond fair by
more than the taker fee + an edge is taken right away, instead of letting it
pin our quotes behind it.

Fills are handled by requoting: a filled bid raises the position, which lowers
the reservation price and so the whole book — there is no explicit "flip".

Risk: quotes are pulled while the reference price is stale, and a market is
paused (not the whole bot) after a swap failure. Position and estimated P&L are
persisted per market under `state_dir`.
"""

import asyncio
import json
import math
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .base.base_strategy import BaseStrategy, StrategyConfig, StrategyStatus
from connectors.base_exchange import TradingPair, OrderSide, OrderType


@dataclass
class MarketMakerConfig(StrategyConfig):
    exchange: str = "hydra"
    pair: str = "ETH/BTC"

    # quoting
    levels: int = 5                      # per side
    level_size: float = 0.01             # base units per level (before skew/capacity)
    # Per-side overrides for an asymmetric book, e.g. a bigger ask side when the
    # base asset is plentiful and the quote asset is not. None = level_size.
    bid_size: Optional[float] = None
    ask_size: Optional[float] = None
    half_spread_pct: float = 0.15        # innermost quote distance from the reservation price
    level_step_pct: float = 0.15         # additional distance per further level
    min_edge_pct: float = 0.0            # bids <= fair*(1-edge), asks >= fair*(1+edge)

    # inventory (target: 50/50 by value => position 0)
    max_position: float = 0.1            # base units; see soft limits below
    skew_pct_at_max: float = 0.3         # reservation shift at max position
    size_skew: float = 0.5               # at max position: adding side x(1-s), reducing side x(1+s)
    inventory_offset: float = 0.0        # starting position in base units (+ = long base)
    # Soft limits: the side that would grow the position is never dropped at
    # max_position - it gets smaller (over_limit_size of its size from max on)
    # and wider (+over_limit_spread_pct x (position/max)^2), so the book stays
    # two-sided and a buyer at the limit pays for the extra risk. Only beyond
    # hard_limit_mult x max_position is that side pulled.
    hard_limit_mult: float = 1.5
    over_limit_size: float = 0.25
    over_limit_spread_pct: float = 0.5

    # fair value
    fair_value: Optional[float] = None   # fixed fair value; None = outside market
    ref_refresh: float = 15.0            # seconds between outside-price fetches
    ref_max_age: float = 120.0           # pull all quotes while the reference is older

    # order management
    refresh_interval: float = 5.0
    reprice_threshold_pct: float = 0.05  # keep a quote while it is within this of target
    size_tolerance: float = 0.25         # ...and its size within this fraction
    fail_backoff: float = 60.0           # first retry delay for a level that failed to place
    fail_pause: float = 900.0            # market pause after a swap failure
    # The node reserves slightly more than the order size (~0.2% observed on
    # ETH/BTC: rebate + quote rounding), so a quote sized exactly to the free
    # capacity is rejected. Fit to free capacity minus this margin.
    # Rejections at the capacity edge raise it further, 0.5% at a time up to 5%:
    # the balance API also reports a little more free capacity than the node
    # accepts (~0.7% on ETH receiving), so a fixed margin is a guess.
    capacity_margin_pct: float = 0.5
    # Don't place a level at less than this fraction of its target size (dust).
    min_level_fraction: float = 0.2

    # Taking mispriced orders: an ask below fair*(1 - taker fee - edge) (or a
    # bid above fair*(1 + ...)) is bought/sold right away with a limit order at
    # the worst qualifying price, instead of pinning our quotes behind it.
    # None = off. What does not fill by the next pass is cancelled.
    take_edge_pct: Optional[float] = None
    taker_fee_pct: float = 0.4
    take_max_size: float = 0.0           # base units per take
    take_cooldown: float = 30.0          # seconds between takes on the same side
    take_settle: float = 30.0            # a take's swap settles in seconds: don't cancel it sooner


    # Order terms (Hydra App 2026-09-30+; an older node silently drops them):
    # quotes are POST_ONLY — the hub refuses one that would take, so a quote never pays the
    # taker fee; takes are IOC — they fill at once or not at all and never rest — and cancel
    # themselves rather than trade against one of our own quotes.
    # Pause a market after a swap failure only when it may be ours (a channel problem) or is
    # unclear. A failure the hub reports as its own (a restart) or a counterparty's (it never
    # revealed the preimage, "due to client <someone else>") leaves the market quoting.
    pause_on_foreign_failure: bool = False

    post_only: bool = True
    take_tif: str = "ioc"                     # ioc | fok | gtc (gtc = the old resting take)
    take_stp: Optional[str] = "cancel_taker"  # self-trade prevention for takes; None = off

    # bookkeeping
    maker_fee_pct: float = -0.10         # for the P&L estimate (negative = rebate)
    state_dir: str = "state"
    status_interval: float = 600.0


class MarketMakerStrategy(BaseStrategy):

    # Changing these means another market / state file: the CLI rebuilds the strategy.
    RESTART_KEYS = ('exchange', 'pair', 'state_dir')

    @staticmethod
    def build_config(name: str, config: Dict[str, Any]) -> 'MarketMakerConfig':
        """MarketMakerConfig from a bot_config.yaml strategy entry; ValueError if it makes no sense."""
        p = config.get('params', {}) or {}
        d = MarketMakerConfig(name=name)          # defaults
        cfg = MarketMakerConfig(
            name=name,
            enabled=config.get('enabled', True),
            risk_limit=config.get('risk_limit', 1.0),
            max_daily_trades=config.get('max_daily_trades', 1000),
            exchange=config.get('exchange', 'hydra'),
            pair=config.get('pair', d.pair),
            params=p,
            **{k: p.get(k, getattr(d, k)) for k in MarketMakerStrategy.CONFIG_KEYS}
        )
        num = lambda k: getattr(cfg, k)
        bad = []
        if not isinstance(cfg.levels, int) or cfg.levels < 1:
            bad.append(f"levels={cfg.levels!r}")
        for k in ('level_size', 'max_position', 'refresh_interval'):
            if not isinstance(num(k), (int, float)) or num(k) <= 0:
                bad.append(f"{k}={num(k)!r}")
        for k in ('half_spread_pct', 'level_step_pct', 'min_edge_pct', 'skew_pct_at_max', 'size_skew',
                  'over_limit_size', 'over_limit_spread_pct', 'capacity_margin_pct', 'fail_pause'):
            if not isinstance(num(k), (int, float)) or num(k) < 0:
                bad.append(f"{k}={num(k)!r}")
        for k in ('bid_size', 'ask_size', 'fair_value'):
            if num(k) is not None and (not isinstance(num(k), (int, float)) or num(k) <= 0):
                bad.append(f"{k}={num(k)!r}")
        if cfg.take_edge_pct is not None and (not isinstance(cfg.take_edge_pct, (int, float)) or cfg.take_edge_pct < 0):
            bad.append(f"take_edge_pct={cfg.take_edge_pct!r}")
        if not isinstance(cfg.hard_limit_mult, (int, float)) or cfg.hard_limit_mult < 1:
            bad.append(f"hard_limit_mult={cfg.hard_limit_mult!r}")
        if cfg.take_tif not in ('ioc', 'fok', 'gtc'):
            bad.append(f"take_tif={cfg.take_tif!r} (ioc | fok | gtc)")
        if cfg.take_stp not in (None, 'cancel_taker', 'cancel_maker', 'cancel_both'):
            bad.append(f"take_stp={cfg.take_stp!r}")
        if not cfg.pair or '/' not in str(cfg.pair):
            bad.append(f"pair={cfg.pair!r}")
        if bad:
            raise ValueError(f"{name}: " + "; ".join(bad))
        return cfg

    CONFIG_KEYS = (
        'levels', 'level_size', 'bid_size', 'ask_size', 'half_spread_pct', 'level_step_pct', 'min_edge_pct',
        'max_position', 'skew_pct_at_max', 'size_skew', 'inventory_offset',
        'hard_limit_mult', 'over_limit_size', 'over_limit_spread_pct',
        'fair_value', 'ref_refresh', 'ref_max_age',
        'refresh_interval', 'reprice_threshold_pct', 'size_tolerance',
        'fail_backoff', 'fail_pause', 'capacity_margin_pct', 'min_level_fraction',
        'maker_fee_pct', 'take_edge_pct', 'taker_fee_pct', 'take_max_size', 'take_cooldown', 'take_settle',
        'post_only', 'take_tif', 'take_stp', 'pause_on_foreign_failure',
        'state_dir', 'status_interval')

    @staticmethod
    def unknown_params(config: Dict[str, Any]) -> List[str]:
        """Params the market maker ignores (typos): reported on reload, never fatal."""
        return sorted(set(config.get('params', {}) or {}) - set(MarketMakerStrategy.CONFIG_KEYS))

    def __init__(self, name: str, config: Dict[str, Any]):
        cfg = self.build_config(name, config)
        super().__init__(name, cfg, {})
        self.cfg = cfg

        self.exchange = None
        self.order_tracker = None
        self.price_oracle = None
        self.event_loop: Optional[asyncio.AbstractEventLoop] = None
        self.trading_pair: Optional[TradingPair] = None

        # market facts
        self._base_cur = None
        self._quote_cur = None
        self._qp = 6                  # quote (price) precision
        self._bp = 8                  # base (size) precision
        self._min_base = 0.0
        self._min_quote = 0.0

        # live quotes: order_id -> {side, level, price, size, order}
        self.orders: Dict[str, Dict[str, Any]] = {}
        self._cancelling: set = set()
        self._blocked: Dict[Tuple[str, int], Tuple[float, float]] = {}   # (side,lvl) -> (until, delay)

        # reference
        self._fair: Optional[float] = None
        self._fair_at = 0.0
        self._last_ref_fetch = 0.0

        # risk state
        self._paused_until = 0.0
        self._quotes_pulled_reason: Optional[str] = None
        self._capacity_note: Optional[str] = None
        self._haircut = 0.0           # learned extra capacity margin, % (see _place)
        self._last_take = {'buy': 0.0, 'sell': 0.0}
        # fees as the node reports them (volume tiers can lower them); config = fallback
        self._taker_fee_pct = cfg.taker_fee_pct
        self._maker_fee_pct = cfg.maker_fee_pct
        self._fees_at = 0.0
        self._oracle_fair: Optional[float] = None
        self._oracle_at = 0.0
        self._fair_src = "oracle"
        # recently removed orders: id -> (quote, base booked, removed_at). A fill
        # can be confirmed after we cancelled (the cancel races a settling swap).
        self._recent: Dict[str, Tuple[Dict[str, Any], float, float]] = {}

        # accounting (persisted)
        self.position = cfg.inventory_offset
        self.quote_flow = 0.0
        self.rebate_est = 0.0
        self.volume_base = 0.0
        self.fills = 0
        self.started_at = time.time()
        self.rebalance_cost = 0.0     # transfer costs booked with rebalances (quote units)
        self.rebalances = 0

        # hot reload: a new config is swapped in at the start of the next pass
        self._pending_cfg: Optional[MarketMakerConfig] = None

        self._active = False
        self._task: Optional[asyncio.Task] = None
        self._wake: Optional[asyncio.Event] = None
        self._last_status = 0.0

    # ------------------------------------------------------------------ setup
    async def initialize(self) -> bool:
        try:
            self.event_loop = asyncio.get_running_loop()
            self._wake = asyncio.Event()
            if not self.exchange:
                self.log_error("Exchange not set")
                return False

            pairs = await self.exchange.get_trading_pairs()
            self.trading_pair = next((p for p in pairs if p.symbol == self.cfg.pair), None)
            if not self.trading_pair:
                self.log_error(f"Pair {self.cfg.pair} not configured in hydra.yaml")
                return False
            await self.exchange.ensure_market_initialized(self.trading_pair)
            self._load_market_facts()

            if self.order_tracker is not None:
                # One market's failed swap must not take every market down.
                self.order_tracker.stop_on_swap_failure = False
                self.order_tracker.swap_timeout = max(getattr(self.order_tracker, 'swap_timeout', 0), 90.0)
                cbs = getattr(self.order_tracker, 'swap_failure_callbacks', None)
                if cbs is not None and self._on_swap_failure not in cbs:
                    cbs.append(self._on_swap_failure)

            self._load_state()
            await self._refresh_fair(force=True)

            self._active = True
            self._task = asyncio.create_task(self._run())
            sizes = (f"{self.cfg.level_size}" if not (self.cfg.bid_size or self.cfg.ask_size) else
                     f"bid {self.cfg.bid_size or self.cfg.level_size} / ask "
                     f"{self.cfg.ask_size or self.cfg.level_size}")
            self.logger.info(
                f"Market maker {self.cfg.pair}: {self.cfg.levels} levels x {sizes} "
                f"per side, spread {self.cfg.half_spread_pct}% + {self.cfg.level_step_pct}%/level, "
                f"max position {self.cfg.max_position}, fair "
                f"{'fixed ' + str(self.cfg.fair_value) if self.cfg.fair_value else 'outside market'}"
            )
            return True
        except Exception as e:
            self.log_error(f"Failed to initialize market maker: {e}")
            return False


    # ------------------------------------------------------------ hot reload
    def config_changes(self, new: 'MarketMakerConfig') -> Dict[str, Tuple[Any, Any]]:
        """{field: (old, new)} for every setting that differs (bookkeeping fields excluded)."""
        keys = ('enabled',) + self.RESTART_KEYS + tuple(k for k in self.CONFIG_KEYS if k != 'state_dir')
        return {k: (getattr(self.cfg, k), getattr(new, k)) for k in keys
                if getattr(self.cfg, k) != getattr(new, k)}

    def queue_config(self, new: 'MarketMakerConfig') -> Dict[str, Tuple[Any, Any]]:
        """Swap in `new` at the start of the next pass (never mid-pass). The caller
        rebuilds the strategy instead when a RESTART_KEYS field changed."""
        changes = self.config_changes(new)
        if any(k in changes for k in self.RESTART_KEYS):
            raise ValueError(f"{', '.join(k for k in self.RESTART_KEYS if k in changes)} changed: needs a rebuild")
        if changes:
            self._pending_cfg = new
            if self._wake is not None:
                self._wake.set()
        return changes

    def _apply_pending_config(self) -> bool:
        """Swap in a queued config. True when the reference price must be re-fetched."""
        new, self._pending_cfg = self._pending_cfg, None
        if new is None:
            return False
        changes = self.config_changes(new)
        old, self.cfg, self.config = self.cfg, new, new
        refetch = ('fair_value',)
        for k, attr in (('taker_fee_pct', '_taker_fee_pct'), ('maker_fee_pct', '_maker_fee_pct')):
            if k in changes:
                setattr(self, attr, getattr(new, k))
                self._fees_at = 0.0             # the node's (tier) rate wins again on the next refresh
        self._last_status = 0.0                 # show the new book in the next status line
        fmt = lambda v: f"{v:g}" if isinstance(v, float) else str(v)
        self.logger.info(f"🔄 {new.pair}: config reloaded — " +
                         ", ".join(f"{k} {fmt(a)} → {fmt(b)}" for k, (a, b) in changes.items()))
        return any(k in changes for k in refetch)

    def _pause_file(self) -> Optional[str]:
        """state/pause (every market) or state/pause_<strategy> (this one): pulls all quotes."""
        clean = ''.join(ch if ch.isalnum() or ch in '_-' else '_' for ch in self.name)
        for f in ("pause", f"pause_{clean}"):
            path = os.path.join(self.cfg.state_dir, f)
            if os.path.exists(path):
                return path
        return None

    # ------------------------------------------------------ rebalance booking
    def adjust_path(self) -> str:
        clean = lambda x: ''.join(ch if ch.isalnum() or ch in '_-' else '_' for ch in x)
        return os.path.join(self.cfg.state_dir, f"adjust_{clean(self.name)}.json")

    def _apply_adjustment(self):
        """Book a rebalance (funds moved between the channels and elsewhere) dropped in
        as state/adjust_<strategy>.json:

          {"set": 0}  or  {"delta": -517.4}      new position / change, base units
          "price": 1.0                           valuation (default: current fair)
          "cost": 1.5                            transfer costs in quote units (default 0)
          "note": "..."                          shown in the log
          "as_fill": true                        book it as a FILL: counted as one
                                                 (for a fill the bot missed)
          "fee_pct": 0                           with as_fill: the fee to book (default the maker
                                                 rebate; 0 when `price` is already net of fees)

        The position change is booked at `price` against quote_flow, so the P&L only
        moves by the cost. The file is renamed to .done-<ts> (or .rejected-<ts>).
        """
        path = self.adjust_path()
        try:
            if time.time() - os.path.getmtime(path) < 1.0:
                return                          # maybe still being written
        except OSError:
            return
        stamp = time.strftime('%Y%m%d-%H%M%S')

        def reject(why):
            self.logger.warning(f"⚠️  rebalance booking rejected for {self.cfg.pair}: {why} — nothing changed")
            try:
                os.replace(path, f"{path}.rejected-{stamp}")
            except OSError:
                pass

        try:
            with open(path) as f:
                a = json.load(f)
            if not isinstance(a, dict):
                return reject("not a JSON object")
        except Exception as e:
            return reject(f"unreadable ({e})")
        if ('set' in a) == ('delta' in a):
            return reject('give exactly one of "set" or "delta"')
        try:
            target = float(a['set']) if 'set' in a else self.position + float(a['delta'])
            price = float(a['price']) if a.get('price') is not None else self._fair
            cost = float(a.get('cost', 0.0))
        except (TypeError, ValueError) as e:
            return reject(f"bad number ({e})")
        if not price or price <= 0 or not math.isfinite(price):
            return reject("no price given and no fair value yet")
        if cost < 0 or not math.isfinite(target) or not math.isfinite(cost):
            return reject("cost must be >= 0 and amounts finite")
        hard = self.cfg.hard_limit_mult * self.cfg.max_position
        exempt = False
        if not exempt and abs(target) > hard and abs(target) > abs(self.position):
            return reject(f"position {target:+.8g} would be beyond the hard limit ±{hard:g}")
        before, d = self.position, target - self.position
        if a.get('as_fill') and abs(d) > 0:
            # a fill the bot never saw: book it like one (position, quote flow, volume,
            # fills, maker rebate)
            side = 'buy' if d > 0 else 'sell'
            o = {'side': side, 'price': price, 'size': abs(d),
                 'fee_pct': float(a['fee_pct']) if a.get('fee_pct') is not None else self._maker_fee_pct}
            self._book(o, abs(d), 'filled, booked afterwards')
            if cost:
                self.quote_flow -= cost
                self.rebalance_cost += cost
            self.rebalances += 1
            self._save_state()
        else:
            self.position = target
            self.quote_flow -= d * price + cost
            self.rebalance_cost += cost
            self.rebalances += 1
            self._save_state()
        try:
            os.replace(path, f"{path}.done-{stamp}")
        except OSError as e:
            self.logger.warning(f"{self.cfg.pair}: could not archive {path}: {e}")
        note = str(a.get('note') or '').strip()
        quote = self.cfg.pair.split('/')[1]
        self.logger.info(f"🔁 {self.cfg.pair}: rebalance booked — position {before:+.8g} → {target:+.8g} "
                         f"({self._skew():+.0%} of max) @ {price:g}, cost {cost:g} {quote}"
                         + (f" ({note})" if note else ""))

    def _load_market_facts(self):
        cache = (getattr(self.exchange, '_market_cache', None) or {}).get(self.trading_pair.symbol, {})
        self._qp = int(cache.get('quote_precision', self._qp))
        self._bp = int(cache.get('base_precision', self._bp))
        curs = None
        getter = getattr(self.exchange, '_get_currencies_for_pair', None)
        if getter:
            curs = getter(self.trading_pair)
        if curs:
            self._base_cur, self._quote_cur = curs
        elif cache.get('base_currency') is not None:
            self._base_cur, self._quote_cur = cache['base_currency'], cache['quote_currency']
        try:
            client = getattr(self.exchange, 'client', None)
            mi = client.get_market_info(self._base_cur, self._quote_cur) if client else None
            if mi:
                self._min_base = float(mi.min_base_amount.value or 0)
                self._min_quote = float(mi.min_quote_amount.value or 0)
                self._apply_fees(mi)
        except Exception as e:
            self.logger.warning(f"Could not load market minimums: {e}")
        self.logger.info(f"{self.cfg.pair}: precision base {self._bp} quote {self._qp}, "
                         f"minimums base {self._min_base} quote {self._min_quote}")

    def _own_fee_rates(self):
        """Our discounted (fee-tier) rates via JSON-RPC orderbook_getMarketFeeRates; None if unavailable.
        MarketInfo only has LIST rates (see docs: price fills with GetMarketFeeRates)."""
        client = getattr(self.exchange, 'client', None)
        if client is None or self._base_cur is None:
            return None
        import json, urllib.request
        cur = lambda c: {"protocol": int(c.protocol), "networkId": c.network_id, "assetId": c.asset_id}
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "orderbook_getMarketFeeRates",
                           "params": [{"firstCurrency": cur(self._base_cur), "otherCurrency": cur(self._quote_cur)}]})
        url = f"http://{getattr(client, 'host', 'localhost')}:{getattr(client, 'port', 5008)}"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, body.encode(), {"Content-Type": "application/json"}),
                                        timeout=5) as r:
                fr = (json.load(r).get("result") or {}).get("feeRates") or {}
            val = lambda k: float(fr[k]["value"]) * 100 if k in fr else None
            return val("takerBaseFee"), val("makerBaseFee")
        except Exception:
            return None

    def _apply_fees(self, mi):
        """Current taker/maker fee: our own (discounted) rates, else MarketInfo's list rates."""
        pct = lambda d: float(d.value) * 100 if getattr(d, 'value', '') else None
        taker = pct(getattr(mi, 'taker_base_fee', None)) if mi is not None else None
        maker = pct(getattr(mi, 'maker_base_fee', None)) if mi is not None else None
        own = self._own_fee_rates()
        if own and own[0] is not None:
            taker, maker = own
        self._fees_at = time.time()
        for name, new, attr in (("taker", taker, "_taker_fee_pct"), ("maker", maker, "_maker_fee_pct")):
            if new is None:
                continue
            old = getattr(self, attr)
            if abs(new - old) > 1e-9:
                self.logger.info(f"{self.cfg.pair}: {name} fee {old:.4g}% -> {new:.4g}% (from the node)")
                setattr(self, attr, new)

    def _refresh_fees(self):
        if time.time() - self._fees_at < 3600:
            return
        try:
            client = getattr(self.exchange, 'client', None)
            mi = client.get_market_info(self._base_cur, self._quote_cur) if client else None
            self._apply_fees(mi)
        except Exception:
            self._fees_at = time.time()

    async def start(self) -> bool:
        return True

    def prepare_for_shutdown(self):
        """Called before the CLI cancels every order: stop requoting first."""
        self._active = False
        if self._task and not self._task.done():
            self._task.cancel()

    async def stop(self) -> bool:
        self.prepare_for_shutdown()
        await self._cancel_all("strategy stopped")
        self._save_state()
        return True

    # ------------------------------------------------------------------- loop
    async def _run(self):
        while self._active:
            try:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=self.cfg.refresh_interval)
                except asyncio.TimeoutError:
                    pass
                self._wake.clear()
                if not self._active:
                    return
                await self.run_pass()
            except asyncio.CancelledError:
                return
            except Exception as e:
                self.logger.error(f"{self.cfg.pair}: market maker pass failed: {e}")

    async def run_pass(self):
        """One full quote cycle (public so tests can drive it deterministically)."""
        await self._refresh_fair(force=self._apply_pending_config())
        self._apply_adjustment()
        now = time.time()

        why, detail = None, ""
        paused_by = self._pause_file()
        if paused_by:
            why, detail = "paused-by-user", f"paused by the user ({paused_by} exists; delete it to resume)"
        elif now < self._paused_until:
            why, detail = "paused", f"swap failure, resuming in {self._paused_until - now:.0f}s"
        elif self._fair is None:
            why, detail = "no-reference", "no reference price yet"
        elif self.cfg.fair_value is None and now - self._fair_at > self.cfg.ref_max_age:
            why, detail = "stale-reference", f"reference price is {now - self._fair_at:.0f}s old"
        if why:
            if self._quotes_pulled_reason != why:        # log the transition, not every pass
                self.logger.warning(f"⏸️  {self.cfg.pair}: pulling quotes — {detail}")
            self._quotes_pulled_reason = why
            await self._cancel_all(detail)
            return
        if self._quotes_pulled_reason:
            self.logger.info(f"▶️  {self.cfg.pair}: quoting again")
            self._quotes_pulled_reason = None

        others = await self._others_book()
        desired = self.desired_quotes(self._touch(others))
        await self._reconcile(desired)
        await self._take_mispriced(others)
        self._maybe_status()

    # --------------------------------------------------------------- pricing
    async def _refresh_fair(self, force: bool = False):
        if self.cfg.fair_value:
            self._fair, self._fair_at = float(self.cfg.fair_value), time.time()
            return
        await self._refresh_oracle(force)
        if self._oracle_fair:
            self._fair, self._fair_at = self._oracle_fair, self._oracle_at

    async def _refresh_oracle(self, force: bool = False):
        now = time.time()
        if not force and now - self._last_ref_fetch < self.cfg.ref_refresh:
            return
        self._last_ref_fetch = now
        try:
            if self.price_oracle is None:
                from lib.price_oracle import PriceOracle
                self.price_oracle = PriceOracle(self.logger)
            fetch = getattr(self.price_oracle, 'get_market_pair_price', None) \
                or self.price_oracle.get_pair_price
            price = await asyncio.get_running_loop().run_in_executor(None, fetch, self.cfg.pair)
            if price and price > 0:
                self._oracle_fair, self._oracle_at = float(price), now
        except Exception as e:
            self.logger.warning(f"{self.cfg.pair}: outside price unavailable: {e}")

    def _exposure(self) -> float:
        """The position the limits work on."""
        return self.position

    def _skew(self) -> float:
        """Position as a fraction of max_position, clamped to [-1, 1]."""
        if self.cfg.max_position <= 0:
            return 0.0
        return max(-1.0, min(1.0, self.position / self.cfg.max_position))

    def _quote_skew(self) -> float:
        """Inventory for the skew, as a fraction of max_position."""
        return self._skew()

    def desired_quotes(self, touch: Tuple[Optional[float], Optional[float]] = (None, None)
                       ) -> List[Dict[str, Any]]:
        """Target quotes for the current fair value and inventory.

        `touch` is the best bid/ask of OTHER participants; quotes are kept on our
        side of it so nothing we place can take liquidity.
        """
        fair = self._fair
        if not fair:
            return []
        q = self._quote_skew()
        r = fair * (1 - self.cfg.skew_pct_at_max / 100 * q)
        edge = self.cfg.min_edge_pct / 100
        tick = 10 ** -self._qp
        other_bid, other_ask = touch

        base_bid = self.cfg.bid_size if self.cfg.bid_size else self.cfg.level_size
        base_ask = self.cfg.ask_size if self.cfg.ask_size else self.cfg.level_size
        bid_size = base_bid * max(0.0, 1 - self.cfg.size_skew * q)
        ask_size = base_ask * max(0.0, 1 + self.cfg.size_skew * q)

        # soft limits on the side that grows the position (bids when long, asks when short)
        u = self._exposure() / self.cfg.max_position if self.cfg.max_position > 0 else 0.0
        grow_side = 'buy' if u > 0 else 'sell' if u < 0 else None
        quote_bids = not (grow_side == 'buy' and abs(u) >= self.cfg.hard_limit_mult)
        quote_asks = not (grow_side == 'sell' and abs(u) >= self.cfg.hard_limit_mult)
        extra = self.cfg.over_limit_spread_pct / 100 * u * u
        extra_bid = extra if grow_side == 'buy' else 0.0
        extra_ask = extra if grow_side == 'sell' else 0.0
        if abs(u) >= 1.0:
            if grow_side == 'buy':
                bid_size = base_bid * self.cfg.over_limit_size
            elif grow_side == 'sell':
                ask_size = base_ask * self.cfg.over_limit_size
        else:
            bid_size = max(bid_size, base_bid * self.cfg.over_limit_size)
            ask_size = max(ask_size, base_ask * self.cfg.over_limit_size)

        bid_cap = ask_cap = None

        quotes = []
        prev_bid, prev_ask = None, None
        for i in range(self.cfg.levels):
            dist = (self.cfg.half_spread_pct + i * self.cfg.level_step_pct) / 100
            lvl = i * self.cfg.level_step_pct / 100

            if quote_bids:
                bid = min(r * (1 - dist - extra_bid), fair * (1 - edge))
                if other_ask is not None:                       # maker only; a ladder pushed
                    step = 1 - i * self.cfg.level_step_pct / 100  # below it keeps its spacing
                    bid = min(bid, (other_ask - tick) * step)
                if bid_cap is not None:
                    bid = min(bid, bid_cap * (1 - lvl))         # keeps the ladder's spacing
                bid = math.floor(bid / tick + 1e-9) * tick
                if prev_bid is not None:
                    bid = min(bid, prev_bid - tick)             # strictly descending
                prev_bid = bid
                self._add_quote(quotes, 'buy', i, bid, bid_size)

            if quote_asks:
                ask = max(r * (1 + dist + extra_ask), fair * (1 + edge))
                if other_bid is not None:
                    ask = max(ask, (other_bid + tick) * (1 + i * self.cfg.level_step_pct / 100))
                if ask_cap is not None:
                    ask = max(ask, ask_cap * (1 + lvl))
                ask = math.ceil(ask / tick - 1e-9) * tick
                if prev_ask is not None:
                    ask = max(ask, prev_ask + tick)
                prev_ask = ask
                self._add_quote(quotes, 'sell', i, ask, ask_size)
        return quotes

    def _add_quote(self, quotes, side, level, price, size):
        size = math.floor(size * 10 ** self._bp + 1e-9) / 10 ** self._bp
        price = round(price, self._qp)
        if price <= 0 or self._below_min(size, price):
            return
        quotes.append({'side': side, 'level': level, 'price': price, 'size': size})

    def _below_min(self, size: float, price: float) -> bool:
        return size <= 0 or size < self._min_base or size * price < self._min_quote

    async def _others_book(self) -> Tuple[List[Tuple[float, float]], List[Tuple[float, float]]]:
        """Bids and asks of OTHER participants (our own volume subtracted), best first."""
        try:
            ob = await self.exchange.get_orderbook(self.trading_pair, 20)
        except Exception:
            return [], []
        if not ob:
            return [], []
        mine: Dict[Tuple[str, float], float] = {}
        for o in self.orders.values():
            k = (o['side'], round(o['price'], 10))
            mine[k] = mine.get(k, 0.0) + o['size']

        def others(levels, side):
            out = []
            for price, vol in levels or []:
                k = (side, round(price, 10))
                own = min(mine.get(k, 0.0), vol)
                mine[k] = mine.get(k, 0.0) - own
                if vol - own > 1e-12:
                    out.append((price, vol - own))
            return out
        return others(ob.bids, 'buy'), others(ob.asks, 'sell')

    @staticmethod
    def _touch(others) -> Tuple[Optional[float], Optional[float]]:
        bids, asks = others
        return (bids[0][0] if bids else None), (asks[0][0] if asks else None)

    async def _competitor_touch(self) -> Tuple[Optional[float], Optional[float]]:
        return self._touch(await self._others_book())

    async def _take_mispriced(self, others):
        """Buy asks well below fair / sell into bids well above it (see take_edge_pct)."""
        if self.cfg.take_edge_pct is None or self.cfg.take_max_size <= 0 or not self._fair:
            return
        self._refresh_fees()
        th = (self._taker_fee_pct + self.cfg.take_edge_pct) / 100
        bids, asks = others
        now = time.time()
        for side, levels, ok, room in (
                ('buy', asks, lambda px: px <= self._fair * (1 - th), self.cfg.max_position - self._exposure()),
                ('sell', bids, lambda px: px >= self._fair * (1 + th), self.cfg.max_position + self._exposure())):
            if now - self._last_take[side] < self.cfg.take_cooldown:
                continue
            if any(o['level'] < 0 and o['side'] == side for o in self.orders.values()):
                continue                                 # previous take still settling
            want, limit = 0.0, None
            left = min(self.cfg.take_max_size, room)
            for px, vol in levels:
                if not ok(px) or left <= 0:
                    break
                take = min(vol, left)
                want, left, limit = want + take, left - take, px
            if limit is None or want <= 0:
                continue
            cap = self._capacity()
            size = self._fit(cap, side, limit, want)
            while size < want:
                # a take beats our own outermost quote on that side for capacity
                outer = [(oid, o['level']) for oid, o in self.orders.items()
                         if o['side'] == side and o['level'] >= 0]
                if not outer:
                    break
                oid = max(outer, key=lambda x: x[1])[0]
                if not await self._cancel_freeing(cap, oid, "freeing capacity for a take"):
                    break
                size = self._fit(cap, side, limit, want)
            if size <= 0:
                continue
            self._last_take[side] = now
            edge = (1 - limit / self._fair) if side == 'buy' else (limit / self._fair - 1)
            self.logger.info(f"🎯 {self.cfg.pair}: {side} {size:.8g} @ {limit} — mispriced "
                             f"{edge * 100:.2f}% vs fair {self._fair:.8g}, taking it")
            await self._place(side, -1, limit, size, fee_pct=self._taker_fee_pct)

    # -------------------------------------------------------------- capacity
    def _capacity(self) -> Optional[Dict[str, float]]:
        """Free (unreserved) channel capacity for this market's two assets."""
        client = getattr(self.exchange, 'client', None)
        if client is None or self._base_cur is None:
            return None
        key = lambda cur: (str(cur.network_id), str(cur.asset_id).lower())
        want = {key(self._base_cur): 'base', key(self._quote_cur): 'quote'}
        v = lambda d: float(d.value) if getattr(d, 'value', '') else 0.0
        out = {'base_send': 0.0, 'base_recv': 0.0, 'quote_send': 0.0, 'quote_recv': 0.0}
        try:
            for b in client.get_orderbook_balances():
                role = want.get(key(b.currency))
                if role:
                    out[f'{role}_send'] = v(b.balance.sending)
                    out[f'{role}_recv'] = v(b.balance.receiving)
        except Exception as e:
            self.logger.debug(f"capacity unavailable: {e}")
            return None
        return out

    def _fit(self, cap: Optional[Dict[str, float]], side: str, price: float, size: float) -> float:
        """Largest size <= `size` the free capacity in `cap` allows (0 if below minimums)."""
        if cap is None:
            return size
        m = 1 - (self.cfg.capacity_margin_pct + self._haircut) / 100
        if side == 'buy':       # pay quote, receive base
            limit = min(cap['quote_send'] / price, cap['base_recv']) * m
        else:                   # pay base, receive quote
            limit = min(cap['base_send'], cap['quote_recv'] / price) * m
        fit = math.floor(max(0.0, min(size, limit)) * 10 ** self._bp + 1e-9) / 10 ** self._bp
        return 0.0 if self._below_min(fit, price) else fit

    @staticmethod
    def _reserve(cap: Optional[Dict[str, float]], side: str, price: float, size: float):
        if cap is None:
            return
        if side == 'buy':
            cap['quote_send'] -= size * price
            cap['base_recv'] -= size
        else:
            cap['base_send'] -= size
            cap['quote_recv'] -= size * price

    async def _cancel_freeing(self, cap: Optional[Dict[str, float]], oid: str, why: str) -> bool:
        """Cancel and credit what the quote reserved back into `cap` ourselves.

        The node's balance API still shows a cancelled order's capacity as
        reserved for a moment; re-reading it right after a cancel made the edge
        levels flip-flop every pass (placed, cancelled, "not quoting", placed...).
        """
        o = self.orders.get(oid)
        if o is None:
            return True
        open_size = max(0.0, o['size'] - self._partial(o))
        ok = await self._cancel(oid, why)
        if ok:
            self._reserve(cap, o['side'], o['price'], -open_size)
        return ok

    # ------------------------------------------------------------- reconcile
    async def _reconcile(self, desired: List[Dict[str, Any]]):
        want = {(d['side'], d['level']): d for d in desired}
        live: Dict[Tuple[str, int], str] = {}
        stale: List[str] = []
        for oid, o in list(self.orders.items()):
            if o['level'] < 0 and time.time() - o.get('at', 0.0) < self.cfg.take_settle:
                continue                                # a take whose swap may still settle
            k = (o['side'], o['level'])
            if k in live:
                stale.append(oid)                       # duplicate slot
            else:
                live[k] = oid

        def outer_live(side: str, level: int) -> bool:
            return any(o['side'] == side and o['level'] > level for o in self.orders.values())

        th = self.cfg.reprice_threshold_pct / 100
        tol = self.cfg.size_tolerance
        free = self._capacity()                          # beyond what live quotes hold
        to_place: List[Dict[str, Any]] = []
        for k in sorted(want, key=lambda k: k[1]):
            d = want[k]
            oid = live.pop(k, None)
            o = self.orders.get(oid) if oid else None
            if o and abs(o['price'] - d['price']) <= th * d['price']:
                if abs(o['size'] - d['size']) <= tol * d['size']:
                    continue                            # close enough: leave it resting
                if o['size'] < d['size']:
                    # Capacity-limited quote. Replacing it would only re-place the
                    # same size, every pass — keep it unless it could now grow from
                    # free capacity, or an outer level holds capacity it should get.
                    grow = self._fit(free, d['side'], d['price'], d['size'] - o['size'])
                    if grow < tol * d['size'] and not outer_live(d['side'], d['level']):
                        continue
            if oid:
                stale.append(oid)
            to_place.append(d)
        stale.extend(live.values())                     # levels no longer wanted

        cap = free                                       # credited as we cancel, not re-read
        for oid in stale:
            await self._cancel_freeing(cap, oid, "requote")

        short: List[str] = []
        if to_place:
            # innermost first: when capacity is short, the best-priced levels win
            for d in sorted(to_place, key=lambda d: d['level']):
                side, lvl = d['side'], d['level']
                until, _ = self._blocked.get((side, lvl), (0.0, 0.0))
                if time.time() < until:
                    short.append(f"{side} L{lvl} (rejected, backing off)")
                    continue
                floor = self.cfg.min_level_fraction * d['size']
                size = self._fit(cap, side, d['price'], d['size'])
                while size < floor:
                    # take the capacity back from the outermost quote on this side
                    outer = [(oid, o['level']) for oid, o in self.orders.items()
                             if o['side'] == side and o['level'] > lvl]
                    if not outer:
                        break
                    oid = max(outer, key=lambda x: x[1])[0]
                    if not await self._cancel_freeing(cap, oid, "freeing capacity for an inner level"):
                        break
                    size = self._fit(cap, side, d['price'], d['size'])
                if size < floor:
                    short.append(f"{side} L{lvl}")
                    continue
                self._reserve(cap, side, d['price'], size)
                await self._place(side, lvl, d['price'], size)

        for (side, lvl), (until, _) in self._blocked.items():
            tag = f"{side} L{lvl} (rejected, backing off)"
            if time.time() < until and tag not in short:
                short.append(tag)
        note = f"not quoting {', '.join(sorted(set(short)))}" if short else None
        if note != self._capacity_note:
            if note:
                self.logger.warning(f"{self.cfg.pair}: {note}")
            elif self._capacity_note:
                self.logger.info(f"{self.cfg.pair}: full book quoted again")
            self._capacity_note = note

    def _order_terms(self, side: str, level: int) -> Dict[str, Any]:
        """time in force / self-trade prevention / a fresh client_order_id for one placement."""
        import uuid
        terms = {'client_order_id': f"{self.name[:24]}-{side[0]}{level if level >= 0 else 't'}-{uuid.uuid4().hex[:20]}"}
        if level < 0:                                   # a take
            if self.cfg.take_tif != 'gtc':
                terms['time_in_force'] = self.cfg.take_tif
            if self.cfg.take_stp:
                terms['self_trade_prevention'] = self.cfg.take_stp
        elif self.cfg.post_only:
            terms['time_in_force'] = 'post_only'
        return terms

    async def _place(self, side: str, level: int, price: float, size: float,
                     fee_pct: Optional[float] = None):
        k = (side, level)
        order = None
        o_side = OrderSide.BUY if side == 'buy' else OrderSide.SELL
        try:
            order = await self.exchange.place_order(
                pair=self.trading_pair, side=o_side, type=OrderType.LIMIT, amount=size, price=price,
                params=self._order_terms(side, level))
        except Exception as e:
            self.logger.warning(f"{self.cfg.pair}: place {side} {size} @ {price} raised {e}")
        if not order:
            refusal = (getattr(self.exchange, 'last_refusal', None) or {}).get((self.cfg.pair, o_side))
            if refusal in ('post_only_would_cross', 'nothing_to_take', 'fill_or_kill_unfilled', 'self_trade_prevented'):
                # Not a capacity problem: the book moved (a quote would have taken / a take found
                # nothing). Requote on the next pass, no margin, no long backoff.
                self._blocked[k] = (time.time() + self.cfg.refresh_interval, 0.0)
                self.logger.info(f"{self.cfg.pair}: {side} L{level} @ {price} refused ({refusal}) — next pass")
                return
            if self._haircut < 5.0:
                self._haircut = min(self._haircut + 0.5, 5.0)
                self.logger.info(f"{self.cfg.pair}: capacity margin raised to "
                                 f"{self.cfg.capacity_margin_pct + self._haircut:.1f}% after a rejection")
            _, delay = self._blocked.get(k, (0.0, 0.0))
            delay = min(max(self.cfg.fail_backoff, delay * 2), 600.0)
            if delay == self.cfg.fail_backoff:
                self.logger.warning(f"{self.cfg.pair}: {side} L{level} {size} @ {price} was "
                                    f"rejected — retrying with backoff (logged once)")
            self._blocked[k] = (time.time() + delay, delay)
            return
        if k in self._blocked:
            del self._blocked[k]
            self.logger.info(f"{self.cfg.pair}: {side} L{level} placed again")
        self.orders[order.id] = {'side': side, 'level': level, 'price': price,
                                 'size': size, 'order': order, 'at': time.time(),
                                 'fee_pct': self._maker_fee_pct if fee_pct is None else fee_pct}
        if self.order_tracker is not None:
            self.order_tracker.track_order(order)

    async def _cancel(self, order_id: str, why: str) -> bool:
        if order_id not in self.orders:
            return True
        self._cancelling.add(order_id)
        ok = False
        try:
            ok = await self.exchange.cancel_order(order_id, self.trading_pair)
        except Exception as e:
            self.logger.warning(f"{self.cfg.pair}: cancel {order_id[:8]} failed: {e}")
        if ok:
            self._retire(order_id, 'cancelled')
        else:
            self._cancelling.discard(order_id)   # maybe it filled; the fill event settles it
        return ok

    async def _cancel_all(self, why: str):
        for oid in list(self.orders):
            await self._cancel(oid, why)

    # ---------------------------------------------------------------- events
    def on_order_filled(self, order_id: str):
        self._dispatch(order_id, 'filled')

    def on_order_cancelled(self, order_id: str):
        self._dispatch(order_id, 'cancelled')

    def on_order_partial(self, order_id: str):
        """A resting quote filled in part (it stays on the book with the rest)."""
        self._dispatch(order_id, 'partial')

    def _dispatch(self, order_id: str, what: str):
        loop = self.event_loop
        if getattr(self, '_detached', False):
            return                                # replaced by a reload; the new instance books it
        if loop and loop.is_running():
            loop.call_soon_threadsafe(self._on_event, order_id, what)

    def _on_event(self, order_id: str, what: str):
        if what == 'partial':
            if self._book_partial(order_id) and self._wake is not None:
                self._wake.set()                     # requote with the new position
            return
        if order_id in self._cancelling and what == 'cancelled':
            self._cancelling.discard(order_id)   # our own cancel; already accounted
            return
        if self._retire(order_id, what) and self._wake is not None:
            self._wake.set()                     # requote right away

    def _on_swap_failure(self, order_id: str, error: Optional[str] = None):
        loop = self.event_loop
        if getattr(self, '_detached', False):
            return
        if loop and loop.is_running():
            loop.call_soon_threadsafe(self._pause_after_failure, order_id, error)

    def _identity(self) -> Optional[str]:
        if not hasattr(self, '_identity_hex'):
            self._identity_hex = None
            try:
                k = self.exchange.client.get_public_key()
                self._identity_hex = (k.hex() if isinstance(k, (bytes, bytearray)) else str(k)).lower() or None
            except Exception:
                pass
        return self._identity_hex

    def failure_blame(self, error: Optional[str]) -> str:
        """'hub' | 'counterparty' | 'ours' | 'unknown' from the hub's swap-failure text."""
        import re
        e = (error or "").lower()
        if re.search(r"venue_stopping|hub'?s fault|venue'?s fault|no strike|hub (is )?restart", e):
            return 'hub'
        if "counterparty never revealed preimage" in e:
            return 'counterparty'
        m = re.search(r"due to client ([0-9a-f]{64})", e)
        if m:
            me = self._identity()
            if me is None:
                return 'unknown'
            return 'ours' if m.group(1) == me else 'counterparty'
        return 'unknown'

    def _pause_after_failure(self, order_id: str, error: Optional[str] = None):
        if order_id not in self.orders:
            return                                # another market's swap
        blame = self.failure_blame(error)
        if blame in ('hub', 'counterparty') and not self.cfg.pause_on_foreign_failure:
            self.logger.warning(f"⚠️  {self.cfg.pair}: swap failed for {order_id[:8]} — the {blame}'s fault "
                                f"({(error or '')[:90]}); keeps quoting")
            return
        self._paused_until = time.time() + self.cfg.fail_pause
        self.logger.error(f"🛑 {self.cfg.pair}: swap failed for {order_id[:8]} — pausing this "
                          f"market for {self.cfg.fail_pause:.0f}s (other markets keep running)")
        if self._wake is not None:
            self._wake.set()

    # ------------------------------------------------------------ accounting
    def _book_partial(self, order_id: str) -> bool:
        """Book the part of a resting quote filled since the last booking, right away —
        a quote that is never repriced (e.g. a parity pair) would otherwise only be booked
        (and alerted) whenever it happens to be cancelled."""
        o = self.orders.get(order_id)
        if o is None:
            return False
        done = min(self._partial(o), o['size'])
        delta = done - o.get('booked', 0.0)
        if delta <= 10 ** -self._bp / 2:
            return False
        o['booked'] = done
        self._book(o, delta, 'partial fill')
        return True

    def _retire(self, order_id: str, how: str) -> bool:
        """Remove a quote and book whatever of it filled (minus what partial fills already
        booked). Idempotent."""
        o = self.orders.pop(order_id, None)
        if o is None:
            return self._late_fill(order_id) if how == 'filled' else False
        filled = o['size'] if how == 'filled' else max(self._partial(o), o.get('booked', 0.0))
        now = time.time()
        # Remember removed quotes for 24 h (the hub's own retention for an order's id): a swap
        # can settle hours after its order was cancelled (2026-10-01: 3 h 44 min), and its fill
        # must still be booked then.
        self._recent = {k: v for k, v in self._recent.items() if now - v[2] < 86400}
        self._recent[order_id] = (o, filled, now)
        self._book(o, filled - o.get('booked', 0.0), how)
        return True

    def _late_fill(self, order_id: str) -> bool:
        """A fill confirmed after we had already removed the order: book the rest."""
        rec = self._recent.get(order_id)
        if rec is None:
            return False
        o, booked, at = rec
        rest = o['size'] - booked
        if rest <= 1e-12:
            return False
        self._recent[order_id] = (o, o['size'], at)
        self.logger.warning(f"{self.cfg.pair}: {order_id[:8]} filled after we cancelled it — booking it")
        self._book(o, rest, 'filled after cancel')
        return True

    def _book(self, o: Dict[str, Any], filled: float, how: str):
        if filled > 0:
            px = o['price']
            if o['side'] == 'buy':
                self.position += filled
                self.quote_flow -= filled * px
            else:
                self.position -= filled
                self.quote_flow += filled * px
            self.rebate_est += filled * px * (-o.get('fee_pct', self.cfg.maker_fee_pct) / 100)
            self.volume_base += filled
            self.fills += 1
            label = how if how in ('filled', 'filled after cancel', 'partial fill', 'filled, booked afterwards') else 'partial, then ' + how
            where = f"position {self.position:+.8g} ({self._skew():+.0%} of max)"
            self.logger.info(f"💱 {self.cfg.pair}: {o['side']} {filled:.8g} @ {px} ({label}) — {where}")
            self._save_state()

    def _partial(self, o: Dict[str, Any]) -> float:
        """Filled base amount of a quote that is being removed before completing.

        OrderTracker keeps a SELL's remainder in base units but a BUY's in quote
        units (whichever oneof the node set), so a buy's is converted by price.
        """
        order = o.get('order')
        filled = getattr(order, 'filled', 0.0) if order is not None else 0.0
        if not filled or filled <= 0:
            return 0.0
        rem = getattr(order, 'remaining', None)
        if rem is None:
            return 0.0
        rem_base = rem if o['side'] == 'sell' else rem / o['price']
        return max(0.0, min(o['size'], o['size'] - rem_base))

    def pnl(self) -> Dict[str, float]:
        fair = self._fair or 0.0
        moved = self.position - self.cfg.inventory_offset
        trading = self.quote_flow + moved * fair
        out = {'trading': trading, 'rebates': self.rebate_est, 'total': trading + self.rebate_est}
        return out

    def _state_path(self) -> str:
        clean = lambda x: ''.join(ch if ch.isalnum() or ch in '_-' else '_' for ch in x)
        return os.path.join(self.cfg.state_dir, f"mm_{clean(self.name)}__{clean(self.cfg.pair)}.json")

    def _load_state(self):
        try:
            with open(self._state_path()) as f:
                s = json.load(f)
            self.position = float(s.get('position', self.position))
            self.quote_flow = float(s.get('quote_flow', 0.0))
            self.rebate_est = float(s.get('rebate_est', 0.0))
            self.volume_base = float(s.get('volume_base', 0.0))
            self.fills = int(s.get('fills', 0))
            self.started_at = float(s.get('started_at', self.started_at))
            self.rebalance_cost = float(s.get('rebalance_cost', 0.0))
            self.rebalances = int(s.get('rebalances', 0))
            self.logger.info(f"{self.cfg.pair}: restored position {self.position:+.8g}, "
                             f"{self.fills} fills")
        except FileNotFoundError:
            pass
        except Exception as e:
            self.logger.warning(f"{self.cfg.pair}: could not read state: {e}")

    def _save_state(self):
        try:
            os.makedirs(self.cfg.state_dir, exist_ok=True)
            tmp = self._state_path() + ".tmp"
            with open(tmp, 'w') as f:
                json.dump({'pair': self.cfg.pair, 'position': self.position,
                           'quote_flow': self.quote_flow, 'rebate_est': self.rebate_est,
                           'volume_base': self.volume_base, 'fills': self.fills,
                           'started_at': self.started_at, 'rebalance_cost': self.rebalance_cost,
                           'rebalances': self.rebalances, 'saved_at': time.time()}, f)
            os.replace(tmp, self._state_path())
        except Exception as e:
            self.logger.warning(f"{self.cfg.pair}: could not save state: {e}")

    def _maybe_status(self):
        now = time.time()
        if now - self._last_status < self.cfg.status_interval:
            return
        self._last_status = now
        bids = sorted((o['price'] for o in self.orders.values() if o['side'] == 'buy'), reverse=True)
        asks = sorted(o['price'] for o in self.orders.values() if o['side'] == 'sell')
        p = self.pnl()
        extra = ""
        self.logger.info(
            f"📊 {self.cfg.pair}: fair {self._fair} | {len(bids)} bids "
            f"{bids[0] if bids else '-'} / {len(asks)} asks {asks[0] if asks else '-'} | "
            f"position {self.position:+.8g} ({self._skew():+.0%}) | {self.fills} fills, "
            f"volume {self.volume_base:.8g} | est. P&L {p['total']:+.8g} "
            f"(trading {p['trading']:+.8g}, rebates {p['rebates']:+.8g}{extra}) in quote units")

    # --------------------------------------------------------- BaseStrategy
    def get_status(self) -> StrategyStatus:
        return StrategyStatus(name=self.name, running=self._active,
                              active_positions=len(self.orders), total_trades=self.fills,
                              profit_loss=self.pnl()['total'], last_update=self._fair_at,
                              error_count=self.error_count, last_error=self.last_error)

    async def on_market_data(self, exchange_name: str, data: dict):
        pass

    async def on_order_update(self, exchange_name: str, order: Any):
        pass
