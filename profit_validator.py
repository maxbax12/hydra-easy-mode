"""
Profit Validator Module
=======================

Validates arbitrage profit calculations using EstimateOrder from Hydra DEX
to ensure accurate profit predictions before executing trades.
"""

import logging
from typing import Dict, Optional, Tuple, Any
from lib.hydra_pb import primitives_pb2, currency_pb2, orderbook_pb2


class ProfitValidator:
    """
    Validates arbitrage opportunities using EstimateOrder to get accurate
    fee calculations and ensure profitable trades.
    """
    
    def __init__(self, hydra_client, logger: Optional[logging.Logger] = None):
        """
        Initialize the profit validator with a Hydra gRPC client
        
        Args:
            hydra_client: Hydra gRPC client instance
            logger: Optional logger instance
        """
        self.hydra_client = hydra_client
        self.logger = logger or logging.getLogger(__name__)
        
        # Check if orderbook service is available
        self.orderbook_available = hasattr(hydra_client, 'orderbook_stub') and hydra_client.orderbook_stub is not None
        
        if self.orderbook_available:
            self.logger.info("✅ OrderbookService available for EstimateOrder validation")
        else:
            self.logger.warning("⚠️ OrderbookService not available - profit validation will use estimates only")
        
    def get_currencies_for_pair(self, pair: str, base_network_id: str = None, 
                                quote_network_id: str = None, protocol: int = 0) -> Tuple[Optional[Any], Optional[Any]]:
        """
        Create OrderbookCurrency objects for any trading pair
        
        Args:
            pair: Trading pair (e.g., "BTC/USDC", "ETH/USDT", etc.)
            base_network_id: Network ID for base currency (e.g., "0a03cf40" for BTC)
            quote_network_id: Network ID for quote currency (e.g., "11155111" for USDC)
            protocol: Protocol type (default: 0)
            
        Returns:
            Tuple of (base_currency, quote_currency) OrderbookCurrency objects
        """
        try:
            # Parse the trading pair
            if '/' not in pair:
                self.logger.error(f"Invalid pair format: {pair}. Expected format: BASE/QUOTE")
                return None, None
                
            base_symbol, quote_symbol = pair.split('/')
            base_symbol = base_symbol.strip().upper()
            quote_symbol = quote_symbol.strip().upper()
            
            # Default network IDs based on known assets
            if not base_network_id:
                if base_symbol == 'BTC':
                    base_network_id = '0a03cf40'  # Bitcoin Signet
                elif base_symbol in ['ETH', 'USDC', 'USDT']:
                    base_network_id = '11155111'  # Ethereum Sepolia
                else:
                    base_network_id = '11155111'  # Default to Ethereum
                    
            if not quote_network_id:
                if quote_symbol == 'BTC':
                    quote_network_id = '0a03cf40'  # Bitcoin Signet
                elif quote_symbol in ['ETH', 'USDC', 'USDT']:
                    quote_network_id = '11155111'  # Ethereum Sepolia
                else:
                    quote_network_id = '11155111'  # Default to Ethereum
            
            self.logger.debug(f"Creating OrderbookCurrency for: {base_symbol} (network: {base_network_id}) and {quote_symbol} (network: {quote_network_id})")
            
            # For BTC, use the 32-byte zero address as asset_id
            if base_symbol == 'BTC':
                base_asset_id = '0x0000000000000000000000000000000000000000000000000000000000000000'
            elif base_symbol == 'ETH':
                base_asset_id = '0x0000000000000000000000000000000000000000'  # ETH uses 20-byte address
            else:
                base_asset_id = base_symbol
                
            if quote_symbol == 'BTC':
                quote_asset_id = '0x0000000000000000000000000000000000000000000000000000000000000000'
            elif quote_symbol == 'ETH':
                quote_asset_id = '0x0000000000000000000000000000000000000000'  # ETH uses 20-byte address
            else:
                quote_asset_id = quote_symbol
            
            # Special case for USDC on Sepolia
            if quote_symbol == 'USDC' and quote_network_id == '11155111':
                quote_asset_id = '0x8cd0da3d001b013336918b8bc4e56d9dda1347e0'
            if base_symbol == 'USDC' and base_network_id == '11155111':
                base_asset_id = '0x8cd0da3d001b013336918b8bc4e56d9dda1347e0'
            
            # Create OrderbookCurrency objects
            # Set protocol: PROTOCOL_BITCOIN=1 for BTC, PROTOCOL_EVM=2 for everything else
            base_proto = primitives_pb2.PROTOCOL_BITCOIN if base_symbol == 'BTC' else primitives_pb2.PROTOCOL_EVM
            base_currency = currency_pb2.OrderbookCurrency(
                protocol=base_proto,
                network_id=base_network_id,
                asset_id=base_asset_id
            )

            quote_proto = primitives_pb2.PROTOCOL_BITCOIN if quote_symbol == 'BTC' else primitives_pb2.PROTOCOL_EVM
            quote_currency = currency_pb2.OrderbookCurrency(
                protocol=quote_proto,
                network_id=quote_network_id,
                asset_id=quote_asset_id
            )
            
            return base_currency, quote_currency
            
        except Exception as e:
            self.logger.error(f"Failed to create currencies for {pair}: {e}")
            return None, None
            
    def calculate_arbitrage_profit(self,
                                  base_currency,
                                  quote_currency,
                                  buy_exchange_price: float,
                                  sell_exchange_price: float,
                                  amount: float,
                                  external_fees: float = 0.001,
                                  base_symbol: str = "BTC",
                                  hydra_side: str = None) -> Dict[str, Any]:
        """
        Calculate arbitrage profit using EstimateOrder for accurate fee calculation

        Args:
            base_currency: Base currency OrderbookCurrency object
            quote_currency: Quote currency OrderbookCurrency object
            buy_exchange_price: Price on the exchange we're buying from
            sell_exchange_price: Price on the exchange we're selling to
            amount: Amount of base currency to trade
            external_fees: External exchange fee rate (default 0.1%)
            hydra_side: 'buy' if buying from Hydra, 'sell' if selling to Hydra, None to test both

        Returns:
            Dict containing profit analysis with best scenario
        """
        try:
            # Reduced debug logging for cleaner output
            self.logger.debug(f"Calculating profit for {amount:.6f} {base_currency.asset_id[-8:] if base_currency else 'BTC'} (hydra_side={hydra_side})")

            scenarios = []

            # Only test the scenario where Hydra is involved in the profitable direction
            # Skip impossible scenarios (buying high, selling low)

            # Scenario 1: Buy on external exchange, sell on Hydra
            # Only test if: (1) hydra_side=='sell' OR hydra_side is None, AND (2) it makes sense (sell_price > buy_price)
            should_test_sell_hydra = (hydra_side in ['sell', None]) and (sell_exchange_price > buy_exchange_price)

            if should_test_sell_hydra and buy_exchange_price > 0 and self.orderbook_available:
                self.logger.debug(f"Sell on Hydra vs External comparison:")
                self.logger.debug(f"  - Selling exactly {amount} BTC on both exchanges")
                
                # Calculate external buy cost for this amount of BTC
                external_cost = amount * buy_exchange_price
                external_fee = external_cost * external_fees
                total_external_cost = external_cost + external_fee
                
                self.logger.debug(f"  - External: buy {amount} BTC costs ${external_cost:.2f} + ${external_fee:.2f} = ${total_external_cost:.2f}")
                
                # Estimate Hydra sell using EstimateOrder
                try:
                    # Create a market order to sell base for quote
                    # Base amount = SELL order (selling this much base)
                    order_amount = orderbook_pb2.OrderAmount()
                    # Create DecimalString for the amount
                    decimal_amount = primitives_pb2.DecimalString(value=str(amount))
                    order_amount.base.amount.CopyFrom(decimal_amount)
                    
                    # Note: side might not be needed if OrderAmount determines buy/sell
                    # But including it for clarity
                    market_order = orderbook_pb2.OrderVariant.MarketOrder()
                    market_order.base.CopyFrom(base_currency)
                    market_order.quote.CopyFrom(quote_currency)
                    market_order.amount.CopyFrom(order_amount)
                    market_order.side = orderbook_pb2.OrderSide.SELL  # Selling base for quote
                    
                    order_variant = orderbook_pb2.OrderVariant()
                    order_variant.market_order.CopyFrom(market_order)
                    
                    # Get estimate from Hydra
                    order_match = self.hydra_client.estimate_order(order_variant)
                    
                    if order_match:
                        # Extract the receiving amount and fees from PairOrderMatch
                        hydra_revenue = 0
                        hydra_fee = 0
                        
                        if order_match.HasField('pair'):
                            pair_match = order_match.pair.pair_order_match
                            
                            # receiving_amount is what we get (USDC)
                            if hasattr(pair_match, 'receiving_amount') and pair_match.receiving_amount:
                                if hasattr(pair_match.receiving_amount, 'value'):
                                    hydra_revenue = float(pair_match.receiving_amount.value)
                            
                            # receiving_fee is in USDC (the quote currency)
                            if hasattr(pair_match, 'receiving_fee') and pair_match.receiving_fee:
                                if hasattr(pair_match.receiving_fee, 'value'):
                                    hydra_fee = float(pair_match.receiving_fee.value)
                        
                        elif order_match.HasField('swap'):
                            # For swap matches, aggregate the results
                            swap_matches = order_match.swap.pair_order_matches
                            if swap_matches:
                                # In a swap chain, we care about the final output
                                last_match = swap_matches[-1]
                                if hasattr(last_match, 'receiving_amount') and last_match.receiving_amount:
                                    if hasattr(last_match.receiving_amount, 'value'):
                                        hydra_revenue = float(last_match.receiving_amount.value)
                                if hasattr(last_match, 'receiving_fee') and last_match.receiving_fee:
                                    if hasattr(last_match.receiving_fee, 'value'):
                                        hydra_fee = float(last_match.receiving_fee.value)
                        
                        net_hydra_revenue = hydra_revenue - hydra_fee
                        
                        # Profit = what we get from Hydra - what we paid on external
                        gross_profit = net_hydra_revenue - total_external_cost
                        profit_percentage = (gross_profit / total_external_cost * 100) if total_external_cost > 0 else 0
                        
                        self.logger.debug(f"📊 Buy External → Sell Hydra: ${gross_profit:.2f} profit ({profit_percentage:.2f}%)")
                        
                        scenarios.append({
                            'name': 'Buy External → Sell Hydra',
                            'gross_profit': gross_profit,
                            'profit_percentage': profit_percentage,
                            'buy_cost': total_external_cost,
                            'sell_revenue': net_hydra_revenue,
                            'fees': external_fee + hydra_fee,
                            'hydra_estimate': True
                        })
                    else:
                        self.logger.debug("EstimateOrder returned no match")
                        # Fall back to estimate
                        self._add_estimated_scenario(scenarios, 'sell', amount, buy_exchange_price, 
                                                    sell_exchange_price, external_fees)
                        
                except Exception as e:
                    self.logger.debug(f"EstimateOrder failed: {e}")
                    # Fall back to estimate
                    self._add_estimated_scenario(scenarios, 'sell', amount, buy_exchange_price, 
                                                sell_exchange_price, external_fees)
            elif buy_exchange_price > 0:
                # No orderbook service, use estimate
                self._add_estimated_scenario(scenarios, 'sell', amount, buy_exchange_price, 
                                            sell_exchange_price, external_fees)
                    
            # Scenario 2: Buy on Hydra, sell on external exchange
            # Only test if: (1) hydra_side=='buy' OR hydra_side is None, AND (2) it makes sense (buy_price > sell_price)
            should_test_buy_hydra = (hydra_side in ['buy', None]) and (buy_exchange_price > sell_exchange_price)

            if should_test_buy_hydra and sell_exchange_price > 0 and self.orderbook_available:
                try:
                    self.logger.debug(f"Buy on Hydra vs External comparison:")
                    self.logger.debug(f"  - Buying exactly {amount} BTC on both exchanges to compare costs")
                    
                    # Calculate external cost for buying this amount of BTC
                    external_usdc_cost = amount * buy_exchange_price if buy_exchange_price > 0 else amount * sell_exchange_price * 0.99
                    external_fee_usdc = external_usdc_cost * external_fees
                    total_external_usdc = external_usdc_cost + external_fee_usdc
                    
                    self.logger.debug(f"  - External cost for {amount} BTC: ${external_usdc_cost:.2f} + ${external_fee_usdc:.2f} = ${total_external_usdc:.2f}")
                    
                    # Ask Hydra: "How much USDC to buy exactly this amount of BTC?"
                    self.logger.debug(f"  - Asking Hydra: How much USDC to buy exactly {amount} BTC?")
                    
                    # Create the Base amount - we want to BUY exactly this much BTC
                    order_amount = orderbook_pb2.OrderAmount()
                    decimal_amount = primitives_pb2.DecimalString(value=str(amount))
                    order_amount.base.amount.CopyFrom(decimal_amount)
                    
                    # Note: side might not be needed if OrderAmount determines buy/sell
                    # But including it for clarity
                    market_order = orderbook_pb2.OrderVariant.MarketOrder()
                    market_order.base.CopyFrom(base_currency)
                    market_order.quote.CopyFrom(quote_currency)
                    market_order.amount.CopyFrom(order_amount)
                    market_order.side = orderbook_pb2.OrderSide.BUY  # Buying base with quote

                    order_variant = orderbook_pb2.OrderVariant()
                    order_variant.market_order.CopyFrom(market_order)

                    # Debug: Log what we're requesting
                    # Calculate USD size for logging (this is the "buy on Hydra" scenario)
                    usd_size = amount * (sell_exchange_price if sell_exchange_price > 0 else buy_exchange_price)
                    self.logger.debug(f"📤 EstimateOrder Request for size ${usd_size:.2f} ({amount:.8f} {base_symbol}):")
                    self.logger.debug(f"   Base: network={base_currency.network_id}, asset={base_currency.asset_id[:30]}")
                    self.logger.debug(f"   Quote: network={quote_currency.network_id}, asset={quote_currency.asset_id[:30]}")
                    self.logger.debug(f"   Side: BUY (buying {amount:.8f} {base_symbol} with USDC)")
                    self.logger.debug(f"   Amount: {decimal_amount.value} {base_symbol}")

                    # Get estimate from Hydra
                    order_match = self.hydra_client.estimate_order(order_variant)

                    # Debug: Log the raw response
                    if order_match:
                        self.logger.debug(f"🔍 EstimateOrder Response Type: {type(order_match)}")
                        self.logger.debug(f"🔍 Has 'pair' field: {order_match.HasField('pair')}")
                        self.logger.debug(f"🔍 Has 'swap' field: {order_match.HasField('swap')}")

                        # Log all available fields
                        available_fields = [f.name for f in order_match.DESCRIPTOR.fields]
                        self.logger.debug(f"🔍 Available fields in OrderMatch: {available_fields}")

                        # Check which oneof is set
                        oneof_name = order_match.WhichOneof('order_match')
                        self.logger.debug(f"🔍 Active oneof field: {oneof_name}")
                    else:
                        self.logger.warning(f"⚠️  EstimateOrder returned None for {amount:.8f} {base_symbol}")
                        self.logger.debug(f"🔍 Order variant was: base={base_currency.asset_id[:16]}, quote={quote_currency.asset_id[:16]}")

                    if order_match:
                        # Check which type of order match we have
                        if order_match.HasField('pair'):
                            pair_match = order_match.pair.pair_order_match
                            self.logger.debug(f"✅ Got pair order match for {amount:.8f} {base_symbol}")
                        elif order_match.HasField('swap'):
                            swap_matches = order_match.swap.pair_order_matches
                            self.logger.debug(f"✅ Got swap order match with {len(swap_matches)} hops for {amount:.8f} {base_symbol}")
                        else:
                            self.logger.warning(f"❌ OrderMatch has no pair or swap data for {amount:.8f} {base_symbol}")
                            self.logger.warning(f"   Reason: Empty response - liquidity likely insufficient at this size")
                            self.logger.debug(f"   OrderMatch structure: {order_match}")
                        
                        # Extract the cost and BTC amount from Hydra's response
                        hydra_cost = 0  # How much USDC we need to send
                        btc_received_gross = 0  # How much BTC we receive (before fees)
                        btc_fee = 0  # Fee in BTC
                        
                        # For PAIR matches, extract from pair_order_match
                        if order_match.HasField('pair'):
                            pair_match = order_match.pair.pair_order_match
                            
                            # sending_amount is what we're spending (USDC) 
                            if hasattr(pair_match, 'sending_amount') and pair_match.sending_amount:
                                if hasattr(pair_match.sending_amount, 'value'):
                                    hydra_cost = float(pair_match.sending_amount.value)
                            
                            # receiving_amount is gross BTC we receive
                            if hasattr(pair_match, 'receiving_amount') and pair_match.receiving_amount:
                                if hasattr(pair_match.receiving_amount, 'value'):
                                    btc_received_gross = float(pair_match.receiving_amount.value)
                            
                            # receiving_fee is the fee in BTC (deducted from receiving_amount)
                            if hasattr(pair_match, 'receiving_fee') and pair_match.receiving_fee:
                                if hasattr(pair_match.receiving_fee, 'value'):
                                    btc_fee = float(pair_match.receiving_fee.value)
                        
                        # For SWAP matches (multi-hop)
                        elif order_match.HasField('swap'):
                            swap_matches = order_match.swap.pair_order_matches
                            if swap_matches:
                                first_match = swap_matches[0]
                                last_match = swap_matches[-1]
                                # First match shows what we send
                                if hasattr(first_match, 'sending_amount') and first_match.sending_amount:
                                    if hasattr(first_match.sending_amount, 'value'):
                                        hydra_cost = float(first_match.sending_amount.value)
                                # Last match shows what we receive
                                if hasattr(last_match, 'receiving_amount') and last_match.receiving_amount:
                                    if hasattr(last_match.receiving_amount, 'value'):
                                        btc_received_gross = float(last_match.receiving_amount.value)
                                if hasattr(last_match, 'receiving_fee') and last_match.receiving_fee:
                                    if hasattr(last_match.receiving_fee, 'value'):
                                        btc_fee = float(last_match.receiving_fee.value)
                        
                        # Net BTC we actually get (after Hydra fees are deducted)
                        btc_received_net = btc_received_gross - btc_fee
                        
                        # Calculate what we get if we sell that NET BTC amount on external exchange
                        external_revenue = btc_received_net * sell_exchange_price
                        external_fee = external_revenue * external_fees
                        net_external_revenue = external_revenue - external_fee
                        
                        # Profit = what we get from external - what we spent on Hydra  
                        gross_profit = net_external_revenue - hydra_cost
                        profit_percentage = (gross_profit / hydra_cost * 100) if hydra_cost > 0 else 0
                        
                        self.logger.debug(f"📊 Buy Hydra → Sell External: ${gross_profit:.2f} profit ({profit_percentage:.2f}%)")
                        
                        scenarios.append({
                            'name': 'Buy Hydra → Sell External',
                            'gross_profit': gross_profit,
                            'profit_percentage': profit_percentage,
                            'buy_cost': hydra_cost,
                            'sell_revenue': net_external_revenue,
                            'fees': external_fee,  # Only external fees count since Hydra fees are deducted from BTC
                            'hydra_estimate': True,
                            'btc_net_received': btc_received_net
                        })
                    else:
                        self.logger.debug("EstimateOrder returned no match")
                        self._add_estimated_scenario(scenarios, 'buy', amount, buy_exchange_price, 
                                                    sell_exchange_price, external_fees)
                        
                except Exception as e:
                    self.logger.warning(f"EstimateOrder failed for buy scenario: {e}")
                    self.logger.warning(f"Falling back to estimated calculation")
                    self._add_estimated_scenario(scenarios, 'buy', amount, buy_exchange_price, 
                                                sell_exchange_price, external_fees)
            elif sell_exchange_price > 0:
                # No orderbook service, use estimate
                self._add_estimated_scenario(scenarios, 'buy', amount, buy_exchange_price, 
                                            sell_exchange_price, external_fees)
                    
            # Find the best scenario
            best_scenario = None
            if scenarios:
                best_scenario = max(scenarios, key=lambda s: s['gross_profit'])
                if best_scenario.get('hydra_estimate'):
                    self.logger.debug("Using actual EstimateOrder results")
                else:
                    self.logger.debug("Using estimated calculations")
                
            return {
                'scenarios': scenarios,
                'best_scenario': best_scenario,
                'amount': amount
            }
            
        except Exception as e:
            self.logger.error(f"Failed to calculate arbitrage profit: {e}")
            return {
                'scenarios': [],
                'best_scenario': None,
                'amount': amount
            }
            
    def _add_estimated_scenario(self, scenarios: list, side: str, amount: float,
                               buy_price: float, sell_price: float, external_fees: float):
        """
        Add an estimated scenario when EstimateOrder is not available
        
        Args:
            scenarios: List to append scenario to
            side: 'buy' or 'sell' on Hydra
            amount: Amount to trade
            buy_price: External buy price
            sell_price: External sell price
            external_fees: External exchange fee rate
        """
        hydra_fee_rate = 0.003  # Estimated 0.3% Hydra fee
        
        if side == 'sell':
            # Buy external, sell Hydra
            external_cost = amount * buy_price
            external_fee = external_cost * external_fees
            total_cost = external_cost + external_fee
            
            # Estimate Hydra sell with slippage
            estimated_hydra_price = sell_price * 0.99 if sell_price > 0 else buy_price * 1.005
            hydra_revenue = amount * estimated_hydra_price
            hydra_fee = hydra_revenue * hydra_fee_rate
            net_hydra_revenue = hydra_revenue - hydra_fee
            
            gross_profit = net_hydra_revenue - total_cost
            profit_percentage = (gross_profit / total_cost * 100) if total_cost > 0 else 0
            
            scenarios.append({
                'name': 'Buy External → Sell Hydra (est)',
                'gross_profit': gross_profit,
                'profit_percentage': profit_percentage,
                'buy_cost': total_cost,
                'sell_revenue': net_hydra_revenue,
                'fees': external_fee + hydra_fee,
                'hydra_estimate': False
            })
        else:
            # Buy Hydra, sell external
            estimated_hydra_price = buy_price * 1.01 if buy_price > 0 else sell_price * 0.995
            hydra_cost = amount * estimated_hydra_price
            hydra_fee = hydra_cost * hydra_fee_rate
            total_hydra_cost = hydra_cost + hydra_fee
            
            external_revenue = amount * sell_price
            external_fee = external_revenue * external_fees
            net_revenue = external_revenue - external_fee
            
            gross_profit = net_revenue - total_hydra_cost
            profit_percentage = (gross_profit / total_hydra_cost * 100) if total_hydra_cost > 0 else 0
            
            self.logger.debug(f"Estimated Buy Hydra → Sell External calculation:")
            self.logger.debug(f"  - Amount: {amount}")
            self.logger.debug(f"  - Buy price (external): ${buy_price:.2f}")
            self.logger.debug(f"  - Sell price (external): ${sell_price:.2f}")
            self.logger.debug(f"  - Estimated Hydra buy price: ${estimated_hydra_price:.2f} (external_buy * 1.01 or sell * 0.995)")
            self.logger.debug(f"  - Hydra cost: {amount} * ${estimated_hydra_price:.2f} = ${hydra_cost:.2f}")
            self.logger.debug(f"  - Hydra fee: ${hydra_cost:.2f} * {hydra_fee_rate} = ${hydra_fee:.2f}")
            self.logger.debug(f"  - Total Hydra buy cost: ${total_hydra_cost:.2f}")
            self.logger.debug(f"  - External revenue: {amount} * ${sell_price:.2f} = ${external_revenue:.2f}")
            self.logger.debug(f"  - External fee: ${external_fee:.2f}")
            self.logger.debug(f"  - Net revenue: ${net_revenue:.2f}")
            self.logger.debug(f"  - Gross profit: ${net_revenue:.2f} - ${total_hydra_cost:.2f} = ${gross_profit:.2f}")
            
            scenarios.append({
                'name': 'Buy Hydra → Sell External (est)',
                'gross_profit': gross_profit,
                'profit_percentage': profit_percentage,
                'buy_cost': total_hydra_cost,
                'sell_revenue': net_revenue,
                'fees': external_fee + hydra_fee,
                'hydra_estimate': False
            })