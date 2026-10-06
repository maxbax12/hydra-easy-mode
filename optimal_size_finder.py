#!/usr/bin/env python3
"""
Find the optimal trade size for maximum profitability
"""

from typing import Optional, Tuple
from lib.grpc_client import HydraGRPCClient  
from profit_validator import ProfitValidator
import logging

class OptimalSizeFinder:
    """
    Finds the optimal trade size for arbitrage opportunities
    """
    
    def __init__(self, profit_validator: ProfitValidator, logger=None):
        self.profit_validator = profit_validator
        self.logger = logger or logging.getLogger(__name__)
    
    def find_optimal_size(self,
                         base_currency,
                         quote_currency,
                         buy_exchange_price: float,
                         sell_exchange_price: float,
                         min_usd_size: float = 50,
                         max_usd_size: float = 2000,
                         min_profit_threshold: float = 0.5,
                         external_fees: float = 0.001,
                         base_symbol: str = "BTC",
                         hydra_side: str = None) -> Optional[dict]:
        """
        Find the optimal trade size using binary search approach
        
        Returns dict with:
        - optimal_usd_amount: USD amount to trade
        - optimal_btc_amount: BTC amount to trade  
        - expected_profit: Expected profit in USD
        - profit_percentage: Expected profit percentage
        """
        
        self.logger.debug(f"🔍 Finding optimal size between ${min_usd_size} - ${max_usd_size}")
        self.logger.debug(f"   Min profit threshold: {min_profit_threshold}%")

        # Calculate gross spread
        gross_spread = sell_exchange_price - buy_exchange_price
        gross_spread_pct = (gross_spread / buy_exchange_price * 100) if buy_exchange_price > 0 else 0
        self.logger.info(f"📊 Spread: buy ${buy_exchange_price:.2f} → sell ${sell_exchange_price:.2f} = ${gross_spread:.2f} ({gross_spread_pct:.3f}%)")
        self.logger.info(f"🎯 Testing sizes: ${min_usd_size} to ${max_usd_size} (need >{min_profit_threshold}% profit after fees)")

        best_result = None
        test_sizes = []

        # Test multiple size points to find the curve
        for usd_amount in range(int(min_usd_size), int(max_usd_size) + 1, 50):
            btc_amount = usd_amount / buy_exchange_price if buy_exchange_price > 0 else usd_amount / sell_exchange_price
            
            result = self.profit_validator.calculate_arbitrage_profit(
                base_currency=base_currency,
                quote_currency=quote_currency,
                buy_exchange_price=buy_exchange_price,
                sell_exchange_price=sell_exchange_price,
                amount=btc_amount,
                external_fees=external_fees,
                base_symbol=base_symbol,
                hydra_side=hydra_side
            )
            
            if result['best_scenario']:
                scenario = result['best_scenario']
                profit_pct = scenario['profit_percentage']
                profit_usd = scenario['gross_profit']
                
                test_sizes.append({
                    'usd_amount': usd_amount,
                    'btc_amount': btc_amount,
                    'profit_usd': profit_usd,
                    'profit_percentage': profit_pct,
                    'scenario_name': scenario['name'],
                    'uses_estimate': scenario.get('hydra_estimate', False)
                })
                
                self.logger.info(f"  💵 ${usd_amount}: profit={profit_pct:+.3f}% (${profit_usd:+.2f})")

                # Check if this meets our minimum threshold
                if profit_pct >= min_profit_threshold:
                    if not best_result or profit_usd > best_result['expected_profit']:
                        best_result = {
                            'optimal_usd_amount': usd_amount,
                            'optimal_btc_amount': btc_amount,
                            'expected_profit': profit_usd,
                            'profit_percentage': profit_pct,
                            'scenario_name': scenario['name'],
                            'uses_hydra_estimate': scenario.get('hydra_estimate', False)
                        }
            else:
                self.logger.info(f"  💵 ${usd_amount}: No profitable scenario found")
        
        # Log summary of results
        if test_sizes:
            profitable_sizes = [s for s in test_sizes if s['profit_percentage'] >= min_profit_threshold]

            # Only log at INFO if we found profitable sizes, otherwise DEBUG to reduce spam
            log_func = self.logger.info if len(profitable_sizes) > 0 else self.logger.debug
            log_func(f"📊 Size Analysis Results:")
            log_func(f"   Tested {len(test_sizes)} sizes from ${min_usd_size} to ${max_usd_size}")
            log_func(f"   {len(profitable_sizes)} sizes meet {min_profit_threshold}% threshold")
            
            if profitable_sizes:
                max_profit_size = max(profitable_sizes, key=lambda x: x['profit_usd'])
                max_pct_size = max(profitable_sizes, key=lambda x: x['profit_percentage'])
                
                self.logger.info(f"   💰 Max absolute profit: ${max_profit_size['usd_amount']} → ${max_profit_size['profit_usd']:.2f}")
                self.logger.info(f"   📈 Max profit %: ${max_pct_size['usd_amount']} → {max_pct_size['profit_percentage']:.2f}%")
                
                # Show the profit curve shape
                if len(profitable_sizes) > 3:
                    small = profitable_sizes[0]
                    large = profitable_sizes[-1]
                    self.logger.info(f"   📉 Profit degradation: {small['profit_percentage']:.2f}% → {large['profit_percentage']:.2f}%")
        
        return best_result
    
    def find_max_profitable_size(self,
                               base_currency,
                               quote_currency, 
                               buy_exchange_price: float,
                               sell_exchange_price: float,
                               min_profit_threshold: float = 0.5,
                               max_usd_size: float = 2000,
                               external_fees: float = 0.001) -> Optional[dict]:
        """
        Find the maximum trade size that still meets profit threshold
        """
        
        self.logger.info(f"🎯 Finding max profitable size up to ${max_usd_size}")
        
        # Binary search for the maximum profitable size
        low_usd = 50
        high_usd = max_usd_size
        best_profitable_size = None
        
        while high_usd - low_usd > 10:  # Stop when range is small
            mid_usd = (low_usd + high_usd) // 2
            btc_amount = mid_usd / buy_exchange_price if buy_exchange_price > 0 else mid_usd / sell_exchange_price
            
            result = self.profit_validator.calculate_arbitrage_profit(
                base_currency=base_currency,
                quote_currency=quote_currency,
                buy_exchange_price=buy_exchange_price,
                sell_exchange_price=sell_exchange_price,
                amount=btc_amount,
                external_fees=external_fees,
                base_symbol=base_symbol,
                hydra_side=hydra_side
            )
            
            if result['best_scenario'] and result['best_scenario']['profit_percentage'] >= min_profit_threshold:
                # This size is profitable, try larger
                best_profitable_size = {
                    'optimal_usd_amount': mid_usd,
                    'optimal_btc_amount': btc_amount,
                    'expected_profit': result['best_scenario']['gross_profit'],
                    'profit_percentage': result['best_scenario']['profit_percentage'],
                    'scenario_name': result['best_scenario']['name']
                }
                low_usd = mid_usd
                self.logger.debug(f"  ${mid_usd} ✅ profitable ({result['best_scenario']['profit_percentage']:.2f}%) - trying larger")
            else:
                # This size is not profitable enough, try smaller
                high_usd = mid_usd
                self.logger.debug(f"  ${mid_usd} ❌ not profitable enough - trying smaller")
        
        if best_profitable_size:
            self.logger.info(f"🏆 Max profitable size: ${best_profitable_size['optimal_usd_amount']}")
            self.logger.info(f"   Expected profit: ${best_profitable_size['expected_profit']:.2f} ({best_profitable_size['profit_percentage']:.2f}%)")
        
        return best_profitable_size


def main():
    """Test the optimal size finder"""
    client = HydraGRPCClient('localhost', 5008)
    if not client.test_connection():
        print('Cannot connect to Hydra')
        return
        
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    logger = logging.getLogger(__name__)
        
    validator = ProfitValidator(client, logger)
    finder = OptimalSizeFinder(validator, logger)
    
    btc_currency, usdc_currency = validator.get_currencies_for_pair('BTC/USDC')
    
    print('\n=== Optimal Size Finder Test ===')
    external_buy = 110000
    external_sell = 110500
    
    # Find optimal size
    optimal = finder.find_optimal_size(
        base_currency=btc_currency,
        quote_currency=usdc_currency,
        buy_exchange_price=external_buy,
        sell_exchange_price=external_sell,
        min_usd_size=100,
        max_usd_size=1000,
        min_profit_threshold=0.5
    )
    
    if optimal:
        print(f"\n🎯 OPTIMAL TRADE SIZE:")
        print(f"   Amount: ${optimal['optimal_usd_amount']} ({optimal['optimal_btc_amount']:.6f} BTC)")
        print(f"   Expected profit: ${optimal['expected_profit']:.2f} ({optimal['profit_percentage']:.2f}%)")
        print(f"   Strategy: {optimal['scenario_name']}")
        print(f"   Uses EstimateOrder: {optimal.get('uses_hydra_estimate', False)}")
    else:
        print("❌ No profitable size found in the tested range")
    
    # Also find max profitable size
    print(f"\n--- Binary Search for Max Size ---")
    max_profitable = finder.find_max_profitable_size(
        base_currency=btc_currency,
        quote_currency=usdc_currency,
        buy_exchange_price=external_buy,
        sell_exchange_price=external_sell,
        min_profit_threshold=0.5,
        max_usd_size=1500
    )
    
    if max_profitable:
        print(f"🏆 MAX PROFITABLE SIZE: ${max_profitable['optimal_usd_amount']}")
        print(f"   Profit: ${max_profitable['expected_profit']:.2f} ({max_profitable['profit_percentage']:.2f}%)")
    
    client.close()

if __name__ == "__main__":
    main()