#!/usr/bin/env python3
"""
Dynamic sizing module for arbitrage strategy
"""

from typing import Optional, Tuple
from optimal_size_finder import OptimalSizeFinder
import logging

class DynamicPositionSizer:
    """
    Dynamically determines optimal position sizes for arbitrage trades
    """
    
    def __init__(self, profit_validator, min_profit_threshold: float = 0.5, min_position_usd: float = 50.0, logger=None):
        self.profit_validator = profit_validator
        self.min_profit_threshold = min_profit_threshold
        self.min_position_usd = min_position_usd
        self.size_finder = OptimalSizeFinder(profit_validator, logger)
        self.logger = logger or logging.getLogger(__name__)
        
        # Cache results to avoid repeated calculations
        self._cache = {}
        
    def get_optimal_position_size(self,
                                base_currency,
                                quote_currency,
                                buy_exchange_price: float,
                                sell_exchange_price: float,
                                max_position_usd: float,
                                strategy_preference: str = 'max_absolute',
                                external_fees: float = 0.001,
                                base_symbol: str = "BTC",
                                hydra_side: str = None) -> Optional[dict]:
        """
        Get optimal position size based on strategy preference
        
        Args:
            strategy_preference: 'max_absolute', 'max_percentage', or 'max_profitable_size'
            max_position_usd: Maximum position size to consider
            
        Returns:
            dict with optimal_usd_amount, optimal_btc_amount, expected_profit, etc.
        """
        
        # Create cache key
        cache_key = f"{buy_exchange_price}_{sell_exchange_price}_{max_position_usd}_{strategy_preference}"
        if cache_key in self._cache:
            self.logger.debug(f"Using cached position size for {cache_key}")
            return self._cache[cache_key]
        
        self.logger.debug(f"🎯 Finding optimal position size (max: ${max_position_usd}, strategy: {strategy_preference})")
        
        result = None
        
        if strategy_preference == 'max_absolute':
            # Find size that maximizes absolute profit
            result = self.size_finder.find_optimal_size(
                base_currency=base_currency,
                quote_currency=quote_currency,
                buy_exchange_price=buy_exchange_price,
                sell_exchange_price=sell_exchange_price,
                min_usd_size=self.min_position_usd,
                max_usd_size=max_position_usd,
                min_profit_threshold=self.min_profit_threshold,
                hydra_side=hydra_side,
                external_fees=external_fees,
                base_symbol=base_symbol
            )
            
        elif strategy_preference == 'max_percentage':
            # Find size that maximizes profit percentage (usually smallest profitable size)
            sizes_tested = []
            for usd_amount in range(int(self.min_position_usd), int(max_position_usd) + 1, 50):
                btc_amount = usd_amount / buy_exchange_price if buy_exchange_price > 0 else usd_amount / sell_exchange_price
                
                profit_result = self.profit_validator.calculate_arbitrage_profit(
                    base_currency=base_currency,
                    quote_currency=quote_currency,
                    buy_exchange_price=buy_exchange_price,
                    sell_exchange_price=sell_exchange_price,
                    amount=btc_amount,
                    external_fees=external_fees,
                    base_symbol=base_symbol
                )
                
                if profit_result['best_scenario']:
                    scenario = profit_result['best_scenario']
                    self.logger.info(f"🔍 Dynamic sizing test ${usd_amount}: EstimateOrder profit {scenario['profit_percentage']:.3f}% vs threshold {self.min_profit_threshold}%")
                    if scenario['profit_percentage'] >= self.min_profit_threshold:
                        sizes_tested.append({
                            'usd_amount': usd_amount,
                            'btc_amount': btc_amount,
                            'profit_percentage': scenario['profit_percentage'],
                            'expected_profit': scenario['gross_profit'],
                            'scenario_name': scenario['name']
                        })
            
            if sizes_tested:
                # Return the size with highest profit percentage
                best_pct = max(sizes_tested, key=lambda x: x['profit_percentage'])
                result = {
                    'optimal_usd_amount': best_pct['usd_amount'],
                    'optimal_btc_amount': best_pct['btc_amount'],
                    'expected_profit': best_pct['expected_profit'],
                    'profit_percentage': best_pct['profit_percentage'],
                    'scenario_name': best_pct['scenario_name']
                }
                
        elif strategy_preference == 'max_profitable_size':
            # Find the largest size that still meets profit threshold
            result = self.size_finder.find_max_profitable_size(
                base_currency=base_currency,
                quote_currency=quote_currency,
                buy_exchange_price=buy_exchange_price,
                sell_exchange_price=sell_exchange_price,
                min_profit_threshold=self.min_profit_threshold,
                max_usd_size=max_position_usd,
                external_fees=external_fees
            )
        
        # Cache the result
        if result:
            self._cache[cache_key] = result
            self.logger.info(f"✅ Optimal size found: ${result['optimal_usd_amount']} → ${result['expected_profit']:.2f} profit ({result['profit_percentage']:.2f}%)")
        else:
            self.logger.warning(f"❌ No profitable size found with {strategy_preference} strategy")
            
        return result
        
    def should_trade_at_size(self,
                           base_currency,
                           quote_currency,
                           buy_exchange_price: float,
                           sell_exchange_price: float,
                           proposed_usd_size: float,
                           external_fees: float = 0.001) -> Tuple[bool, Optional[dict]]:
        """
        Check if a proposed trade size meets profitability requirements
        
        Returns:
            (should_trade: bool, profit_info: dict)
        """
        
        btc_amount = proposed_usd_size / buy_exchange_price if buy_exchange_price > 0 else proposed_usd_size / sell_exchange_price
        
        profit_result = self.profit_validator.calculate_arbitrage_profit(
            base_currency=base_currency,
            quote_currency=quote_currency,
            buy_exchange_price=buy_exchange_price,
            sell_exchange_price=sell_exchange_price,
            amount=btc_amount,
            external_fees=external_fees,
            base_symbol=base_symbol
        )
        
        if not profit_result['best_scenario']:
            return False, None
            
        scenario = profit_result['best_scenario']
        should_trade = scenario['profit_percentage'] >= self.min_profit_threshold
        
        profit_info = {
            'usd_amount': proposed_usd_size,
            'btc_amount': btc_amount,
            'expected_profit': scenario['gross_profit'],
            'profit_percentage': scenario['profit_percentage'],
            'scenario_name': scenario['name'],
            'meets_threshold': should_trade
        }
        
        return should_trade, profit_info


def main():
    """Test dynamic sizing"""
    from lib.grpc_client import HydraGRPCClient
    from profit_validator import ProfitValidator
    
    client = HydraGRPCClient('localhost', 5008)
    if not client.test_connection():
        print('Cannot connect to Hydra')
        return
        
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    logger = logging.getLogger(__name__)
        
    validator = ProfitValidator(client, logger)
    sizer = DynamicPositionSizer(validator, min_profit_threshold=0.5, logger=logger)
    
    btc_currency, usdc_currency = validator.get_currencies_for_pair('BTC/USDC')
    
    external_buy = 110000
    external_sell = 110500
    max_position = 1000
    
    print(f"\n=== Dynamic Position Sizing Test ===")
    print(f"Max position: ${max_position}")
    print(f"Min profit threshold: 0.5%")
    
    # Test different strategies
    strategies = ['max_absolute', 'max_percentage', 'max_profitable_size']
    
    for strategy in strategies:
        print(f"\n--- Strategy: {strategy} ---")
        
        optimal = sizer.get_optimal_position_size(
            base_currency=btc_currency,
            quote_currency=usdc_currency,
            buy_exchange_price=external_buy,
            sell_exchange_price=external_sell,
            max_position_usd=max_position,
            strategy_preference=strategy
        )
        
        if optimal:
            print(f"Optimal size: ${optimal['optimal_usd_amount']}")
            print(f"Expected profit: ${optimal['expected_profit']:.2f} ({optimal['profit_percentage']:.2f}%)")
        else:
            print("No optimal size found")
    
    # Test specific size validation
    print(f"\n--- Size Validation Test ---")
    test_sizes = [200, 500, 800]
    
    for size in test_sizes:
        should_trade, info = sizer.should_trade_at_size(
            base_currency=btc_currency,
            quote_currency=usdc_currency,
            buy_exchange_price=external_buy,
            sell_exchange_price=external_sell,
            proposed_usd_size=size
        )
        
        if info:
            status = "✅ TRADE" if should_trade else "❌ SKIP"
            print(f"${size}: {status} - {info['profit_percentage']:.2f}% profit (${info['expected_profit']:.2f})")
    
    client.close()

if __name__ == "__main__":
    main()