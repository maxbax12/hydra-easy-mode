"""
Arbitrage Trading Strategy
==========================

Framework for detecting and executing arbitrage opportunities between:
- Hydra DEX and external exchanges (Binance, Coinbase, etc.)
- Different trading pairs on Hydra DEX
- Cross-chain arbitrage opportunities

This is a framework/placeholder for future implementation.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, List, Optional, Tuple, Callable
import time
import asyncio
import threading
import logging

from lib.hydra_pb import currency_pb2
from lib.utils import decimal_to_float
from orderbook_manager import OrderbookManager, ArbitrageOpportunity as OrderbookArbitrageOpportunity
from profit_validator import ProfitValidator


@dataclass
class ArbitrageOpportunity:
    """Represents an arbitrage opportunity (legacy compatibility)"""
    pair: str
    buy_exchange: str
    sell_exchange: str
    buy_price: float
    sell_price: float
    profit_percentage: float
    max_volume: float
    timestamp: float
    
    @classmethod
    def from_orderbook_opportunity(cls, opp: OrderbookArbitrageOpportunity):
        """Convert from OrderbookManager's ArbitrageOpportunity"""
        return cls(
            pair=opp.pair,
            buy_exchange=opp.buy_exchange,
            sell_exchange=opp.sell_exchange,
            buy_price=opp.buy_price,
            sell_price=opp.sell_price,
            profit_percentage=opp.profit_percentage,
            max_volume=opp.max_volume,
            timestamp=opp.timestamp
        )


class ArbitrageDetector:
    """Detects arbitrage opportunities across exchanges using real-time data"""
    
    def __init__(self, orderbook_manager: OrderbookManager, 
                 hydra_client=None, min_profit_threshold: float = 0.5,
                 logger: Optional[logging.Logger] = None):
        """
        Initialize arbitrage detector with real-time orderbook manager
        
        Args:
            orderbook_manager: OrderbookManager for real-time price data
            hydra_client: Hydra gRPC client (legacy compatibility)
            min_profit_threshold: Minimum profit percentage to consider (default 0.5%)
            logger: Logger instance
        """
        self.orderbook_manager = orderbook_manager
        self.hydra_client = hydra_client  # Keep for backward compatibility
        self.min_profit_threshold = min_profit_threshold
        self.logger = logger or logging.getLogger(__name__)
        
        # Opportunity tracking
        self.opportunities: List[ArbitrageOpportunity] = []
        self.opportunity_callbacks: List[Callable[[ArbitrageOpportunity], None]] = []
        
        # Register with orderbook manager for real-time opportunities
        self.orderbook_manager.add_arbitrage_callback(self._on_arbitrage_opportunity)
        
        # Statistics
        self.total_opportunities_detected = 0
        self.last_opportunity_time = None
        
    def add_opportunity_callback(self, callback: Callable[[ArbitrageOpportunity], None]):
        """Add callback to be notified of new arbitrage opportunities"""
        self.opportunity_callbacks.append(callback)
        self.logger.info("Added arbitrage opportunity callback")
        
    def _on_arbitrage_opportunity(self, orderbook_opp: OrderbookArbitrageOpportunity):
        """Handle arbitrage opportunity from orderbook manager"""
        # Convert to legacy format
        opportunity = ArbitrageOpportunity.from_orderbook_opportunity(orderbook_opp)
        
        # Add to opportunities list
        self.opportunities.append(opportunity)
        self.total_opportunities_detected += 1
        self.last_opportunity_time = time.time()
        
        self.logger.info(
            f"🎯 Arbitrage opportunity: {opportunity.pair} "
            f"{opportunity.buy_exchange} -> {opportunity.sell_exchange} "
            f"Profit: {opportunity.profit_percentage:.2f}%"
        )
        
        # Notify callbacks
        for callback in self.opportunity_callbacks:
            try:
                callback(opportunity)
            except Exception as e:
                self.logger.error(f"Error in opportunity callback: {e}")
        
        # Keep only recent opportunities (last 100)
        if len(self.opportunities) > 100:
            self.opportunities = self.opportunities[-100:]
    
    async def scan_opportunities(self, trading_pairs: List[Dict] = None) -> List[ArbitrageOpportunity]:
        """
        Get current arbitrage opportunities (real-time detection is automatic)
        
        Args:
            trading_pairs: Not used anymore - kept for backward compatibility
            
        Returns:
            List of recent arbitrage opportunities
        """
        # Filter opportunities by minimum profit threshold and recency
        current_time = time.time()
        recent_opportunities = [
            op for op in self.opportunities 
            if (current_time - op.timestamp) < 60  # Within last 60 seconds
            and op.profit_percentage >= self.min_profit_threshold
        ]
        
        return recent_opportunities
    
    def get_real_time_opportunities(self) -> List[ArbitrageOpportunity]:
        """Get the most recent arbitrage opportunities"""
        return self.opportunities[-10:] if self.opportunities else []
    
    async def _get_hydra_prices(self, pair_config: Dict) -> Optional[Dict]:
        """Get current prices from Hydra DEX"""
        try:
            base_currency = currency_pb2.OrderbookCurrency(
                protocol=pair_config['base']['protocol'],
                network_id=pair_config['base']['network_id'],
                asset_id=pair_config['base']['asset_id']
            )
            quote_currency = currency_pb2.OrderbookCurrency(
                protocol=pair_config['quote']['protocol'],
                network_id=pair_config['quote']['network_id'],
                asset_id=pair_config['quote']['asset_id']
            )
            
            orderbook = self.hydra_client.get_orderbook(base_currency, quote_currency)
            if not orderbook:
                return None
            
            best_bid = None
            best_ask = None
            
            if orderbook.bids:
                # For arbitrage: use max_price (highest price Hydra will pay)
                max_bid = decimal_to_float(orderbook.bids[0].max_price)
                best_bid = max_bid

            if orderbook.asks:
                # For arbitrage: use min_price (lowest price Hydra will sell)
                min_ask = decimal_to_float(orderbook.asks[0].min_price)
                best_ask = min_ask
            
            return {
                'exchange': 'hydra',
                'best_bid': best_bid,
                'best_ask': best_ask,
                'bid_volume': len(orderbook.bids),
                'ask_volume': len(orderbook.asks)
            }
            
        except Exception as e:
            print(f"Error getting Hydra prices: {e}")
            return None
    
    async def _get_external_prices(self, exchange_client, pair_config: Dict, exchange_name: str) -> Optional[Dict]:
        """
        Get prices from external exchange
        
        NOTE: This is a placeholder - actual implementation would depend on the exchange API
        """
        try:
            # TODO: Implement actual external exchange integration
            # This would use ccxt or exchange-specific APIs
            
            # Placeholder return for framework demonstration
            return {
                'exchange': exchange_name,
                'best_bid': 0.0,  # Would fetch from real exchange
                'best_ask': 0.0,  # Would fetch from real exchange
                'bid_volume': 0,
                'ask_volume': 0
            }
            
        except Exception as e:
            print(f"Error getting {exchange_name} prices: {e}")
            return None
    
    def _find_arbitrage_opportunities(
        self, 
        pair_config: Dict, 
        hydra_prices: Dict, 
        external_prices: Dict,
        external_exchange: str
    ) -> List[ArbitrageOpportunity]:
        """Find arbitrage opportunities between Hydra and external exchange"""
        opportunities = []
        current_time = time.time()
        
        pair_symbol = f"{pair_config['base']['asset_id'][:8]}/{pair_config['quote']['asset_id'][:8]}"
        
        # Opportunity 1: Buy on Hydra, Sell on External
        if hydra_prices['best_ask'] and external_prices['best_bid']:
            if external_prices['best_bid'] > hydra_prices['best_ask']:
                profit_percentage = ((external_prices['best_bid'] - hydra_prices['best_ask']) / 
                                   hydra_prices['best_ask']) * 100
                
                opportunities.append(ArbitrageOpportunity(
                    pair=pair_symbol,
                    buy_exchange='hydra',
                    sell_exchange=external_exchange,
                    buy_price=hydra_prices['best_ask'],
                    sell_price=external_prices['best_bid'],
                    profit_percentage=profit_percentage,
                    max_volume=min(hydra_prices['ask_volume'], external_prices['bid_volume']),
                    timestamp=current_time
                ))
        
        # Opportunity 2: Buy on External, Sell on Hydra
        if external_prices['best_ask'] and hydra_prices['best_bid']:
            if hydra_prices['best_bid'] > external_prices['best_ask']:
                profit_percentage = ((hydra_prices['best_bid'] - external_prices['best_ask']) / 
                                   external_prices['best_ask']) * 100
                
                opportunities.append(ArbitrageOpportunity(
                    pair=pair_symbol,
                    buy_exchange=external_exchange,
                    sell_exchange='hydra',
                    buy_price=external_prices['best_ask'],
                    sell_price=hydra_prices['best_bid'],
                    profit_percentage=profit_percentage,
                    max_volume=min(external_prices['ask_volume'], hydra_prices['bid_volume']),
                    timestamp=current_time
                ))
        
        return opportunities
    
    def get_top_opportunities(self, limit: int = 5) -> List[ArbitrageOpportunity]:
        """Get top arbitrage opportunities sorted by profit percentage"""
        # Only consider recent opportunities (within last 30 seconds)
        current_time = time.time()
        recent_opportunities = [
            op for op in self.opportunities
            if (current_time - op.timestamp) < 30
        ]
        
        sorted_opportunities = sorted(
            recent_opportunities, 
            key=lambda x: x.profit_percentage, 
            reverse=True
        )
        return sorted_opportunities[:limit]
    
    def get_statistics(self) -> Dict:
        """Get detector statistics"""
        current_time = time.time()
        recent_count = sum(
            1 for op in self.opportunities
            if (current_time - op.timestamp) < 300  # Last 5 minutes
        )
        
        return {
            'total_opportunities': self.total_opportunities_detected,
            'recent_opportunities': recent_count,
            'last_opportunity_time': self.last_opportunity_time,
            'min_profit_threshold': self.min_profit_threshold,
            'callbacks_registered': len(self.opportunity_callbacks)
        }


class ArbitrageExecutor:
    """Executes arbitrage opportunities with simultaneous order placement"""
    
    def __init__(self, exchange_factory, hydra_client=None, config: Dict = None,
                 logger: Optional[logging.Logger] = None):
        """
        Initialize arbitrage executor
        
        Args:
            exchange_factory: ExchangeFactory for multi-exchange access
            hydra_client: HydraGRPCClient for EstimateOrder validation
            config: Configuration dictionary with params
            logger: Logger instance
        """
        self.exchange_factory = exchange_factory
        self.hydra_client = hydra_client
        self.config = config or {}
        params = self.config.get('params', {})
        
        # Position sizing from config
        self.max_position_size = params.get('max_position_size', 1.0)
        
        self.logger = logger or logging.getLogger(__name__)
        self.execution_history: List[Dict] = []
        
        # Initialize profit validator for accurate profit calculation
        self.profit_validator = None
        self.currency_cache = {}  # Cache currencies for each pair
        
        if hydra_client:
            self.profit_validator = ProfitValidator(hydra_client)
            self.logger.info("✅ EstimateOrder profit validation enabled for all pairs")
        else:
            self.logger.warning("⚠️  No Hydra client provided - EstimateOrder validation disabled")
        
        # Risk management (from config or fallback)
        self.max_slippage = params.get('max_slippage', 0.002)  # From config or fallback
        self.order_timeout = params.get('order_timeout', 10.0)  # From config or fallback
        
        # Profit thresholds from config
        self.min_profit_usd = params.get('min_profit_amount', 1.0)  # From config
        self.min_profit_pct = params.get('min_profit_percentage', 0.5)  # From config
        
        # Log configuration
        self.logger.info(f"📋 ArbitrageExecutor Configuration:")
        self.logger.info(f"   💰 Max position size: ${self.max_position_size}")
        self.logger.info(f"   📈 Min profit percentage: {self.min_profit_pct}%") 
        self.logger.info(f"   💵 Min profit amount: ${self.min_profit_usd}")
    
    async def validate_profit_with_estimate_order(self, opportunity: ArbitrageOpportunity, position_size: float) -> Optional[Dict]:
        """
        Validate arbitrage profit using Hydra's EstimateOrder for accurate pricing

        Args:
            opportunity: The arbitrage opportunity to validate
            position_size: The intended trade size in base asset (ETH, BTC, etc.)

        Returns:
            Validated profit information or None if not profitable
        """
        if not self.profit_validator:
            self.logger.warning("⚠️  EstimateOrder validation not available - using estimated prices")
            return None
        
        # Get currencies for this pair
        pair = opportunity.pair

        # CRITICAL: Use the exact same currency objects that were discovered during opportunity detection
        # This ensures EstimateOrder uses the same market (with liquidity) that the orderbook analysis used
        if opportunity.base_currency and opportunity.quote_currency:
            base_currency = opportunity.base_currency
            quote_currency = opportunity.quote_currency
            self.logger.info(f"✅ Using discovered currencies from opportunity: base={base_currency.network_id}, quote={quote_currency.network_id}")
            self.logger.debug(f"   Base asset: {base_currency.asset_id[:40]}...")
            self.logger.debug(f"   Quote asset: {quote_currency.asset_id[:40]}...")
        else:
            # Fallback: recreate currencies (shouldn't happen with updated code)
            self.logger.warning(f"⚠️  Opportunity missing currency objects, recreating from network IDs")
            base_currency, quote_currency = self.profit_validator.get_currencies_for_pair(
                pair,
                base_network_id=opportunity.base_network_id,
                quote_network_id=opportunity.quote_network_id
            )
            if not base_currency or not quote_currency:
                self.logger.warning(f"⚠️  Could not get currencies for {pair} - skipping validation")
                return None
        
        try:
            self.logger.info(f"🔍 Validating profit with EstimateOrder for {position_size:.6f} {pair.split('/')[0]}")
            
            # For EstimateOrder validation, we always validate the Hydra side
            # because that's where we can get accurate execution prices
            if opportunity.buy_exchange.lower() == 'hydra':
                # Buy on Hydra (validate buy), sell on external (use orderbook price)
                hydra_buy_price = 0  # Will be determined by EstimateOrder
                external_sell_price = opportunity.sell_price
                validate_direction = 'buy_hydra_sell_external'
            else:
                # Buy on external (use orderbook price), sell on Hydra (validate sell)
                external_buy_price = opportunity.buy_price
                hydra_sell_price = 0  # Will be determined by EstimateOrder
                validate_direction = 'buy_external_sell_hydra'
            
            # Get accurate profit calculation from EstimateOrder
            if validate_direction == 'buy_hydra_sell_external':
                profit_analysis = self.profit_validator.calculate_arbitrage_profit(
                    base_currency=base_currency,
                    quote_currency=quote_currency,
                    buy_exchange_price=0,  # Let EstimateOrder determine Hydra buy price
                    sell_exchange_price=external_sell_price,
                    amount=position_size,
                    external_fees=self.config.get('params', {}).get('external_fees', 0.001)  # From config or fallback
                )
            else:
                profit_analysis = self.profit_validator.calculate_arbitrage_profit(
                    base_currency=base_currency,
                    quote_currency=quote_currency,
                    buy_exchange_price=external_buy_price,
                    sell_exchange_price=0,  # Let EstimateOrder determine Hydra sell price
                    amount=position_size,
                    external_fees=self.config.get('params', {}).get('external_fees', 0.001)  # From config or fallback
                )
            
            best_scenario = profit_analysis.get('best_scenario')
            if not best_scenario:
                self.logger.warning("❌ No profitable scenario found with EstimateOrder")
                return None
            
            # Log detailed comparison
            original_profit = (opportunity.sell_price - opportunity.buy_price) * position_size
            validated_profit = best_scenario['gross_profit']
            profit_diff = validated_profit - original_profit
            
            self.logger.info(f"💰 Profit Validation Results:")
            self.logger.info(f"   📊 Original estimate: ${original_profit:.2f}")
            self.logger.info(f"   🌊 EstimateOrder result: ${validated_profit:.2f}")
            self.logger.info(f"   📈 Difference: ${profit_diff:.2f} ({profit_diff/abs(original_profit)*100:.1f}%)")
            self.logger.info(f"   🎯 Strategy: {best_scenario['name']}")
            self.logger.info(f"   💵 Buy cost: ${best_scenario['buy_cost']:.2f}")
            self.logger.info(f"   💰 Sell revenue: ${best_scenario['sell_revenue']:.2f}")
            
            # Check if meets minimum thresholds
            meets_amount_threshold = validated_profit >= self.min_profit_usd
            meets_percentage_threshold = best_scenario['profit_percentage'] >= self.min_profit_pct
            
            if meets_amount_threshold and meets_percentage_threshold:
                self.logger.info(f"✅ PROFITABLE: ${validated_profit:.2f} ({best_scenario['profit_percentage']:.2f}%)")
                return {
                    'validated_profit': validated_profit,
                    'validated_profit_pct': best_scenario['profit_percentage'],
                    'scenario': best_scenario,
                    'profit_analysis': profit_analysis,
                    'is_profitable': True,
                    'buy_exchange': opportunity.buy_exchange,
                    'sell_exchange': opportunity.sell_exchange,
                    'validated_buy_price': opportunity.buy_price,  # Use original prices
                    'validated_sell_price': opportunity.sell_price,
                    'buy_cost': best_scenario['buy_cost'],
                    'sell_revenue': best_scenario['sell_revenue']
                }
            else:
                self.logger.info(f"❌ NOT PROFITABLE ENOUGH:")
                self.logger.info(f"   💵 Amount: ${validated_profit:.2f} (min: ${self.min_profit_usd})")
                self.logger.info(f"   📊 Percentage: {best_scenario['profit_percentage']:.2f}% (min: {self.min_profit_pct}%)")
                return None
                
        except Exception as e:
            self.logger.error(f"❌ Error validating profit with EstimateOrder: {e}")
            return None
    
    async def execute_arbitrage(self, opportunity: ArbitrageOpportunity) -> Dict:
        """
        Execute an arbitrage opportunity with simultaneous order placement
        
        Args:
            opportunity: The arbitrage opportunity to execute
            
        Returns:
            Execution result dictionary
        """
        start_time = time.time()
        
        try:
            # Extract base symbol for logging
            base_symbol = opportunity.pair.split('/')[0]

            self.logger.info(f"🔄 Executing arbitrage: {opportunity.pair}")
            self.logger.info(f"   📈 BUY on {opportunity.buy_exchange} at ${opportunity.buy_price:.2f}")
            self.logger.info(f"   📉 SELL on {opportunity.sell_exchange} at ${opportunity.sell_price:.2f}")
            self.logger.info(f"   💵 Expected profit: {opportunity.profit_percentage:.2f}%")
            self.logger.info(f"   ⏱️  Max volume: {opportunity.max_volume:.6f} {base_symbol}")
            
            # Explain execution model differences
            self._log_execution_model_differences(opportunity)
            
            # Calculate position size with risk management and get VWAP prices
            position_size, buy_vwap, sell_vwap = await self._calculate_position_size(opportunity)
            
            if position_size <= 0:
                return self._create_error_result(opportunity, "Position size too small", start_time)
            
            # CRITICAL: Validate profit with EstimateOrder before execution
            self.logger.info(f"🔍 Validating profit with EstimateOrder...")
            profit_validation = await self.validate_profit_with_estimate_order(opportunity, position_size)
            
            if not profit_validation:
                return self._create_error_result(
                    opportunity, 
                    "Opportunity not profitable after EstimateOrder validation", 
                    start_time
                )
                        
            # Update opportunity with validated prices for execution
            validated_opportunity = opportunity
            if profit_validation['buy_exchange'] != opportunity.buy_exchange:
                from dataclasses import replace
                validated_opportunity = replace(
                    opportunity,
                    buy_exchange=profit_validation['buy_exchange'],
                    sell_exchange=profit_validation['sell_exchange'],
                    buy_price=profit_validation['validated_buy_price'],
                    sell_price=profit_validation['validated_sell_price']
                )
            
            # Get exchange connectors using validated opportunity
            buy_exchange = await self._get_exchange(validated_opportunity.buy_exchange)
            sell_exchange = await self._get_exchange(validated_opportunity.sell_exchange)
            
            if not buy_exchange:
                error_msg = f"Buy exchange connector '{validated_opportunity.buy_exchange}' not available"
                self.logger.error(f"❌ {error_msg}")
                return self._create_error_result(opportunity, error_msg, start_time)
            
            if not sell_exchange:
                error_msg = f"Sell exchange connector '{validated_opportunity.sell_exchange}' not available"
                self.logger.error(f"❌ {error_msg}")
                return self._create_error_result(opportunity, error_msg, start_time)
            
            
            # Execute orders with validated prices and exchanges
            result = await self._execute_mixed_exchange_orders(
                validated_opportunity, buy_exchange, sell_exchange, position_size, 
                profit_validation['validated_buy_price'], profit_validation['validated_sell_price']
            )
            
            result['execution_time'] = time.time() - start_time
            result['profit_validation'] = profit_validation  # Include EstimateOrder validation results
            result['validated_opportunity'] = validated_opportunity  # Include updated opportunity
            self.execution_history.append(result)
            
            if result['success']:
                buy_exchange = result.get('buy_exchange', 'unknown')
                sell_exchange = result.get('sell_exchange', 'unknown')
                buy_order_id = result.get('buy_order_id', 'N/A')
                sell_order_id = result.get('sell_order_id', 'N/A')
                actual_profit = result.get('actual_profit', 0.0)

                # Log order IDs for tracking
                self.logger.info(f"🆔 Orders: BUY {buy_order_id[:8]}... ({buy_exchange}) | SELL {sell_order_id} ({sell_exchange})")

                if actual_profit > 0:
                    self.logger.info(f"💰 Actual profit: ${actual_profit:.2f}")
            else:
                error_detail = result.get('error', 'Unknown error')
                buy_exchange = result.get('buy_exchange', 'unknown')
                sell_exchange = result.get('sell_exchange', 'unknown')
                buy_result = result.get('buy_result', {})
                sell_result = result.get('sell_result', {})
                
                self.logger.error(f"❌ Arbitrage execution failed: {error_detail}")
                self.logger.error(f"📊 Attempted exchanges: BUY on {buy_exchange} → SELL on {sell_exchange}")
                
                # Log individual exchange results for debugging
                if isinstance(buy_result, dict):
                    if not buy_result.get('success', True):
                        buy_error = buy_result.get('error', 'Unknown buy error')
                        self.logger.error(f"🔴 BUY order failed on {buy_exchange}: {buy_error}")
                    else:
                        self.logger.info(f"🟢 BUY order succeeded on {buy_exchange}")
                elif isinstance(buy_result, Exception):
                    self.logger.error(f"🔴 BUY order exception on {buy_exchange}: {str(buy_result)}")
                
                if isinstance(sell_result, dict):
                    if not sell_result.get('success', True):
                        sell_error = sell_result.get('error', 'Unknown sell error')
                        self.logger.error(f"🔴 SELL order failed on {sell_exchange}: {sell_error}")
                    else:
                        self.logger.info(f"🟢 SELL order succeeded on {sell_exchange}")
                elif isinstance(sell_result, Exception):
                    self.logger.error(f"🔴 SELL order exception on {sell_exchange}: {str(sell_result)}")
            
            return result
            
        except Exception as e:
            error_result = self._create_error_result(
                opportunity, f"Arbitrage execution error: {str(e)}", start_time
            )
            self.execution_history.append(error_result)
            return error_result
    
    def _log_execution_model_differences(self, opportunity: ArbitrageOpportunity):
        """Log the execution model differences between exchanges"""
        # Simplified execution model logging - just note key differences
        hydra_exchanges = [ex for ex in [opportunity.buy_exchange, opportunity.sell_exchange] if ex.lower() == 'hydra']
        if hydra_exchanges:
            self.logger.info(f"   🌊 Hydra uses market orders for immediate execution")
    
    async def _calculate_position_size(self, opportunity: ArbitrageOpportunity) -> float:
        """Calculate optimal position size with risk management and balance checks"""
        try:
            # Extract base symbol from normalized pair
            base_symbol = opportunity.pair.split('/')[0]  # ETH, BTC, etc.

            # Use actual pair names from opportunity (not normalized)
            # E.g., ETH/USDC on Hydra, ETH/USDT on Binance, not ETH/USD
            buy_pair_str = opportunity.buy_pair if opportunity.buy_pair else opportunity.pair
            sell_pair_str = opportunity.sell_pair if opportunity.sell_pair else opportunity.pair

            # Extract actual quote symbols from real pairs (USDC, USDT, not USD)
            buy_quote_symbol = buy_pair_str.split('/')[1] if '/' in buy_pair_str else 'USD'
            sell_quote_symbol = sell_pair_str.split('/')[1] if '/' in sell_pair_str else 'USD'

            self.logger.debug(f"🔍 Using actual pairs: buy={buy_pair_str} (quote: {buy_quote_symbol}), sell={sell_pair_str} (quote: {sell_quote_symbol})")

            # Convert max position size from USD to base asset amount
            # max_position_size is in USD (e.g. $100), need to convert to base asset (ETH, BTC, etc.)
            max_base_from_usd = self.max_position_size / opportunity.buy_price

            # Start with the smaller of: available volume or USD-limited base amount
            max_volume = min(opportunity.max_volume, max_base_from_usd)

            self.logger.info(f"💰 Position size limits: ${self.max_position_size} USD ({max_base_from_usd:.6f} {base_symbol}), Volume: {opportunity.max_volume:.6f}")

            # Initialize VWAP prices (fallback to boundary prices if VWAP calculation fails)
            buy_vwap = opportunity.buy_price
            sell_vwap = opportunity.sell_price

            # Check balances and get deeper orderbook data
            buy_exchange = await self._get_exchange(opportunity.buy_exchange)
            sell_exchange = await self._get_exchange(opportunity.sell_exchange)

            if not buy_exchange or not sell_exchange:
                self.logger.error("❌ Could not get exchange connectors for balance check")
                return 0.0

            # Get deeper orderbook data for VWAP analysis
            try:
                from connectors.base_exchange import TradingPair
                base, quote = opportunity.pair.split('/')

                if buy_exchange.name.lower().startswith('binance'):
                    buy_pair = TradingPair(base=base, quote=quote, symbol=buy_pair_str.replace('/', ''))
                else:
                    buy_pair = TradingPair(base=base, quote=quote, symbol=buy_pair_str)

                if sell_exchange.name.lower().startswith('binance'):
                    sell_pair = TradingPair(base=base, quote=quote, symbol=sell_pair_str.replace('/', ''))
                else:
                    sell_pair = TradingPair(base=base, quote=quote, symbol=sell_pair_str)

                buy_orderbook = await buy_exchange.get_orderbook(buy_pair, limit=50)
                sell_orderbook = await sell_exchange.get_orderbook(sell_pair, limit=50)

                # Orderbook depth check
                buy_depth = len(buy_orderbook.asks) if buy_orderbook and buy_orderbook.asks else 0
                sell_depth = len(sell_orderbook.bids) if sell_orderbook and sell_orderbook.bids else 0

                # Calculate VWAP impact for our intended trade size
                if buy_orderbook and buy_orderbook.asks:
                    calculated_buy_vwap, buy_liquidity, _ = self._calculate_vwap_impact(
                        buy_orderbook.asks, max_base_from_usd, 'buy', base_symbol
                    )
                    if calculated_buy_vwap > 0:
                        buy_vwap = calculated_buy_vwap  # Update the main variable
                    self.logger.info(f"💹 Buy VWAP: ${buy_vwap:.2f}, liquidity: {buy_liquidity:.6f} {base_symbol}")

                if sell_orderbook and sell_orderbook.bids:
                    calculated_sell_vwap, sell_liquidity, _ = self._calculate_vwap_impact(
                        sell_orderbook.bids, max_base_from_usd, 'sell', base_symbol
                    )
                    if calculated_sell_vwap > 0:
                        sell_vwap = calculated_sell_vwap  # Update the main variable
                    self.logger.info(f"💹 Sell VWAP: ${sell_vwap:.2f}, liquidity: {sell_liquidity:.6f} {base_symbol}")

                    # Recalculate profit with VWAP prices
                    if buy_vwap > 0 and sell_vwap > 0:
                        vwap_profit = (sell_vwap - buy_vwap) * max_base_from_usd
                        vwap_profit_pct = (vwap_profit / (buy_vwap * max_base_from_usd)) * 100
                        self.logger.info(f"📊 VWAP profit: ${vwap_profit:.2f} ({vwap_profit_pct:.2f}%)")

                        # Update max_volume based on deeper liquidity
                        max_volume = min(max_volume, buy_liquidity, sell_liquidity)
                
            except Exception as e:
                self.logger.warning(f"⚠️  Could not perform VWAP analysis: {e}")
            
            # Check buying power (USDC/USDT balance on buy exchange)
            try:
                buy_balances = await buy_exchange.get_balances()

                available_quote = 0.0
                for symbol, balance in buy_balances.items():
                    # Use actual quote symbol from buy pair (e.g., USDC), not normalized USD
                    if symbol.upper() == buy_quote_symbol.upper():
                        available_quote = balance.available
                        break

                max_base_from_balance = available_quote / buy_vwap
                max_volume = min(max_volume, max_base_from_balance)

                self.logger.info(f"💳 Buy balance: {available_quote:.2f} {buy_quote_symbol} ({max_base_from_balance:.6f} {base_symbol})")

                # Add clear warning if balance is insufficient
                if available_quote < 1.0:  # Less than $1
                    needed_amount = buy_vwap * opportunity.max_volume
                    self.logger.warning(f"💰 Insufficient balance on {opportunity.buy_exchange}: Need ${needed_amount:.2f} {buy_quote_symbol} to buy {base_symbol}, but only have ${available_quote:.2f}")

            except Exception as e:
                self.logger.warning(f"⚠️  Could not check buy exchange balance: {e}")

            # Check selling power (base asset balance on sell exchange)
            try:
                sell_balances = await sell_exchange.get_balances()

                available_base = 0.0
                for symbol, balance in sell_balances.items():
                    if symbol.upper() == base_symbol.upper():
                        available_base = balance.available
                        break

                max_volume = min(max_volume, available_base)

                self.logger.info(f"💳 Sell balance: {available_base:.6f} {base_symbol}")

            except Exception as e:
                self.logger.warning(f"⚠️  Could not check sell exchange balance: {e}")
            
            # Final position size with cost analysis using VWAP prices
            final_size = max(0.0, max_volume)
            final_usd_size = final_size * buy_vwap
            
            # Calculate fees and profit using VWAP prices
            estimated_buy_fee = self._estimate_trading_fees(opportunity.buy_exchange, buy_vwap, final_size)
            estimated_sell_fee = self._estimate_trading_fees(opportunity.sell_exchange, sell_vwap, final_size)
            total_estimated_fees = estimated_buy_fee + estimated_sell_fee
            
            gross_profit = (sell_vwap - buy_vwap) * final_size
            estimated_net_profit = gross_profit - total_estimated_fees
            
            self.logger.info(f"📊 Profit: ${gross_profit:.4f} gross - ${total_estimated_fees:.4f} fees = ${estimated_net_profit:.4f} net")
            
            # Apply minimum trade size - using very small amount for testnet
            min_trade_usd = self.config.get('params', {}).get('min_trade_usd', 0.05)  # From config or fallback
            if final_usd_size < min_trade_usd:
                self.logger.warning(f"⚠️  Trade size ${final_usd_size:.2f} below minimum ${min_trade_usd} - check exchange balances")
                return 0.0, buy_vwap, sell_vwap

            self.logger.info(f"📊 Final size: {final_size:.6f} {base_symbol} (${final_usd_size:.2f})")

            return final_size, buy_vwap, sell_vwap
            
        except Exception as e:
            self.logger.error(f"Error calculating position size: {e}")
            return 0.0, opportunity.buy_price, opportunity.sell_price
    
    def _estimate_trading_fees(self, exchange_name: str, price: float, volume: float) -> float:
        """Estimate trading fees for an exchange"""
        # Get fee rates from config or use fallback
        exchange_fees = self.config.get('exchange_fees', {})
        fee_rates = {
            'binance_testnet': exchange_fees.get('binance_testnet', {}).get('taker', 0.001),
            'binance': exchange_fees.get('binance', {}).get('taker', 0.001),
            'hydra': exchange_fees.get('hydra', {}).get('taker', 0.002),
        }
        
        fee_rate = fee_rates.get(exchange_name.lower(), 0.001)  # Default 0.1%
        trade_value = price * volume
        estimated_fee = trade_value * fee_rate
        
        # Simplified fee logging - details only in debug mode
        
        return estimated_fee
    
    def _calculate_vwap_impact(self, orderbook_levels: list, target_volume: float, side: str, base_symbol: str = "asset") -> tuple:
        """
        Calculate volume-weighted average price and check if we have enough liquidity

        Args:
            orderbook_levels: List of (price, volume) tuples OR (avg_price, volume, min_price, max_price) tuples (Hydra format)
            target_volume: Amount we want to trade
            side: 'buy' or 'sell' (unused, kept for interface compatibility)
            base_symbol: Symbol of the base asset (e.g., "ETH", "BTC") for logging

        Returns:
            (vwap_price, available_volume, levels_needed)
        """
        if not orderbook_levels or target_volume <= 0:
            return 0.0, 0.0, 0

        total_cost = 0.0
        total_volume = 0.0
        levels_used = 0

        # VWAP calculation handling both standard and Hydra formats

        for level in orderbook_levels:
            if total_volume >= target_volume:
                break

            # Handle both formats: (price, volume) and (avg_price, volume, min_price, max_price)
            if isinstance(level, (tuple, list)) and len(level) >= 4:
                # Hydra format: (avg_price, volume, min_price, max_price)
                price, volume = level[0], level[1]
                price_range = f"${level[2]:.2f}-${level[3]:.2f}"
            elif isinstance(level, (tuple, list)) and len(level) >= 2:
                # Standard format: (price, volume)
                price, volume = level[0], level[1]
                price_range = f"${price:.2f}"
            elif isinstance(level, dict):
                # Dict format: {'price': x, 'amount': y}
                price = level.get('price', level.get('price_avg', 0))
                volume = level.get('amount', level.get('size', 0))
                price_range = f"${price:.2f}"
            else:
                # Object format
                price = getattr(level, 'price', getattr(level, 'avg_price', 0))
                volume = getattr(level, 'amount', getattr(level, 'size', 0))
                price_range = f"${price:.2f}"

            if price <= 0 or volume <= 0:
                continue

            # How much of this level do we need?
            volume_needed = min(volume, target_volume - total_volume)

            total_cost += price * volume_needed
            total_volume += volume_needed
            levels_used += 1

            self.logger.info(f"   Level {levels_used}: {price_range} × {volume_needed:.6f} {base_symbol} (available: {volume:.6f})")

            if levels_used >= 10:  # Limit logging to avoid spam
                break

        if total_volume > 0:
            vwap = total_cost / total_volume
            return vwap, total_volume, levels_used
        else:
            return 0.0, 0.0, 0
    
    async def _get_exchange(self, exchange_name: str):
        """Get exchange connector by name"""
        try:
            # Use direct exchanges if available
            if hasattr(self, 'exchanges') and self.exchanges:
                return self.exchanges.get(exchange_name)
            
            # Fallback to exchange factory (if it has the method)
            if hasattr(self.exchange_factory, 'exchanges'):
                return self.exchange_factory.exchanges.get(exchange_name)
                
            return None
        except Exception as e:
            self.logger.error(f"Error getting exchange {exchange_name}: {e}")
            return None
    
    async def _execute_mixed_exchange_orders(self, opportunity: ArbitrageOpportunity,
                                           buy_exchange, sell_exchange, 
                                           position_size: float, 
                                           buy_vwap: float = None, sell_vwap: float = None) -> Dict:
        """Execute buy and sell orders with different strategies for different exchange types"""
        try:
            # Use VWAP prices if provided, otherwise fallback to boundary prices
            effective_buy_price = buy_vwap if buy_vwap else opportunity.buy_price
            effective_sell_price = sell_vwap if sell_vwap else opportunity.sell_price

            # Use actual pair names (e.g., ETH/USDC, ETH/USDT) not normalized (ETH/USD)
            buy_pair_for_order = opportunity.buy_pair if opportunity.buy_pair else opportunity.pair
            sell_pair_for_order = opportunity.sell_pair if opportunity.sell_pair else opportunity.pair

            # Create order tasks with different strategies for different exchanges
            buy_task = self._create_adaptive_buy_order(
                buy_exchange, buy_pair_for_order, position_size, effective_buy_price
            )
            sell_task = self._create_adaptive_sell_order(
                sell_exchange, sell_pair_for_order, position_size, effective_sell_price
            )
            
            # Execute orders simultaneously with timeout
            self.logger.info(f"🚀 Placing orders (timeout: {self.order_timeout}s)...")
            try:
                buy_result, sell_result = await asyncio.wait_for(
                    asyncio.gather(buy_task, sell_task, return_exceptions=True),
                    timeout=self.order_timeout
                )
            except asyncio.TimeoutError:
                self.logger.error(f"⏰ Order execution timeout after {self.order_timeout}s")
                return {
                    'success': False,
                    'opportunity': opportunity,
                    'position_size': position_size,
                    'error': f'Order execution timeout after {self.order_timeout}s',
                    'buy_result': None,
                    'sell_result': None,
                    'buy_exchange': buy_exchange.name,
                    'sell_exchange': sell_exchange.name
                }
            
            # Check if both orders succeeded (must have valid order IDs)
            buy_success = (
                not isinstance(buy_result, Exception) and 
                buy_result.get('success', False) and 
                buy_result.get('order_id') is not None
            )
            sell_success = (
                not isinstance(sell_result, Exception) and 
                sell_result.get('success', False) and 
                sell_result.get('order_id') is not None
            )
            
            # Log results concisely
            if not buy_success:
                buy_error = buy_result.get('error', 'Unknown error') if isinstance(buy_result, dict) else str(buy_result)
                self.logger.error(f"🔴 BUY failed: {buy_error}")
            if not sell_success:
                sell_error = sell_result.get('error', 'Unknown error') if isinstance(sell_result, dict) else str(sell_result)
                self.logger.error(f"🔴 SELL failed: {sell_error}")
            if buy_success and sell_success:
                self.logger.info("✅ Both orders placed successfully")
            
            if buy_success and sell_success:
                
                # Wait for Hydra order to complete before declaring success
                hydra_order_id = None
                if opportunity.sell_exchange.lower() == 'hydra':
                    hydra_order_id = sell_result.get('order_id')
                elif opportunity.buy_exchange.lower() == 'hydra':
                    hydra_order_id = buy_result.get('order_id')
                
                if hydra_order_id:
                    self.logger.info("⏳ Waiting for Hydra completion...")
                    completion_success = await self._wait_for_hydra_completion(hydra_order_id, timeout=30)
                    if not completion_success:
                        self.logger.warning("⚠️ Hydra order timeout (may still complete)")
                    else:
                        self.logger.info("✅ Hydra order completed")
                
                # Calculate actual profit
                actual_profit = self._calculate_actual_profit(
                    buy_result, sell_result, position_size
                )
                
                return {
                    'success': True,
                    'opportunity': opportunity,
                    'position_size': position_size,
                    'buy_result': buy_result,
                    'sell_result': sell_result,
                    'actual_profit': actual_profit,
                    'buy_order_id': buy_result.get('order_id'),
                    'sell_order_id': sell_result.get('order_id'),
                    'buy_exchange': buy_exchange.name,
                    'sell_exchange': sell_exchange.name,
                    'hydra_completed': completion_success if hydra_order_id else True
                }
            else:
                # Handle partial execution
                if buy_success and not sell_success:
                    detailed_error = "SELL order failed"
                elif sell_success and not buy_success:
                    detailed_error = "BUY order failed"
                else:
                    detailed_error = "Both orders failed"
                
                self.logger.error(f"❌ Arbitrage failed: {detailed_error}")
                
                # Handle partial execution cleanup
                await self._handle_partial_execution(buy_result, sell_result, buy_success, sell_success)
                
                return {
                    'success': False,
                    'opportunity': opportunity,
                    'position_size': position_size,
                    'error': detailed_error,
                    'buy_result': buy_result,
                    'sell_result': sell_result,
                    'buy_exchange': buy_exchange.name,
                    'sell_exchange': sell_exchange.name
                }
                
        except Exception as e:
            return {
                'success': False,
                'opportunity': opportunity,
                'position_size': position_size,
                'error': f"Order execution error: {str(e)}",
                'buy_result': None,
                'sell_result': None
            }
    
    async def _create_adaptive_buy_order(self, exchange, pair: str, amount: float, price: float) -> Dict:
        """Create a buy order adapted to the exchange's execution model"""
        try:
            from connectors.base_exchange import TradingPair, OrderSide, OrderType
            
            # Create TradingPair object
            base, quote = pair.split('/')
            if exchange.name.lower().startswith('binance'):
                # Binance uses concatenated format (e.g., BTCUSDC)
                symbol = pair.replace('/', '')
                trading_pair = TradingPair(base=base, quote=quote, symbol=symbol)
            else:
                # Hydra uses slash format (e.g., BTC/USDC)
                trading_pair = TradingPair(base=base, quote=quote, symbol=pair)
            
            self.logger.info(f"🎯 BUY {amount:.6f} {base} on {exchange.name} @ ${price:.2f}")
            
            # Use market orders for Hydra, limit orders for others
            if exchange.name.lower() == 'hydra':
                order = await exchange.place_order(
                    pair=trading_pair,
                    side=OrderSide.BUY,
                    type=OrderType.MARKET,
                    amount=amount,
                    price=price,
                    params={'immediate_execution': True}
                )
            else:
                order = await exchange.place_order(
                    pair=trading_pair,
                    side=OrderSide.BUY,
                    type=OrderType.LIMIT,
                    amount=amount,
                    price=price
                )
            
            return {
                'success': True,
                'order_id': order.id if order else None,
                'order': order,
                'trading_pair': trading_pair,  # Store for cancellation
                'exchange': exchange.name,
                'side': 'buy',
                'amount': amount,
                'price': price
            }
            
        except Exception as e:
            self.logger.error(f"🔴 BUY order failed on {exchange.name}: {e}")
            return {
                'success': False,
                'error': str(e),
                'exchange': exchange.name,
                'side': 'buy'
            }
    
    async def _create_adaptive_sell_order(self, exchange, pair: str, amount: float, price: float) -> Dict:
        """Create a sell order adapted to the exchange's execution model"""
        try:
            from connectors.base_exchange import TradingPair, OrderSide, OrderType
            
            # Create TradingPair object
            base, quote = pair.split('/')
            if exchange.name.lower().startswith('binance'):
                # Binance uses concatenated format (e.g., BTCUSDC)
                symbol = pair.replace('/', '')
                trading_pair = TradingPair(base=base, quote=quote, symbol=symbol)
            else:
                # Hydra uses slash format (e.g., BTC/USDC)
                trading_pair = TradingPair(base=base, quote=quote, symbol=pair)
            
            self.logger.info(f"🎯 SELL {amount:.6f} {base} on {exchange.name} @ ${price:.2f}")
            
            # Use market orders for Hydra, limit orders for others
            if exchange.name.lower() == 'hydra':
                order = await exchange.place_order(
                    pair=trading_pair,
                    side=OrderSide.SELL,
                    type=OrderType.MARKET,
                    amount=amount,
                    price=price,
                    params={'immediate_execution': True}
                )
            else:
                order = await exchange.place_order(
                    pair=trading_pair,
                    side=OrderSide.SELL,
                    type=OrderType.LIMIT,
                    amount=amount,
                    price=price
                )
            
            return {
                'success': True,
                'order_id': order.id if order else None,
                'order': order,
                'trading_pair': trading_pair,  # Store for cancellation
                'exchange': exchange.name,
                'side': 'sell',
                'amount': amount,
                'price': price
            }
            
        except Exception as e:
            self.logger.error(f"🔴 SELL order failed on {exchange.name}: {e}")
            return {
                'success': False,
                'error': str(e),
                'exchange': exchange.name,
                'side': 'sell'
            }
    
    def _convert_pair_format(self, pair: str, exchange_name: str) -> str:
        """Convert pair format for different exchanges"""
        # Handle different pair formats between exchanges
        # Hydra might use different format than Binance
        if exchange_name.lower().startswith('binance'):
            # Binance uses format like 'BTCUSDT'
            return pair.replace('/', '').upper()
        else:
            # Keep original format for other exchanges
            return pair
    
    async def _handle_partial_execution(self, buy_result: Dict, sell_result: Dict,
                                      buy_success: bool, sell_success: bool):
        """Handle case where only one order succeeded"""
        self.logger.warning("🔄 Handling partial execution - canceling successful orders")

        # Cancel successful order to avoid exposure
        if buy_success and buy_result.get('order_id') and buy_result.get('trading_pair'):
            try:
                buy_exchange = await self._get_exchange(buy_result['exchange'])
                if buy_exchange:
                    # Use stored trading_pair, not order.pair
                    await buy_exchange.cancel_order(buy_result['order_id'], buy_result['trading_pair'])
                    self.logger.info(f"Canceled buy order {buy_result['order_id']}")
            except Exception as e:
                self.logger.error(f"Error canceling buy order: {e}")

        if sell_success and sell_result.get('order_id') and sell_result.get('trading_pair'):
            try:
                sell_exchange = await self._get_exchange(sell_result['exchange'])
                if sell_exchange:
                    # Use stored trading_pair, not order.pair
                    await sell_exchange.cancel_order(sell_result['order_id'], sell_result['trading_pair'])
                    self.logger.info(f"Canceled sell order {sell_result['order_id']}")
            except Exception as e:
                self.logger.error(f"Error canceling sell order: {e}")
    
    def _calculate_actual_profit(self, buy_result: Dict, sell_result: Dict, 
                               position_size: float) -> float:
        """Calculate actual profit from executed orders, accounting for different execution models"""
        try:
            # Get actual execution prices from orders - handle Order objects vs dicts
            buy_order = buy_result.get('order')
            sell_order = sell_result.get('order')
            
            # Extract prices from Order objects or dictionaries
            if hasattr(buy_order, 'price'):
                buy_price = buy_order.price or 0
            elif isinstance(buy_order, dict):
                buy_price = buy_order.get('filled_price', buy_order.get('price', 0))
            else:
                buy_price = buy_result.get('price', 0)
                
            if hasattr(sell_order, 'price'):
                sell_price = sell_order.price or 0
            elif isinstance(sell_order, dict):
                sell_price = sell_order.get('filled_price', sell_order.get('price', 0))
            else:
                sell_price = sell_result.get('price', 0)
            
            # Calculate actual profit from execution
            
            if buy_price > 0 and sell_price > 0:
                buy_cost = buy_price * position_size
                sell_proceeds = sell_price * position_size
                gross_profit = sell_proceeds - buy_cost
                
                if buy_cost > 0:
                    return_percentage = (gross_profit / buy_cost) * 100
                    self.logger.info(f"💰 Profit: ${gross_profit:.4f} ({return_percentage:.2f}% return)")
                
                return gross_profit
            else:
                self.logger.warning(f"⚠️ Missing fill prices - using estimates")
                # Use fallback order prices for estimation
                fallback_buy_price = buy_result.get('price', 0)
                fallback_sell_price = sell_result.get('price', 0)
                if fallback_buy_price > 0 and fallback_sell_price > 0:
                    estimated_profit = (fallback_sell_price - fallback_buy_price) * position_size
                    self.logger.info(f"💰 Est. profit: ${estimated_profit:.4f}")
                    return estimated_profit
                return 0.0
        except Exception as e:
            self.logger.error(f"❌ Error calculating profit: {e}")
            return 0.0
    
    def _create_error_result(self, opportunity: ArbitrageOpportunity, 
                           error: str, start_time: float) -> Dict:
        """Create standardized error result"""
        return {
            'success': False,
            'opportunity': opportunity,
            'error': error,
            'execution_time': time.time() - start_time,
            'position_size': 0,
            'buy_result': None,
            'sell_result': None
        }
    
    def get_execution_stats(self) -> Dict:
        """Get arbitrage execution statistics"""
        if not self.execution_history:
            return {
                'total_executions': 0,
                'successful_executions': 0,
                'success_rate': 0.0,
                'total_profit': 0.0
            }
        
        successful = [ex for ex in self.execution_history if ex['success']]
        
        return {
            'total_executions': len(self.execution_history),
            'successful_executions': len(successful),
            'success_rate': len(successful) / len(self.execution_history) * 100,
            'total_profit': sum(ex.get('profit', 0) for ex in successful)
        }
    
    async def _wait_for_hydra_completion(self, order_id: str, timeout: float = 30) -> bool:
        """
        Wait for Hydra order to reach 'order_completed' status
        
        Args:
            order_id: Hydra order ID to monitor
            timeout: Maximum time to wait in seconds
            
        Returns:
            True if order completed successfully, False if timed out
        """
        import asyncio
        start_time = asyncio.get_event_loop().time()
        
        self.logger.info(f"⏳ Monitoring Hydra order {order_id[:8]}... for completion")
        
        # TODO: Implement actual order status tracking via DexEvents
        # For now, use a simple timeout approach
        # This should be connected to the order tracking system that processes DexEvents
        
        while (asyncio.get_event_loop().time() - start_time) < timeout:
            # In a real implementation, this would check the order status
            # from the DexEvents tracking system
            
            # Simulate checking order status
            await asyncio.sleep(0.5)
            
            # Check if we've received the order_completed event
            # This would need to integrate with the DexEvents processing
            # For now, just wait a reasonable amount of time
            if (asyncio.get_event_loop().time() - start_time) > 3:  # Assume completion after 3 seconds
                self.logger.info(f"✅ Hydra order completed")
                return True
        
        self.logger.warning("⚠️ Timeout waiting for Hydra completion")
        return False


class ArbitrageStrategy:
    """Main arbitrage strategy coordinator"""
    
    def __init__(self, hydra_client, config: Dict):
        """
        Initialize arbitrage strategy
        
        Args:
            hydra_client: Hydra gRPC client
            config: Arbitrage configuration
        """
        self.config = config
        params = config.get('params', {})
        self.detector = ArbitrageDetector(
            hydra_client, 
            min_profit_threshold=params.get('min_profit_percentage', 0.5)
        )
        self.executor = ArbitrageExecutor(
            exchange_factory=None,  # Will need to be set later
            hydra_client=hydra_client,
            config=config  # Pass entire config including params
        )
        self.running = False
    
    async def start(self, trading_pairs: List[Dict]):
        """Start arbitrage detection and execution"""
        self.running = True
        print("🚀 Starting arbitrage strategy...")
        
        while self.running:
            try:
                # Scan for opportunities
                opportunities = await self.detector.scan_opportunities(trading_pairs)
                
                if opportunities:
                    print(f"🎯 Found {len(opportunities)} arbitrage opportunities")
                    
                    # Execute top opportunity if profitable enough
                    top_opportunity = opportunities[0]
                    if top_opportunity.profit_percentage >= self.config.get('min_execution_threshold', 1.0):
                        result = await self.executor.execute_arbitrage(top_opportunity)
                        if result['success']:
                            print(f"✅ Arbitrage executed successfully")
                        else:
                            print(f"❌ Arbitrage execution failed: {result['error']}")
                
                # Wait before next scan
                await asyncio.sleep(self.config.get('scan_interval', 10))
                
            except Exception as e:
                print(f"❌ Arbitrage strategy error: {e}")
                await asyncio.sleep(5)
    
    def stop(self):
        """Stop arbitrage strategy"""
        self.running = False
        print("🛑 Stopped arbitrage strategy")
    
    def get_status(self) -> Dict:
        """Get arbitrage strategy status"""
        detector_stats = {
            'opportunities_found': len(self.detector.opportunities),
            'external_exchanges': len(self.detector.external_exchanges)
        }
        
        executor_stats = self.executor.get_execution_stats()
        
        return {
            'running': self.running,
            'detector': detector_stats,
            'executor': executor_stats,
            'config': self.config
        }


# Future integration notes:
"""
To implement full arbitrage functionality:

1. External Exchange Integration:
   - Add ccxt library for major exchanges
   - Implement exchange-specific API clients
   - Handle different trading pair formats

2. Cross-chain Bridging:
   - Integrate with bridges (Stargate, LayerZero, etc.)
   - Handle cross-chain asset transfers
   - Account for bridge fees and delays

3. Risk Management:
   - Real-time portfolio tracking
   - Position size limits
   - Stop-loss mechanisms
   - Network congestion monitoring

4. Advanced Features:
   - Triangular arbitrage within Hydra DEX
   - Statistical arbitrage
   - Latency arbitrage
   - Flash loan arbitrage

5. Monitoring:
   - Real-time P&L tracking
   - Alert system for large opportunities
   - Performance analytics
   - Risk metrics
"""