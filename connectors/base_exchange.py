"""
Base Exchange Connector Interface
=================================

Abstract base class that defines the interface all exchange connectors must implement.
This allows strategies to work with any exchange without knowing the implementation details.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, List, Optional, Tuple
from enum import Enum
import time


class OrderSide(Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(Enum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(Enum):
    PENDING = "pending"
    OPEN = "open"
    FILLED = "filled"
    CANCELED = "canceled"
    FAILED = "failed"


@dataclass
class TradingPair:
    """Standardized trading pair representation"""
    base: str      # e.g., "BTC"
    quote: str     # e.g., "USDC"
    symbol: str    # Exchange-specific symbol, e.g., "BTCUSDC"
    
    def __str__(self) -> str:
        return f"{self.base}/{self.quote}"


@dataclass 
class OrderBook:
    """Standardized orderbook representation"""
    pair: TradingPair
    bids: List[Tuple[float, float]]  # [(price, amount), ...]
    asks: List[Tuple[float, float]]  # [(price, amount), ...]
    timestamp: float


@dataclass
class Ticker:
    """Standardized ticker information"""
    pair: TradingPair
    bid: Optional[float] = None
    ask: Optional[float] = None
    last: Optional[float] = None
    volume: Optional[float] = None
    timestamp: Optional[float] = None


@dataclass
class Balance:
    """Standardized balance representation"""
    asset: str
    free: float        # Available for trading
    locked: float      # Locked in orders
    total: float       # Total balance
    
    @property
    def available(self) -> float:
        return self.free


@dataclass
class Order:
    """Standardized order representation"""
    id: str
    pair: TradingPair
    side: OrderSide
    type: OrderType
    amount: float
    price: Optional[float]
    filled: float
    remaining: float
    status: OrderStatus
    timestamp: float
    fee: Optional[float] = None


@dataclass
class Trade:
    """Standardized trade representation"""
    id: str
    order_id: str
    pair: TradingPair
    side: OrderSide
    amount: float
    price: float
    fee: float
    timestamp: float


class BaseExchange(ABC):
    """
    Base exchange connector that all exchange implementations must inherit from.
    
    This provides a standardized interface for all exchanges, allowing strategies
    to work with any exchange without modification.
    """
    
    def __init__(self, config: Dict):
        """
        Initialize exchange connector
        
        Args:
            config: Exchange-specific configuration
        """
        self.config = config
        self.name = config.get('name', 'unknown')
        self.connected = False
        self.trading_fees = config.get('trading_fees', {})
        
    @abstractmethod
    async def connect(self) -> bool:
        """
        Connect to the exchange
        
        Returns:
            True if connection successful, False otherwise
        """
        pass
    
    @abstractmethod
    async def disconnect(self):
        """Disconnect from the exchange"""
        pass
    
    @abstractmethod
    async def get_trading_pairs(self) -> List[TradingPair]:
        """
        Get all available trading pairs
        
        Returns:
            List of available trading pairs
        """
        pass
    
    @abstractmethod
    async def get_orderbook(self, pair: TradingPair, limit: int = 100) -> Optional[OrderBook]:
        """
        Get orderbook for a trading pair
        
        Args:
            pair: Trading pair
            limit: Number of levels to return
            
        Returns:
            Orderbook data or None if not available
        """
        pass
    
    @abstractmethod
    async def get_ticker(self, pair: TradingPair) -> Optional[Ticker]:
        """
        Get ticker information for a trading pair
        
        Args:
            pair: Trading pair
            
        Returns:
            Ticker information or None if not available
        """
        pass
    
    @abstractmethod
    async def get_balances(self) -> Dict[str, Balance]:
        """
        Get account balances
        
        Returns:
            Dictionary mapping asset symbols to Balance objects
        """
        pass
    
    @abstractmethod
    async def place_order(
        self,
        pair: TradingPair,
        side: OrderSide,
        type: OrderType,
        amount: float,
        price: Optional[float] = None,
        params: Optional[Dict] = None
    ) -> Optional[Order]:
        """
        Place an order
        
        Args:
            pair: Trading pair
            side: Order side (buy/sell)
            type: Order type (market/limit)
            amount: Order amount
            price: Order price (required for limit orders)
            params: Additional exchange-specific parameters
            
        Returns:
            Order object if successful, None otherwise
        """
        pass
    
    @abstractmethod
    async def cancel_order(self, order_id: str, pair: TradingPair) -> bool:
        """
        Cancel an order
        
        Args:
            order_id: Order ID to cancel
            pair: Trading pair
            
        Returns:
            True if cancellation successful, False otherwise
        """
        pass
    
    @abstractmethod
    async def get_order(self, order_id: str, pair: TradingPair) -> Optional[Order]:
        """
        Get order information
        
        Args:
            order_id: Order ID
            pair: Trading pair
            
        Returns:
            Order object or None if not found
        """
        pass
    
    @abstractmethod
    async def get_orders(self, pair: TradingPair, status: Optional[OrderStatus] = None) -> List[Order]:
        """
        Get orders for a trading pair
        
        Args:
            pair: Trading pair
            status: Filter by order status (optional)
            
        Returns:
            List of orders
        """
        pass
    
    @abstractmethod
    async def get_trades(self, pair: TradingPair, limit: int = 100) -> List[Trade]:
        """
        Get recent trades for a trading pair
        
        Args:
            pair: Trading pair
            limit: Maximum number of trades to return
            
        Returns:
            List of recent trades
        """
        pass
    
    # Helper methods that can be overridden but have default implementations
    
    def get_trading_fee(self, pair: TradingPair, side: OrderSide) -> float:
        """
        Get trading fee for a pair and side
        
        Args:
            pair: Trading pair
            side: Order side
            
        Returns:
            Trading fee as percentage (0.001 = 0.1%)
        """
        return self.trading_fees.get('default', 0.001)
    
    def normalize_pair_symbol(self, base: str, quote: str) -> str:
        """
        Convert base/quote to exchange-specific symbol format
        
        Args:
            base: Base asset symbol
            quote: Quote asset symbol
            
        Returns:
            Exchange-specific trading pair symbol
        """
        return f"{base}{quote}"
    
    def parse_pair_symbol(self, symbol: str) -> Tuple[str, str]:
        """
        Parse exchange symbol into base/quote
        
        Args:
            symbol: Exchange-specific symbol
            
        Returns:
            Tuple of (base, quote)
        """
        # Default implementation - exchanges should override this
        if len(symbol) == 6:  # e.g., BTCUSD
            return symbol[:3], symbol[3:]
        elif "/" in symbol:
            return symbol.split("/", 1)
        else:
            raise ValueError(f"Cannot parse pair symbol: {symbol}")
    
    def calculate_market_impact(self, orderbook: OrderBook, side: OrderSide, amount: float) -> Dict:
        """
        Calculate market impact of an order
        
        Args:
            orderbook: Current orderbook
            side: Order side
            amount: Order amount
            
        Returns:
            Dictionary with impact analysis
        """
        levels = orderbook.asks if side == OrderSide.BUY else orderbook.bids
        
        if not levels:
            return {
                'average_price': 0,
                'slippage': float('inf'),
                'total_cost': 0,
                'levels_consumed': 0
            }
        
        remaining_amount = amount
        total_cost = 0
        levels_consumed = 0
        best_price = levels[0][0]
        
        for price, volume in levels:
            if remaining_amount <= 0:
                break
                
            consumed = min(remaining_amount, volume)
            total_cost += consumed * price
            remaining_amount -= consumed
            levels_consumed += 1
        
        if remaining_amount > 0:
            # Not enough liquidity
            return {
                'average_price': 0,
                'slippage': float('inf'),
                'total_cost': total_cost,
                'levels_consumed': levels_consumed,
                'liquidity_shortfall': remaining_amount
            }
        
        average_price = total_cost / amount
        slippage = abs(average_price - best_price) / best_price
        
        return {
            'average_price': average_price,
            'slippage': slippage,
            'total_cost': total_cost,
            'levels_consumed': levels_consumed,
            'best_price': best_price
        }
    
    def __str__(self) -> str:
        return f"{self.name} Exchange"
    
    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}(name='{self.name}', connected={self.connected})>"