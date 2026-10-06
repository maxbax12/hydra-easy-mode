#!/usr/bin/env python3
"""
Enhanced Grid Trading Strategy with OrderbookUpdate Integration
===============================================================

Grid trading strategy that uses real-time OrderbookUpdate events for:
- Immediate fill detection
- Market movement response  
- Dynamic grid adjustment
"""

import asyncio
import math
import time
from decimal import Decimal
from typing import Dict, List, Optional, Any, Callable
from dataclasses import dataclass

from .base.base_strategy import BaseStrategy, StrategyConfig, StrategyStatus
from connectors.base_exchange import TradingPair, OrderSide, OrderType
from orderbook_manager import OrderbookManager, OrderbookSnapshot


@dataclass
class EnhancedGridConfig(StrategyConfig):
    """Configuration for enhanced grid trading strategy"""
    # Grid parameters
    grid_levels: int = 10
    grid_spacing: float = 0.5  # Percentage spacing between levels
    profit_percentage: float = 1.0  # Profit margin between buy/sell pairs
    order_amount: float = 0.01  # Base amount per order
    
    # Price limits
    upper_price: Optional[float] = None
    lower_price: Optional[float] = None
    center_price: Optional[float] = None
    
    # Dynamic adjustment
    auto_adjust: bool = True  # Automatically adjust grid on market moves
    adjustment_threshold: float = 2.0  # Percentage move to trigger adjustment
    min_spread: float = 0.1  # Minimum spread to maintain
    
    # Real-time response
    react_to_fills: bool = True  # React immediately to orderbook fills
    react_to_trades: bool = True  # React to public trades
    price_update_threshold: float = 0.05  # Minimum price change to react to

    # Rebuilding orders cancelled outside the bot (by the node, or in the app).
    # Without this a cancelled level is simply gone: the strategy only places
    # orders in response to fills.
    rebuild_cancelled: bool = True
    rebuild_delay: float = 15.0          # seconds a level must stay empty first
    rebuild_check_interval: float = 10.0
    max_rebuilds_per_hour: int = 6       # per level; stops a cancel/re-place loop

    # Following the market. The orderbook-callback path never fires in
    # production (it hangs off the arbitrage callback and needs a market-events
    # subscription that this pair does not get), so drift is polled here.
    price_check_interval: float = 30.0
    recenter_cooldown: float = 60.0      # min seconds between recenters

    # Top-of-book quoting. A fixed ladder sits wherever the spacing puts it, so a
    # competitor quoting inside our innermost level takes all the flow on that
    # side. The innermost live order per side is re-priced to just inside the
    # touch; the rest of the ladder stays where it is.
    peg_inner_quotes: bool = True
    peg_interval: float = 15.0           # min seconds between re-pegs per side
    peg_min_ticks: int = 2               # hysteresis, also avoids 1-tick wars

    # External-price guard. This venue's book can lag the real market by percent
    # (2.4% on 2026-09-19, with every one of our asks under the true price). The
    # grid still trades Hydra's book, but never sells below, nor buys above, what
    # the outside market says: sells ladder from max(hydra mid, external), buys
    # from min(...), and every placement is clamped to external -/+ guard pct.
    external_guard: bool = True
    external_guard_pct: float = 0.5      # disagreement reported as "engaged" (log/hold only)
    # Hard limit on every quote: buys at most ext*(1-clamp), sells at least
    # ext*(1+clamp). It used to be ext -/+ external_guard_pct, i.e. bids allowed
    # 0.5% ABOVE the outside market — wider than the 0.4% taker fee, so selling
    # into them was profitable; on 2026-09-21 11:03 someone did, sweeping four bids
    # 0.4-0.7% over the market.
    external_clamp_pct: float = 0.0
    external_max_age: float = 900.0      # seconds an external price stays trusted

    # Exchange and pair
    exchange: str = "hydra"
    pair: str = "BTC/USDT"


class EnhancedGridStrategy(BaseStrategy):
    """
    Enhanced grid strategy using real-time OrderbookUpdate events
    
    Improvements over basic grid:
    1. Uses OrderbookUpdate events for immediate fill detection
    2. Reacts to market price changes in real-time
    3. Dynamically adjusts grid based on market conditions
    4. Monitors orderbook depth for competitive positioning
    """
    
    def __init__(self, name: str, config: Dict[str, Any], 
                 orderbook_manager: OrderbookManager):
        # Create StrategyConfig object from dict
        params = config.get('params', {})
        
        grid_config = EnhancedGridConfig(
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
            auto_adjust=params.get('auto_adjust', True),
            adjustment_threshold=params.get('adjustment_threshold', 2.0),
            min_spread=params.get('min_spread', 0.1),
            react_to_fills=params.get('react_to_fills', True),
            react_to_trades=params.get('react_to_trades', True),
            price_update_threshold=params.get('price_update_threshold', 0.05),
            rebuild_cancelled=params.get('rebuild_cancelled', True),
            rebuild_delay=params.get('rebuild_delay', 15.0),
            rebuild_check_interval=params.get('rebuild_check_interval', 10.0),
            max_rebuilds_per_hour=params.get('max_rebuilds_per_hour', 6),
            price_check_interval=params.get('price_check_interval', 30.0),
            recenter_cooldown=params.get('recenter_cooldown', 60.0),
            peg_inner_quotes=params.get('peg_inner_quotes', True),
            peg_interval=params.get('peg_interval', 15.0),
            peg_min_ticks=params.get('peg_min_ticks', 2),
            external_guard=params.get('external_guard', True),
            external_guard_pct=params.get('external_guard_pct', 0.5),
            external_clamp_pct=params.get('external_clamp_pct', 0.0),
            external_max_age=params.get('external_max_age', 900.0),
            exchange=config.get('exchange', 'hydra'),
            pair=config.get('pair', 'BTC/USDT'),
            params=params
        )
        
        super().__init__(name, grid_config, {})
        
        self.grid_config = grid_config
        self.orderbook_manager = orderbook_manager
        
        # Grid state
        self.active_orders: Dict[str, Dict] = {}  # order_id -> order_info
        self.grid_levels: List[float] = []
        self.center_price: Optional[float] = None
        self.last_orderbook: Optional[OrderbookSnapshot] = None
        self.trading_pair: Optional[TradingPair] = None
        
        # Exchange reference
        self.exchange = None
        self.target_exchange = None

        # Set by the CLI; orders must be registered with it or no fill event
        # ever reaches us. on_order_filled() fires on the OrderTracker thread,
        # so we also need the running loop to marshal work back onto.
        self.order_tracker = None
        self.event_loop = None

        # Slot model. Every grid order occupies a slot {side, price, amount,
        # order_id, empty_since, rebuilds}. A fill moves the slot to its flip
        # order; a cancel from outside the bot empties it (empty_since set) until
        # _rebuild_loop re-places it. order_id None with empty_since None means a
        # flip is in flight and the slot must be left alone.
        self.slots: Dict[int, Dict[str, Any]] = {}
        self._next_slot_id = 0
        self._order_slot: Dict[str, int] = {}
        # Order objects as registered with the OrderTracker, which updates their
        # filled/remaining in place — needed to size a partially filled rebuild.
        self._order_objs: Dict[str, Any] = {}
        self._intentional_cancels: set = set()

        # Our own liveness flag. BaseStrategy.start() returns None, so start()
        # below always returns False and self.running is never set.
        self._active = False
        self._rebuild_task: Optional[asyncio.Task] = None
        self._ticker_cache = None            # (ticker, fetched_at)
        self._last_price_poll = 0.0
        self._last_recenter = 0.0
        self._last_peg = {'buy': 0.0, 'sell': 0.0}
        # Market minimums. The exchange's _market_cache only carries precisions,
        # so these are fetched from GetMarketInfo at startup.
        self._min_base = 0.0
        self._min_quote = 0.0

        # External reference (set by the CLI: Pyth, CoinGecko fallback)
        self.price_oracle = None
        self._ext_price: Optional[float] = None
        self._ext_at = 0.0
        self._last_ext_poll = 0.0
        self._guard_engaged = False
        self._guard_engagements = 0
        self._ext_stale_warned = 0.0
        self._built_refs = None              # (buy_ref, sell_ref) of the live ladder
        
        # Performance tracking
        self.filled_orders_count = 0
        self.grid_profit = 0.0
        self.last_grid_update = time.time()
        self.last_price_check = time.time()
        
        # Real-time monitoring
        self.orderbook_callbacks: List[Callable] = []
        self.is_monitoring_orderbook = False
        
    async def initialize(self) -> bool:
        """Initialize the enhanced grid strategy"""
        try:
            self.logger.info(f"Initializing enhanced grid strategy: {self.name}")
            self.event_loop = asyncio.get_running_loop()

            if not self.exchange:
                self.log_error("Exchange not set for enhanced grid strategy")
                return False
            self.target_exchange = self.exchange
            
            # Parse trading pair
            pair_symbol = self.grid_config.pair
            if '/' not in pair_symbol:
                self.log_error(f"Invalid pair format: {pair_symbol}")
                return False
            
            # Get trading pair from exchange
            trading_pairs = await self.exchange.get_trading_pairs()
            self.trading_pair = next(
                (pair for pair in trading_pairs if pair.symbol == pair_symbol), 
                None
            )
            
            if not self.trading_pair:
                self.log_error(f"Trading pair {pair_symbol} not found on {self.exchange.name}")
                return False
            
            self._load_market_minimums()
            for attempt in range(3):
                await self._refresh_external(force=True)
                if self._ext_price or not self.grid_config.external_guard:
                    break
                await asyncio.sleep(2)
            if self.grid_config.external_guard and not self._ext_price:
                self.logger.warning(
                    "🛡️ No outside price at startup — the first ladder is built WITHOUT the "
                    "external guard; it engages as soon as a price arrives"
                )

            # Set center price
            await self._initialize_center_price()
            
            # Start real-time monitoring
            await self._start_orderbook_monitoring()
            
            # Initialize grid
            await self._initialize_grid()
            self._load_market_minimums()     # the market cache is populated by now
            
            self._active = True
            if self.grid_config.rebuild_cancelled:
                self._rebuild_task = asyncio.create_task(self._rebuild_loop())

            self.logger.info(f"Enhanced grid strategy initialized with {len(self.grid_levels)} levels")
            return True
            
        except Exception as e:
            self.log_error(f"Failed to initialize enhanced grid strategy: {e}")
            return False
    
    async def _start_orderbook_monitoring(self):
        """Start monitoring orderbook updates for this pair"""
        try:
            # Add callback for orderbook updates
            self.orderbook_manager.add_arbitrage_callback(self._on_orderbook_update)
            self.is_monitoring_orderbook = True
            
            self.logger.info(f"Started real-time orderbook monitoring for {self.grid_config.pair}")
            
        except Exception as e:
            self.logger.error(f"Error starting orderbook monitoring: {e}")
    
    def _on_orderbook_update(self, opportunity):
        """Handle orderbook update (we'll filter for our pair)"""
        if opportunity.pair.replace('/', '') != self.grid_config.pair.replace('/', ''):
            return  # Not our pair
        
        # Schedule async processing
        asyncio.create_task(self._process_orderbook_update())
    
    async def _process_orderbook_update(self):
        """Process orderbook update for our trading pair"""
        try:
            # Get current orderbook snapshot
            current_orderbook = self.orderbook_manager.get_orderbook(
                self.grid_config.exchange,
                self.grid_config.pair
            )
            
            if not current_orderbook:
                return
            
            # Check for significant price changes
            if self.last_orderbook:
                await self._check_price_movement(current_orderbook, self.last_orderbook)
            
            # Check if any of our orders might have been filled
            if self.grid_config.react_to_fills:
                await self._check_potential_fills(current_orderbook)
            
            # Update last orderbook
            self.last_orderbook = current_orderbook
            self.last_price_check = time.time()
            
        except Exception as e:
            self.logger.error(f"Error processing orderbook update: {e}")
    
    async def _check_price_movement(self, current: OrderbookSnapshot, previous: OrderbookSnapshot):
        """Check for significant price movements that require grid adjustment"""
        if not current.best_bid or not current.best_ask or not previous.best_bid or not previous.best_ask:
            return
        
        # Drift is measured against the grid's own center, not against the
        # previous snapshot: comparing consecutive snapshots only ever fires on
        # a single-tick jump of adjustment_threshold%, which in practice never
        # happens, so the grid stayed pinned wherever it started.
        current_mid = (current.best_bid + current.best_ask) / 2
        if not self.center_price:
            return

        price_change_pct = abs(current_mid - self.center_price) / self.center_price * 100
        
        # Check if adjustment is needed
        if (self.grid_config.auto_adjust and 
            price_change_pct > self.grid_config.adjustment_threshold):
            
            self.logger.info(
                f"Grid drifted {price_change_pct:.2f}% from center "
                f"{self.center_price:.8f} (threshold: "
                f"{self.grid_config.adjustment_threshold:.2f}%) — recentering"
            )
            
            await self._adjust_grid_to_market(current_mid)
    
    async def _check_potential_fills(self, orderbook: OrderbookSnapshot):
        """Check if any of our orders might have been filled based on orderbook"""
        # The OrderTracker reports fills authoritatively from SubscribeDexEvents.
        # This heuristic treats any order that vanished from the book as filled,
        # so it would flip an order that was actually cancelled. Fallback only.
        if self.order_tracker:
            return
        if not self.active_orders or not orderbook.best_bid or not orderbook.best_ask:
            return
        
        orders_to_check = []
        
        # Check if any of our orders are at or better than current market prices
        for order_id, order_info in self.active_orders.items():
            order_price = order_info['price']
            order_side = order_info['side']
            
            # Buy order filled if market moved down to our price
            if (order_side == 'buy' and 
                orderbook.best_ask and order_price >= orderbook.best_ask):
                orders_to_check.append(order_id)
            
            # Sell order filled if market moved up to our price  
            elif (order_side == 'sell' and 
                  orderbook.best_bid and order_price <= orderbook.best_bid):
                orders_to_check.append(order_id)
        
        # Check these orders for fills
        if orders_to_check:
            self.logger.debug(f"Checking {len(orders_to_check)} orders for potential fills")
            await self._verify_order_fills(orders_to_check)
    
    async def _verify_order_fills(self, order_ids: List[str]):
        """Verify if specific orders have been filled"""
        try:
            # Get current orders from exchange
            if hasattr(self.exchange, 'get_orders'):
                current_orders = await self.exchange.get_orders(self.trading_pair)
                current_order_ids = {order.id for order in current_orders}
                
                # Check which of our tracked orders are missing (likely filled)
                for order_id in order_ids:
                    if order_id not in current_order_ids:
                        # Order is missing - likely filled
                        await self._handle_suspected_fill(order_id)
        
        except Exception as e:
            self.logger.error(f"Error verifying order fills: {e}")
    
    async def _handle_suspected_fill(self, order_id: str):
        """Handle a suspected order fill"""
        if order_id not in self.active_orders:
            return

        self.logger.info(f"🎯 Suspected fill detected for order {order_id[:8]}... via orderbook analysis")
        await self._on_fill(order_id)
        
    async def _adjust_grid_to_market(self, new_center_price: float):
        """Adjust grid to new market conditions"""
        try:
            self.logger.info(f"Adjusting grid to new center price: {new_center_price}")
            
            # Cancel existing orders
            await self._cancel_all_grid_orders()
            
            # Update center price
            self.center_price = new_center_price
            
            # Recalculate grid levels
            await self._calculate_grid_levels()
            
            # Place new grid orders
            await self._place_grid_orders()
            
            self.last_grid_update = time.time()
            
        except Exception as e:
            self.logger.error(f"Error adjusting grid: {e}")
    
    async def _initialize_center_price(self):
        """Initialize center price for grid"""
        if self.grid_config.center_price:
            configured = self.grid_config.center_price
            self.center_price = configured
            self.logger.info(f"Using configured center price: {self.center_price}")

            # A configured center goes stale as the market moves, and every taker
            # fill in this bot's history came from starting a grid around a stale
            # center. Start at the market instead when it has drifted.
            if self.grid_config.auto_adjust:
                mid, _, _ = await self._market_mid()
                if mid:
                    drift = abs(mid - configured) / configured * 100
                    if drift >= self.grid_config.adjustment_threshold:
                        self.logger.warning(
                            f"Configured center {configured} is {drift:.2f}% from the mid "
                            f"{mid:.8f} — starting at the market instead"
                        )
                        self.center_price = mid
        else:
            # Get current market price
            ticker = await self.exchange.get_ticker(self.trading_pair)
            if ticker and ticker.last:
                self.center_price = ticker.last
                self.logger.info(f"Using market center price: {self.center_price}")
            else:
                raise ValueError("Could not determine center price")
    
    async def _initialize_grid(self):
        """Initialize the trading grid"""
        await self._calculate_grid_levels()
        
        # Adjust order amounts based on available balance
        await self._adjust_order_amounts_for_balance()
        
        await self._place_grid_orders()
    
    def _quote_precision(self) -> int:
        """Exchange quote precision for this pair, used to round level prices."""
        try:
            cache = getattr(self.target_exchange, '_market_cache', None)
            if cache and self.trading_pair and self.trading_pair.symbol in cache:
                return int(cache[self.trading_pair.symbol].get('quote_precision', 6))
        except Exception:
            pass
        return 6  # safe default

    def _round_to_precision(self, price: float) -> float:
        return round(price, self._quote_precision())

    async def _calculate_grid_levels(self):
        """Calculate grid price levels"""
        if not self.center_price:
            return
        
        self.grid_levels = []
        spacing = self.grid_config.grid_spacing / 100
        
        # Calculate levels above and below center
        levels_per_side = self.grid_config.grid_levels // 2

        buy_ref, sell_ref, engaged = self._refs(self.center_price)
        self._built_refs = (buy_ref, sell_ref)
        self._note_guard_state(engaged)

        for i in range(1, levels_per_side + 1):
            # Buy levels (below the buy reference)
            buy_level = self._round_to_precision(buy_ref * (1 - spacing * i))
            if not self.grid_config.lower_price or buy_level >= self.grid_config.lower_price:
                self.grid_levels.append(('buy', buy_level))
            
            # Sell levels (above the sell reference)
            sell_level = self._round_to_precision(sell_ref * (1 + spacing * i))
            if not self.grid_config.upper_price or sell_level <= self.grid_config.upper_price:
                self.grid_levels.append(('sell', sell_level))

        if engaged:
            self.logger.info(
                f"Calculated {len(self.grid_levels)} grid levels: buys below {buy_ref:.8f}, "
                f"sells above {sell_ref:.8f} (external guard engaged)"
            )
        else:
            self.logger.info(f"Calculated {len(self.grid_levels)} grid levels around {self.center_price}")
    
    async def _adjust_order_amounts_for_balance(self):
        """Adjust order amounts based on available balance"""
        try:
            balances = await self.exchange.get_balances()
            
            # Find our quote and base currency balances
            quote_balance = 0
            base_balance = 0
            
            quote_symbol = self.trading_pair.quote
            base_symbol = self.trading_pair.base
            
            # Check for quote currency balance (for buy orders)
            for key in [quote_symbol, quote_symbol.upper(), quote_symbol.lower(), f"{quote_symbol[:8]}..."]:
                if key in balances:
                    quote_balance = balances[key].free
                    break
            
            # Check for base currency balance (for sell orders)
            for key in [base_symbol, base_symbol.upper(), base_symbol.lower(), f"{base_symbol[:8]}..."]:
                if key in balances:
                    base_balance = balances[key].free
                    break
            
            # Count buy and sell orders in our grid
            buy_orders = sum(1 for side, _ in self.grid_levels if side == 'buy')
            sell_orders = sum(1 for side, _ in self.grid_levels if side == 'sell')
            
            # Calculate average price for buy orders to estimate total cost
            buy_prices = [price for side, price in self.grid_levels if side == 'buy']
            if buy_prices and buy_orders > 0:
                avg_buy_price = sum(buy_prices) / len(buy_prices)
                total_buy_cost = buy_orders * self.grid_config.order_amount * avg_buy_price
                
                if total_buy_cost > quote_balance * 0.9:  # Leave 10% buffer
                    # Reduce order amount to fit within balance
                    max_order_amount = (quote_balance * 0.9) / (buy_orders * avg_buy_price)
                    if max_order_amount < self.grid_config.order_amount:
                        self.logger.warning(
                            f"Reducing order amount from {self.grid_config.order_amount} to {max_order_amount:.6f} "
                            f"due to insufficient {quote_symbol} balance ({quote_balance:.6f})"
                        )
                        self.grid_config.order_amount = max_order_amount
            
            # Check if we have enough base currency for sell orders
            total_sell_amount = sell_orders * self.grid_config.order_amount
            if total_sell_amount > base_balance * 0.9:  # Leave 10% buffer
                max_order_amount = (base_balance * 0.9) / sell_orders if sell_orders > 0 else 0
                if max_order_amount < self.grid_config.order_amount:
                    self.logger.warning(
                        f"Reducing order amount from {self.grid_config.order_amount} to {max_order_amount:.6f} "
                        f"due to insufficient {base_symbol} balance ({base_balance:.6f})"
                    )
                    self.grid_config.order_amount = max_order_amount
            
            if self.grid_config.order_amount > 0:
                self.logger.info(f"Grid order amount set to: {self.grid_config.order_amount:.6f}")
            else:
                self.logger.error("Order amount reduced to zero - insufficient balance for grid trading")
                
        except Exception as e:
            self.logger.error(f"Error adjusting order amounts: {e}")
    
    async def _place_grid_orders(self):
        """Place orders at all grid levels, one slot per level"""
        self.slots.clear()
        self._order_slot.clear()
        self._order_objs.clear()
        for side, price in self.grid_levels:
            slot_id = self._new_slot(side, price, self.grid_config.order_amount)
            try:
                await self._place_grid_order(side, price, self.grid_config.order_amount, slot_id)
                await asyncio.sleep(0.1)  # Small delay between orders
            except Exception as e:
                self.logger.error(f"Error placing grid order at {price}: {e}")

    def _new_slot(self, side: str, price: float, amount: float) -> int:
        slot_id = self._next_slot_id
        self._next_slot_id += 1
        self.slots[slot_id] = {
            'side': side, 'price': price, 'amount': amount,
            'order_id': None, 'empty_since': None, 'rebuilds': [],
            # where the ladder put this level; a peg never quotes worse than this
            'base_price': price,
        }
        return slot_id

    async def _place_grid_order(self, side: str, price: float, amount: float,
                                slot_id: Optional[int] = None,
                                allow_cross: bool = False):
        """Place a single grid order with balance and crossing checks.

        Returns the order, or None if it was not placed. A slot whose order could
        not be placed is marked empty so the rebuild loop retries it once the
        market has moved back.
        """
        guarded = self._guard_price(side, price)
        if guarded != price:
            self.logger.info(
                f"🛡️ {side} {price} moved to {guarded}: outside market is {self._external():.8f}"
            )
            price = guarded
            if slot_id is not None and slot_id in self.slots:
                self.slots[slot_id]['price'] = price

        if not allow_cross and self._would_cross(side, price, await self._get_ticker()):
            self.logger.info(
                f"Skipping {side} {amount} @ {price} — it would cross the book and pay "
                f"the taker fee; will retry when price moves back"
            )
            slot = self.slots.get(slot_id) if slot_id is not None else None
            if slot is not None:
                slot['order_id'] = None
                slot['empty_since'] = slot['empty_since'] or time.time()
            return None

        order = await self._submit_order(side, price, amount)
        slot = self.slots.get(slot_id) if slot_id is not None else None
        if slot is not None:
            if order:
                slot['order_id'] = order.id
                slot['empty_since'] = None
                self._order_slot[order.id] = slot_id
            else:
                slot['order_id'] = None
                slot['empty_since'] = slot['empty_since'] or time.time()
        return order

    async def _submit_order(self, side: str, price: float, amount: float):
        try:
            # Check if we have sufficient balance before placing order
            if not await self._check_sufficient_balance(side, price, amount):
                self.logger.warning(f"Insufficient balance for {side} order: {amount} at {price}")
                return None
            
            order_side = OrderSide.BUY if side == 'buy' else OrderSide.SELL
            
            order = await self.exchange.place_order(
                pair=self.trading_pair,
                side=order_side,
                type=OrderType.LIMIT,
                amount=amount,
                price=price
            )
            
            if order:
                self.active_orders[order.id] = {
                    'side': side,
                    'price': price,
                    'amount': amount,
                    'timestamp': time.time()
                }
                
                self._order_objs[order.id] = order
                if self.order_tracker:
                    self.order_tracker.track_order(order)

                self.logger.debug(f"Placed {side} order at {price}: {order.id[:8]}...")
            return order

        except Exception as e:
            if "Not enough receiving capacity" in str(e) or "insufficient" in str(e).lower():
                self.logger.warning(f"Insufficient balance for {side} order at {price}: {e}")
            else:
                self.logger.error(f"Error placing {side} order at {price}: {e}")
            return None
    
    async def _place_replacement_order(self, filled_price: float, filled_side: str, amount: float,
                                       slot_id: Optional[int] = None):
        """Place replacement order after a fill; the slot moves to the flip order"""
        try:
            # Calculate replacement order price
            spacing = self.grid_config.grid_spacing / 100
            profit_margin = self.grid_config.profit_percentage / 100
            
            if filled_side == 'buy':
                # After buy fill, place sell order above
                new_price = self._round_to_precision(filled_price * (1 + profit_margin))
                new_side = 'sell'
            else:
                # After sell fill, place buy order below
                new_price = self._round_to_precision(filled_price * (1 - profit_margin))
                new_side = 'buy'
            
            # Check price limits
            if self._is_price_within_limits(new_price, new_side):
                if slot_id is not None and slot_id in self.slots:
                    self.slots[slot_id].update(side=new_side, price=new_price,
                                               amount=amount, base_price=new_price)
                order = await self._place_grid_order(new_side, new_price, amount, slot_id)
                if order:
                    self.logger.info(
                        f"📈 Placed replacement {new_side} order at {new_price} "
                        f"after {filled_side} fill at {filled_price}"
                    )
                else:
                    self.logger.warning(
                        f"Replacement {new_side} at {new_price} after {filled_side} fill at "
                        f"{filled_price} was not placed — will retry via rebuild"
                    )
            else:
                self.logger.warning(f"Replacement order price {new_price} outside limits")
                if slot_id is not None:
                    self.slots.pop(slot_id, None)
        
        except Exception as e:
            self.logger.error(f"Error placing replacement order: {e}")
    
    def _is_price_within_limits(self, price: float, side: str) -> bool:
        """Check if price is within configured limits"""
        if side == 'buy' and self.grid_config.lower_price:
            return price >= self.grid_config.lower_price
        elif side == 'sell' and self.grid_config.upper_price:
            return price <= self.grid_config.upper_price
        return True
    
    async def _check_sufficient_balance(self, side: str, price: float, amount: float) -> bool:
        """Check if we have sufficient balance for an order"""
        try:
            balances = await self.exchange.get_balances()
            
            if side == 'buy':
                # For buy orders, we need quote currency (e.g., USDC to buy BTC)
                quote_symbol = self.trading_pair.quote
                required_amount = amount * price
                
                # Check various possible quote asset formats
                possible_keys = [
                    quote_symbol, 
                    quote_symbol.upper(), 
                    quote_symbol.lower(),
                    f"{quote_symbol[:8]}..."  # Hydra format with truncated asset ID
                ]
                
                available_balance = 0
                for key in possible_keys:
                    if key in balances:
                        available_balance = balances[key].free
                        self.logger.debug(f"Found {key} balance: {available_balance}")
                        break
                
                if available_balance >= required_amount:
                    return True
                else:
                    self.logger.warning(f"Insufficient {quote_symbol}: need {required_amount:.8f}, have {available_balance:.8f}")
                    # Show available balances for debugging
                    self.logger.debug(f"Available balances: {list(balances.keys())}")
                    return False
                    
            else:  # sell
                # For sell orders, we need base currency (e.g., BTC to sell for USDC)
                base_symbol = self.trading_pair.base
                
                # Check various possible base asset formats
                possible_keys = [
                    base_symbol, 
                    base_symbol.upper(), 
                    base_symbol.lower(),
                    f"{base_symbol[:8]}..."  # Hydra format with truncated asset ID
                ]
                
                available_balance = 0
                for key in possible_keys:
                    if key in balances:
                        available_balance = balances[key].free
                        self.logger.debug(f"Found {key} balance: {available_balance}")
                        break
                
                if available_balance >= amount:
                    return True
                else:
                    self.logger.warning(f"Insufficient {base_symbol}: need {amount:.8f}, have {available_balance:.8f}")
                    # Show available balances for debugging
                    self.logger.debug(f"Available balances: {list(balances.keys())}")
                    return False
                    
        except Exception as e:
            self.logger.error(f"Error checking balance: {e}")
            return False  # Assume insufficient balance on error
    
    async def _cancel_all_grid_orders(self):
        """Cancel all active grid orders"""
        # Our own cancels must not be rebuilt. Their cancel events arrive later,
        # on the tracker thread, so remember the ids and forget the slots now.
        self._intentional_cancels.update(self.active_orders.keys())
        self.slots.clear()
        self._order_slot.clear()
        self._order_objs.clear()

        if not self.active_orders:
            self.logger.info("No active orders to cancel")
            return
            
        if not self.trading_pair:
            self.logger.error("Trading pair not set - cannot cancel orders")
            return
            
        self.logger.info(f"Canceling {len(self.active_orders)} active grid orders...")
        
        for order_id in list(self.active_orders.keys()):
            try:
                success = await self.exchange.cancel_order(order_id, self.trading_pair)
                if success:
                    self.logger.debug(f"Canceled order {order_id[:8]}...")
                else:
                    self.logger.warning(f"Failed to cancel order {order_id[:8]}...")
            except Exception as e:
                self.logger.error(f"Error canceling order {order_id[:8]}...: {e}")
        
        self.active_orders.clear()
        self.logger.info("✅ All grid orders canceled")
    
    # Strategy lifecycle methods
    async def start(self) -> bool:
        """Start the enhanced grid strategy"""
        if not await super().start():
            return False
        
        self.logger.info(f"🚀 Enhanced grid strategy started with real-time orderbook monitoring")
        return True
    
    def prepare_for_shutdown(self):
        """Stop reacting to order events before the bot cancels all orders.

        trading_bot_cli.shutdown() cancels every order *before* stopping
        strategies; without this the rebuild loop would re-place them.
        """
        self._active = False
        if self._rebuild_task and not self._rebuild_task.done():
            self._rebuild_task.cancel()

    async def stop(self) -> bool:
        """Stop the enhanced grid strategy"""
        self.prepare_for_shutdown()
        self.is_monitoring_orderbook = False
        await self._cancel_all_grid_orders()
        
        return await super().stop()
    
    def get_status(self) -> StrategyStatus:
        """Get current strategy status"""
        return StrategyStatus(
            name=self.name,
            running=self.running,
            active_positions=len(self.active_orders),
            total_trades=self.filled_orders_count,
            profit_loss=self.grid_profit,
            last_update=self.last_price_check,
            error_count=self.error_count,
            last_error=self.last_error
        )
    
    # BaseStrategy interface methods
    async def on_market_data(self, exchange_name: str, data: dict):
        """Handle market data updates"""
        # Enhanced grid uses OrderbookUpdates instead of market data
        # This method is required by BaseStrategy but not used
        pass
    
    # Legacy callbacks for compatibility
    async def on_order_update(self, exchange_name: str, order: Any):
        """Handle order updates from DexEvents"""
        if exchange_name != self.grid_config.exchange:
            return
        
        order_id = order.id
        if order_id not in self.active_orders:
            return
        
        if order.status.value in ['filled', 'closed']:
            self.logger.info(f"Grid order filled (DexEvent): {order_id[:8]}...")
            await self._handle_order_fill_confirmed(order_id, order)
    
    # OrderTracker callbacks run on the tracker thread. All grid state is owned by
    # the event loop, so they only hand the event over.
    def on_order_filled(self, order_id: str):
        """Fill notification from OrderTracker (SubscribeDexEvents -> order_completed)."""
        self._dispatch(self._on_fill(order_id), order_id, "fill")

    def on_order_cancelled(self, order_id: str):
        """Cancel notification from OrderTracker (SubscribeDexEvents -> order_canceled)."""
        self._dispatch(self._on_cancel(order_id), order_id, "cancel")

    def _dispatch(self, coro, order_id: str, what: str):
        if self.event_loop and self.event_loop.is_running():
            asyncio.run_coroutine_threadsafe(coro, self.event_loop)
        else:
            coro.close()
            self.logger.warning(f"No running loop — cannot handle {what} of {order_id[:8]}...")

    async def _on_fill(self, order_id: str):
        order_info = self.active_orders.pop(order_id, None)
        if order_info is None:
            return  # not ours, or already handled
        slot_id = self._order_slot.pop(order_id, None)
        self._order_objs.pop(order_id, None)
        self.filled_orders_count += 1
        if slot_id is not None and slot_id in self.slots:
            self.slots[slot_id].update(order_id=None, empty_since=None)  # flip in flight
        await self._place_replacement_order(
            order_info['price'], order_info['side'], order_info['amount'], slot_id
        )

    async def _on_cancel(self, order_id: str):
        order_info = self.active_orders.pop(order_id, None)
        slot_id = self._order_slot.pop(order_id, None)
        order = self._order_objs.pop(order_id, None)

        if order_id in self._intentional_cancels:
            self._intentional_cancels.discard(order_id)
            return
        if order_info is None or slot_id is None or slot_id not in self.slots:
            return

        side, price = order_info['side'], order_info['price']
        if not (self._active and self.grid_config.rebuild_cancelled):
            self.slots.pop(slot_id, None)
            return

        unfilled = self._unfilled_base_amount(order, order_info)
        if unfilled is None:
            self.logger.warning(
                f"{side} @ {price} ({order_id[:8]}) cancelled outside the bot after a partial "
                f"fill of unknown size — not rebuilding this level"
            )
            self.slots.pop(slot_id, None)
            return

        slot = self.slots[slot_id]
        if self._below_minimum(unfilled, price):
            # Nearly filled: the remainder is dust the node will not accept, so it
            # is dropped rather than retried forever. The filled part still flips.
            filled = slot['amount'] - unfilled
            self.logger.info(
                f"{side} @ {price} ({order_id[:8]}) cancelled with {unfilled} unfilled, below the "
                f"market minimum (base {self._min_base}, quote {self._min_quote}) — not rebuilding it"
            )
            self.slots.pop(slot_id, None)
            if filled > 0 and not self._below_minimum(filled, price):
                self.filled_orders_count += 1
                await self._place_replacement_order(
                    price, side, filled, self._new_slot(side, price, filled)
                )
            return

        if unfilled < slot['amount'] - 1e-12:
            filled = slot['amount'] - unfilled
            self.logger.warning(
                f"{side} @ {price} ({order_id[:8]}) was partially filled before the cancel: "
                f"rebuilding the unfilled {unfilled}, flipping the filled {filled:.6g}"
            )
            slot['amount'] = unfilled
            # The filled part earned its flip just as a completed fill would; it
            # gets its own slot so it is tracked and rebuilt like any other level.
            if not self._below_minimum(filled, price):
                await self._place_replacement_order(
                    price, side, filled, self._new_slot(side, price, filled)
                )
        slot.update(order_id=None, empty_since=time.time())
        self.logger.warning(
            f"🧱 {side} {slot['amount']} @ {price} ({order_id[:8]}) was cancelled outside the "
            f"bot — rebuilding in ≥{self.grid_config.rebuild_delay:.0f}s"
        )

    def _unfilled_base_amount(self, order: Any, order_info: Dict) -> Optional[float]:
        """Unfilled size in base units, or None if it cannot be determined safely."""
        amount = order_info['amount']
        filled = getattr(order, 'filled', 0.0) if order is not None else 0.0
        if not filled or filled <= 0:
            return amount
        remaining = getattr(order, 'remaining', None)
        if remaining is None:
            return None
        # OrderTracker keeps a SELL's remainder in base units but a BUY's in quote
        # units (its _order_amount_to_float returns whichever oneof is set), so a
        # buy's remainder is converted back through its price.
        base = remaining if order_info['side'] == 'sell' else remaining / order_info['price']
        if base < 0 or base > amount * 1.0001:
            return None
        precision = int(self._market_value('base_precision', 6))
        return math.floor(min(base, amount) * 10 ** precision) / 10 ** precision

    def _load_market_minimums(self):
        """min_base_amount / min_quote_amount for this market.

        An order must clear BOTH: 0.000132 ETH passes min_base 0.0001 but at
        0.0316 it is 0.0000042 BTC, under min_quote 0.00001, and the node refuses it.
        """
        self._min_base = self._market_value('min_base_amount', 0.0)
        self._min_quote = self._market_value('min_quote_amount', 0.0)
        try:
            cache = getattr(self.target_exchange, '_market_cache', None) or {}
            entry = cache.get(self.trading_pair.symbol, {})
            client = getattr(self.target_exchange, 'client', None)
            if client and entry.get('base_currency') and entry.get('quote_currency'):
                mi = client.get_market_info(entry['base_currency'], entry['quote_currency'])
                if mi:
                    self._min_base = float(mi.min_base_amount.value or 0)
                    self._min_quote = float(mi.min_quote_amount.value or 0)
        except Exception as e:
            self.logger.warning(f"Could not load market minimums: {e}")
        if self._min_base or self._min_quote:
            self.logger.info(f"Market minimums: base {self._min_base}, quote {self._min_quote}")

    def _below_minimum(self, amount: float, price: float) -> bool:
        if not self._min_base and not self._min_quote:
            self._load_market_minimums()     # cache was empty when we first asked
        return (amount < max(self._min_base, 1e-12)
                or amount * price < self._min_quote)

    def _market_value(self, key: str, default: float) -> float:
        try:
            cache = getattr(self.target_exchange, '_market_cache', None) or {}
            value = cache.get(self.trading_pair.symbol, {}).get(key)
            return float(value) if value is not None else default
        except Exception:
            return default

    async def _refresh_external(self, force: bool = False):
        """Fetch the outside market's price for this pair. Never raises."""
        if not self.grid_config.external_guard:
            return
        now = time.time()
        if not force and now - self._last_ext_poll < self.grid_config.price_check_interval:
            return
        self._last_ext_poll = now
        try:
            if self.price_oracle is None:
                from lib.price_oracle import PriceOracle
                self.price_oracle = PriceOracle(self.logger)
            # the oracle uses blocking HTTP — keep it off the event loop
            loop = asyncio.get_running_loop()
            fetch = getattr(self.price_oracle, 'get_market_pair_price', None) \
                or self.price_oracle.get_pair_price
            price = await loop.run_in_executor(None, fetch, self.grid_config.pair)
            if price and price > 0:
                self._ext_price, self._ext_at = float(price), now
        except Exception as e:
            self.logger.warning(f"External price unavailable: {e}")

    def _external(self) -> Optional[float]:
        """The external price, or None when the guard is off or the price is stale.

        Fails open: with no trustworthy external price the grid behaves exactly as
        it did before the guard existed, rather than pulling its quotes.
        """
        if not self.grid_config.external_guard or not self._ext_price:
            return None
        if time.time() - self._ext_at > self.grid_config.external_max_age:
            if not self._guard_engaged:
                return None
            # Engaged and the feed went quiet: releasing would rebuild our sells at
            # the very prices the guard moved them away from. Hold the last price.
            if time.time() - self._ext_stale_warned > 600:
                self._ext_stale_warned = time.time()
                self.logger.warning(
                    f"🛡️ Outside price is {time.time() - self._ext_at:.0f}s old — holding the "
                    f"guard at {self._ext_price:.8f} until a fresh one arrives"
                )
        return self._ext_price

    def _refs(self, center: float):
        """(buy_ref, sell_ref, engaged) for a ladder centered on `center`.

        Continuous: buys always ladder from the lower of Hydra's mid and the outside
        price, sells from the higher. The old version switched between "center" and
        min/max at a threshold; Hydra's mid oscillating across it rebuilt the whole
        grid 28 times on 2026-09-21 alone. `engaged` now only drives logging and the
        hold-last-price behaviour, never a rebuild.
        """
        ext = self._external()
        if not ext or not center:
            return center, center, False
        gap = abs(ext / center - 1) * 100
        limit = self.grid_config.external_guard_pct * (0.6 if self._guard_engaged else 1.0)
        return min(center, ext), max(center, ext), gap > limit

    def _note_guard_state(self, engaged: bool, mid: Optional[float] = None):
        if engaged and not self._guard_engaged:
            self._guard_engagements += 1
            ext = self._external()
            mid = mid or self.center_price
            self.logger.warning(
                f"🛡️ External guard ENGAGED (#{self._guard_engagements}): outside market "
                f"{ext:.8f} vs Hydra mid {mid:.8f} "
                f"({(mid / ext - 1) * 100:+.2f}%) — this book is stale"
            )
        elif not engaged and self._guard_engaged:
            self.logger.info("🛡️ External guard released: Hydra's book is back in line")
        self._guard_engaged = engaged

    def _guard_price(self, side: str, price: float) -> float:
        """Hard clamp applied to every placement, flips and rebuilds included."""
        ext = self._external()
        if not ext:
            return price
        g = self.grid_config.external_clamp_pct / 100
        scale = 10 ** self._quote_precision()
        if side == 'sell':
            floor = math.ceil(ext * (1 + g) * scale - 1e-9) / scale
            return max(price, floor)
        cap = math.floor(ext * (1 - g) * scale + 1e-9) / scale
        return min(price, cap)

    async def _get_ticker(self, max_age: float = 2.0):
        """Best bid/ask, cached briefly so a 20-order batch is not 20 RPCs."""
        now = time.time()
        if self._ticker_cache and now - self._ticker_cache[1] <= max_age:
            return self._ticker_cache[0]
        try:
            ticker = await self.exchange.get_ticker(self.trading_pair)
        except Exception as e:
            self.logger.debug(f"Ticker unavailable: {e}")
            return None
        if ticker:
            self._ticker_cache = (ticker, now)
        return ticker

    def _would_cross(self, side: str, price: float, ticker) -> bool:
        """True if this order would take liquidity instead of resting.

        Crossing the book pays the 0.40% taker fee against a 0.10% maker rebate,
        a 0.5% swing that is larger than a round trip earns. Every taker fill in
        this bot's history came from placing into a market that had moved.
        """
        if ticker is None:
            return False
        bid, ask = getattr(ticker, 'bid', None), getattr(ticker, 'ask', None)
        if side == 'sell' and bid is not None and price <= bid:
            return True
        if side == 'buy' and ask is not None and price >= ask:
            return True
        return False

    async def _maybe_recenter(self):
        """Poll the mid and move the whole ladder when it has drifted."""
        if not self.grid_config.auto_adjust or not self.center_price:
            return
        now = time.time()
        if now - self._last_price_poll < self.grid_config.price_check_interval:
            return
        self._last_price_poll = now
        if now - self._last_recenter < self.grid_config.recenter_cooldown:
            return

        mid, _, _ = await self._market_mid()
        if mid is None:
            return
        buy_ref, sell_ref, engaged = self._refs(mid)
        self._note_guard_state(engaged, mid)          # log only; never a rebuild
        built_buy, built_sell = self._built_refs or (self.center_price, self.center_price)
        drift = max(abs(buy_ref / built_buy - 1), abs(sell_ref / built_sell - 1)) * 100
        if drift < self.grid_config.adjustment_threshold:
            return

        self.logger.info(
            f"Mid {mid:.8f}, outside market {self._external()} vs ladder built on "
            f"{built_buy:.8f}/{built_sell:.8f}: references drifted {drift:.2f}% "
            f"(threshold {self.grid_config.adjustment_threshold:.2f}%) — recentering"
        )
        self._last_recenter = now
        await self._adjust_grid_to_market(mid)

    def _tick(self) -> float:
        return 10 ** -int(self._market_value('quote_precision', 6))

    async def _competitor_touch(self):
        """Best bid/ask with our own resting volume removed.

        The book includes our own orders, so pegging to the raw touch would make
        the strategy outbid itself a tick at a time.
        """
        try:
            ob = await self.exchange.get_orderbook(self.trading_pair, 20)
        except Exception as e:
            self.logger.debug(f"Orderbook unavailable for pegging: {e}")
            return None, None
        if not ob:
            return None, None

        ours = {}
        for slot in self.slots.values():
            if slot['order_id']:
                key = (slot['side'], round(slot['price'], 10))
                ours[key] = ours.get(key, 0.0) + slot['amount']

        def first_other(levels, side):
            for price, volume in levels or []:
                mine = ours.get((side, round(price, 10)), 0.0)
                if volume - mine > 1e-9:      # somebody else is also at this price
                    return price
            return None

        return first_other(ob.bids, 'buy'), first_other(ob.asks, 'sell')

    async def _market_mid(self):
        """Mid of the *competitors'* book.

        get_ticker builds its book from all orders including ours, so a stale
        ladder on one side drags the mid toward itself: the grid then recenters
        halfway and half the new levels cannot be placed without crossing.
        """
        bid, ask = await self._competitor_touch()
        if bid is not None and ask is not None:
            return (bid + ask) / 2, bid, ask
        # One-sided: nobody else quotes the other side. Falling back to the raw
        # ticker would average their quote with OUR OWN — on 2026-09-19 that put
        # the "mid" at 0.03211 (their bid 0.03169, our ask 0.03254) and recentering
        # onto it lifted our bids 1.2% above the only other bidder for nothing.
        # The side that exists is the only outside information, so it is the mid.
        if bid is not None:
            return bid, bid, ask
        if ask is not None:
            return ask, bid, ask
        return self._external(), bid, ask

    async def _maintain_inner_quotes(self):
        """Keep the innermost order on each side just inside the touch."""
        if not (self.grid_config.peg_inner_quotes and self._active):
            return
        now = time.time()
        if now - min(self._last_peg.values()) < self.grid_config.peg_interval:
            return

        mid, bid, ask = await self._market_mid()
        ticker = await self._get_ticker()
        if mid is None or ticker is None or ticker.bid is None or ticker.ask is None:
            return
        tick = self._tick()
        gap = tick * max(1, self.grid_config.peg_min_ticks)

        live = [(sid, s) for sid, s in list(self.slots.items()) if s['order_id']]
        for side in ('buy', 'sell'):
            if now - self._last_peg[side] < self.grid_config.peg_interval:
                continue
            candidates = [(sid, s) for sid, s in live if s['side'] == side]
            if not candidates:
                continue
            # innermost = highest buy / lowest sell
            slot_id, slot = (max(candidates, key=lambda kv: kv[1]['price']) if side == 'buy'
                             else min(candidates, key=lambda kv: kv[1]['price']))
            touch = bid if side == 'buy' else ask
            if touch is None:
                continue

            # far side for the clamp: their quote, else the outside market, else ours
            ext = self._external()
            far = (ask if side == 'buy' else bid) or ext or \
                  (ticker.ask if side == 'buy' else ticker.bid)
            mid = (touch + far) / 2

            if side == 'buy':
                # the outside market caps it too: the touch may be stale
                desired = self._guard_price('buy', min(touch + tick, mid - tick))
                # never worse than the ladder, never through the far side
                if desired <= slot['base_price'] or desired >= ticker.ask:
                    continue
                better = desired - slot['price'] >= gap
            else:
                desired = self._guard_price('sell', max(touch - tick, mid + tick))
                if desired >= slot['base_price'] or desired <= ticker.bid:
                    continue
                better = slot['price'] - desired >= gap

            if not better:
                continue
            desired = self._round_to_precision(desired)
            if not self._is_price_within_limits(desired, side):
                continue

            order_id = slot['order_id']
            self.logger.info(
                f"Re-pegging inner {side} {slot['price']} -> {desired} "
                f"(touch {touch}, ladder {slot['base_price']})"
            )
            self._intentional_cancels.add(order_id)
            try:
                await self.exchange.cancel_order(order_id, self.trading_pair)
            except Exception as e:
                self._intentional_cancels.discard(order_id)
                self.logger.warning(f"Could not cancel {order_id[:8]} to re-peg: {e}")
                continue
            self.active_orders.pop(order_id, None)
            self._order_slot.pop(order_id, None)
            self._order_objs.pop(order_id, None)
            slot.update(order_id=None, price=desired, empty_since=None)
            self._last_peg[side] = now
            if not await self._place_grid_order(side, desired, slot['amount'], slot_id):
                slot['empty_since'] = time.time()   # rebuild loop retries it

    async def _enforce_outside_price(self):
        """Reprice any resting order that the outside market has moved past.

        The clamp used to apply only at placement. On 2026-09-21 bids placed while
        ETH/BTC was higher were still resting after it fell 0.3%, and were sold into
        at 11:03. Only violating orders move; the rest of the ladder is untouched.
        """
        if not self._external():
            return
        moved = []
        for slot_id, slot in list(self.slots.items()):
            order_id = slot['order_id']
            if not order_id:
                continue
            target = self._guard_price(slot['side'], slot['price'])
            if target == slot['price']:
                continue
            self._intentional_cancels.add(order_id)
            try:
                await self.exchange.cancel_order(order_id, self.trading_pair)
            except Exception as e:
                self._intentional_cancels.discard(order_id)
                self.logger.warning(f"Could not cancel {order_id[:8]} to reprice: {e}")
                continue
            self.active_orders.pop(order_id, None)
            self._order_slot.pop(order_id, None)
            self._order_objs.pop(order_id, None)
            moved.append(f"{slot['side']} {slot['price']}->{target}")
            slot.update(order_id=None, price=target, empty_since=None)
            if not await self._place_grid_order(slot['side'], target, slot['amount'], slot_id):
                slot['empty_since'] = time.time()   # rebuild loop retries it
        if moved:
            self.logger.info(
                f"🛡️ Outside market {self._external():.8f} moved past resting orders — "
                f"repriced {len(moved)}: {', '.join(moved)}"
            )

    async def _rebuild_loop(self):
        interval = max(1.0, float(self.grid_config.rebuild_check_interval))
        while self._active:
            try:
                await asyncio.sleep(interval)
                if self._active:
                    await self._refresh_external()
                if self._active:
                    await self._enforce_outside_price()
                if self._active:
                    await self._maybe_recenter()
                if self._active:
                    await self._maintain_inner_quotes()
                if self._active:
                    await self._rebuild_empty_slots()
            except asyncio.CancelledError:
                return
            except Exception as e:
                self.logger.error(f"Error rebuilding grid levels: {e}")

    async def _rebuild_empty_slots(self):
        now = time.time()
        due = [
            (slot_id, slot) for slot_id, slot in list(self.slots.items())
            if slot['order_id'] is None and slot['empty_since']
            and now - slot['empty_since'] >= self.grid_config.rebuild_delay
        ]
        if not due:
            return

        for slot_id, slot in due:
            if not self._active:
                return
            if self.slots.get(slot_id) is not slot or slot['order_id'] is not None:
                continue  # changed while we were awaiting

            side, price, amount = slot['side'], slot['price'], slot['amount']
            if self._would_cross(side, price, await self._get_ticker()):
                continue  # still through the market; retry next pass

            slot['rebuilds'] = [t for t in slot['rebuilds'] if now - t < 3600]
            if len(slot['rebuilds']) >= self.grid_config.max_rebuilds_per_hour:
                if not slot.get('rate_limited') and not slot.get('alarmed'):
                    n = len(slot['rebuilds'])
                    if slot.get('fail_streak', 0) >= n:
                        what = f"could not be placed ({n} failed attempts in the last hour)"
                    else:
                        what = f"was rebuilt {n}x in the last hour and keeps disappearing"
                    self.logger.error(
                        f"🛑 {side} {amount} @ {price} {what} — pausing rebuilds for this level "
                        f"(retrying hourly; logged once until it succeeds)"
                    )
                    slot['alarmed'] = True
                slot['rate_limited'] = True
                continue
            slot['rate_limited'] = False
            slot['rebuilds'].append(now)

            order = await self._place_grid_order(side, price, amount, slot_id)
            if order:
                slot['fail_streak'] = 0
                if slot.pop('alarmed', False):
                    self.logger.info(f"✅ {side} {amount} @ {price} placed again after failing")
                self.logger.info(f"🧱 Rebuilt {side} {amount} @ {price} ({order.id[:8]})")
            else:
                slot['fail_streak'] = slot.get('fail_streak', 0) + 1
                slot['empty_since'] = time.time()  # wait a full delay before retrying

    async def _handle_order_fill_confirmed(self, order_id: str, order: Any):
        """Handle confirmed order fill from DexEvent"""
        order_info = self.active_orders.get(order_id)
        if order_info is None:
            return
        await self._on_fill(order_id)
        self.grid_profit += self._calculate_trade_profit(
            order_info['price'],
            order_info['side'],
            order_info['amount']
        )
    
    def _calculate_trade_profit(self, price: float, side: str, amount: float) -> float:
        """Calculate estimated profit from a trade"""
        # This is a simplified calculation
        # In reality, profit comes from the spread between buy and sell
        return 0.0  # Will be calculated when matched trades complete