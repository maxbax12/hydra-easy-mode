#!/usr/bin/env python3
"""
Volume Maker Strategy
====================

Creates randomized buy/sell orders to generate trading volume for a specific pair.
Useful for market making and providing liquidity with realistic trading patterns.
"""

import asyncio
import random
import time
from typing import Dict, Any, Optional
from dataclasses import dataclass

from strategies.base.base_strategy import BaseStrategy, StrategyStatus
from connectors.base_exchange import TradingPair, OrderSide, OrderType


@dataclass
class VolumeMakerConfig:
    """Configuration for volume maker strategy"""
    pair: str                      # Trading pair (e.g., "BTC/USDC")
    base_amount: float             # Base order amount
    amount_randomization: float    # Percentage randomization (0.0-1.0, e.g. 0.2 = ±20%)
    time_interval: float          # Time between orders in seconds
    time_randomization: float     # Time interval randomization (0.0-1.0)
    price_spread: float           # Spread from market price (e.g., 0.01 = 1%)
    buy_sell_ratio: float         # Probability of buy orders (0.0-1.0, 0.5 = 50/50)
    max_active_orders: int        # Maximum number of active orders at once
    max_swap_failures: int = 1   # Maximum allowed swap failures before stopping (default: 10)
    burst_mode: bool = False      # Enable burst mode for maximum throughput (ignores time_interval)
    burst_size: int = 10          # Number of orders to fire in each burst (only used if burst_mode=True)
    enabled: bool = True


class VolumeMakerStrategy(BaseStrategy):
    """
    Volume maker strategy that creates randomized buy/sell orders
    to generate trading volume and provide market liquidity.
    """
    
    def __init__(self, name: str, config: Dict[str, Any]):
        from strategies.base.base_strategy import StrategyConfig
        
        # Create StrategyConfig object
        strategy_config = StrategyConfig(
            name=name,
            enabled=config.get('enabled', True),
            risk_limit=config.get('risk_limit', 1.0),
            max_daily_trades=config.get('max_daily_trades', 1000),
            params=config
        )
        
        # Initialize with empty exchanges dict (will be set later)
        super().__init__(name, strategy_config, {})
        
        # Parse configuration
        self.vm_config = VolumeMakerConfig(
            pair=config.get('pair', 'BTC/USDC'),
            base_amount=config.get('base_amount', 0.0001),
            amount_randomization=config.get('amount_randomization', 0.2),
            time_interval=config.get('time_interval', 30.0),
            time_randomization=config.get('time_randomization', 0.3),
            price_spread=config.get('price_spread', 0.005),
            buy_sell_ratio=config.get('buy_sell_ratio', 0.5),
            max_active_orders=config.get('max_active_orders', 10),
            max_swap_failures=config.get('max_swap_failures', 1),
            burst_mode=config.get('burst_mode', False),
            burst_size=config.get('burst_size', 10),
            enabled=config.get('enabled', True)
        )
        
        # Strategy state
        self.trading_pair: Optional[TradingPair] = None
        self.active_orders = set()
        self.last_market_price = 0.0
        self.orders_created = 0
        self.orders_filled = 0

        # Failure tracking
        self.order_failure_count = 0
        self.consecutive_failures = 0
        self.max_consecutive_failures = config.get('max_swap_failures', 1)

        # Task management
        self.volume_task: Optional[asyncio.Task] = None

        # Exchange reference (set by CLI)
        self.exchange = None
    
    async def initialize(self) -> bool:
        """Initialize the volume maker strategy"""
        try:
            if not self.vm_config.enabled:
                self.logger.info(f"📊 Volume maker strategy '{self.name}' is disabled")
                return True
                
            # Parse trading pair
            base, quote = self.vm_config.pair.split('/', 1)
            self.trading_pair = TradingPair(base=base, quote=quote, symbol=self.vm_config.pair)
            
            return True
        except Exception as e:
            self.logger.error(f"❌ Failed to initialize volume maker strategy: {e}")
            return False
    
    async def on_market_data(self, exchange_name: str, pair: str, data: Dict):
        """Handle market data updates (not used by volume maker)"""
        pass
    
    async def on_order_update(self, exchange_name: str, order: Any):
        """Handle order updates"""
        # This is handled by the order tracker callbacks
        pass
        
    def get_status(self) -> StrategyStatus:
        """Get current strategy status"""
        return StrategyStatus(
            name=self.name,
            running=self.running,
            active_positions=len(self.active_orders),
            total_trades=self.orders_filled,
            profit_loss=0.0,  # Volume maker doesn't track P&L
            last_update=time.time(),
            error_count=0,
            last_error=None
        )
    
    async def start(self):
        """Start the volume maker strategy"""
        if not self.vm_config.enabled:
            self.logger.info(f"📊 Volume maker strategy '{self.name}' is disabled")
            return
            
        if self.running:
            return
            
        self.running = True
        
        # Parse trading pair
        try:
            base, quote = self.vm_config.pair.split('/', 1)
            self.trading_pair = TradingPair(base=base, quote=quote, symbol=self.vm_config.pair)
        except ValueError:
            self.logger.error(f"❌ Invalid trading pair format: {self.vm_config.pair}")
            return
        
        # Ensure market is initialized on exchange before trading
        if self.exchange and hasattr(self.exchange, 'ensure_market_initialized'):
            initialized = await self.exchange.ensure_market_initialized(self.trading_pair)
            if not initialized:
                self.logger.error(f"❌ Failed to initialize market {self.vm_config.pair} on exchange")
                self.running = False
                return

        self.logger.info(f"🎯 Starting Volume Maker strategy for {self.vm_config.pair}")
        self.logger.info(f"   Base amount: {self.vm_config.base_amount}")
        self.logger.info(f"   Time interval: {self.vm_config.time_interval}s (±{self.vm_config.time_randomization*100}%)")
        self.logger.info(f"   Amount randomization: ±{self.vm_config.amount_randomization*100}%")
        self.logger.info(f"   Price spread: {self.vm_config.price_spread*100}%")
        self.logger.info(f"   Buy/Sell ratio: {self.vm_config.buy_sell_ratio*100}% buy orders")
        
        # Start volume making task
        self.logger.info("📦 Creating volume maker task...")
        self.volume_task = asyncio.create_task(self._volume_maker_loop())
        self.logger.info(f"📦 Volume maker task created: {self.volume_task}")
        # Give the task a chance to start
        await asyncio.sleep(0)
    
    async def stop(self):
        """Stop the volume maker strategy"""
        self.running = False
        
        if self.volume_task:
            self.volume_task.cancel()
            try:
                await self.volume_task
            except asyncio.CancelledError:
                pass
        
        self.logger.info(f"⏹️  Volume maker strategy '{self.name}' stopped")
        self.logger.info(f"   📊 Total orders created: {self.orders_created}")
        self.logger.info(f"   ✅ Total orders filled: {self.orders_filled}")
    
    async def _volume_maker_loop(self):
        """Main volume making loop"""
        self.logger.info(f"🚀 Volume maker loop started for {self.vm_config.pair}")
        self.logger.info(f"   Trading pair: {self.trading_pair}")
        self.logger.info(f"   Exchange set: {self.exchange is not None}")

        if self.vm_config.burst_mode:
            self.logger.info(f"⚡ BURST MODE ENABLED - Firing {self.vm_config.burst_size} orders per burst for maximum throughput")
        else:
            self.logger.info(f"📊 Standard mode - {self.vm_config.time_interval}s interval between orders")

        # Cache for orderbook prices (updated frequently to track grid changes)
        last_orderbook_fetch = 0
        orderbook_cache_duration = 5.0  # Refresh orderbook every 5 seconds

        order_count = 0  # Track orders for periodic logging

        while self.running:
            try:
                # Get orderbook prices only occasionally (not every iteration)
                current_time = time.time()
                if current_time - last_orderbook_fetch > orderbook_cache_duration:
                    best_bid, best_ask = await self._get_orderbook_prices()
                    if best_bid <= 0 or best_ask <= 0:
                        self.logger.warning(f"⚠️  Could not get orderbook prices (bid: {best_bid}, ask: {best_ask}), retrying...")
                        await asyncio.sleep(10)
                        continue

                    self.last_market_price = (best_bid + best_ask) / 2.0
                    last_orderbook_fetch = current_time
                    self.logger.info(f"📊 Orderbook updated: Bid {best_bid:.2f}, Ask {best_ask:.2f}, Mid {self.last_market_price:.2f}")

                if self.vm_config.burst_mode:
                    # BURST MODE: Fire multiple orders at once for maximum throughput
                    burst_tasks = []
                    for _ in range(self.vm_config.burst_size):
                        # Determine order side (buy or sell)
                        is_buy = random.random() < self.vm_config.buy_sell_ratio
                        side = OrderSide.BUY if is_buy else OrderSide.SELL

                        # Generate randomized amount
                        amount = self._randomize_amount(self.vm_config.base_amount)

                        # Create task (will execute concurrently)
                        task = asyncio.create_task(self._place_volume_order(side, amount))
                        burst_tasks.append(task)

                    order_count += self.vm_config.burst_size

                    # Log burst
                    self.logger.info(f"⚡ Fired burst of {self.vm_config.burst_size} orders (total: {order_count})")

                    # Small delay to prevent completely overwhelming the system
                    await asyncio.sleep(0.5)  # 200ms between bursts

                else:
                    # STANDARD MODE: Single order with time interval
                    # Check if we can place more orders
                    if len(self.active_orders) >= self.vm_config.max_active_orders:
                        self.logger.info(f"📊 Max active orders reached ({self.vm_config.max_active_orders}), waiting...")
                        await asyncio.sleep(5)  # Check again in 5 seconds
                        continue

                    # Determine order side (buy or sell)
                    is_buy = random.random() < self.vm_config.buy_sell_ratio
                    side = OrderSide.BUY if is_buy else OrderSide.SELL

                    # Generate randomized amount
                    amount = self._randomize_amount(self.vm_config.base_amount)

                    # Place market order asynchronously (fire and forget for speed)
                    asyncio.create_task(self._place_volume_order(side, amount))

                    order_count += 1

                    # Reduced logging - only log every 10 orders
                    if order_count % 10 == 0:
                        self.logger.info(f"📊 Placed {order_count} orders (total created: {self.orders_created}, filled: {self.orders_filled})")

                    # Wait for next order with randomized interval
                    wait_time = self._randomize_time(self.vm_config.time_interval)
                    await asyncio.sleep(wait_time)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"❌ Error in volume maker loop: {e}")
                await asyncio.sleep(10)  # Wait before retrying
    
    async def _get_market_price(self) -> float:
        """Get current market price for the trading pair"""
        try:
            if not self.exchange:
                self.logger.error(f"❌ No exchange set for volume maker")
                return 0.0
            
            self.logger.debug(f"Getting orderbook for {self.trading_pair.symbol}")
            # Get orderbook to determine market price
            orderbook = await self.exchange.get_orderbook(self.trading_pair)
            if not orderbook:
                self.logger.error(f"❌ No orderbook returned for {self.trading_pair.symbol}")
                return 0.0
            
            # OrderBook is an object with bids and asks attributes
            if not orderbook.bids or not orderbook.asks:
                self.logger.error(f"❌ Empty orderbook for {self.trading_pair.symbol}")
                return 0.0
            
            # Calculate mid price - bids/asks might be tuples or dicts
            if isinstance(orderbook.bids[0], tuple):
                best_bid = orderbook.bids[0][0] if orderbook.bids else 0  # price is first element
                best_ask = orderbook.asks[0][0] if orderbook.asks else 0
            elif isinstance(orderbook.bids[0], dict):
                best_bid = orderbook.bids[0]['price'] if orderbook.bids else 0
                best_ask = orderbook.asks[0]['price'] if orderbook.asks else 0
            else:
                best_bid = orderbook.bids[0].price if orderbook.bids else 0
                best_ask = orderbook.asks[0].price if orderbook.asks else 0
            
            self.logger.debug(f"Best bid: {best_bid}, Best ask: {best_ask}")
            
            if best_bid > 0 and best_ask > 0:
                return (best_bid + best_ask) / 2.0
            elif best_bid > 0:
                return best_bid
            elif best_ask > 0:
                return best_ask
            else:
                return 0.0
                
        except Exception as e:
            self.logger.error(f"❌ Error getting market price: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
            return 0.0
    
    def _randomize_amount(self, base_amount: float) -> float:
        """Generate randomized order amount"""
        if self.vm_config.amount_randomization <= 0:
            return base_amount
        
        # Generate random multiplier within the range
        randomization = self.vm_config.amount_randomization
        multiplier = 1.0 + random.uniform(-randomization, randomization)
        
        return base_amount * multiplier
    
    def _randomize_time(self, base_time: float) -> float:
        """Generate randomized time interval"""
        if self.vm_config.time_randomization <= 0:
            return base_time

        # Generate random multiplier within the range
        randomization = self.vm_config.time_randomization
        multiplier = 1.0 + random.uniform(-randomization, randomization)

        # Ensure minimum wait time of 0.1 seconds to prevent excessive spam
        return max(0.01, base_time * multiplier)
    
    async def _get_orderbook_prices(self) -> tuple[float, float]:
        """Get current best bid and ask prices from orderbook"""
        try:
            if not self.exchange:
                return 0.0, 0.0
            
            orderbook = await self.exchange.get_orderbook(self.trading_pair)
            if not orderbook or not orderbook.bids or not orderbook.asks:
                return 0.0, 0.0
            
            # Handle different orderbook formats
            if isinstance(orderbook.bids[0], tuple):
                best_bid = orderbook.bids[0][0] if orderbook.bids else 0
                best_ask = orderbook.asks[0][0] if orderbook.asks else 0
            elif isinstance(orderbook.bids[0], dict):
                best_bid = orderbook.bids[0]['price'] if orderbook.bids else 0
                best_ask = orderbook.asks[0]['price'] if orderbook.asks else 0
            else:
                best_bid = orderbook.bids[0].price if orderbook.bids else 0
                best_ask = orderbook.asks[0].price if orderbook.asks else 0
                
            return best_bid, best_ask
            
        except Exception as e:
            self.logger.error(f"❌ Error getting orderbook prices: {e}")
            return 0.0, 0.0
    
    def _generate_price(self, side: OrderSide, best_bid: float, best_ask: float) -> float:
        """Generate order price to instantly fill at orderbook prices"""
        if side == OrderSide.BUY:
            # Buy orders at current ask price (instant fill)
            return best_ask
        else:
            # Sell orders at current bid price (instant fill)  
            return best_bid
    
    async def _place_volume_order(self, side: OrderSide, amount: float):
        """Place a volume-making order"""
        try:
            if not self.exchange or not self.trading_pair:
                return

            # Place market order for instant fill
            # Pass cached price so place_order skips the orderbook fetch
            order = await self.exchange.place_order(
                pair=self.trading_pair,
                side=side,
                type=OrderType.MARKET,
                amount=amount,
                price=self.last_market_price if self.last_market_price > 0 else None
            )

            if order:
                self.consecutive_failures = 0
                self.orders_created += 1
                self.orders_filled += 1

                side_emoji = "🟢" if side == OrderSide.BUY else "🔴"
                self.logger.info(f"{side_emoji} Volume MARKET {side.value}: {amount:.6f} {self.trading_pair.base} (Order: {order.id[:8]}...)")

                asyncio.create_task(self._cleanup_order(order.id))
            else:
                # No liquidity or order failed — just skip and retry next cycle
                self.consecutive_failures += 1
                self.logger.warning(f"⚠️  No fill for {side.value} order, retrying... ({self.consecutive_failures} consecutive)")

        except Exception as e:
            self.consecutive_failures += 1
            self.logger.warning(f"⚠️  Order error: {e}, retrying... ({self.consecutive_failures} consecutive)")
    
    async def _cleanup_order(self, order_id: str):
        """Cancel unfilled remainder of a market order after swap completes."""
        try:
            await asyncio.sleep(1)
            result = await self.exchange.cancel_order(order_id, self.trading_pair)
            if result:
                self.logger.info(f"🧹 Cleaned up unfilled remainder: {order_id[:8]}...")
        except Exception:
            pass

    def on_order_filled(self, order_id: str):
        """Handle order fill notification"""
        if order_id in self.active_orders:
            self.active_orders.remove(order_id)
            self.orders_filled += 1
            self.logger.debug(f"✅ Volume order filled: {order_id[:8]}... (Active: {len(self.active_orders)})")
    
    def on_order_cancelled(self, order_id: str):
        """Handle order cancellation notification"""
        if order_id in self.active_orders:
            self.active_orders.remove(order_id)
            self.logger.debug(f"❌ Volume order cancelled: {order_id[:8]}... (Active: {len(self.active_orders)})")
    
    async def update_config(self, new_config: Dict[str, Any]):
        """Update strategy configuration"""
        # Update configuration
        old_enabled = self.vm_config.enabled
        
        for key, value in new_config.items():
            if hasattr(self.vm_config, key):
                setattr(self.vm_config, key, value)
        
        # Handle enable/disable
        if old_enabled and not self.vm_config.enabled:
            await self.stop()
        elif not old_enabled and self.vm_config.enabled and not self.running:
            await self.start()
        
        self.logger.info(f"📊 Volume maker configuration updated for '{self.name}'")