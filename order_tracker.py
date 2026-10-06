#!/usr/bin/env python3
"""
Order Tracking Manager for Trading Bot CLI
==========================================

Provides real-time order status tracking using Hydra's SubscribePrivateUpdates
and SubscribeMarketUpdates streams.
"""

import asyncio
import threading
import time
import queue
from datetime import datetime
from typing import Dict, Optional, List, Callable
from dataclasses import dataclass
from enum import Enum

import sys
from pathlib import Path

# Add lib directory to path for imports
lib_path = Path(__file__).parent / "lib"
sys.path.insert(0, str(lib_path))

from grpc_client import HydraGRPCClient
from connectors.base_exchange import Order, OrderStatus, OrderSide
from hydra_pb import orderbook_pb2


class OrderEventType(Enum):
    CREATED = "created"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELLED = "cancelled"
    FAILED = "failed"
    UPDATED = "updated"


@dataclass
class OrderEvent:
    """Order status change event"""
    order_id: str
    event_type: OrderEventType
    order: Optional[Order] = None
    filled_amount: float = 0.0
    remaining_amount: float = 0.0
    price: Optional[float] = None
    timestamp: float = 0.0
    message: Optional[str] = None


class OrderTracker:
    """Real-time order tracking service"""
    
    def __init__(self, client: HydraGRPCClient, logger=None, exchange=None):
        self.client = client
        self.logger = logger
        self.exchange = exchange  # Reference to exchange for currency mapping

        # Order tracking
        self.active_orders: Dict[str, Order] = {}
        self.order_history: List[OrderEvent] = []

        # Event callbacks
        self.event_callbacks: List[Callable[[OrderEvent], None]] = []
        self.shutdown_callback: Optional[Callable[[], None]] = None  # Called on critical errors
        self.resync_callback: Optional[Callable[[], None]] = None  # Called after stream reconnect
        self.stop_on_swap_failure: bool = True  # Whether swap failures should stop the bot (default: yes)
        self.max_swap_failures: int = 1  # Maximum allowed swap failures before stopping (default: 1)
        # Called with the order_id of any failed, errored or stuck swap. Lets a
        # strategy pause just its own market (set stop_on_swap_failure=False).
        self.swap_failure_callbacks: list = []
        self._swap_orders: dict = {}  # swap_id -> order_id, for the watchdog
        self.swap_failure_count: int = 0  # Current count of swap failures

        # Swap timeout tracking
        self.pending_swaps: Dict[str, float] = {}  # swap_id -> start_time
        self.swap_timeout: float = 90.0  # seconds before a swap is considered stuck

        # Metrics
        self.total_paid: float = 0.0
        self.total_received: float = 0.0
        self.swap_count: int = 0

        # Threading
        self.running = False
        self.dex_events_thread: Optional[threading.Thread] = None
        self.dex_events_processor_thread: Optional[threading.Thread] = None
        self.polling_thread: Optional[threading.Thread] = None
        self.swap_watchdog_thread: Optional[threading.Thread] = None
        self.market_updates_threads: Dict[str, threading.Thread] = {}

        # Queue for DexEvents processing
        self.dex_events_queue = queue.Queue()

        # Polling configuration
        self.polling_interval = 0.5  # seconds between polls (fast for grid replacement)

        # Market subscriptions (pair_symbol -> thread)
        self.subscribed_pairs: set = set()
    
    def add_event_callback(self, callback: Callable[[OrderEvent], None]):
        """Add callback function that will be called on order events"""
        self.event_callbacks.append(callback)
    
    def start(self):
        """Start the order tracking service"""
        if self.running:
            return

        self.running = True

        # Start dex events listener thread (connects to gRPC and enqueues updates)
        self.dex_events_thread = threading.Thread(
            target=self._dex_events_listener,
            daemon=True,
            name="OrderTracker-DexEvents-Listener"
        )
        self.dex_events_thread.start()

        # Start dex events processor thread (consumes from queue and handles updates)
        self.dex_events_processor_thread = threading.Thread(
            target=self._dex_events_processor,
            daemon=True,
            name="OrderTracker-DexEvents-Processor"
        )
        self.dex_events_processor_thread.start()

        # Start heartbeat thread to monitor DexEvents stream health
        self.heartbeat_thread = threading.Thread(
            target=self._dex_events_heartbeat,
            daemon=True,
            name="OrderTracker-DexEvents-Heartbeat"
        )
        self.heartbeat_thread.start()

        # Start swap watchdog thread to detect stuck swaps
        self.swap_watchdog_thread = threading.Thread(
            target=self._swap_watchdog,
            daemon=True,
            name="OrderTracker-SwapWatchdog"
        )
        self.swap_watchdog_thread.start()

        # Disable polling - DexEvents is more reliable and real-time
        # Polling via GetOwnOrders is slow and causes false positives
        # self.polling_thread = threading.Thread(
        #     target=self._poll_order_status,
        #     daemon=True,
        #     name="OrderTracker-Polling"
        # )
        # self.polling_thread.start()

        if self.logger:
            self.logger.info("📡 Order tracking service started (DexEvents only)")
    
    def stop(self):
        """Stop the order tracking service"""
        self.running = False
        
        # Stop all market update threads
        for thread in self.market_updates_threads.values():
            if thread.is_alive():
                # Note: gRPC streams will terminate when the main thread exits
                pass
        
        if self.logger:
            self.logger.info("📡 Order tracking service stopped")
    
    def track_order(self, order: Order):
        """Start tracking a new order"""
        self.active_orders[order.id] = order
        
        # Subscribe to market updates for this pair if not already subscribed
        pair_key = order.pair.symbol
        if pair_key not in self.subscribed_pairs:
            self._start_market_subscription(order.pair)
        
        # Fire order created event
        event = OrderEvent(
            order_id=order.id,
            event_type=OrderEventType.CREATED,
            order=order,
            timestamp=time.time(),
            message=f"Order created: {order.side.value} {order.amount} {order.pair.symbol} at {order.price}"
        )
        self._fire_event(event)
        
        if self.logger:
            self.logger.info(f"📋 Tracking order {order.id[:8]}...")
    
    def get_order_status(self, order_id: str) -> Optional[Order]:
        """Get current status of an order"""
        return self.active_orders.get(order_id)
    
    def get_active_orders(self) -> List[Order]:
        """Get all currently active orders"""
        return list(self.active_orders.values())
    
    def get_recent_events(self, limit: int = 10) -> List[OrderEvent]:
        """Get recent order events"""
        return self.order_history[-limit:] if self.order_history else []
    
    def _start_market_subscription(self, pair):
        """Start market updates subscription for a trading pair"""
        pair_key = pair.symbol
        
        if pair_key in self.subscribed_pairs:
            return  # Already subscribed
        
        self.subscribed_pairs.add(pair_key)
        
        # Start market events listener thread for this pair
        thread_name = f"OrderTracker-MarketEvents-{pair_key}"
        market_thread = threading.Thread(
            target=self._market_events_listener,
            args=(pair,),
            daemon=True,
            name=thread_name
        )
        
        self.market_updates_threads[pair_key] = market_thread
        market_thread.start()
        
        if self.logger:
            self.logger.debug(f"📈 Started market updates subscription for {pair_key}")
    
    def _market_events_listener(self, pair):
        """Listen to market events for a specific trading pair"""
        from connectors.hydra_exchange import HydraExchange
        
        try:
            # Get base and quote currencies from pair
            if hasattr(pair, 'base_currency') and hasattr(pair, 'quote_currency'):
                base_currency = pair.base_currency
                quote_currency = pair.quote_currency
            else:
                # If pair doesn't have currency objects, skip market subscription
                if self.logger:
                    self.logger.warning(f"Cannot subscribe to market events for {pair.symbol}: missing currency data")
                return
            
            if self.logger:
                self.logger.info(f"📡 Starting market events stream for {pair.symbol}")
            
            # Subscribe to market events using the gRPC client
            for market_event in self.client.subscribe_market_events(base_currency, quote_currency):
                if not self.running:
                    break
                
                try:
                    self._process_market_event(pair.symbol, market_event)
                except Exception as e:
                    if self.logger:
                        self.logger.error(f"Error processing market event for {pair.symbol}: {e}")
                    
        except Exception as e:
            if self.logger:
                self.logger.error(f"Error in market events listener for {pair.symbol}: {e}")
    
    def _process_market_event(self, pair_symbol: str, market_event):
        """Process a market event and notify orderbook manager"""
        try:
            # Check if this is an orderbook update
            if hasattr(market_event, 'orderbook_update') and market_event.HasField('orderbook_update'):
                orderbook_update = market_event.orderbook_update
                
                # Notify orderbook manager if available
                if hasattr(self, 'orderbook_manager') and self.orderbook_manager:
                    self.orderbook_manager.update_from_hydra_orderbook_update(
                        pair_symbol, orderbook_update
                    )
                
                if self.logger:
                    self.logger.debug(f"📊 Processed orderbook update for {pair_symbol}")
            
            # Process trade updates for fill price tracking
            elif hasattr(market_event, 'trade_update') and market_event.HasField('trade_update'):
                trade_update = market_event.trade_update
                
                if self.logger:
                    self.logger.debug(f"📈 Trade update for {pair_symbol}: {trade_update}")
            
            # Process other market events
            elif hasattr(market_event, 'daily_stats_update') and market_event.HasField('daily_stats_update'):
                if self.logger:
                    self.logger.debug(f"📊 Daily stats update for {pair_symbol}")
            
            elif hasattr(market_event, 'candlestick_update') and market_event.HasField('candlestick_update'):
                if self.logger:
                    self.logger.debug(f"🕯️ Candlestick update for {pair_symbol}")
                    
        except Exception as e:
            if self.logger:
                self.logger.error(f"Error processing market event: {e}")
    
    def set_orderbook_manager(self, orderbook_manager):
        """Set the orderbook manager for market event notifications"""
        self.orderbook_manager = orderbook_manager
        if self.logger:
            self.logger.info("📊 Orderbook manager connected to order tracker")
    
    def _poll_order_status(self):
        """Background thread for polling order status"""        
        while self.running:
            try:
                # Sleep first to avoid immediate polling
                time.sleep(self.polling_interval)
                
                if not self.running:
                    break
                
                # Check status of all active orders
                self._check_active_orders()
                
            except Exception as e:
                if self.logger:
                    self.logger.error(f"Error in order polling: {e}")
                time.sleep(self.polling_interval)
    
    def _check_active_orders(self):
        """Check status of all active orders using GetOwnOrders"""
        if not self.active_orders:
            return
        
        if self.logger:
            self.logger.debug(f"📊 Checking {len(self.active_orders)} active orders...")
        
        # Group orders by trading pair for efficient querying
        pairs_to_check = {}
        for order_id, order in self.active_orders.items():
            pair_key = order.pair.symbol
            if pair_key not in pairs_to_check:
                pairs_to_check[pair_key] = []
            pairs_to_check[pair_key].append((order_id, order))
        
        # Check each pair
        for pair_key, orders in pairs_to_check.items():
            try:
                self._check_orders_for_pair(orders)
            except Exception as e:
                if self.logger:
                    self.logger.error(f"Error checking orders for {pair_key}: {e}")
    
    def _check_orders_for_pair(self, orders):
        """Check orders for a specific trading pair"""
        if not orders:
            return
        
        # Get currencies for the first order (all should be same pair)
        sample_order = orders[0][1]
        currencies = self._get_currencies_for_order(sample_order)
        if not currencies:
            return
        
        base_currency, quote_currency = currencies
        
        # Get own orders from Hydra
        try:
            own_orders = self.client.get_own_orders(base_currency, quote_currency)
            
            # Check each of our tracked orders
            for order_id, tracked_order in orders:
                self._check_single_order(order_id, tracked_order, own_orders)
                
        except Exception as e:
            if self.logger:
                self.logger.error(f"Error getting own orders: {e}")
    
    def _get_currencies_for_order(self, order):
        """Get currencies for an order using exchange's currency mapping"""
        if self.exchange and hasattr(self.exchange, '_get_currencies_for_pair'):
            try:
                return self.exchange._get_currencies_for_pair(order.pair)
            except Exception as e:
                if self.logger:
                    self.logger.error(f"Error getting currencies for {order.pair}: {e}")
        return None
    
    def _check_single_order(self, order_id, tracked_order, own_orders):
        """Check a single order against the GetOwnOrders response"""
        if order_id not in own_orders:
            # Order not found in own orders - this could mean:
            # 1. Order was filled/completed
            # 2. Order was cancelled
            # 3. Order failed
            # 4. API error/connection issue
            # 5. Order was just placed and hasn't propagated to API yet

            # Add grace period - don't assume filled too soon after placement
            order_age = time.time() - tracked_order.timestamp
            if order_age < 1.0:
                # Too soon, give it time to propagate
                return

            if self.logger:
                self.logger.info(f"🔍 Order {order_id[:8]}... not found in GetOwnOrders (age: {order_age:.1f}s) - assuming FILLED!")

            # Assume it's filled if not found in active orders
            event = OrderEvent(
                order_id=order_id,
                event_type=OrderEventType.FILLED,
                order=tracked_order,
                filled_amount=tracked_order.amount,
                remaining_amount=0.0,
                timestamp=time.time(),
                message=f"✅ Order filled (not found in GetOwnOrders): {tracked_order.amount} {tracked_order.pair.base}"
            )

            if self.logger:
                self.logger.info(f"🎉 FILL DETECTED: Order {order_id[:8]}... not in GetOwnOrders - assuming filled!")

            tracked_order.status = OrderStatus.FILLED
            tracked_order.filled = tracked_order.amount
            tracked_order.remaining = 0.0
            
            self.active_orders.pop(order_id, None)
            self._fire_event(event)
            return
            
        else:
            # Order still exists - check remaining amount
            hydra_order = own_orders[order_id]
            
            # Extract remaining amount based on order structure
            remaining = self._extract_remaining_amount(hydra_order)
            
            if remaining == 0.0:
                # Order is completed
                
                event = OrderEvent(
                    order_id=order_id,
                    event_type=OrderEventType.FILLED,
                    order=tracked_order,
                    filled_amount=tracked_order.amount,
                    remaining_amount=0.0,
                    timestamp=time.time(),
                    message=f"✅ Order completed: {tracked_order.amount} {tracked_order.pair.base}"
                )
                
                if self.logger:
                    self.logger.info(f"🎉 FILL DETECTED: Order {order_id[:8]}... filled completely!")
                
                tracked_order.status = OrderStatus.FILLED
                tracked_order.filled = tracked_order.amount
                tracked_order.remaining = 0.0
                
                self.active_orders.pop(order_id, None)
                self._fire_event(event)
                
            elif remaining != tracked_order.remaining and remaining < tracked_order.amount:
                # Partial fill
                filled_amount = tracked_order.amount - remaining
                
                event = OrderEvent(
                    order_id=order_id,
                    event_type=OrderEventType.PARTIALLY_FILLED,
                    order=tracked_order,
                    filled_amount=filled_amount,
                    remaining_amount=remaining,
                    timestamp=time.time(),
                    message=f"📊 Partial fill: {filled_amount}/{tracked_order.amount} {tracked_order.pair.base}"
                )
                
                tracked_order.filled = filled_amount
                tracked_order.remaining = remaining
                tracked_order.status = OrderStatus.OPEN
                
                self._fire_event(event)
    
    def _extract_remaining_amount(self, hydra_order):
        """Extract remaining amount from Hydra order object

        Supports the new Hydra orderbook structure with Limit and Market orders.
        Liquidity orders are deprecated (Hydra moved to traditional CEX model).
        """
        try:
            # Check for different possible order types and structures
            if hasattr(hydra_order, 'pair_order') and hydra_order.pair_order:
                pair_order = hydra_order.pair_order

                # Check if it's a limit order (NEW in latest Hydra)
                if hasattr(pair_order, 'limit_order') and pair_order.limit_order:
                    limit_order = pair_order.limit_order

                    if self.logger:
                        self.logger.debug(f"Processing limit order with remaining_amount")

                    # Limit orders have remaining_amount field (OrderAmount oneof)
                    return self._order_amount_to_float(
                        getattr(limit_order, 'remaining_amount', None))

                # Check if it's a market order
                elif hasattr(pair_order, 'market_order') and pair_order.market_order:
                    market_order = pair_order.market_order
                    return self._order_amount_to_float(
                        getattr(market_order, 'remaining_amount', None))

                # Legacy: Check if it's a liquidity order (deprecated, but handle for backwards compat)
                elif hasattr(pair_order, 'liquidity_order') and pair_order.liquidity_order:
                    if self.logger:
                        self.logger.warning(f"⚠️  Liquidity order detected - these are deprecated in latest Hydra!")

                    # Liquidity orders no longer used, but if we encounter one, mark as complete
                    # to avoid blocking the bot
                    return 0.0

            # If we couldn't find remaining amount, assume order is complete
            return 0.0

        except Exception as e:
            if self.logger:
                self.logger.error(f"Error extracting remaining amount: {e}")
            return 0.0
    
    def _order_amount_to_float(self, order_amount) -> float:
        """Extract a float from an OrderAmount oneof (base | quote).

        Uses WhichOneof — for protobuf oneofs, `hasattr(x, 'base')` is always
        True and `x.base` is always a (possibly-empty) message, so the old
        truthiness checks silently failed for BUY orders (quote-denominated).
        Returns the amount in whatever unit it's set; callers only need to know
        whether it's > 0 (a real fill is confirmed via order_completed).
        """
        if order_amount is None:
            return 0.0
        try:
            which = order_amount.WhichOneof('amount') if hasattr(order_amount, 'WhichOneof') else None
            if which == 'base':
                return self._decimal_to_float(order_amount.base.amount)
            if which == 'quote':
                return self._decimal_to_float(order_amount.quote.amount)
        except Exception as e:
            if self.logger:
                self.logger.error(f"Error extracting order amount: {e}")
        return 0.0

    def _decimal_to_float(self, decimal_value):
        """Convert protobuf DecimalString to float"""
        try:
            # Use the decimal conversion utility from utils
            from lib.utils import decimal_to_float
            return decimal_to_float(decimal_value)
        except Exception as e:
            if self.logger:
                self.logger.error(f"Error converting decimal: {e}")
            return 0.0
    
    def _dex_events_listener(self):
        """Background thread that listens to gRPC DexEvents stream and enqueues them.

        Reconnects on stream end / error with exponential backoff. After each
        reconnect, fires the resync_callback so consumers can reconcile state.
        """
        if self.logger:
            self.logger.info("📡 Starting DexEvents listener thread (with auto-reconnect)...")

        backoff = 1.0
        max_backoff = 30.0
        while self.running:
            try:
                got_any = False
                for update in self.client.subscribe_to_dex_events():
                    if not self.running:
                        return
                    got_any = True
                    backoff = 1.0  # reset on successful event
                    self.dex_events_queue.put(update)

                # Stream ended (server closed it). Treat as disconnect.
                if self.logger:
                    self.logger.warning(f"📡 DexEvents stream ended (got_any={got_any}); reconnecting in {backoff:.1f}s")
            except Exception as e:
                if self.logger:
                    self.logger.error(f"📡 DexEvents stream error: {e}; reconnecting in {backoff:.1f}s")

            if not self.running:
                return

            time.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)

            # Notify consumers to resync state after reconnect (we may have missed events)
            cb = getattr(self, 'resync_callback', None)
            if cb:
                try:
                    cb()
                except Exception as e:
                    if self.logger:
                        self.logger.error(f"Resync callback failed: {e}")

        if self.logger:
            self.logger.info("📡 DexEvents listener thread exiting")
    
    def _dex_events_processor(self):
        """Background thread that processes queued DexEvents"""
        if self.logger:
            self.logger.info("📡 Starting DexEvents processor thread...")
            
        processed_count = 0
        while self.running:
            try:
                # Get update from queue with timeout
                dex_event = self.dex_events_queue.get(timeout=1.0)
                
                processed_count += 1
                try:
                    self._handle_private_update(dex_event)
                except Exception as e:
                    if self.logger:
                        self.logger.error(f"Error processing dex event: {e}")
                
                # Mark task as done
                self.dex_events_queue.task_done()
                
            except queue.Empty:
                # Timeout waiting for updates - this is normal
                continue
            except Exception as e:
                if self.logger:
                    self.logger.error(f"Error in DexEvents processor: {e}")
                    
        if self.logger:
            self.logger.info(f"📡 DexEvents processor stopped after processing {processed_count} updates")
    
    def _dex_events_heartbeat(self):
        """Monitor DexEvents stream health"""
        if self.logger:
            self.logger.debug("💓 Starting DexEvents heartbeat monitor...")

        last_report = time.time()
        last_metrics_log = time.time()
        while self.running:
            try:
                time.sleep(30)  # Check every 30 seconds

                current_time = time.time()
                if current_time - last_report >= 30:
                    queue_size = self.dex_events_queue.qsize()
                    # Only log if there are events in the queue (activity)
                    if self.logger and queue_size > 0:
                        self.logger.debug(f"💓 DexEvents heartbeat: queue size = {queue_size}")
                    last_report = current_time

                if self.logger and current_time - last_metrics_log >= 60:
                    self.logger.info(
                        f"📊 Metrics — swaps: {self.swap_count}, "
                        f"total paid: {self.total_paid:.6g}, "
                        f"total received: {self.total_received:.6g}"
                    )
                    last_metrics_log = current_time

            except Exception as e:
                if self.logger:
                    self.logger.error(f"Error in DexEvents heartbeat: {e}")

        if self.logger:
            self.logger.debug("💓 DexEvents heartbeat monitor stopped")
    
    def _swap_watchdog(self):
        """Monitor pending swaps and stop the bot if any swap times out."""
        if self.logger:
            self.logger.info(f"🐕 Swap watchdog started (timeout={self.swap_timeout}s)")

        while self.running:
            try:
                time.sleep(5)  # Check every 5 seconds
                if not self.running:
                    break

                now = time.time()
                for swap_id, start_time in list(self.pending_swaps.items()):
                    elapsed = now - start_time
                    if elapsed > self.swap_timeout:
                        self.pending_swaps.pop(swap_id, None)
                        order_id = self._swap_orders.pop(swap_id, None)
                        if not self.stop_on_swap_failure:
                            # A slow swap is usually the counterparty (or an on-chain channel
                            # update) — not a reason to pull a market. Only a failure the node
                            # reports (SWAP_FAILED / error) pauses it; the HTLC resolves or
                            # refunds on its own.
                            self.logger.warning(f"⏳ swap {swap_id} still pending after {elapsed:.0f}s "
                                                f"(order {str(order_id)[:8]}) — waiting for the node, market keeps quoting")
                            continue
                        self.logger.error(f"🛑 SWAP TIMEOUT: {swap_id} has been pending for {elapsed:.0f}s (limit: {self.swap_timeout}s)")
                        self.logger.error(f"🛑 STOPPING BOT — stuck swap detected")
                        if self.shutdown_callback:
                            self.shutdown_callback()
                        else:
                            from lib.crash_notifier import notify_and_exit
                            notify_and_exit(f"Swap {swap_id} stuck for {elapsed:.0f}s")
                        return

            except Exception as e:
                if self.logger:
                    self.logger.error(f"Error in swap watchdog: {e}")

        if self.logger:
            self.logger.info("🐕 Swap watchdog stopped")

    def _notify_swap_failure(self, order_id, error=None):
        """Tell the strategies; `error` is the hub's failure text (who was at fault)."""
        for cb in list(self.swap_failure_callbacks):
            try:
                try:
                    cb(order_id, error)
                except TypeError:
                    cb(order_id)                      # an older one-argument callback
            except Exception as e:
                if self.logger:
                    self.logger.error(f"swap failure callback raised: {e}")

    def _handle_private_update(self, dex_event):
        """Handle incoming DexEvent message"""
        if self.logger:
            self.logger.debug(f"📨 DEX EVENT RECEIVED: {type(dex_event)}")  # Reduced to debug

        # Check what type of update this is using protobuf's WhichOneof
        update_type = dex_event.WhichOneof('update')
        if self.logger:
            self.logger.debug(f"🔍 DEX EVENT UPDATE TYPE: {update_type}")  # Reduced to debug

        # Check different types of updates in the DexEvent
        if update_type == 'order_update':
            self._handle_order_update(dex_event.order_update)
        elif update_type == 'balance_update':
            if self.logger:
                self.logger.debug(f"💳 BALANCE UPDATE received")  # Reduced logging
        elif update_type == 'order_matched':
            if self.logger:
                self.logger.debug(f"🎯 ORDER MATCHED: {dex_event.order_matched.own_order_id}")  # Reduced logging
        elif update_type == 'market_trade_update':
            if self.logger:
                self.logger.debug(f"💰 MARKET TRADE UPDATE received")  # Reduced logging
        elif update_type == 'swap_update':
            swap_update = dex_event.swap_update
            if self.logger:
                status_name = "UNKNOWN"
                if swap_update.progress:
                    status_value = swap_update.progress.status
                    status_names = {
                        0: "UNSPECIFIED",
                        1: "ORDER_MATCHED",
                        2: "RECEIVING_INVOICE_CREATED",
                        3: "PAYING_INVOICE_RECEIVED",
                        4: "PAYMENT_SENT",
                        5: "PAYMENT_RECEIVED",
                        6: "PAYMENT_CLAIMED",
                        7: "PAYMENT_CLAIMED_BY_COUNTERPARTY",
                        8: "SWAP_COMPLETED",
                        9: "SWAP_FAILED"
                    }
                    status_name = status_names.get(status_value, f"UNKNOWN({status_value})")
                    self.logger.info(f"🔄 SWAP UPDATE: {swap_update.order_id} {swap_update.swap_id} - Status: {status_name}")
                else:
                    self.logger.info(f"🔄 SWAP UPDATE: {swap_update}")

            # Track pending swaps for timeout detection
            swap_id = swap_update.swap_id
            if swap_update.progress:
                status = swap_update.progress.status
                # Track new swap on ORDER_MATCHED
                if status == orderbook_pb2.SWAP_STATUS_ORDER_MATCHED:
                    self.pending_swaps[swap_id] = time.time()
                    self._swap_orders[swap_id] = swap_update.order_id
                # Remove on completion or failure
                elif status in (orderbook_pb2.SWAP_STATUS_SWAP_COMPLETED,
                                orderbook_pb2.SWAP_STATUS_SWAP_FAILED):
                    self.pending_swaps.pop(swap_id, None)
                    self._swap_orders.pop(swap_id, None)

            # Check if swap failed or has an error
            if swap_update.progress:
                # Check for explicit SWAP_FAILED status
                if swap_update.progress.status == orderbook_pb2.SWAP_STATUS_SWAP_FAILED:
                    error_msg = swap_update.progress.error if swap_update.progress.HasField('error') else "Unknown error"
                    self.logger.error(f"🛑 SWAP FAILED for order {swap_update.order_id}")
                    self.logger.error(f"🛑 Error: {error_msg}")
                    self._notify_swap_failure(swap_update.order_id, error_msg)

                    if self.stop_on_swap_failure:
                        # Increment failure counter
                        self.swap_failure_count += 1
                        self.logger.error(f"🛑 Swap failure count: {self.swap_failure_count}/{self.max_swap_failures}")

                        if self.swap_failure_count >= self.max_swap_failures:
                            self.logger.error(f"🛑 STOPPING BOT - Maximum swap failures ({self.max_swap_failures}) reached")
                            # Trigger shutdown callback if registered, otherwise force exit
                            if self.shutdown_callback:
                                self.shutdown_callback()
                            else:
                                from lib.crash_notifier import notify_and_exit
                                notify_and_exit(f"Swap failed for order {swap_update.order_id}: {error_msg}")
                        else:
                            self.logger.warning(f"⚠️  Continuing despite swap failure ({self.max_swap_failures - self.swap_failure_count} failures remaining)")
                    else:
                        self.logger.warning(f"⚠️  Swap failure detected but continuing (stop_on_swap_failure=False)")

                # Also check if there's an error field set (even without FAILED status)
                elif swap_update.progress.HasField('error') and swap_update.progress.error:
                    self.logger.error(f"🛑 SWAP ERROR for order {swap_update.order_id}")
                    self.logger.error(f"🛑 Error: {swap_update.progress.error}")
                    self._notify_swap_failure(swap_update.order_id, swap_update.progress.error)

                    if self.stop_on_swap_failure:
                        # Increment failure counter
                        self.swap_failure_count += 1
                        self.logger.error(f"🛑 Swap failure count: {self.swap_failure_count}/{self.max_swap_failures}")

                        if self.swap_failure_count >= self.max_swap_failures:
                            self.logger.error(f"🛑 STOPPING BOT - Maximum swap failures ({self.max_swap_failures}) reached")
                            # Trigger shutdown callback if registered, otherwise force exit
                            if self.shutdown_callback:
                                self.shutdown_callback()
                            else:
                                from lib.crash_notifier import notify_and_exit
                                notify_and_exit(f"Swap error for order {swap_update.order_id}: {swap_update.progress.error}")
                        else:
                            self.logger.warning(f"⚠️  Continuing despite swap error ({self.max_swap_failures - self.swap_failure_count} failures remaining)")
                    else:
                        self.logger.warning(f"⚠️  Swap error detected but continuing (stop_on_swap_failure=False)")

            # Check if this swap completed
            if swap_update.progress and swap_update.progress.status == orderbook_pb2.SWAP_STATUS_SWAP_COMPLETED:
                order_id = swap_update.order_id
                progress = swap_update.progress
                try:
                    self.total_paid += float(progress.paying_amount.value) if progress.paying_amount.value else 0.0
                    self.total_received += float(progress.receiving_amount.value) if progress.receiving_amount.value else 0.0
                    self.swap_count += 1
                except Exception:
                    pass
                if self.logger:
                    self.logger.info(
                        f"✅ SWAP COMPLETED for order {order_id} — "
                        f"paid: {progress.paying_amount.value}, received: {progress.receiving_amount.value} | "
                        f"totals: paid={self.total_paid:.6g}, received={self.total_received:.6g}, swaps={self.swap_count}"
                    )

                # NOTE: Do NOT trigger FILLED event here!
                # One order can have MULTIPLE swaps (matched against multiple counterparties).
                # We should only trigger FILLED when we receive the final 'order_completed' event,
                # which indicates ALL swaps for this order have completed.
                #
                # Triggering FILLED here would cause:
                # 1. Multiple FILLED events for partial fills (one per swap)
                # 2. Grid strategy placing replacement orders too early
                # 3. "Order not in active_orders" warnings for subsequent swaps
                #
                # The order_completed event (handled in _handle_order_completed) is the
                # authoritative signal that an order is fully filled.
        elif update_type == 'swap_trade_update':
            if self.logger:
                self.logger.info(f"🔄 SWAP TRADE UPDATE!")
        elif update_type == 'is_synced':
            if self.logger:
                self.logger.info(f"🔄 DEX SYNCED: {dex_event.is_synced}")
        else:
            if self.logger:
                self.logger.info(f"❓ UNKNOWN DEX EVENT TYPE: {update_type}")
                self.logger.info(f"   Full event: {dex_event}")
    
    def _handle_order_update(self, order_update):
        """Handle OrderUpdate from DexEvent"""
        # Debug: Let's see what's in the order_update
        if self.logger:
            self.logger.info(f"🔍 ORDER UPDATE TYPE: {order_update.WhichOneof('update')}")
            
        # Extract order ID from different update types
        order_id = None
        update_type = order_update.WhichOneof('update')
        
        if update_type == 'order_created':
            order_created = order_update.order_created
            order_id = order_created.order_id
            if self.logger:
                self.logger.info(f"📝 ORDER CREATED: {order_id}")
                self.logger.debug(f"   Order details: {order_created.order}")
                
        elif update_type == 'order_updated':
            order_updated = order_update.order_updated
            order_id = order_updated.order_id
            if self.logger:
                self.logger.info(f"📝 ORDER UPDATED: {order_id}")
                self.logger.debug(f"   Order details: {order_updated.order}")

            # Update remaining amount from the order data
            if order_id in self.active_orders:
                tracked = self.active_orders[order_id]
                remaining = self._extract_remaining_amount(order_updated.order)
                old_remaining = tracked.remaining
                if remaining is not None and remaining != old_remaining:
                    filled = tracked.amount - remaining
                    tracked.filled = filled
                    tracked.remaining = remaining
                    tracked.status = OrderStatus.OPEN if remaining > 0 else OrderStatus.FILLED
                    if self.logger:
                        self.logger.info(f"📊 Order {order_id[:8]}... updated: filled={filled}, remaining={remaining}")
                    if remaining > 0:
                        event = OrderEvent(
                            order_id=order_id,
                            event_type=OrderEventType.PARTIALLY_FILLED,
                            order=tracked,
                            filled_amount=filled,
                            remaining_amount=remaining,
                            timestamp=time.time(),
                            message=f"Partial fill: {filled}/{tracked.amount}"
                        )
                        self._fire_event(event)
                
        elif update_type == 'order_completed':
            order_completed = order_update.order_completed
            order_id = order_completed.order_id
            if self.logger:
                self.logger.info(f"✅ ORDER COMPLETED EVENT for: {order_id}")
                # Debug: print the full order_completed object
                self.logger.debug(f"   Full order_completed data: {order_completed}")
            if order_id in self.active_orders:
                order = self.active_orders[order_id]
                self._handle_order_completed(order, order_completed)
            elif self.logger:
                self.logger.info(f"✅ ORDER COMPLETED (not tracked): {order_id}")
                
        elif update_type == 'order_canceled':
            order_canceled = order_update.order_canceled
            order_id = order_canceled.order_id
            if order_id in self.active_orders:
                order = self.active_orders[order_id]
                # Mostly our own requotes/shutdown; strategies log anything unexpected themselves
                if self.logger:
                    self.logger.info(f"❌ ORDER CANCELLED: {order_id}")
                self._handle_order_cancelled(order, order_canceled)
            elif self.logger:
                self.logger.info(f"❌ ORDER CANCELLED (not tracked): {order_id}")
        else:
            if self.logger:
                self.logger.info(f"❓ UNKNOWN ORDER UPDATE TYPE: {update_type}")
                self.logger.info(f"   Full update: {order_update}")
    
    def _handle_order_completed(self, order: Order, completed_update):
        """Handle order completion.

        Uses the filled/remaining amounts tracked via order_updated events
        rather than assuming the full original amount was filled.
        """
        filled_amount = order.filled if order.filled > 0 else order.amount
        order.status = OrderStatus.FILLED
        order.filled = filled_amount
        order.remaining = 0.0

        event = OrderEvent(
            order_id=order.id,
            event_type=OrderEventType.FILLED,
            order=order,
            filled_amount=filled_amount,
            remaining_amount=0.0,
            timestamp=time.time(),
            message=f"✅ Order completed: filled {filled_amount}/{order.amount} {order.pair.base} at {order.price}"
        )

        # Remove from active orders
        self.active_orders.pop(order.id, None)

        self._fire_event(event)
    
    def _handle_order_partially_filled(self, order: Order, partial_update):
        """Handle partial order fill"""
        # Extract fill information (implementation depends on protobuf structure)
        filled_amount = getattr(partial_update, 'filled_amount', 0.0)
        
        # Update order
        order.filled = filled_amount
        order.remaining = order.amount - filled_amount
        order.status = OrderStatus.OPEN
        
        event = OrderEvent(
            order_id=order.id,
            event_type=OrderEventType.PARTIALLY_FILLED,
            order=order,
            filled_amount=filled_amount,
            remaining_amount=order.remaining,
            timestamp=time.time(),
            message=f"📊 Partial fill: {filled_amount}/{order.amount} {order.pair.base}"
        )
        
        self._fire_event(event)
    
    def _handle_order_cancelled(self, order: Order, cancel_update):
        """Handle order cancellation"""
        order.status = OrderStatus.CANCELED
        
        event = OrderEvent(
            order_id=order.id,
            event_type=OrderEventType.CANCELLED,
            order=order,
            timestamp=time.time(),
            message=f"❌ Order cancelled: {order.pair.symbol}"
        )
        
        # Remove from active orders
        self.active_orders.pop(order.id, None)
        
        self._fire_event(event)
    
    def _handle_order_failed(self, order: Order, failed_update):
        """Handle order failure"""
        order.status = OrderStatus.FAILED
        
        # Extract failure reason if available
        reason = getattr(failed_update, 'reason', 'Unknown error')
        
        event = OrderEvent(
            order_id=order.id,
            event_type=OrderEventType.FAILED,
            order=order,
            timestamp=time.time(),
            message=f"❌ Order failed: {reason}"
        )
        
        # Remove from active orders
        self.active_orders.pop(order.id, None)
        
        self._fire_event(event)
    
    def _fire_event(self, event: OrderEvent):
        """Fire event to all registered callbacks"""
        # Add to history
        self.order_history.append(event)
        
        # Keep history size manageable
        if len(self.order_history) > 1000:
            self.order_history = self.order_history[-500:]
        
        # Call all callbacks
        for callback in self.event_callbacks:
            try:
                callback(event)
            except Exception as e:
                if self.logger:
                    self.logger.error(f"Error in order event callback: {e}")


def format_order_event(event: OrderEvent) -> str:
    """Format order event for display"""
    timestamp = datetime.fromtimestamp(event.timestamp).strftime("%H:%M:%S")
    order_id_short = event.order_id[:8] if event.order_id else "unknown"
    
    return f"[{timestamp}] {order_id_short}: {event.message}"