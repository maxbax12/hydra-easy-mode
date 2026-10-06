"""
Base Strategy Interface
=======================

Abstract base class that defines the interface all trading strategies must implement.
This allows the bot to run multiple strategies simultaneously and manage them uniformly.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, List, Optional, Any
import time
import logging


@dataclass
class StrategyConfig:
    """Base configuration for all strategies"""
    name: str
    enabled: bool = True
    risk_limit: float = 1.0  # Maximum position size
    max_daily_trades: int = 1000
    
    # Strategy-specific config stored as dict
    params: Dict[str, Any] = None
    
    def __post_init__(self):
        if self.params is None:
            self.params = {}


@dataclass
class StrategyStatus:
    """Strategy status information"""
    name: str
    running: bool
    active_positions: int
    total_trades: int
    profit_loss: float
    last_update: float
    error_count: int = 0
    last_error: Optional[str] = None


class BaseStrategy(ABC):
    """
    Base class for all trading strategies.
    
    Strategies implement specific trading logic while the connectors handle
    exchange communication. This separation allows strategies to work with
    any exchange that implements the BaseExchange interface.
    """
    
    def __init__(self, name: str, config: StrategyConfig, exchanges: Dict[str, Any]):
        """
        Initialize base strategy
        
        Args:
            name: Strategy name
            config: Strategy configuration
            exchanges: Dictionary of exchange connectors {name: exchange}
        """
        self.name = name
        self.config = config
        self.exchanges = exchanges
        self.running = False
        
        # Performance tracking
        self.total_trades = 0
        self.profit_loss = 0.0
        self.error_count = 0
        self.last_error = None
        
        # Setup logging
        self.logger = logging.getLogger(f"strategy.{name}")
        
        # Strategy state storage
        self.state: Dict[str, Any] = {}
        
    @abstractmethod
    async def initialize(self) -> bool:
        """
        Initialize the strategy
        
        Called once before the strategy starts running.
        Should validate configuration and setup any required state.
        
        Returns:
            True if initialization successful, False otherwise
        """
        pass
    
    @abstractmethod
    async def start(self):
        """
        Start the strategy
        
        Called to begin strategy execution. Should set self.running = True
        and begin the main strategy loop.
        """
        pass
    
    @abstractmethod
    async def stop(self):
        """
        Stop the strategy
        
        Called to stop strategy execution. Should set self.running = False
        and cleanup any resources.
        """
        pass
    
    @abstractmethod
    async def on_market_data(self, exchange_name: str, pair: str, data: Dict):
        """
        Handle market data updates
        
        Args:
            exchange_name: Name of the exchange
            pair: Trading pair symbol
            data: Market data (orderbook, ticker, trades, etc.)
        """
        pass
    
    @abstractmethod
    async def on_order_update(self, exchange_name: str, order: Any):
        """
        Handle order updates (fills, cancellations, etc.)
        
        Args:
            exchange_name: Name of the exchange
            order: Order update information
        """
        pass
    
    @abstractmethod
    def get_status(self) -> StrategyStatus:
        """
        Get current strategy status
        
        Returns:
            StrategyStatus object with current information
        """
        pass
    
    # Helper methods available to all strategies
    
    def get_exchange(self, name: str) -> Optional[Any]:
        """Get exchange connector by name"""
        return self.exchanges.get(name)
    
    def log_trade(self, exchange: str, pair: str, side: str, amount: float, price: float, profit: float = 0):
        """Log a completed trade"""
        self.total_trades += 1
        self.profit_loss += profit
        
        self.logger.info(
            f"Trade executed: {side} {amount:.8f} {pair} @ {price:.8f} on {exchange} "
            f"(P&L: {profit:+.8f}, Total: {self.profit_loss:+.8f})"
        )
    
    def log_error(self, error: str):
        """Log an error"""
        self.error_count += 1
        self.last_error = error
        self.logger.error(f"Strategy error: {error}")
    
    def update_state(self, key: str, value: Any):
        """Update strategy state"""
        self.state[key] = value
    
    def get_state(self, key: str, default: Any = None) -> Any:
        """Get strategy state"""
        return self.state.get(key, default)
    
    def calculate_position_size(self, exchange: str, pair: str, risk_amount: float) -> float:
        """
        Calculate position size based on risk management
        
        Args:
            exchange: Exchange name
            pair: Trading pair
            risk_amount: Maximum risk amount
            
        Returns:
            Position size to trade
        """
        # Default implementation - can be overridden by strategies
        max_position = self.config.risk_limit
        return min(risk_amount, max_position)
    
    async def get_market_price(self, exchange_name: str, pair: str) -> Optional[float]:
        """Get current market price for a pair"""
        exchange = self.get_exchange(exchange_name)
        if not exchange:
            return None
        
        try:
            from connectors.base_exchange import TradingPair
            trading_pair = TradingPair(base="", quote="", symbol=pair)  # Simplified
            ticker = await exchange.get_ticker(trading_pair)
            return ticker.last if ticker else None
            
        except Exception as e:
            self.log_error(f"Error getting market price for {pair} on {exchange_name}: {e}")
            return None
    
    async def place_order(
        self, 
        exchange_name: str, 
        pair: str, 
        side: str, 
        amount: float, 
        price: Optional[float] = None,
        order_type: str = "limit"
    ) -> Optional[Any]:
        """
        Place an order on an exchange
        
        Args:
            exchange_name: Exchange name
            pair: Trading pair
            side: Order side ('buy' or 'sell')
            amount: Order amount
            price: Order price (required for limit orders)
            order_type: Order type ('limit' or 'market')
            
        Returns:
            Order object if successful, None otherwise
        """
        exchange = self.get_exchange(exchange_name)
        if not exchange:
            self.log_error(f"Exchange {exchange_name} not available")
            return None
        
        try:
            from connectors.base_exchange import TradingPair, OrderSide, OrderType
            
            trading_pair = TradingPair(base="", quote="", symbol=pair)
            order_side = OrderSide.BUY if side.lower() == 'buy' else OrderSide.SELL
            order_type_enum = OrderType.MARKET if order_type.lower() == 'market' else OrderType.LIMIT
            
            order = await exchange.place_order(
                pair=trading_pair,
                side=order_side,
                type=order_type_enum,
                amount=amount,
                price=price
            )
            
            if order:
                self.logger.info(f"Order placed: {side} {amount} {pair} @ {price} on {exchange_name}")
            else:
                self.log_error(f"Failed to place order: {side} {amount} {pair} on {exchange_name}")
            
            return order
            
        except Exception as e:
            self.log_error(f"Error placing order: {e}")
            return None
    
    def __str__(self) -> str:
        return f"{self.name} Strategy"
    
    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}(name='{self.name}', running={self.running})>"


class StrategyManager:
    """
    Manages multiple strategies and coordinates their execution
    """
    
    def __init__(self):
        self.strategies: Dict[str, BaseStrategy] = {}
        self.running = False
        self.logger = logging.getLogger("strategy_manager")
    
    def register_strategy(self, strategy: BaseStrategy):
        """Register a strategy for management"""
        self.strategies[strategy.name] = strategy
        self.logger.info(f"Registered strategy: {strategy.name}")
    
    def unregister_strategy(self, name: str):
        """Unregister a strategy"""
        if name in self.strategies:
            del self.strategies[name]
            self.logger.info(f"Unregistered strategy: {name}")
    
    async def start_all(self):
        """Start all registered strategies"""
        self.running = True
        self.logger.info("Starting all strategies...")
        
        for name, strategy in self.strategies.items():
            try:
                if strategy.config.enabled:
                    if await strategy.initialize():
                        await strategy.start()
                        self.logger.info(f"Started strategy: {name}")
                    else:
                        self.logger.error(f"Failed to initialize strategy: {name}")
                else:
                    self.logger.info(f"Strategy disabled: {name}")
                    
            except Exception as e:
                self.logger.error(f"Error starting strategy {name}: {e}")
    
    async def stop_all(self):
        """Stop all running strategies"""
        self.running = False
        self.logger.info("Stopping all strategies...")
        
        for name, strategy in self.strategies.items():
            try:
                await strategy.stop()
                self.logger.info(f"Stopped strategy: {name}")
            except Exception as e:
                self.logger.error(f"Error stopping strategy {name}: {e}")
    
    async def start_strategy(self, name: str):
        """Start a specific strategy"""
        if name in self.strategies:
            strategy = self.strategies[name]
            try:
                if await strategy.initialize():
                    await strategy.start()
                    self.logger.info(f"Started strategy: {name}")
                else:
                    self.logger.error(f"Failed to initialize strategy: {name}")
            except Exception as e:
                self.logger.error(f"Error starting strategy {name}: {e}")
        else:
            self.logger.error(f"Strategy not found: {name}")
    
    async def stop_strategy(self, name: str):
        """Stop a specific strategy"""
        if name in self.strategies:
            try:
                await self.strategies[name].stop()
                self.logger.info(f"Stopped strategy: {name}")
            except Exception as e:
                self.logger.error(f"Error stopping strategy {name}: {e}")
        else:
            self.logger.error(f"Strategy not found: {name}")
    
    def get_all_status(self) -> Dict[str, StrategyStatus]:
        """Get status of all strategies"""
        status = {}
        for name, strategy in self.strategies.items():
            try:
                status[name] = strategy.get_status()
            except Exception as e:
                self.logger.error(f"Error getting status for {name}: {e}")
                status[name] = StrategyStatus(
                    name=name,
                    running=False,
                    active_positions=0,
                    total_trades=0,
                    profit_loss=0.0,
                    last_update=time.time(),
                    error_count=1,
                    last_error=str(e)
                )
        
        return status
    
    def list_strategies(self) -> List[str]:
        """Get list of registered strategy names"""
        return list(self.strategies.keys())