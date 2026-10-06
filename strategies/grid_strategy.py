"""
Grid Trading Strategy
====================

Grid trading strategy implementation that inherits from BaseStrategy.
Places buy and sell orders at regular intervals to provide liquidity.
"""

import asyncio
import time
from typing import Dict, List, Optional, Any
from dataclasses import dataclass

from .base.base_strategy import BaseStrategy, StrategyConfig, StrategyStatus
from connectors.base_exchange import TradingPair, OrderSide


@dataclass
class GridConfig(StrategyConfig):
    """Configuration for grid trading strategy"""
    # Grid parameters
    grid_levels: int = 10
    grid_spacing: float = 0.5  # Percentage spacing between levels
    profit_percentage: float = 1.0  # Profit margin between buy/sell pairs
    order_amount: float = 0.01  # Base amount per order

    # Price limits (optional)
    upper_price: Optional[float] = None
    lower_price: Optional[float] = None
    center_price: Optional[float] = None

    # Behavior settings
    stop_on_swap_failure: bool = False
    price_deviation: float = 0.2

    # Oracle rebalancing settings
    use_oracle: bool = False
    oracle_check_interval: float = 300
    oracle_rebalance_threshold: float = 0.5  # Rebuild only if center drifts > this %

    # Sizing: inner orders (closest to mid) get full size, outer taper down
    size_taper_factor: float = 0.5  # Outermost order = this fraction of inner size

    # Exchange and pair
    exchange: str = "hydra"
    pair: str = "BTC/USDC"


class GridStrategy(BaseStrategy):
    """
    Grid trading strategy that places orders at regular intervals.

    Maintains a grid of buy/sell orders around a center price.
    Inner orders (closest to mid) are largest and get replaced immediately on fill.
    Outer orders are refilled every cycle without cancelling live orders.
    A full rebuild only happens when price drifts beyond oracle_rebalance_threshold.
    """

    def __init__(self, name: str, config: Dict[str, Any]):
        params = config.get('params', {})

        grid_config = GridConfig(
            name=name,
            enabled=config.get('enabled', True),
            risk_limit=config.get('risk_limit', 1.0),
            max_daily_trades=config.get('max_daily_trades', 1000),
            grid_levels=params.get('grid_levels', 10),
            grid_spacing=params.get('grid_spacing', 0.5),
            profit_percentage=params.get('profit_percentage', 1.0),
            order_amount=params.get('order_amount', 0.01),
            upper_price=params.get('upper_price'),
            lower_price=params.get('lower_price'),
            center_price=params.get('center_price'),
            stop_on_swap_failure=params.get('stop_on_swap_failure', False),
            price_deviation=params.get('price_deviation', 0.2),
            use_oracle=params.get('use_oracle', False),
            oracle_check_interval=params.get('oracle_check_interval', 300),
            oracle_rebalance_threshold=params.get('oracle_rebalance_threshold', 0.5),
            size_taper_factor=params.get('size_taper_factor', 0.5),
            exchange=config.get('exchange', 'hydra'),
            pair=config.get('pair', 'BTC/USDC'),
            params=params
        )

        super().__init__(name, grid_config, {})

        self.grid_config = grid_config
        # order_id -> {price, side, amount, placed_time}
        self.active_orders: Dict[str, Dict] = {}
        self.grid_levels: List[float] = []
        self.center_price: Optional[float] = None
        self.trading_pair: Optional[TradingPair] = None

        # Exchange reference (set by CLI)
        self.exchange = None
        self.target_exchange = None

        # Order tracker reference (set by CLI)
        self.order_tracker = None
        self._callback_registered = False

        # Event loop reference for cross-thread task scheduling
        self.event_loop = None

        # Price oracle (set by CLI if available)
        self.price_oracle = None
        self.oracle_price = None

        # Track center price at last placement for drift detection
        self.last_placed_center: Optional[float] = None

        # Inner order IDs (1 closest buy + 1 closest sell) — replaced immediately on fill
        self.inner_order_ids: set = set()

        # Performance tracking
        self.filled_orders_count = 0
        self.grid_profit = 0.0
        self.last_grid_update = time.time()

        # Balance cache (refresh per refill cycle)
        self._balance_cache: Dict[str, float] = {}
        self._balance_cache_time: float = 0.0
        self._balance_cache_ttl: float = 3.0

    async def initialize(self) -> bool:
        """Initialize the grid strategy"""
        try:
            self.logger.info(f"Initializing grid strategy: {self.name}")

            if not self.exchange:
                self.log_error("Exchange not set for grid strategy")
                return False
            self.target_exchange = self.exchange

            pair_symbol = self.grid_config.pair
            if '/' not in pair_symbol:
                self.log_error(f"Invalid pair format: {pair_symbol}")
                return False

            base, quote = pair_symbol.split('/', 1)
            self.trading_pair = TradingPair(base=base, quote=quote, symbol=pair_symbol)

            if hasattr(self.target_exchange, 'ensure_market_initialized'):
                initialized = await self.target_exchange.ensure_market_initialized(self.trading_pair)
                if not initialized:
                    self.log_error(f"Failed to initialize market {pair_symbol} on exchange")
                    return False

            await self._update_center_price()

            if not self.center_price:
                self.log_error("Could not determine center price")
                return False

            self._calculate_grid_levels()
            self.logger.info(f"Grid initialized: center={self.center_price:.8f}, levels={len(self.grid_levels)}")
            return True

        except Exception as e:
            self.log_error(f"Initialization failed: {e}")
            return False

    async def start(self):
        """Start the grid strategy"""
        self.logger.info(f"Starting grid strategy: {self.name}")
        self.running = True
        self.event_loop = asyncio.get_running_loop()

        if self.order_tracker:
            self.order_tracker.stop_on_swap_failure = False
            # Register resync callback (thread-safe) — fires after DexEvents reconnect
            def _on_resync():
                if self.event_loop and self.event_loop.is_running():
                    asyncio.run_coroutine_threadsafe(self._resync_after_reconnect(), self.event_loop)
            # Chain with any existing callback rather than overwriting
            existing = getattr(self.order_tracker, 'resync_callback', None)
            if existing:
                def _chained(prev=existing, mine=_on_resync):
                    prev()
                    mine()
                self.order_tracker.resync_callback = _chained
            else:
                self.order_tracker.resync_callback = _on_resync

        await self._place_initial_grid()
        asyncio.create_task(self._monitoring_loop())

    async def _resync_after_reconnect(self):
        """After a DexEvents reconnect, force a refill from exchange truth.
        We may have missed fills/cancels during the disconnect."""
        self.logger.info("🔄 DexEvents reconnect detected — resyncing grid state")
        try:
            await self._refill_grid()
        except Exception as e:
            self.log_error(f"Resync failed: {e}")

    async def stop(self):
        """Stop the grid strategy"""
        self.logger.info(f"Stopping grid strategy: {self.name}")
        self.running = False
        await self._cancel_all_orders()
        self.logger.info(f"Grid strategy stopped: {self.name}")

    async def on_market_data(self, exchange_name: str, pair: str, data: Dict):
        """Handle market data updates"""
        if exchange_name != self.grid_config.exchange or pair != self.grid_config.pair:
            return

        if 'ticker' in data:
            current_price = data['ticker'].get('last')
            if current_price and self.center_price:
                price_change = abs(current_price - self.center_price) / self.center_price
                threshold = 2 * self.grid_config.grid_spacing / 100
                if price_change > threshold:
                    self.center_price = current_price
                    self._calculate_grid_levels()

    async def on_order_update(self, exchange_name: str, order: Any):
        """Handle order status updates from exchange"""
        if exchange_name != self.grid_config.exchange:
            return
        order_id = order.id
        if order_id not in self.active_orders:
            return
        if order.status.value in ['canceled', 'cancelled', 'expired']:
            self.active_orders.pop(order_id, None)
            self.inner_order_ids.discard(order_id)

    def on_order_filled(self, order_id: str):
        """Remove from tracking. Immediately replace ANY filled grid order (not just inner)."""
        order_info = self.active_orders.pop(order_id, None)
        self.inner_order_ids.discard(order_id)
        self.filled_orders_count += 1
        # Invalidate balance cache — fill changes balance materially
        self._balance_cache_time = 0.0

        # Replace every filled grid order immediately for HFT robustness.
        # Use thread-safe scheduling — this callback runs in the OrderTracker thread.
        if order_info and self.event_loop and self.event_loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self._replace_filled_order(order_info), self.event_loop
            )

    async def _replace_filled_order(self, order_info: Dict):
        """Immediately place a fresh order at the same price/side/amount."""
        price = order_info['price']
        side = OrderSide.SELL if order_info['side'] == 'sell' else OrderSide.BUY
        amount = order_info['amount']
        self.logger.info(f"🔁 Replacing filled order @ {price:.8f} ({side.value}) amount={amount:.6g}")
        new_id = await self._place_grid_order(price, side, amount, retries=2)
        if not new_id:
            self.logger.warning(f"⚠️  Failed to replace order @ {price:.8f} after retries — refill loop will retry")

    def on_order_cancelled(self, order_id: str):
        self.active_orders.pop(order_id, None)
        self.inner_order_ids.discard(order_id)

    def get_status(self) -> StrategyStatus:
        return StrategyStatus(
            name=self.name,
            running=self.running,
            active_positions=len(self.active_orders),
            total_trades=self.filled_orders_count,
            profit_loss=self.grid_profit,
            last_update=time.time(),
            error_count=self.error_count,
            last_error=self.last_error
        )

    # -------------------------------------------------------------------------
    # Price / level management
    # -------------------------------------------------------------------------

    async def _update_center_price(self):
        """Update center price from oracle, config, or exchange."""
        if self.grid_config.use_oracle and self.price_oracle:
            try:
                oracle_price = self.price_oracle.get_pair_price(self.grid_config.pair)
                if oracle_price:
                    self.center_price = oracle_price
                    self.oracle_price = oracle_price
                    self.logger.info(f"Center price from oracle: {self.center_price:.8f}")
                    return
            except Exception as e:
                self.logger.warning(f"Oracle error, falling back: {e}")

        if self.grid_config.center_price:
            self.center_price = self.grid_config.center_price
            self.logger.info(f"Center price from config: {self.center_price:.8f}")
            return

        try:
            ticker = await self.target_exchange.get_ticker(self.trading_pair)
            if ticker and ticker.last:
                self.center_price = ticker.last
            else:
                orderbook = await self.target_exchange.get_orderbook(self.trading_pair, 1)
                if orderbook and orderbook.bids and orderbook.asks:
                    self.center_price = (orderbook.bids[0][0] + orderbook.asks[0][0]) / 2
            self.logger.info(f"Center price from exchange: {self.center_price:.8f}")
        except Exception as e:
            self.log_error(f"Error updating center price: {e}")

    def _quote_precision(self) -> int:
        """Look up the exchange's quote precision for this pair (used to round
        grid level prices so they bucket-match the prices the exchange returns)."""
        try:
            cache = getattr(self.target_exchange, '_market_cache', None)
            if cache and self.trading_pair and self.trading_pair.symbol in cache:
                return int(cache[self.trading_pair.symbol].get('quote_precision', 6))
        except Exception:
            pass
        return 6  # safe default

    def _round_to_precision(self, price: float) -> float:
        """Round a price to the exchange's quote precision."""
        return round(price, self._quote_precision())

    def _calculate_grid_levels(self):
        """Calculate grid price levels around center, rounded to exchange precision
        so they bucket-match prices returned by get_orders."""
        if not self.center_price:
            return

        self.grid_levels = []
        spacing = self.grid_config.grid_spacing / 100

        for i in range(1, self.grid_config.grid_levels + 1):
            sell_price = self._round_to_precision(self.center_price * (1 + spacing * i))
            if self.grid_config.upper_price and sell_price > self.grid_config.upper_price:
                break
            self.grid_levels.append(sell_price)

        for i in range(1, self.grid_config.grid_levels + 1):
            buy_price = self._round_to_precision(self.center_price * (1 - spacing * i))
            if self.grid_config.lower_price and buy_price < self.grid_config.lower_price:
                break
            self.grid_levels.append(buy_price)

        self.grid_levels.sort()
        self.logger.info(f"Grid levels: {len(self.grid_levels)} total")

    def _tapered_amounts(self, levels: List[float]) -> List[float]:
        """Return linearly tapered amounts for a sorted-by-proximity list of levels."""
        n = len(levels)
        base = self.grid_config.order_amount
        taper = self.grid_config.size_taper_factor
        if n <= 1:
            return [base] * n
        return [base * (1.0 - (1.0 - taper) * i / (n - 1)) for i in range(n)]

    # -------------------------------------------------------------------------
    # Order placement
    # -------------------------------------------------------------------------

    async def _place_initial_grid(self):
        """Place the full grid with tapered sizing. Inner orders tracked separately."""
        if not self.grid_levels or not self.center_price:
            self.log_error("Cannot place grid — missing levels or center price")
            return

        buy_levels = sorted(
            [p for p in self.grid_levels if p < self.center_price],
            key=lambda p: self.center_price - p  # closest first
        )
        sell_levels = sorted(
            [p for p in self.grid_levels if p >= self.center_price],
            key=lambda p: p - self.center_price  # closest first
        )

        buy_amounts = self._tapered_amounts(buy_levels)
        sell_amounts = self._tapered_amounts(sell_levels)

        tasks, is_inner_flags = [], []
        for levels, amounts, side in [
            (buy_levels, buy_amounts, OrderSide.BUY),
            (sell_levels, sell_amounts, OrderSide.SELL),
        ]:
            for rank, (price, amount) in enumerate(zip(levels, amounts)):
                tasks.append(self._place_grid_order(price, side, amount))
                is_inner_flags.append(rank == 0)

        results = await asyncio.gather(*tasks)

        self.inner_order_ids.clear()
        for order_id, is_inner in zip(results, is_inner_flags):
            if order_id and is_inner:
                self.inner_order_ids.add(order_id)

        self.last_placed_center = self.center_price
        self.logger.info(f"Grid placed: {len([r for r in results if r])} orders, inner={self.inner_order_ids}")

    async def _refresh_balance_cache(self):
        """Refresh balance cache if stale. Returns dict of symbol → free balance."""
        now = time.time()
        if now - self._balance_cache_time < self._balance_cache_ttl and self._balance_cache:
            return self._balance_cache
        try:
            balances = await self.target_exchange.get_balances()
            self._balance_cache = {sym: bal.free for sym, bal in balances.items()}
            self._balance_cache_time = now
        except Exception as e:
            self.logger.warning(f"Balance refresh failed: {e}")
        return self._balance_cache

    async def _has_balance_for(self, side: OrderSide, price: float, amount: float) -> bool:
        """Check whether free balance covers this order. Uses cached balances."""
        if not self.trading_pair:
            return True  # fail open if not initialized
        bals = await self._refresh_balance_cache()
        if side == OrderSide.BUY:
            need = price * amount
            sym = self.trading_pair.quote
        else:
            need = amount
            sym = self.trading_pair.base
        # Symbol may appear with network suffix; fall back to prefix match
        have = bals.get(sym)
        if have is None:
            for k, v in bals.items():
                if k.startswith(sym):
                    have = v
                    break
        if have is None:
            return True  # unknown symbol — fail open, exchange will reject if truly insufficient
        # 5% headroom for fees / pending in_use accounting
        return have >= need * 1.05

    async def _place_grid_order(self, price: float, side: OrderSide, amount: float,
                                retries: int = 1) -> Optional[str]:
        """Place a single grid order with retries. Slot dedup is enforced by callers."""
        if not await self._has_balance_for(side, price, amount):
            self.logger.warning(f"Skip {side.value}@{price:.8f} amt={amount:.6g} — insufficient balance")
            return None
        last_err = None
        for attempt in range(retries + 1):
            try:
                order = await self.place_order(
                    exchange_name=self.grid_config.exchange,
                    pair=self.grid_config.pair,
                    side=side.value,
                    amount=amount,
                    price=price,
                    order_type="limit"
                )
                if order:
                    self.active_orders[order.id] = {
                        'price': price,
                        'side': side.value,
                        'amount': amount,
                        'placed_time': time.time(),
                    }
                    if self.order_tracker:
                        self.order_tracker.track_order(order)
                    self.logger.info(f"Placed {side.value} {amount:.6g} @ {price:.8f}")
                    return order.id
                last_err = "place_order returned None"
            except Exception as e:
                last_err = str(e)
            if attempt < retries:
                await asyncio.sleep(0.5 * (attempt + 1))
        self.log_error(f"Failed to place {side.value} @ {price:.8f} after {retries+1} attempts: {last_err}")
        return None

    # -------------------------------------------------------------------------
    # Monitoring loop
    # -------------------------------------------------------------------------

    async def _monitoring_loop(self):
        """Two-tier loop: fast refill (5s) for HFT robustness, oracle/rebuild check (60s)."""
        last_oracle_check = 0.0
        oracle_interval = 60.0  # check oracle / consider rebuild every minute
        refill_interval = 5.0   # refill gaps every 5s

        while self.running:
            try:
                await asyncio.sleep(refill_interval)
                if not self.running:
                    break

                now = time.time()
                do_oracle = (now - last_oracle_check) >= oracle_interval

                if do_oracle and self.grid_config.use_oracle and self.price_oracle:
                    await self._check_oracle_price()
                    last_oracle_check = now

                # Decide: fast refill, or full rebuild?
                if do_oracle and self.last_placed_center and self.center_price:
                    drift_pct = abs(self.center_price - self.last_placed_center) / self.last_placed_center * 100
                    if drift_pct >= self.grid_config.oracle_rebalance_threshold:
                        self.logger.info(f"Drift {drift_pct:.2f}% ≥ threshold — walking grid")
                        self._calculate_grid_levels()
                        await self._walk_grid()
                        self.last_grid_update = now
                        continue

                # Default: fast refill
                await self._refill_grid()
                self.last_grid_update = now

            except Exception as e:
                self.log_error(f"Error in monitoring loop: {e}")
                import traceback
                self.logger.error(traceback.format_exc())
                await asyncio.sleep(5)

    async def _refill_grid(self):
        """Slot-based refill: each grid level is a slot with an expected (price, side, amount).
        Refill a slot if it has no live order, or if the live order's remaining size is
        significantly below the expected amount (partial fill).
        """
        if not self.grid_levels or not self.last_placed_center:
            return

        try:
            live_orders = await self.target_exchange.get_orders(self.trading_pair)
        except Exception as e:
            self.log_error(f"Could not fetch live orders for refill: {e}")
            return

        # Sync active_orders from exchange truth (fix state drift from missed events)
        live_ids = {o.id for o in live_orders} if live_orders else set()
        stale = [oid for oid in self.active_orders if oid not in live_ids]
        for oid in stale:
            self.active_orders.pop(oid, None)
            self.inner_order_ids.discard(oid)
        if stale:
            self.logger.info(f"Cleaned {len(stale)} stale tracked orders not on exchange")

        # Build expected slots from grid_levels (anchored to last_placed_center)
        ref_center = self.last_placed_center
        buy_levels = sorted(
            [p for p in self.grid_levels if p < ref_center],
            key=lambda p: ref_center - p
        )
        sell_levels = sorted(
            [p for p in self.grid_levels if p >= ref_center],
            key=lambda p: p - ref_center
        )
        buy_amounts = self._tapered_amounts(buy_levels)
        sell_amounts = self._tapered_amounts(sell_levels)

        # Bucket prices at exchange precision so grid levels match what the exchange returns
        precision = self._quote_precision()
        scale = 10 ** precision

        def rnd(p: float) -> int:
            return round(p * scale)

        # Index live orders by (side, rounded_price) → order
        live_by_slot: Dict[tuple, Any] = {}
        for o in (live_orders or []):
            key = (o.side, rnd(o.price))
            existing = live_by_slot.get(key)
            # If duplicates at same slot, keep the largest one
            if not existing or o.remaining > existing.remaining:
                live_by_slot[key] = o

        slots = []  # (price, side, expected_amount, rank)
        for levels, amounts, side in [
            (buy_levels, buy_amounts, OrderSide.BUY),
            (sell_levels, sell_amounts, OrderSide.SELL),
        ]:
            for rank, (price, amount) in enumerate(zip(levels, amounts)):
                slots.append((price, side, amount, rank))

        self.logger.info(f"Refill: {len(live_orders) if live_orders else 0} live, "
                         f"{len(slots)} expected slots, {len(self.active_orders)} tracked")

        # Cancel + replace dust orders (remaining < dust_threshold * expected).
        # Otherwise tiny partial fills clog slots until they're filled or expire.
        dust_threshold = 0.3  # remaining < 30% of expected → cancel and refresh
        cancel_tasks = []
        dust_keys: set = set()
        for price, side, expected_amount, _ in slots:
            key = (side, rnd(price))
            live = live_by_slot.get(key)
            if live and 0 < live.remaining < expected_amount * dust_threshold:
                dust_keys.add(key)
                cancel_tasks.append(self._cancel_dust_order(live.id, price, side, live.remaining))

        if cancel_tasks:
            self.logger.info(f"Cancelling {len(cancel_tasks)} dust order(s)")
            await asyncio.gather(*cancel_tasks, return_exceptions=True)
            # The cancelled slots are now empty for placement below
            for k in dust_keys:
                live_by_slot.pop(k, None)

        tasks, missing = [], []
        for price, side, expected_amount, rank in slots:
            key = (side, rnd(price))
            live = live_by_slot.get(key)
            if not live:
                tag = "(was-dust)" if key in dust_keys else "(empty)"
                missing.append(f"{side.value}@{price:.8f} {tag}")
                tasks.append(self._place_grid_order(price, side, expected_amount, retries=2))

        if missing:
            self.logger.info(f"Refilling slots: {missing}")

        if not tasks:
            self.logger.info("Grid fully covered — nothing to refill")
            self._refresh_inner_ids(live_orders, ref_center)
            return

        await asyncio.gather(*tasks)
        # Refresh inner IDs from a fresh snapshot
        try:
            updated = await self.target_exchange.get_orders(self.trading_pair)
            self._refresh_inner_ids(updated, ref_center)
        except Exception:
            pass

    async def _cancel_dust_order(self, order_id: str, price: float, side: OrderSide, remaining: float):
        try:
            await self.target_exchange.cancel_order(order_id, self.trading_pair)
            self.active_orders.pop(order_id, None)
            self.inner_order_ids.discard(order_id)
            self.logger.info(f"Cancelled dust {side.value}@{price:.8f} (rem={remaining:.6g})")
        except Exception as e:
            self.logger.warning(f"Dust cancel failed for {order_id}: {e}")

    def _refresh_inner_ids(self, live_orders: list, ref_center: float):
        """Ensure inner_order_ids always has the closest buy + closest sell."""
        if not live_orders or not ref_center:
            return
        closest_buy, closest_sell = None, None
        best_buy_dist, best_sell_dist = float('inf'), float('inf')
        for o in live_orders:
            dist = abs(o.price - ref_center)
            if o.side == OrderSide.BUY and dist < best_buy_dist:
                best_buy_dist = dist
                closest_buy = o.id
            elif o.side == OrderSide.SELL and dist < best_sell_dist:
                best_sell_dist = dist
                closest_sell = o.id
        self.inner_order_ids.clear()
        if closest_buy:
            self.inner_order_ids.add(closest_buy)
        if closest_sell:
            self.inner_order_ids.add(closest_sell)

    async def _walk_grid(self):
        """Walk the grid: cancel only orders that no longer match a slot, then refill.
        Preserves liquidity throughout — no cancel-all gap. Used when oracle drift
        crosses the rebalance threshold."""
        if not self.grid_levels or not self.center_price:
            return

        try:
            live_orders = await self.target_exchange.get_orders(self.trading_pair)
        except Exception as e:
            self.log_error(f"Walk: could not fetch live orders: {e}")
            return

        # Bucket prices at exchange precision so grid levels match what the exchange returns
        precision = self._quote_precision()
        scale = 10 ** precision

        def rnd(p: float) -> int:
            return round(p * scale)

        # New grid slots (anchored to current center_price, since we're walking)
        new_slot_keys = {(OrderSide.BUY if p < self.center_price else OrderSide.SELL, rnd(p))
                        for p in self.grid_levels}

        # Cancel only orders whose (side, price) no longer matches any new slot
        cancelled = 0
        for o in (live_orders or []):
            key = (o.side, rnd(o.price))
            if key not in new_slot_keys:
                try:
                    await self.target_exchange.cancel_order(o.id, self.trading_pair)
                    self.active_orders.pop(o.id, None)
                    self.inner_order_ids.discard(o.id)
                    cancelled += 1
                except Exception:
                    pass

        # Anchor for refill is the new center
        self.last_placed_center = self.center_price
        self.logger.info(f"Walk: cancelled {cancelled} out-of-range orders; refilling new slots")
        await self._refill_grid()

    async def _cancel_all_orders(self):
        """Cancel all orders on the book for this pair."""
        try:
            exchange_orders = await self.target_exchange.get_orders(self.trading_pair)
            if exchange_orders:
                for order in exchange_orders:
                    try:
                        await self.target_exchange.cancel_order(order.id, self.trading_pair)
                    except Exception:
                        pass
                self.logger.info(f"Cancelled {len(exchange_orders)} orders")
        except Exception as e:
            self.log_error(f"Error cancelling orders: {e}")
        self.active_orders.clear()

    async def _check_oracle_price(self):
        """Fetch oracle price and store it, but do NOT recalculate grid levels.

        Grid levels are only recalculated when a full rebuild is triggered
        (drift exceeds threshold). This prevents the grid level prices from
        drifting away from the live order prices, which would break refill
        matching.
        """
        try:
            oracle_price = self.price_oracle.get_pair_price(self.grid_config.pair)
            if not oracle_price:
                return
            old = self.center_price
            self.oracle_price = oracle_price
            self.center_price = oracle_price
            if old:
                drift = abs(oracle_price - old) / old * 100
                self.logger.info(f"Oracle: {oracle_price:.6f} (drift {drift:.2f}% from previous center)")
        except Exception as e:
            self.logger.error(f"Oracle error: {e}")

    async def _consider_grid_rebalance(self, new_price: float):
        self.center_price = new_price
        self._calculate_grid_levels()
