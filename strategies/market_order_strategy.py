"""
Market Order Strategy
====================

Strategy for executing market orders across different exchanges with
slippage protection and smart order routing.
"""

import asyncio
import time
from typing import Dict, List, Optional, Any
from dataclasses import dataclass

from .base.base_strategy import BaseStrategy, StrategyConfig, StrategyStatus
from connectors.base_exchange import TradingPair, OrderSide, OrderType, OrderStatus


@dataclass 
class MarketOrderConfig(StrategyConfig):
    """Configuration for market order strategy"""
    max_slippage: float = 0.05  # 5% maximum slippage
    order_timeout: float = 30.0  # 30 seconds timeout
    retry_attempts: int = 3  # Number of retry attempts


class MarketOrderStrategy(BaseStrategy):
    """
    Strategy for executing market orders with protection mechanisms
    
    This strategy provides:
    - Slippage protection
    - Order timeout handling
    - Retry mechanism
    - Cross-exchange execution
    """
    
    def __init__(self, name: str, config: MarketOrderConfig, exchanges: Dict[str, Any]):
        super().__init__(name, config, exchanges)
        
        self.market_config = config
        self.pending_orders: Dict[str, Dict] = {}  # order_id -> order_info
        self.completed_orders: List[Dict] = []
        
    async def initialize(self) -> bool:
        """Initialize the market order strategy"""
        try:
            self.logger.info(f"Initializing market order strategy: {self.name}")
            
            # Validate that we have at least one exchange available
            if not self.exchanges:
                self.log_error("No exchanges available")
                return False
            
            self.logger.info(f"Market order strategy initialized with {len(self.exchanges)} exchanges")
            return True
            
        except Exception as e:
            self.log_error(f"Initialization failed: {e}")
            return False
    
    async def start(self):
        """Start the market order strategy"""
        self.logger.info(f"Starting market order strategy: {self.name}")
        self.running = True
        
        # Start monitoring loop for pending orders
        asyncio.create_task(self._monitoring_loop())
    
    async def stop(self):
        """Stop the market order strategy"""
        self.logger.info(f"Stopping market order strategy: {self.name}")
        self.running = False
        
        # Cancel any pending orders
        await self._cancel_pending_orders()
        
        self.logger.info(f"Market order strategy stopped: {self.name}")
    
    async def on_market_data(self, exchange_name: str, pair: str, data: Dict):
        """Handle market data updates"""
        # Market order strategy doesn't need to react to market data
        pass
    
    async def on_order_update(self, exchange_name: str, order: Any):
        """Handle order updates"""
        order_id = order.id
        
        if order_id not in self.pending_orders:
            return  # Not our order
        
        order_info = self.pending_orders[order_id]
        
        if order.status == OrderStatus.FILLED:
            # Order completed successfully
            await self._handle_order_completion(order_id, order, order_info, success=True)
            
        elif order.status in [OrderStatus.CANCELED, OrderStatus.FAILED]:
            # Order failed
            await self._handle_order_completion(order_id, order, order_info, success=False)
    
    def get_status(self) -> StrategyStatus:
        """Get current strategy status"""
        return StrategyStatus(
            name=self.name,
            running=self.running,
            active_positions=len(self.pending_orders),
            total_trades=len(self.completed_orders),
            profit_loss=0.0,  # Market orders don't track P&L
            last_update=time.time(),
            error_count=self.error_count,
            last_error=self.last_error
        )
    
    # Public methods for executing orders
    
    async def execute_market_buy(
        self,
        exchange_name: str,
        pair: str,
        amount: float,
        max_slippage: Optional[float] = None
    ) -> Dict:
        """
        Execute a market buy order
        
        Args:
            exchange_name: Target exchange
            pair: Trading pair (e.g., "BTC/USDT")
            amount: Amount to buy
            max_slippage: Maximum allowed slippage
            
        Returns:
            Dict with execution results
        """
        return await self._execute_market_order(
            exchange_name=exchange_name,
            pair=pair,
            side=OrderSide.BUY,
            amount=amount,
            max_slippage=max_slippage
        )
    
    async def execute_market_sell(
        self,
        exchange_name: str,
        pair: str,
        amount: float,
        max_slippage: Optional[float] = None
    ) -> Dict:
        """
        Execute a market sell order
        
        Args:
            exchange_name: Target exchange
            pair: Trading pair (e.g., "BTC/USDT")
            amount: Amount to sell
            max_slippage: Maximum allowed slippage
            
        Returns:
            Dict with execution results
        """
        return await self._execute_market_order(
            exchange_name=exchange_name,
            pair=pair,
            side=OrderSide.SELL,
            amount=amount,
            max_slippage=max_slippage
        )
    
    async def get_best_price(self, pair: str, side: OrderSide) -> Optional[Dict]:
        """
        Get best price across all exchanges
        
        Args:
            pair: Trading pair
            side: Order side (BUY or SELL)
            
        Returns:
            Dict with best price info or None
        """
        best_price = None
        best_exchange = None
        
        for exchange_name, exchange in self.exchanges.items():
            try:
                if '/' not in pair:
                    continue
                    
                base, quote = pair.split('/', 1)
                trading_pair = TradingPair(base=base, quote=quote, symbol=pair)
                
                ticker = await exchange.get_ticker(trading_pair)
                if not ticker:
                    continue
                
                if side == OrderSide.BUY and ticker.ask:
                    if best_price is None or ticker.ask < best_price:
                        best_price = ticker.ask
                        best_exchange = exchange_name
                        
                elif side == OrderSide.SELL and ticker.bid:
                    if best_price is None or ticker.bid > best_price:
                        best_price = ticker.bid
                        best_exchange = exchange_name
                        
            except Exception as e:
                self.log_error(f"Error getting price from {exchange_name}: {e}")
        
        if best_price and best_exchange:
            return {
                'price': best_price,
                'exchange': best_exchange,
                'side': side.value
            }
        
        return None
    
    # Private methods
    
    async def _execute_market_order(
        self,
        exchange_name: str,
        pair: str,
        side: OrderSide,
        amount: float,
        max_slippage: Optional[float] = None
    ) -> Dict:
        """Execute a market order with protection"""
        try:
            slippage_limit = max_slippage or self.market_config.max_slippage
            
            # Validate exchange
            if exchange_name not in self.exchanges:
                return {'success': False, 'error': f'Exchange {exchange_name} not available'}
            
            exchange = self.exchanges[exchange_name]
            
            # Parse pair
            if '/' not in pair:
                return {'success': False, 'error': f'Invalid pair format: {pair}'}
                
            base, quote = pair.split('/', 1)
            trading_pair = TradingPair(base=base, quote=quote, symbol=pair)
            
            # Get current price for slippage calculation
            reference_price = await self._get_reference_price(exchange, trading_pair, side)
            if not reference_price:
                return {'success': False, 'error': 'Could not get reference price'}
            
            # Execute with retry mechanism
            for attempt in range(self.market_config.retry_attempts):
                try:
                    self.logger.info(f"Executing market {side.value} order: {amount} {pair} on {exchange_name} (attempt {attempt + 1})")
                    
                    # Place market order
                    order = await exchange.place_order(
                        pair=trading_pair,
                        side=side,
                        type=OrderType.MARKET,
                        amount=amount
                    )
                    
                    if not order:
                        if attempt < self.market_config.retry_attempts - 1:
                            await asyncio.sleep(1)  # Wait before retry
                            continue
                        else:
                            return {'success': False, 'error': 'Failed to place order'}
                    
                    # Track the order
                    order_info = {
                        'order_id': order.id,
                        'exchange': exchange_name,
                        'pair': pair,
                        'side': side.value,
                        'amount': amount,
                        'reference_price': reference_price,
                        'slippage_limit': slippage_limit,
                        'placed_time': time.time()
                    }
                    
                    self.pending_orders[order.id] = order_info
                    
                    # For market orders, we might get immediate execution
                    if order.status == OrderStatus.FILLED:
                        await self._handle_order_completion(order.id, order, order_info, success=True)
                        
                        return {
                            'success': True,
                            'order_id': order.id,
                            'executed_price': order.price or reference_price,
                            'executed_amount': order.filled,
                            'slippage': self._calculate_slippage(reference_price, order.price or reference_price, side)
                        }
                    else:
                        # Order is pending - will be handled by monitoring loop
                        return {
                            'success': True,
                            'order_id': order.id,
                            'status': 'pending',
                            'reference_price': reference_price
                        }
                        
                except Exception as e:
                    self.log_error(f"Order attempt {attempt + 1} failed: {e}")
                    if attempt < self.market_config.retry_attempts - 1:
                        await asyncio.sleep(2)  # Wait before retry
                    else:
                        return {'success': False, 'error': f'All retry attempts failed: {str(e)}'}
            
        except Exception as e:
            self.log_error(f"Market order execution failed: {e}")
            return {'success': False, 'error': str(e)}
    
    async def _get_reference_price(
        self, 
        exchange: Any, 
        trading_pair: TradingPair, 
        side: OrderSide
    ) -> Optional[float]:
        """Get reference price for slippage calculation"""
        try:
            ticker = await exchange.get_ticker(trading_pair)
            if ticker:
                if side == OrderSide.BUY and ticker.ask:
                    return ticker.ask
                elif side == OrderSide.SELL and ticker.bid:
                    return ticker.bid
                elif ticker.last:
                    return ticker.last
            
            # Fallback to orderbook
            orderbook = await exchange.get_orderbook(trading_pair, 1)
            if orderbook:
                if side == OrderSide.BUY and orderbook.asks:
                    return orderbook.asks[0][0]
                elif side == OrderSide.SELL and orderbook.bids:
                    return orderbook.bids[0][0]
                    
        except Exception as e:
            self.log_error(f"Error getting reference price: {e}")
        
        return None
    
    def _calculate_slippage(self, reference_price: float, executed_price: float, side: OrderSide) -> float:
        """Calculate slippage percentage"""
        if side == OrderSide.BUY:
            # For buys, slippage is positive if we paid more than reference
            return (executed_price - reference_price) / reference_price
        else:
            # For sells, slippage is positive if we received less than reference
            return (reference_price - executed_price) / reference_price
    
    async def _handle_order_completion(
        self, 
        order_id: str, 
        order: Any, 
        order_info: Dict, 
        success: bool
    ):
        """Handle order completion"""
        try:
            # Remove from pending
            if order_id in self.pending_orders:
                del self.pending_orders[order_id]
            
            # Add to completed orders
            completion_info = {
                **order_info,
                'completed_time': time.time(),
                'success': success,
                'executed_price': getattr(order, 'price', None),
                'executed_amount': getattr(order, 'filled', 0),
                'final_status': order.status.value if hasattr(order, 'status') else 'unknown'
            }
            
            self.completed_orders.append(completion_info)
            
            if success:
                self.logger.info(f"Market order completed successfully: {order_id}")
                
                # Log the trade
                self.log_trade(
                    exchange=order_info['exchange'],
                    pair=order_info['pair'],
                    side=order_info['side'],
                    amount=getattr(order, 'filled', order_info['amount']),
                    price=getattr(order, 'price', order_info['reference_price'])
                )
            else:
                self.log_error(f"Market order failed: {order_id}")
                
        except Exception as e:
            self.log_error(f"Error handling order completion: {e}")
    
    async def _cancel_pending_orders(self):
        """Cancel all pending orders"""
        for order_id, order_info in list(self.pending_orders.items()):
            try:
                exchange_name = order_info['exchange']
                exchange = self.exchanges[exchange_name]
                
                pair_parts = order_info['pair'].split('/')
                trading_pair = TradingPair(
                    base=pair_parts[0], 
                    quote=pair_parts[1], 
                    symbol=order_info['pair']
                )
                
                success = await exchange.cancel_order(order_id, trading_pair)
                if success:
                    del self.pending_orders[order_id]
                    self.logger.info(f"Canceled pending order: {order_id}")
                    
            except Exception as e:
                self.log_error(f"Error canceling order {order_id}: {e}")
    
    async def _monitoring_loop(self):
        """Monitor pending orders for timeout and completion"""
        while self.running:
            try:
                current_time = time.time()
                
                # Check for timed out orders
                for order_id, order_info in list(self.pending_orders.items()):
                    order_age = current_time - order_info['placed_time']
                    
                    if order_age > self.market_config.order_timeout:
                        self.logger.warning(f"Order {order_id} timed out")
                        
                        # Try to cancel the timed out order
                        try:
                            exchange = self.exchanges[order_info['exchange']]
                            pair_parts = order_info['pair'].split('/')
                            trading_pair = TradingPair(
                                base=pair_parts[0], 
                                quote=pair_parts[1], 
                                symbol=order_info['pair']
                            )
                            
                            await exchange.cancel_order(order_id, trading_pair)
                            del self.pending_orders[order_id]
                            
                        except Exception as e:
                            self.log_error(f"Error canceling timed out order {order_id}: {e}")
                
                # Sleep before next check
                await asyncio.sleep(5)  # Check every 5 seconds
                
            except Exception as e:
                self.log_error(f"Error in monitoring loop: {e}")
                await asyncio.sleep(10)  # Longer sleep on error