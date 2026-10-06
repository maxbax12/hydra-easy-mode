#!/usr/bin/env python3
"""
Orderbook Manager for Real-time Arbitrage Detection
====================================================

Manages real-time orderbook state from multiple exchanges and triggers
arbitrage opportunity detection on price updates.
"""

import asyncio
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional, Callable, Tuple, Any
from collections import defaultdict
import logging

from connectors.base_exchange import TradingPair


# Import dynamic sizing components (will be initialized if available)
try:
    from optimal_size_finder import OptimalSizeFinder
    from dynamic_sizing import DynamicPositionSizer
    DYNAMIC_SIZING_AVAILABLE = True
except ImportError:
    DYNAMIC_SIZING_AVAILABLE = False


@dataclass
class OrderbookLevel:
    """Represents a single price level in the orderbook"""
    price: float
    volume: float
    timestamp: float = field(default_factory=time.time)


@dataclass
class OrderbookSnapshot:
    """Snapshot of orderbook state at a point in time"""
    exchange: str
    pair: str
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    bid_volume: float = 0.0
    ask_volume: float = 0.0
    bids: List[OrderbookLevel] = field(default_factory=list)
    asks: List[OrderbookLevel] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)
    
    @property
    def spread(self) -> Optional[float]:
        """Calculate bid-ask spread"""
        if self.best_bid and self.best_ask:
            return self.best_ask - self.best_bid
        return None
    
    @property
    def spread_percentage(self) -> Optional[float]:
        """Calculate spread as percentage of mid price"""
        if self.best_bid and self.best_ask:
            mid = (self.best_bid + self.best_ask) / 2
            return (self.spread / mid) * 100 if mid > 0 else None
        return None
    
    def get_depth_at_price(self, price: float, side: str) -> float:
        """Get total volume available up to a certain price level"""
        total_volume = 0.0
        levels = self.bids if side == 'bid' else self.asks
        
        for level in levels:
            if side == 'bid' and level.price >= price:
                total_volume += level.volume
            elif side == 'ask' and level.price <= price:
                total_volume += level.volume
        
        return total_volume


@dataclass
class ArbitrageOpportunity:
    """Represents a detected arbitrage opportunity"""
    pair: str  # Normalized pair for strategy matching (e.g., "ETH/USD")
    buy_exchange: str
    sell_exchange: str
    buy_price: float
    sell_price: float
    max_volume: float
    profit: float
    profit_percentage: float
    buy_fees: float
    sell_fees: float
    net_profit: float
    timestamp: float = field(default_factory=time.time)

    # Actual pair names on each exchange (for orderbook fetching)
    buy_pair: Optional[str] = None  # e.g., "ETH/USDT" on Binance
    sell_pair: Optional[str] = None  # e.g., "ETH/USDC" on Hydra

    # Optional: Store currency network info for Hydra to ensure correct market is used
    # This is critical when there are multiple markets for the same symbol (e.g., multiple USDC on different networks)
    base_network_id: Optional[str] = None
    quote_network_id: Optional[str] = None

    # Store the actual currency objects to ensure EstimateOrder uses exact same market
    base_currency: Optional[Any] = None
    quote_currency: Optional[Any] = None

    @property
    def is_profitable(self) -> bool:
        """Check if opportunity is still profitable after fees"""
        return self.net_profit > 0


class OrderbookManager:
    """
    Manages real-time orderbook state from multiple exchanges
    and detects arbitrage opportunities
    """

    @staticmethod
    def normalize_pair(pair: str) -> str:
        """
        Normalize stablecoin pairs for cross-quote arbitrage.
        Treats USDC, USDT, BUSD, etc. as equivalent USD.

        Examples:
            BTC/USDC -> BTC/USD
            ETH/USDT -> ETH/USD
            BNB/BUSD -> BNB/USD
        """
        # List of stablecoins to normalize to USD
        stablecoins = ['USDC', 'USDC2', 'USDT', 'BUSD', 'DAI', 'TUSD', 'USDP']

        for stable in stablecoins:
            if pair.endswith(f'/{stable}'):
                return pair.replace(f'/{stable}', '/USD')

        return pair

    def __init__(self, logger: Optional[logging.Logger] = None, hydra_client=None, config: Dict = None):
        self.logger = logger or logging.getLogger(__name__)
        self.hydra_client = hydra_client
        self.config = config or {}
        
        # Initialize basic attributes first
        self.profit_validator = None
        self.currency_cache = {}  # Cache for currency objects by pair
        self.last_validation_prices = {}  # Track last validated prices: pair -> (buy_price, sell_price, timestamp)
        self.price_change_threshold = 10.0  # Only re-validate if price changed by $10+
        self.dynamic_sizer = None  # Dynamic position sizing
        self.exchange_connectors = {}  # Store exchange connectors for currency discovery

        # Orderbook storage: {exchange: {pair: OrderbookSnapshot}}
        self.orderbooks: Dict[str, Dict[str, OrderbookSnapshot]] = defaultdict(dict)

        # Thread safety
        self.lock = threading.RLock()

        # Track which pairs have active (enabled) strategies
        # Stores normalized pairs (e.g., "ETH/USD") to avoid expensive analysis for disabled strategies
        self.active_strategy_pairs = set()

        # Arbitrage detection callbacks
        self.arbitrage_callbacks: List[Callable[[ArbitrageOpportunity], None]] = []
        
        # Fee structure for each exchange (from config with fallbacks)
        exchange_fees_config = self.config.get('exchange_fees', {})
        self.exchange_fees: Dict[str, Dict[str, float]] = {
            'hydra': exchange_fees_config.get('hydra', {'maker': 0.001, 'taker': 0.002}),
            'binance': exchange_fees_config.get('binance', {'maker': 0.001, 'taker': 0.001}),
            'binance_testnet': exchange_fees_config.get('binance_testnet', {'maker': 0.001, 'taker': 0.001})
        }
        
        # Position sizing configuration (from config with fallbacks)
        params = self.config.get('params', {})
        self.max_position_usd = params.get('max_position_size', 200.0)  # From config or fallback
        self.dynamic_sizing_strategy = params.get('dynamic_sizing_strategy', 'max_absolute')  # Strategy preference
        self.sizing_max_limit = params.get('sizing_max_limit', self.max_position_usd * 5)  # Max limit for dynamic sizing
        
        self.logger.info(f"🔧 Configured position sizing: max_position=${self.max_position_usd}, sizing_max_limit=${self.sizing_max_limit}, strategy={self.dynamic_sizing_strategy}")
        
        # Minimum profit thresholds (from config with fallbacks)  
        self.min_profit_percentage = params.get('min_profit_percentage', 0.5)  # From config or fallback
        self.min_profit_amount = params.get('min_profit_amount', 1.0)  # From config or fallback
        
        # Log configuration
        self.logger.info(f"📋 OrderbookManager Configuration:")
        self.logger.info(f"   💰 Max position size: ${self.max_position_usd}")
        self.logger.info(f"   📈 Min profit percentage: {self.min_profit_percentage}%")
        self.logger.info(f"   💵 Min profit amount: ${self.min_profit_amount}")
        self.logger.info(f"   🎯 Dynamic sizing strategy: {self.dynamic_sizing_strategy}")
        self.logger.info(f"   🔄 Sizing max limit: ${self.sizing_max_limit}")
        
        # Statistics
        self.opportunities_detected = 0
        self.last_opportunity_time = None
        
        # Initialize EstimateOrder validation if Hydra client available (after config is loaded)
        if hydra_client:
            try:
                from profit_validator import ProfitValidator
                self.profit_validator = ProfitValidator(hydra_client)
                self.logger.info("✅ OrderbookManager: Profit validator initialized")
                
                # Initialize dynamic sizing if available
                if DYNAMIC_SIZING_AVAILABLE and self.profit_validator:
                    # Get min position size from config or use sensible default
                    min_position_usd = params.get('min_position_size', 50.0)
                    self.dynamic_sizer = DynamicPositionSizer(
                        self.profit_validator, 
                        min_profit_threshold=self.min_profit_percentage,  # Now config is loaded
                        min_position_usd=min_position_usd,  # Use config-driven minimum
                        logger=self.logger
                    )
                    self.logger.info("✅ OrderbookManager: Dynamic position sizing initialized")
                else:
                    self.logger.info("ℹ️  OrderbookManager: Dynamic sizing not available, using fixed sizing")
                    
                self.logger.info("ℹ️  Note: Full EstimateOrder validation pending Swap service implementation")
            except Exception as e:
                self.logger.error(f"❌ OrderbookManager: Failed to initialize profit validator: {e}")
        else:
            self.logger.info("ℹ️  OrderbookManager: Running without profit validation (fast mode)")
        
    def update_orderbook(self, exchange: str, pair: str, 
                        best_bid: Optional[float] = None,
                        best_ask: Optional[float] = None,
                        bids: Optional[List[OrderbookLevel]] = None,
                        asks: Optional[List[OrderbookLevel]] = None,
                        bid_volume: Optional[float] = None,
                        ask_volume: Optional[float] = None):
        """
        Update orderbook state for a specific exchange and pair
        
        Args:
            exchange: Exchange name (e.g., 'hydra', 'binance')
            pair: Trading pair symbol
            best_bid: Best bid price
            best_ask: Best ask price
            bids: List of bid levels
            asks: List of ask levels
            bid_volume: Total bid volume
            ask_volume: Total ask volume
        """
        with self.lock:
            # Get or create orderbook snapshot
            if pair not in self.orderbooks[exchange]:
                self.orderbooks[exchange][pair] = OrderbookSnapshot(
                    exchange=exchange,
                    pair=pair
                )
            
            snapshot = self.orderbooks[exchange][pair]
            
            # Update prices
            if best_bid is not None:
                snapshot.best_bid = best_bid
            if best_ask is not None:
                snapshot.best_ask = best_ask
            
            # Update order levels
            if bids is not None:
                snapshot.bids = bids
            if asks is not None:
                snapshot.asks = asks
            
            # Update volumes
            if bid_volume is not None:
                snapshot.bid_volume = bid_volume
            elif bids:
                snapshot.bid_volume = sum(level.volume for level in bids)
            
            if ask_volume is not None:
                snapshot.ask_volume = ask_volume
            elif asks:
                snapshot.ask_volume = sum(level.volume for level in asks)
            
            # Update timestamp
            snapshot.timestamp = time.time()
            
            self.logger.debug(f"Updated {exchange} {pair}: bid={best_bid}, ask={best_ask}")
            
            # Check for arbitrage opportunities
            self._check_arbitrage_opportunities(pair)
    
    def update_from_hydra_orderbook_update(self, pair: str, orderbook_update):
        """
        Update orderbook from Hydra OrderbookUpdate message
        
        Args:
            pair: Trading pair symbol
            orderbook_update: Hydra OrderbookUpdate protobuf message
        """
        # Convert Decimal to float - using the range boundaries for real-time arbitrage
        # Note: For static analysis we use averages, but for real-time we use boundary prices
        best_bid = None
        best_ask = None
        
        if orderbook_update.buy_max_price:
            best_bid = self._decimal_to_float(orderbook_update.buy_max_price)
        
        if orderbook_update.sell_min_price:
            best_ask = self._decimal_to_float(orderbook_update.sell_min_price)
        
        # Convert liquidity ticks to OrderbookLevel
        bids = []
        asks = []
        
        for tick in orderbook_update.updated_buy_liquidity_ticks:
            if tick.amount:
                bids.append(OrderbookLevel(
                    price=self._decimal_to_float(tick.price),
                    volume=self._decimal_to_float(tick.amount.liquidity)
                ))
        
        for tick in orderbook_update.updated_sell_liquidity_ticks:
            if tick.amount:
                asks.append(OrderbookLevel(
                    price=self._decimal_to_float(tick.price),
                    volume=self._decimal_to_float(tick.amount.liquidity)
                ))
        
        self.update_orderbook(
            exchange='hydra',
            pair=pair,
            best_bid=best_bid,
            best_ask=best_ask,
            bids=bids,
            asks=asks
        )
    
    def _decimal_to_float(self, decimal_proto) -> float:
        """Convert protobuf DecimalString to float"""
        if decimal_proto is None or not hasattr(decimal_proto, 'value') or not decimal_proto.value:
            return 0.0
        
        try:
            return float(decimal_proto.value)
        except (ValueError, TypeError):
            return 0.0
    
    def _check_arbitrage_opportunities(self, pair: str):
        """
        Check for arbitrage opportunities for a specific pair
        across all exchanges. Supports cross-quote arbitrage by
        normalizing stablecoin pairs (USDC, USDT treated as USD).
        """
        # Normalize the pair for cross-quote matching
        normalized_pair = self.normalize_pair(pair)

        # Find all exchanges with this normalized pair
        # Map: exchange -> actual_pair_name
        exchanges_with_pair = {}

        with self.lock:
            for exchange, pairs in self.orderbooks.items():
                for actual_pair, orderbook in pairs.items():
                    if self.normalize_pair(actual_pair) == normalized_pair:
                        if orderbook.best_bid and orderbook.best_ask:
                            exchanges_with_pair[exchange] = actual_pair

        # Need at least 2 exchanges for arbitrage
        if len(exchanges_with_pair) < 2:
            return

        # Check all exchange combinations
        exchange_list = list(exchanges_with_pair.keys())
        for i, buy_exchange in enumerate(exchange_list):
            for sell_exchange in exchange_list[i+1:]:
                buy_pair = exchanges_with_pair[buy_exchange]
                sell_pair = exchanges_with_pair[sell_exchange]
                self._check_arbitrage_between_exchanges_cross_quote(
                    buy_exchange, buy_pair, sell_exchange, sell_pair
                )
                self._check_arbitrage_between_exchanges_cross_quote(
                    sell_exchange, sell_pair, buy_exchange, buy_pair
                )
    
    def _check_arbitrage_between_exchanges_cross_quote(self,
                                          buy_exchange: str,
                                          buy_pair: str,
                                          sell_exchange: str,
                                          sell_pair: str):
        """
        Check for arbitrage opportunity between two specific exchanges
        with potentially different quote currencies (e.g., USDC vs USDT).

        Args:
            buy_exchange: Exchange to buy from
            buy_pair: Trading pair on buy exchange (e.g., "ETH/USDT")
            sell_exchange: Exchange to sell to
            sell_pair: Trading pair on sell exchange (e.g., "ETH/USDC")
        """
        # Use normalized pair for display (e.g., ETH/USD for both ETH/USDC and ETH/USDT)
        pair_display = self.normalize_pair(buy_pair)

        with self.lock:
            buy_book = self.orderbooks[buy_exchange].get(buy_pair)
            sell_book = self.orderbooks[sell_exchange].get(sell_pair)

            if not buy_book or not sell_book:
                return

            if not buy_book.best_ask or not sell_book.best_bid:
                return

            # Check if there's a profitable price difference
            # For arbitrage: we buy at ask price, sell at bid price
            # Profitable if: sell_bid > buy_ask (we sell higher than we buy)
            if sell_book.best_bid <= buy_book.best_ask:
                return  # No arbitrage opportunity - don't log every check

            # Calculate potential profit
            buy_price = buy_book.best_ask    # Price we pay to buy
            sell_price = sell_book.best_bid  # Price we receive when selling
            gross_profit_per_btc = sell_price - buy_price

            # Log at debug level initially - will log at INFO if profitable after validation
            self.logger.debug(f"💡 {pair_display}: POTENTIAL ARBITRAGE! Buy at ${buy_price:.2f} ({buy_exchange}/{buy_pair}) -> Sell at ${sell_price:.2f} ({sell_exchange}/{sell_pair}) = ${gross_profit_per_btc:.2f}/unit profit")
            
            # Quick pre-validation based on gross profit
            if self.profit_validator and 'hydra' in [buy_exchange, sell_exchange]:
                # EstimateOrder typically shows 1-3% worse results than orderbook estimates
                # If gross profit is less than $10 per unit, it's likely unprofitable after EstimateOrder
                if gross_profit_per_btc < 10.0:
                    return  # Skip - likely unprofitable after fees
                
            # Determine maximum volume with position sizing limits
            available_buy_volume = buy_book.ask_volume if buy_book.ask_volume > 0 else float('inf')
            available_sell_volume = sell_book.bid_volume if sell_book.bid_volume > 0 else float('inf')
            
            # Calculate max position in BTC for logging (always calculated)
            max_position_btc = self.max_position_usd / buy_price  # Convert to BTC

            # Check if this pair has an active strategy registered
            # Skip expensive dynamic sizing for pairs without active strategies
            has_active_strategy = pair_display in self.active_strategy_pairs

            if not has_active_strategy:
                self.logger.debug(f"⏭️  {pair_display}: Skipping analysis - no active strategy registered")
                return  # Skip this pair entirely - no strategy is listening

            # Extract base symbol for logging (e.g., "ETH" from "ETH/USD")
            base_symbol = pair_display.split('/')[0]

            # Apply dynamic position sizing if available, otherwise use fixed limits
            if self.dynamic_sizer and self.profit_validator:
                self.logger.debug(f"🎯 {pair_display}: Using dynamic position sizing...")

                # Get currencies from Hydra (EstimateOrder always uses Hydra)
                # Use the Hydra-side pair for currency discovery to get correct asset_ids
                hydra_pair = buy_pair if buy_exchange == 'hydra' else sell_pair
                self.logger.debug(f"Using {hydra_pair} (from {buy_exchange if buy_exchange == 'hydra' else sell_exchange}) for currency discovery")
                base_currency, quote_currency = self._get_currencies_for_pair(hydra_pair)
                if base_currency and quote_currency:
                    try:
                        # Find optimal size using dynamic sizing
                        max_pos_calc = min(self.sizing_max_limit, available_buy_volume * buy_price, available_sell_volume * sell_price)

                        # Check if we've already validated these prices recently (deduplication)
                        validation_key = f"{pair_display}_{buy_exchange}_{sell_exchange}"
                        if validation_key in self.last_validation_prices:
                            last_buy, last_sell, last_time = self.last_validation_prices[validation_key]
                            price_change = abs(buy_price - last_buy) + abs(sell_price - last_sell)
                            time_since = time.time() - last_time

                            # Skip if prices changed < $10 and validated within last 10 seconds
                            if price_change < self.price_change_threshold and time_since < 10.0:
                                self.logger.debug(f"⏭️  Skipping {pair_display}: prices changed by ${price_change:.2f} (need >${self.price_change_threshold:.0f}) in {time_since:.1f}s")
                                return

                        # Generate unique ID for this validation request (for debugging timing)
                        import uuid
                        validation_id = str(uuid.uuid4())[:8]
                        validation_start = time.time()

                        # Determine which side Hydra is on to skip impossible scenarios
                        hydra_side = 'buy' if buy_exchange == 'hydra' else 'sell' if sell_exchange == 'hydra' else None

                        self.logger.info(f"🔧 [ID:{validation_id}] Starting validation for {pair_display}: buy @ ${buy_price:.2f} ({buy_exchange}), sell @ ${sell_price:.2f} ({sell_exchange}), hydra_side={hydra_side}")

                        optimal_sizing = self.dynamic_sizer.get_optimal_position_size(
                            base_currency=base_currency,
                            quote_currency=quote_currency,
                            buy_exchange_price=buy_price,
                            sell_exchange_price=sell_price,
                            max_position_usd=max_pos_calc,
                            strategy_preference=self.dynamic_sizing_strategy,
                            external_fees=self.exchange_fees.get('binance', {}).get('taker', 0.001),
                            base_symbol=base_symbol,
                            hydra_side=hydra_side
                        )

                        validation_duration = (time.time() - validation_start) * 1000  # Convert to milliseconds
                        self.logger.info(f"✅ [ID:{validation_id}] Validation completed in {validation_duration:.1f}ms")

                        # Update cache to prevent re-validating same prices
                        self.last_validation_prices[validation_key] = (buy_price, sell_price, time.time())

                        if optimal_sizing:
                            optimal_btc = optimal_sizing['optimal_btc_amount']
                            # Only log when actually profitable to reduce spam
                            # self.logger.debug(f"✅ Dynamic sizing found optimal: ${optimal_sizing['optimal_usd_amount']} ({optimal_btc:.6f} BTC)")
                            # self.logger.debug(f"   Expected profit: ${optimal_sizing['expected_profit']:.2f} ({optimal_sizing['profit_percentage']:.2f}%)")

                            # Take minimum of: optimal size, available liquidity
                            max_volume = min(
                                available_buy_volume,
                                available_sell_volume,
                                optimal_btc
                            )
                            self.logger.debug(f"🎯 Using dynamically optimized volume: {max_volume:.6f} BTC")
                        else:
                            # EstimateOrder found no profitable opportunities - trust this result and skip trade
                            self.logger.debug(f"🔍 EstimateOrder validation: No profitable sizes found in range ${self.dynamic_sizer.min_position_usd}-${self.sizing_max_limit}")
                            self.logger.debug(f"   Fixed calc would show: {buy_price:.2f} -> {sell_price:.2f} = {((sell_price-buy_price)/buy_price*100):.3f}% gross spread")
                            self.logger.debug(f"❌ Trusting EstimateOrder results - skipping potentially unprofitable trade")
                            self.logger.debug(f"   Recommendation: Wait for better market conditions or lower profit thresholds")

                            # Update cache even for unprofitable results to prevent re-validation
                            self.last_validation_prices[validation_key] = (buy_price, sell_price, time.time())
                            return  # Skip this trade - EstimateOrder shows it's not profitable
                            
                    except Exception as e:
                        self.logger.warning(f"⚠️ Dynamic sizing failed: {e}")
                        self.logger.info(f"   Falling back to fixed sizing for this trade")
                        max_volume = min(available_buy_volume, available_sell_volume, max_position_btc)
                else:
                    self.logger.warning(f"⚠️ Could not get currencies for {pair_display}, using fixed sizing")
                    max_volume = min(available_buy_volume, available_sell_volume, max_position_btc)
            else:
                # Fall back to fixed position sizing
                self.logger.debug(f"📊 {pair_display}: Using fixed position sizing")

                # Take minimum of: available liquidity, position limit
                max_volume = min(
                    available_buy_volume,
                    available_sell_volume,
                    max_position_btc
                )

            self.logger.debug(f"📊 {pair_display}: Position Sizing:")
            self.logger.debug(f"   💰 Max position: ${self.max_position_usd:.0f} = {max_position_btc:.6f} BTC")
            self.logger.debug(f"   🛍️ Available buy liquidity: {available_buy_volume:.6f} BTC")
            self.logger.debug(f"   💰 Available sell liquidity: {available_sell_volume:.6f} BTC")
            self.logger.debug(f"   🎯 Final volume limit: {max_volume:.6f} BTC")
            
            # Enhanced liquidity check with validation
            self.logger.debug(f"📈 {pair_display}: Liquidity Analysis:")
            self.logger.debug(f"   🛍️ BUY side ({buy_exchange}): Need to take from asks")
            self.logger.debug(f"      Available: {available_buy_volume:.6f} BTC at ${buy_price:.2f}")
            self.logger.debug(f"   💰 SELL side ({sell_exchange}): Need buyers (bids)")
            self.logger.debug(f"      Available: {available_sell_volume:.6f} BTC at ${sell_price:.2f}")
            self.logger.debug(f"   🎯 Selected volume: {max_volume:.6f} BTC (${max_volume * buy_price:.2f} USD)")
            
            # Warn if volume is constrained by position limits vs liquidity
            if max_volume == max_position_btc:
                self.logger.debug(f"   ℹ️  Volume limited by position size (${self.max_position_usd:.0f} max)")
            elif max_volume < 0.0001:  # Less than 0.0001 BTC
                self.logger.debug(f"   ⚠️  EXTREMELY LOW LIQUIDITY: Only {max_volume:.8f} BTC (${max_volume * buy_price:.2f})")
            elif max_volume < 0.001:  # Less than 0.001 BTC
                self.logger.debug(f"   ⚠️  Very low liquidity: Only {max_volume:.6f} BTC (${max_volume * buy_price:.2f})")
            
            if max_volume == float('inf') or max_volume <= 0:
                self.logger.warning(f"⚠️  {pair_display}: No liquidity available (max_volume: {max_volume})")
                return  # No liquidity
            
            # Calculate fees with detailed breakdown
            self.logger.debug(f"💸 {pair_display}: Fee Calculation:")
            buy_fees = self._calculate_fees(buy_exchange, buy_price, max_volume, 'taker')
            sell_fees = self._calculate_fees(sell_exchange, sell_price, max_volume, 'taker')
            total_fees = buy_fees + sell_fees

            # Calculate profit with detailed breakdown
            self.logger.debug(f"💰 {pair_display}: Profit Calculation:")
            gross_profit = (sell_price - buy_price) * max_volume
            buy_cost = buy_price * max_volume
            sell_proceeds = sell_price * max_volume
            net_profit = gross_profit - total_fees
            profit_percentage = (net_profit / buy_cost) * 100 if buy_cost > 0 else 0

            self.logger.debug(f"   📊 Price spread: ${sell_price:.2f} - ${buy_price:.2f} = ${sell_price - buy_price:.2f} per unit")
            self.logger.debug(f"   🎯 Volume: {max_volume:.6f} BTC")
            self.logger.debug(f"   💵 Buy cost: ${buy_price:.2f} × {max_volume:.6f} = ${buy_cost:.2f}")
            self.logger.debug(f"   💰 Sell proceeds: ${sell_price:.2f} × {max_volume:.6f} = ${sell_proceeds:.2f}")
            self.logger.debug(f"   📈 Gross profit: ${sell_proceeds:.2f} - ${buy_cost:.2f} = ${gross_profit:.2f}")
            self.logger.debug(f"   💸 Total fees: ${buy_fees:.4f} + ${sell_fees:.4f} = ${total_fees:.4f}")
            self.logger.debug(f"   💵 Net profit: ${gross_profit:.2f} - ${total_fees:.4f} = ${net_profit:.2f}")
            self.logger.debug(f"   📊 Profit %: ${net_profit:.2f} ÷ ${buy_cost:.2f} × 100 = {profit_percentage:.2f}%")
            
            # Check if profitable with detailed thresholds
            meets_amount_threshold = net_profit > self.min_profit_amount
            meets_percentage_threshold = profit_percentage > self.min_profit_percentage

            self.logger.debug(f"✅ {pair_display}: Profitability Check:")
            self.logger.debug(f"   💵 Amount threshold: ${net_profit:.2f} > ${self.min_profit_amount:.2f}? {'✅ YES' if meets_amount_threshold else '❌ NO'}")
            self.logger.debug(f"   📊 Percentage threshold: {profit_percentage:.2f}% > {self.min_profit_percentage:.2f}%? {'✅ YES' if meets_percentage_threshold else '❌ NO'}")
            
            if meets_amount_threshold and meets_percentage_threshold:
                self.logger.info(f"🚨 {pair_display}: PROFITABLE ARBITRAGE FOUND! Net profit: ${net_profit:.2f} ({profit_percentage:.2f}%)")

                # Extract network IDs and currency objects from the discovered currencies (if available)
                base_network_id = None
                quote_network_id = None
                if base_currency and quote_currency:
                    base_network_id = base_currency.network_id if hasattr(base_currency, 'network_id') else None
                    quote_network_id = quote_currency.network_id if hasattr(quote_currency, 'network_id') else None
                    if base_network_id and quote_network_id:
                        self.logger.info(f"📍 Using networks: base={base_network_id}, quote={quote_network_id}")

                opportunity = ArbitrageOpportunity(
                    pair=pair_display,  # Normalized pair for strategy matching (ETH/USD)
                    buy_exchange=buy_exchange,
                    sell_exchange=sell_exchange,
                    buy_pair=buy_pair,  # Actual pair on buy exchange (e.g., ETH/USDT)
                    sell_pair=sell_pair,  # Actual pair on sell exchange (e.g., ETH/USDC)
                    buy_price=buy_price,
                    sell_price=sell_price,
                    max_volume=max_volume,
                    profit=gross_profit,
                    profit_percentage=profit_percentage,
                    buy_fees=buy_fees,
                    sell_fees=sell_fees,
                    net_profit=net_profit,
                    base_network_id=base_network_id,
                    quote_network_id=quote_network_id,
                    base_currency=base_currency,  # Pass the actual discovered currency objects
                    quote_currency=quote_currency
                )

                self._notify_arbitrage_opportunity(opportunity)
            else:
                self.logger.debug(f"❌ {pair_display}: Not profitable enough - Net: ${net_profit:.2f} (min: ${self.min_profit_amount}), %: {profit_percentage:.2f}% (min: {self.min_profit_percentage}%)")
                if not meets_amount_threshold:
                    shortage = self.min_profit_amount - net_profit
                    self.logger.debug(f"   💸 Need ${shortage:.2f} more profit to meet amount threshold")
                if not meets_percentage_threshold:
                    shortage = self.min_profit_percentage - profit_percentage
                    self.logger.debug(f"   📊 Need {shortage:.2f}% more profit margin to meet percentage threshold")
    
    def _calculate_fees(self, exchange: str, price: float, volume: float,
                       order_type: str = 'taker') -> float:
        """Calculate trading fees for an order with detailed logging"""
        if exchange not in self.exchange_fees:
            self.logger.debug(f"   💸 {exchange} fees: No fee structure configured, using 0%")
            return 0.0

        fee_rate = self.exchange_fees[exchange].get(order_type, 0.001)  # Keep fallback for safety
        trade_value = price * volume
        fees = trade_value * fee_rate

        self.logger.debug(f"   💸 {exchange} {order_type} fees: {fee_rate:.4f} rate × ${trade_value:.2f} trade value = ${fees:.4f}")

        return fees
    
    def _notify_arbitrage_opportunity(self, opportunity: ArbitrageOpportunity):
        """Notify all registered callbacks about an arbitrage opportunity"""
        self.opportunities_detected += 1
        self.last_opportunity_time = time.time()
        
        self.logger.info(
            f"💰 Arbitrage opportunity detected: {opportunity.pair} "
            f"Buy {opportunity.buy_exchange} @ {opportunity.buy_price:.4f} "
            f"Sell {opportunity.sell_exchange} @ {opportunity.sell_price:.4f} "
            f"Net profit: ${opportunity.net_profit:.2f} ({opportunity.profit_percentage:.2f}%)"
        )
        
        # Notify all callbacks
        for callback in self.arbitrage_callbacks:
            try:
                callback(opportunity)
            except Exception as e:
                self.logger.error(f"Error in arbitrage callback: {e}")
    
    def add_arbitrage_callback(self, callback: Callable[[ArbitrageOpportunity], None]):
        """Register a callback to be notified of arbitrage opportunities"""
        self.arbitrage_callbacks.append(callback)
        self.logger.info(f"✅ Arbitrage callback registered")

    def register_active_strategy_pair(self, pair: str):
        """
        Register a pair as having an active (enabled) strategy.
        This allows OrderbookManager to skip expensive dynamic sizing for disabled pairs.

        Args:
            pair: Trading pair (e.g., 'ETH/USDC', 'BTC/USDT')
        """
        normalized_pair = self.normalize_pair(pair)
        self.active_strategy_pairs.add(normalized_pair)
        self.logger.info(f"📌 Registered active strategy for {normalized_pair} (from {pair})")
        self.logger.info(f"📌 Active strategy pairs now: {self.active_strategy_pairs}")

    def unregister_active_strategy_pair(self, pair: str):
        """
        Unregister a pair when its strategy is stopped/disabled.

        Args:
            pair: Trading pair (e.g., 'ETH/USDC', 'BTC/USDT')
        """
        normalized_pair = self.normalize_pair(pair)
        self.active_strategy_pairs.discard(normalized_pair)
        self.logger.info(f"📌 Unregistered strategy for {normalized_pair}")

    def get_orderbook(self, exchange: str, pair: str) -> Optional[OrderbookSnapshot]:
        """Get current orderbook snapshot for an exchange and pair"""
        with self.lock:
            return self.orderbooks.get(exchange, {}).get(pair)
    
    def get_all_orderbooks(self, pair: str) -> Dict[str, OrderbookSnapshot]:
        """Get orderbooks for a pair across all exchanges"""
        result = {}
        with self.lock:
            for exchange, pairs in self.orderbooks.items():
                if pair in pairs:
                    result[exchange] = pairs[pair]
        return result
    
    def connect_exchange(self, exchange_name: str, exchange_connector):
        """Connect an exchange to receive price updates"""
        self.logger.info(f"📡 Connecting {exchange_name} to OrderbookManager")

        # Store exchange connector for currency discovery
        self.exchange_connectors[exchange_name] = exchange_connector

        # Start price monitoring for this exchange
        asyncio.create_task(self._monitor_exchange_prices(exchange_name, exchange_connector))
    
    async def _monitor_exchange_prices(self, exchange_name: str, exchange_connector):
        """Monitor prices from an exchange connector"""
        self.logger.info(f"🔄 Starting price monitoring for {exchange_name}")
        
        # Log available pairs
        pairs = getattr(exchange_connector, 'pair_mappings', {})
        self.logger.info(f"📋 {exchange_name} available pairs: {list(pairs.keys())}")
        
        while True:
            try:
                for pair_symbol in pairs.keys():
                    try:
                        
                        # Create TradingPair object from string
                        from connectors.base_exchange import TradingPair
                        base, quote = pair_symbol.split('/')
                        
                        if exchange_name == 'hydra':
                            trading_pair = TradingPair(symbol=pair_symbol, base=base, quote=quote)
                        else:
                            # For Binance, use BTCUSDC format
                            binance_symbol = pair_symbol.replace('/', '')
                            trading_pair = TradingPair(symbol=binance_symbol, base=base, quote=quote)
                        
                        # Get current ticker/orderbook from exchange
                        ticker = await exchange_connector.get_ticker(trading_pair)
                        if ticker:
                            # Get actual orderbook for real liquidity data
                            try:
                                orderbook = await exchange_connector.get_orderbook(trading_pair, limit=50)  # Get deeper book
                                
                                if orderbook and orderbook.bids and orderbook.asks:
                                    # Calculate deeper liquidity (top 10 levels for market impact)
                                    bid_volume = sum(level[1] for level in orderbook.bids[:10])  # Top 10 levels
                                    ask_volume = sum(level[1] for level in orderbook.asks[:10])  # Top 10 levels

                                    # Reduced logging - only show summary at debug level
                                    self.logger.debug(f"📖 {exchange_name} {pair_symbol}: {len(orderbook.bids)} bids, {len(orderbook.asks)} asks, liquidity: {bid_volume:.4f}/{ask_volume:.4f} BTC")
                                else:
                                    bid_volume = 0.0
                                    ask_volume = 0.0
                                    
                            except Exception as e:
                                self.logger.debug(f"Could not get orderbook for {pair_symbol} on {exchange_name}: {e}")
                                bid_volume = 0.0
                                ask_volume = 0.0
                            
                            # Update orderbook with ticker data and real volume (only if bid/ask are not None)
                            if ticker.bid is not None and ticker.ask is not None:
                                self.update_orderbook(
                                    exchange=exchange_name,
                                    pair=pair_symbol,
                                    best_bid=ticker.bid,
                                    best_ask=ticker.ask,
                                    bid_volume=bid_volume,
                                    ask_volume=ask_volume
                                )
                                # Price updates logged at debug level to reduce noise
                                self.logger.debug(f"📊 {exchange_name} {pair_symbol}: bid=${ticker.bid:.2f}, ask=${ticker.ask:.2f}")
                            else:
                                self.logger.debug(f"⚠️  Ticker for {pair_symbol} on {exchange_name} has None values (bid={ticker.bid}, ask={ticker.ask})")
                        else:
                            self.logger.warning(f"⚠️  No ticker data for {pair_symbol} on {exchange_name}")
                    except Exception as e:
                        self.logger.error(f"❌ Error getting ticker for {pair_symbol} on {exchange_name}: {e}")
                
                # Wait 30 seconds before next update
                await asyncio.sleep(30)
                
            except Exception as e:
                self.logger.error(f"Error monitoring {exchange_name}: {e}")
                await asyncio.sleep(10)  # Wait longer on error

    def get_statistics(self) -> Dict:
        """Get orderbook manager statistics"""
        with self.lock:
            total_pairs = sum(len(pairs) for pairs in self.orderbooks.values())
            exchanges = list(self.orderbooks.keys())
            
            return {
                'exchanges': len(exchanges),
                'exchange_list': exchanges,
                'total_pairs': total_pairs,
                'opportunities_detected': self.opportunities_detected,
                'last_opportunity_time': self.last_opportunity_time,
                'orderbooks': {
                    exchange: list(pairs.keys())
                    for exchange, pairs in self.orderbooks.items()
                }
            }
    
    def set_profit_thresholds(self, min_percentage: float = None, 
                             min_amount: float = None):
        """Update minimum profit thresholds"""
        if min_percentage is not None:
            self.min_profit_percentage = min_percentage
        if min_amount is not None:
            self.min_profit_amount = min_amount
        
        self.logger.info(
            f"Updated profit thresholds: {self.min_profit_percentage}% / ${self.min_profit_amount}"
        )
    
    def set_max_position_size(self, max_usd: float):
        """Set maximum position size per arbitrage trade"""
        self.max_position_usd = max_usd
        self.logger.info(f"Updated max position size: ${max_usd:.0f} per trade")
    
    def _get_currencies_for_pair(self, pair: str):
        """Get currency objects for a trading pair, using cache"""
        if pair in self.currency_cache:
            return self.currency_cache[pair]

        # If Hydra exchange connector is available, use its discovery method
        # This ensures we use the correct market with liquidity instead of default networks
        if 'hydra' in self.exchange_connectors:
            try:
                from connectors.base_exchange import TradingPair
                base, quote = pair.split('/')
                trading_pair = TradingPair(symbol=pair, base=base, quote=quote)

                # Use Hydra's discovery method which ranks by liquidity
                hydra_connector = self.exchange_connectors['hydra']
                currencies = hydra_connector._discover_currencies_for_pair(trading_pair)
                if currencies:
                    base_currency, quote_currency = currencies
                    self.logger.info(f"✅ Discovered currencies for {pair} from Hydra: "
                                   f"base={base_currency.network_id}, quote={quote_currency.network_id}")
                    self.currency_cache[pair] = (base_currency, quote_currency)
                    return base_currency, quote_currency
            except Exception as e:
                self.logger.warning(f"Failed to discover currencies from Hydra for {pair}: {e}")

        # Fallback to profit_validator's default logic
        if self.profit_validator:
            try:
                base_currency, quote_currency = self.profit_validator.get_currencies_for_pair(pair)
                if base_currency and quote_currency:
                    self.currency_cache[pair] = (base_currency, quote_currency)
                    return base_currency, quote_currency
            except Exception as e:
                self.logger.warning(f"Failed to get currencies for {pair}: {e}")

        return None, None
    
    def set_exchange_fees(self, exchange: str, maker_fee: float, taker_fee: float):
        """Update fee structure for an exchange"""
        self.exchange_fees[exchange] = {
            'maker': maker_fee,
            'taker': taker_fee
        }
        self.logger.info(
            f"Updated {exchange} fees: maker={maker_fee}, taker={taker_fee}"
        )
    
    async def _validate_with_estimate_order(self, pair: str, buy_exchange: str, sell_exchange: str, 
                                          buy_price: float, sell_price: float, estimated_profit: float):
        """
        Validate arbitrage opportunity using EstimateOrder before proceeding with detailed analysis
        
        Args:
            pair: Trading pair (e.g., 'BTC/USDC')
            buy_exchange: Exchange to buy from
            sell_exchange: Exchange to sell to
            buy_price: Estimated buy price from orderbook
            sell_price: Estimated sell price from orderbook
            estimated_profit: Estimated profit per BTC from orderbook analysis
        """
        try:
            self.logger.info(f"🔍 {pair}: Validating with EstimateOrder...")
            
            # Test with a reasonable amount (0.001 BTC ≈ $100)
            test_amount = 0.001
            
            # Determine which exchange is external and build the opportunity
            if buy_exchange == 'hydra':
                # Buy on Hydra, sell on external
                external_sell_price = sell_price
                external_buy_price = 0  # Not used
            else:
                # Buy on external, sell on Hydra
                external_buy_price = buy_price
                external_sell_price = 0  # Not used
            
            # Get currencies for this pair (cached if already retrieved)
            if pair not in self.currency_cache:
                base_symbol, quote_symbol = pair.split('/')
                base_currency, quote_currency = self.profit_validator.get_currencies_for_pair(pair)
                self.currency_cache[pair] = (base_currency, quote_currency)
            else:
                base_currency, quote_currency = self.currency_cache[pair]
            
            # If we couldn't get currencies, skip validation
            if not base_currency or not quote_currency:
                self.logger.warning(f"⚠️  {pair}: Skipping EstimateOrder validation (currencies not available)")
                return True  # Continue without validation
            
            # Get accurate profit calculation from EstimateOrder
            profit_analysis = self.profit_validator.calculate_arbitrage_profit(
                base_currency=base_currency,
                quote_currency=quote_currency,
                buy_exchange_price=external_buy_price,
                sell_exchange_price=external_sell_price,
                amount=test_amount,
                external_fees=self.exchange_fees.get('binance', {}).get('taker', 0.001)  # Use config or fallback
            )
            
            best_scenario = profit_analysis.get('best_scenario')
            if not best_scenario:
                self.logger.warning(f"❌ {pair}: EstimateOrder found no profitable scenario")
                return False
            
            # Compare orderbook estimate vs EstimateOrder reality
            orderbook_estimate = estimated_profit * test_amount
            estimate_order_result = best_scenario['gross_profit']
            difference = estimate_order_result - orderbook_estimate
            difference_pct = (difference / abs(orderbook_estimate) * 100) if orderbook_estimate != 0 else 0
            
            self.logger.info(f"📊 {pair}: EstimateOrder Validation Results:")
            self.logger.info(f"   📈 Orderbook estimate: ${orderbook_estimate:.2f}")
            self.logger.info(f"   🌊 EstimateOrder result: ${estimate_order_result:.2f}")
            self.logger.info(f"   📉 Difference: ${difference:.2f} ({difference_pct:+.1f}%)")
            self.logger.info(f"   🎯 Strategy: {best_scenario['name']}")
            
            # Check if still profitable after validation (use configured thresholds)
            if (estimate_order_result >= self.min_profit_amount and 
                best_scenario['profit_percentage'] >= self.min_profit_percentage):
                
                self.logger.info(f"✅ {pair}: VALIDATED - Proceeding with detailed analysis")
                return True
            else:
                self.logger.info(f"❌ {pair}: NOT PROFITABLE after EstimateOrder validation")
                self.logger.info(f"   💰 Result: ${estimate_order_result:.2f} (min: ${self.min_profit_amount})")
                self.logger.info(f"   📊 Percentage: {best_scenario['profit_percentage']:.2f}% (min: {self.min_profit_percentage}%)")
                self.logger.info(f"   🚫 REJECTING opportunity to prevent losses")
                return False
                
        except Exception as e:
            self.logger.error(f"❌ {pair}: Error validating with EstimateOrder: {e}")
            self.logger.info(f"⚠️  {pair}: Continuing without EstimateOrder validation")
            return True  # Continue with basic validation if EstimateOrder fails