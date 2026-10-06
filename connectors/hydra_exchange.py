"""
Hydra DEX Exchange Connector
============================

Implementation of the BaseExchange interface for the Hydra DEX.
Wraps the existing Hydra gRPC client to provide the standardized interface.
"""

import asyncio
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# Add lib directory to path for imports
lib_path = Path(__file__).parent.parent / "lib"
sys.path.insert(0, str(lib_path))

from lib.grpc_client import HydraGRPCClient
from lib.hydra_pb import primitives_pb2, currency_pb2, orderbook_pb2
from lib.utils import decimal_to_float, float_to_decimal

from .base_exchange import (
    BaseExchange, TradingPair, OrderBook, Ticker, Balance, Order, Trade,
    OrderSide, OrderType, OrderStatus
)


class HydraExchange(BaseExchange):
    """Hydra DEX connector implementing the standard exchange interface"""
    
    def __init__(self, config: Dict):
        """
        Initialize Hydra exchange connector
        
        Args:
            config: Configuration containing host, port, and trading pairs
        """
        super().__init__(config)
        self.client: Optional[HydraGRPCClient] = None
        self.pair_mappings: Dict[str, Dict] = {}  # Maps pair strings to currency configs
        self._market_cache: Dict[str, Dict] = {}  # Cache currencies/precision per pair
        
        # Load pair mappings from config
        for pair_config in config.get('trading_pairs', []):
            base_symbol = pair_config.get('base_symbol', pair_config['base']['asset_id'][:8])
            quote_symbol = pair_config.get('quote_symbol', pair_config['quote']['asset_id'][:8])
            pair_key = f"{base_symbol}/{quote_symbol}"
            self.pair_mappings[pair_key] = pair_config
    
    async def connect(self) -> bool:
        """Connect to Hydra DEX"""
        try:
            self.client = HydraGRPCClient(
                host=self.config.get('host', 'localhost') or 'localhost',
                port=int(self.config.get('port', 5008) or 5008)
            )
            
            # Test connection by getting networks
            networks = self.client.get_networks()
            if networks:
                self.connected = True
                return True
            else:
                self.connected = False
                return False
                
        except Exception as e:
            print(f"❌ Failed to connect to Hydra DEX: {e}")
            self.connected = False
            return False
    
    async def disconnect(self):
        """Disconnect from Hydra DEX"""
        self.connected = False
        self.client = None
    
    async def get_trading_pairs(self) -> List[TradingPair]:
        """Get all configured trading pairs"""
        pairs = []
        for pair_key, pair_config in self.pair_mappings.items():
            base_symbol = pair_config.get('base_symbol', pair_config['base']['asset_id'][:8])
            quote_symbol = pair_config.get('quote_symbol', pair_config['quote']['asset_id'][:8])
            
            pairs.append(TradingPair(
                base=base_symbol,
                quote=quote_symbol,
                symbol=pair_key
            ))
        
        return pairs
    
    async def ensure_market_initialized(self, pair: TradingPair) -> bool:
        """Ensure a market is initialized on Hydra and cache its precision.

        Calls init_market via gRPC which is idempotent — safe to call
        even if the market is already initialized.
        """
        currencies = self._get_currencies_for_pair(pair)
        if not currencies:
            print(f"No currency config for {pair.symbol}")
            return False

        base_currency, quote_currency = currencies
        try:
            market_info = await asyncio.to_thread(
                self.client.init_market, base_currency, quote_currency
            )
            if market_info:
                base_precision = int(market_info.base_precision) if getattr(market_info, 'base_precision', 0) > 0 else 8
                quote_precision = int(market_info.quote_precision) if getattr(market_info, 'quote_precision', 0) > 0 else 6
                self._market_cache[pair.symbol] = {
                    'base_currency': base_currency,
                    'quote_currency': quote_currency,
                    'base_precision': base_precision,
                    'quote_precision': quote_precision,
                }
                print(f"Market {pair.symbol} initialized: base_precision={base_precision}, quote_precision={quote_precision}")
                return True
            else:
                print(f"Market {pair.symbol} init returned no info")
                return False
        except Exception as e:
            print(f"Failed to initialize market {pair.symbol}: {e}")
            return False

    async def init_all_markets(self) -> Dict[str, Dict[str, Any]]:
        """Initialize every configured trading pair via `InitMarket` (idempotent).

        Returns a per-symbol dict with what the exchange reported back:
          - ok: bool (True if the market is now initialized)
          - error: str (set when ok=False)
          - already_initialized: bool (True if the server didn't change state)
          - base_precision / quote_precision: int
          - min_base_amount / min_quote_amount: float (0.0 if unset)
          - taker_base_fee / taker_quote_fee: float (fee ratios)
          - maker_base_fee / maker_quote_fee: float

        Idempotent — safe to run on a node that already has markets initialized.
        """
        if not self.client:
            return {sym: {"ok": False, "error": "exchange not connected"}
                    for sym in self.pair_mappings}

        # Snapshot what's already initialized so we can tell the user which
        # pairs were touched vs which were already live.
        already_set: set = set()
        try:
            existing = await asyncio.to_thread(self.client.get_initialized_markets)
            for pair_info in (existing or []):
                # CurrencyInfoPair has base / quote CurrencyInfo with asset_id
                try:
                    base_id = pair_info.base.asset_id
                    quote_id = pair_info.quote.asset_id
                    already_set.add((base_id.lower(), quote_id.lower()))
                    # Markets are symmetric — accept either orientation
                    already_set.add((quote_id.lower(), base_id.lower()))
                except Exception:
                    continue
        except Exception:
            pass  # not fatal — we just lose the "already initialized" hint

        results: Dict[str, Dict[str, Any]] = {}
        for symbol, pair_config in self.pair_mappings.items():
            base_symbol = pair_config.get('base_symbol')
            quote_symbol = pair_config.get('quote_symbol')
            pair = TradingPair(base=base_symbol, quote=quote_symbol, symbol=symbol)
            currencies = self._get_currencies_for_pair(pair)
            if not currencies:
                results[symbol] = {"ok": False, "error": "no currency mapping"}
                continue

            base_currency, quote_currency = currencies
            key = (base_currency.asset_id.lower(), quote_currency.asset_id.lower())
            already = key in already_set

            try:
                mi = await asyncio.to_thread(
                    self.client.init_market, base_currency, quote_currency
                )
            except Exception as e:
                results[symbol] = {"ok": False, "error": f"init_market raised: {e}"}
                continue

            if mi is None:
                results[symbol] = {
                    "ok": False,
                    "error": "init_market returned no MarketInfo",
                    "already_initialized": already,
                }
                continue

            def _dec(field) -> float:
                """Pull a DecimalString -> float, 0.0 if unset."""
                try:
                    return decimal_to_float(field) if field and field.value else 0.0
                except Exception:
                    return 0.0

            base_precision = int(mi.base_precision) if getattr(mi, 'base_precision', 0) > 0 else 8
            quote_precision = int(mi.quote_precision) if getattr(mi, 'quote_precision', 0) > 0 else 6
            self._market_cache[symbol] = {
                'base_currency': base_currency,
                'quote_currency': quote_currency,
                'base_precision': base_precision,
                'quote_precision': quote_precision,
            }
            results[symbol] = {
                "ok": True,
                "already_initialized": already,
                "base_precision": base_precision,
                "quote_precision": quote_precision,
                "min_base_amount": _dec(getattr(mi, 'min_base_amount', None)),
                "min_quote_amount": _dec(getattr(mi, 'min_quote_amount', None)),
                "taker_base_fee": _dec(getattr(mi, 'taker_base_fee', None)),
                "taker_quote_fee": _dec(getattr(mi, 'taker_quote_fee', None)),
                "maker_base_fee": _dec(getattr(mi, 'maker_base_fee', None)),
                "maker_quote_fee": _dec(getattr(mi, 'maker_quote_fee', None)),
            }

        return results

    def _get_currencies_for_pair(self, pair: TradingPair) -> Optional[tuple]:
        """Get Hydra currency objects for a trading pair"""
        if pair.symbol not in self.pair_mappings:
            return None
        
        pair_config = self.pair_mappings[pair.symbol]
        
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
        
        return base_currency, quote_currency

    def _asset_matches(self, symbol: str, config_asset: Optional[str], market_asset: str, market_currency) -> bool:
        """
        Check if a market asset matches the requested symbol.
        Uses exact contract address if config is available, otherwise uses symbol-based matching.
        """
        # If we have a config address, use exact matching (most reliable)
        if config_asset:
            return market_asset.lower() == config_asset.lower()

        # Otherwise, use symbol-based pattern matching
        # BTC matching (Bitcoin protocol with zero asset ID)
        if symbol == "BTC" and market_currency.protocol == 1 and "0x000000" in market_asset:
            return True

        # ETH matching (EVM protocol with zero asset ID)
        if symbol == "ETH" and market_currency.protocol == 2 and "0x000000" in market_asset:
            return True

        # ERC20 tokens (USDC, USDT, DAI, HDN, etc.) - match by ERC20 prefix only if no config
        if symbol in ["USDC", "USDT", "DAI", "HDN"] and "ERC20:" in market_asset:
            return True

        return False

    def _discover_currencies_for_pair(self, pair: TradingPair) -> Optional[tuple]:
        """
        Dynamically discover correct currency objects for a trading pair by matching
        with initialized markets from Hydra. Since market orientation can change on Hydra,
        we search for any market containing both assets regardless of which is base/quote.
        """
        if not self.client:
            return None

        # Get asset addresses from config (if available) to match exact contracts
        config_base_asset = None
        config_quote_asset = None
        config_currencies = self._get_currencies_for_pair(pair)
        if config_currencies:
            config_base_asset = config_currencies[0].asset_id
            config_quote_asset = config_currencies[1].asset_id

        # Auto-discover from initialized markets
        # Since market orientation can change on Hydra, search for any market
        # containing both assets, regardless of which is base/quote
        initialized_markets = self.client.get_initialized_markets()

        # Parse the trading pair symbol (e.g., "BTC/USDC" -> "BTC", "USDC")
        base_symbol, quote_symbol = pair.symbol.split('/')

        # Collect ALL matching markets (market might be in either orientation)
        matching_markets = []

        # Look for markets with EXACT orientation match
        # User must specify pair in same orientation as Hydra has it
        for market in initialized_markets:
            market_base = market.base.asset_id
            market_quote = market.quote.asset_id

            # Check if market matches user's requested orientation EXACTLY
            # User's base must match market's base, user's quote must match market's quote
            user_base_matches_market_base = self._asset_matches(base_symbol, config_base_asset, market_base, market.base)
            user_quote_matches_market_quote = self._asset_matches(quote_symbol, config_quote_asset, market_quote, market.quote)

            # ONLY match if orientation is exact (no reversed matching)
            if user_base_matches_market_base and user_quote_matches_market_quote:
                matching_markets.append(market)

        if not matching_markets:
            return None

        # If multiple matches, prefer markets with liquidity
        if len(matching_markets) > 1:
            # Check all markets and rank by liquidity depth
            market_scores = []
            for market in matching_markets:
                base_currency = currency_pb2.OrderbookCurrency(
                    protocol=market.base.protocol,
                    network_id=market.base.network_id,
                    asset_id=market.base.asset_id
                )
                quote_currency = currency_pb2.OrderbookCurrency(
                    protocol=market.quote.protocol,
                    network_id=market.quote.network_id,
                    asset_id=market.quote.asset_id
                )

                # Check liquidity depth for this market
                try:
                    test_orderbook = self.client.get_orderbook(base_currency, quote_currency)
                    if test_orderbook and hasattr(test_orderbook, 'orders'):
                        order_count = len(test_orderbook.orders)
                        if order_count > 0:
                            market_scores.append({
                                'market': market,
                                'base_currency': base_currency,
                                'quote_currency': quote_currency,
                                'order_count': order_count
                            })
                except:
                    pass

            # If we found markets with liquidity, use the one with most orders
            if market_scores:
                # Sort by order count (descending)
                market_scores.sort(key=lambda x: x['order_count'], reverse=True)
                best_market = market_scores[0]
                return best_market['base_currency'], best_market['quote_currency']

        # Use the first matching market - return in MARKET'S actual orientation
        # User should use 'marketinfo' command first to check correct orientation
        market = matching_markets[0]

        base_currency = currency_pb2.OrderbookCurrency(
            protocol=market.base.protocol,
            network_id=market.base.network_id,
            asset_id=market.base.asset_id
        )

        quote_currency = currency_pb2.OrderbookCurrency(
            protocol=market.quote.protocol,
            network_id=market.quote.network_id,
            asset_id=market.quote.asset_id
        )

        return base_currency, quote_currency

    async def get_orderbook(self, pair: TradingPair, limit: int = 100) -> Optional[OrderBook]:
        """Get orderbook for a trading pair"""
        if not self.client:
            return None

        currencies = self._discover_currencies_for_pair(pair)
        if not currencies:
            return None

        base_currency, quote_currency = currencies

        try:
            # Use EXACTLY what the user requested - no automatic reversal
            # User should check orientation with 'marketinfo' command first
            # Run in thread to avoid blocking the event loop (gRPC call is synchronous)
            hydra_orderbook = await asyncio.to_thread(self.client.get_orderbook, base_currency, quote_currency)

            if not hydra_orderbook or not hasattr(hydra_orderbook, 'orders') or len(hydra_orderbook.orders) == 0:
                return None
            
            # Convert new Hydra orderbook structure to standardized format
            # NEW STRUCTURE: hydra_orderbook now has:
            # - buy_liquidity_orders = bids (people wanting to buy base with quote)
            # - sell_liquidity_orders = asks (people wanting to sell base for quote)
            # Each is a list of LiquidityRangeOrdersMap with price ranges

            bids = []
            asks = []
            total_bid_usdc = 0
            total_bid_btc = 0
            total_ask_usdc = 0
            total_ask_btc = 0

            # Process orders from the new Hydra orderbook structure
            # NEW STRUCTURE: LiquidityPosition has simplified fields:
            # - side: OrderSide (BUY = 0, SELL = 1) - BUT might be missing in response!
            # - price: single price level
            # - amount: unified amount (base for sell, quote for buy)
            # - matched_amount: how much has been matched
            for order_id, liquidity_position in hydra_orderbook.orders.items():
                try:
                    # Extract fields from new structure
                    # NOTE: In proto3, enum fields with value 0 are not serialized (omitted from wire format)
                    # but they ARE accessible as attributes and default to 0 if not set.
                    # So we can always access liquidity_position.side safely.
                    side = liquidity_position.side  # 0 = BUY, 1 = SELL (defaults to 0 if not in proto)

                    price = decimal_to_float(liquidity_position.price)
                    amount = decimal_to_float(liquidity_position.amount)
                    matched_amount = decimal_to_float(liquidity_position.matched_amount)
                    remaining_amount = amount - matched_amount

                    if remaining_amount <= 0:
                        continue  # Skip fully matched orders

                    # Add to bids if BUY order (wanting to buy base with quote)
                    # ORDER_SIDE_BUY=1, ORDER_SIDE_SELL=2
                    if side == 1:  # ORDER_SIDE_BUY
                        # amount is in quote currency to spend
                        # price is quote per base
                        # Calculate base amount that can be bought
                        base_amount_available = remaining_amount / price if price > 0 else 0
                        if base_amount_available > 0:
                            bids.append((price, base_amount_available))
                            total_bid_usdc += remaining_amount
                            total_bid_btc += base_amount_available

                    # Add to asks if SELL order (wanting to sell base for quote)
                    elif side == 2:  # ORDER_SIDE_SELL
                        # amount is in base currency to sell
                        # price is quote per base
                        if remaining_amount > 0:
                            asks.append((price, remaining_amount))
                            total_ask_btc += remaining_amount
                            total_ask_usdc += remaining_amount * price

                except Exception as e:
                    # Silently skip malformed orders
                    continue

            # Sort orderbook levels: bids highest price first, asks lowest price first
            bids.sort(key=lambda x: x[0], reverse=True)  # Highest price first for bids
            asks.sort(key=lambda x: x[0])  # Lowest price first for asks

            return OrderBook(
                pair=pair,
                bids=bids,
                asks=asks,
                timestamp=time.time()
            )

        except Exception as e:
            print(f"❌ Error getting orderbook for {pair}: {e}")
            return None
    
    def validate_calculation_with_estimate(self, pair: TradingPair, side: str, amount: float, price: float = None) -> Dict:
        """
        Validate our orderbook calculations using Hydra's EstimateOrder API
        
        Args:
            pair: Trading pair (e.g., BTC/USDC)
            side: 'buy' or 'sell'
            amount: Amount to trade in base currency (BTC)
            price: Optional price for limit orders
            
        Returns:
            Dict with validation results
        """
        if not self.client:
            return {'error': 'No client available'}
        
        currencies = self._get_currencies_for_pair(pair)
        if not currencies:
            return {'error': f'No currency mapping for {pair.symbol}'}
        
        base_currency, quote_currency = currencies
        
        try:
            # Create the appropriate OrderVariant for EstimateOrder
            if side.lower() == 'buy':
                # For BUY: we send quote currency (USDC) to get base currency (BTC)
                # Calculate quote amount needed
                if price is None:
                    # Market order - get current ask price
                    orderbook = self.client.get_orderbook(base_currency, quote_currency)
                    if not orderbook or not orderbook.asks:
                        return {'error': 'No ask liquidity available'}
                    
                    # Use boundary price for execution: min_price for buying (cheapest we can buy)
                    min_ask = decimal_to_float(orderbook.asks[0].min_price)
                    max_ask = decimal_to_float(orderbook.asks[0].max_price)
                    price = min_ask  # Use min_price for buying - the actual execution price
                
                quote_amount = amount * price  # USDC amount to spend
                
                # Create market order
                order_amount = orderbook_pb2.OrderAmount()
                order_amount.quote.amount.value = str(quote_amount)
                
                market_order = orderbook_pb2.OrderVariant.MarketOrder(
                    base=base_currency,
                    quote=quote_currency,
                    amount=order_amount,
                    side=orderbook_pb2.BUY
                )
                order_variant = orderbook_pb2.OrderVariant(market_order=market_order)
                
            else:  # sell
                # For SELL: we send base currency (BTC) to get quote currency (USDC)
                order_amount = orderbook_pb2.OrderAmount()
                order_amount.base.amount.value = str(amount)
                
                market_order = orderbook_pb2.OrderVariant.MarketOrder(
                    base=base_currency,
                    quote=quote_currency,
                    amount=order_amount,
                    side=orderbook_pb2.SELL
                )
                order_variant = orderbook_pb2.OrderVariant(market_order=market_order)
            
            # Call EstimateOrder
            order_match = self.client.estimate_order(order_variant)
            
            if not order_match:
                return {'error': 'EstimateOrder returned no match'}
            
            # Parse the result
            result = {'success': True}
            
            if hasattr(order_match, 'pair') and order_match.pair:
                pair_match = order_match.pair.pair_order_match
                
                # Extract amounts
                sending_amount = decimal_to_float(pair_match.sending_amount)
                receiving_amount = decimal_to_float(pair_match.receiving_amount)
                receiving_fee = decimal_to_float(pair_match.receiving_fee)
                
                result.update({
                    'sending_amount': sending_amount,
                    'receiving_amount': receiving_amount,
                    'receiving_fee': receiving_fee,
                    'sending_currency': pair_match.sending_currency.asset_id[:8],
                    'receiving_currency': pair_match.receiving_currency.asset_id[:8]
                })
                
                # Calculate effective price
                if side.lower() == 'buy':
                    # We sent USDC, received BTC
                    if receiving_amount > 0:
                        effective_price = sending_amount / receiving_amount
                        result['effective_price'] = effective_price
                        result['price_difference'] = abs(effective_price - price) if price else 0
                        result['price_difference_pct'] = (abs(effective_price - price) / price * 100) if price and price > 0 else 0
                else:  # sell
                    # We sent BTC, received USDC
                    if sending_amount > 0:
                        effective_price = receiving_amount / sending_amount
                        result['effective_price'] = effective_price
                        result['price_difference'] = abs(effective_price - price) if price else 0
                        result['price_difference_pct'] = (abs(effective_price - price) / price * 100) if price and price > 0 else 0
                
                # Add validation status
                result['validation_status'] = 'MATCH' if result.get('price_difference_pct', 0) < 1.0 else 'MISMATCH'
                
            return result
            
        except Exception as e:
            return {'error': f'Validation failed: {str(e)}'}
    
    async def validate_orderbook_level(self, pair: TradingPair, level_index: int = 0) -> Dict:
        """
        Validate a specific orderbook level using EstimateOrder
        
        Args:
            pair: Trading pair
            level_index: Which level to validate (0 = best)
            
        Returns:
            Dict with validation results for both bid and ask
        """
        try:
            # Get current orderbook
            orderbook = await self.get_orderbook(pair, limit=level_index + 1)
            if not orderbook:
                return {'error': 'No orderbook available'}
            
            if len(orderbook.bids) <= level_index:
                return {'error': f'Not enough bid levels (need {level_index + 1})'}
            
            if len(orderbook.asks) <= level_index:
                return {'error': f'Not enough ask levels (need {level_index + 1})'}
            
            results = {}
            
            # Validate bid level (someone wants to buy BTC with USDC)
            bid_data = orderbook.bids[level_index]
            if len(bid_data) >= 4:  # (avg_price, base_amount, min_price, max_price)
                avg_price, base_amount, min_price, max_price = bid_data[:4]
                
                # Test our calculation vs EstimateOrder
                bid_validation = self.validate_calculation_with_estimate(
                    pair, 'sell', base_amount, avg_price
                )
                results['bid'] = {
                    'level': level_index,
                    'our_calculation': {
                        'avg_price': avg_price,
                        'base_amount': base_amount,
                        'price_range': f"${min_price:.2f}-${max_price:.2f}"
                    },
                    'estimate_order_result': bid_validation
                }
            
            # Validate ask level (someone wants to sell BTC for USDC)
            ask_data = orderbook.asks[level_index]
            if len(ask_data) >= 4:  # (avg_price, base_amount, min_price, max_price)
                avg_price, base_amount, min_price, max_price = ask_data[:4]
                
                # Test our calculation vs EstimateOrder
                ask_validation = self.validate_calculation_with_estimate(
                    pair, 'buy', base_amount, avg_price
                )
                results['ask'] = {
                    'level': level_index,
                    'our_calculation': {
                        'avg_price': avg_price,
                        'base_amount': base_amount,
                        'price_range': f"${min_price:.2f}-${max_price:.2f}"
                    },
                    'estimate_order_result': ask_validation
                }
            
            return results
            
        except Exception as e:
            return {'error': f'Validation failed: {str(e)}'}
    
    def print_validation_results(self, results: Dict):
        """Print validation results in a formatted way"""
        if 'error' in results:
            print(f"❌ Validation Error: {results['error']}")
            return
        
        print("🔍 EstimateOrder Validation Results:")
        print("=" * 50)
        
        for side in ['bid', 'ask']:
            if side in results:
                result = results[side]
                print(f"\n📊 {side.upper()} Level {result['level']}:")
                calc = result['our_calculation']
                estimate = result['estimate_order_result']
                
                print(f"  Our Calculation:")
                print(f"    Price Range: {calc['price_range']}")
                print(f"    Avg Price: ${calc['avg_price']:.2f}")
                print(f"    Amount: {calc['base_amount']:.6f} BTC")
                
                if 'error' not in estimate:
                    print(f"  EstimateOrder Result:")
                    print(f"    Effective Price: ${estimate.get('effective_price', 0):.2f}")
                    print(f"    Sending: {estimate.get('sending_amount', 0):.6f} {estimate.get('sending_currency', '')}")
                    print(f"    Receiving: {estimate.get('receiving_amount', 0):.6f} {estimate.get('receiving_currency', '')}")
                    print(f"    Fee: {estimate.get('receiving_fee', 0):.6f}")
                    
                    status = estimate.get('validation_status', 'UNKNOWN')
                    diff_pct = estimate.get('price_difference_pct', 0)
                    if status == 'MATCH':
                        print(f"    ✅ Validation: {status} (diff: {diff_pct:.2f}%)")
                    else:
                        print(f"    ⚠️  Validation: {status} (diff: {diff_pct:.2f}%)")
                else:
                    print(f"    ❌ EstimateOrder Error: {estimate['error']}")
        
        print("=" * 50)
    
    async def get_ticker(self, pair: TradingPair) -> Optional[Ticker]:
        """Get ticker information"""
        orderbook = await self.get_orderbook(pair)
        if not orderbook:
            print(f"🔍 No orderbook for {pair.symbol} on Hydra")
            return None
        
        bid = orderbook.bids[0][0] if orderbook.bids else None
        ask = orderbook.asks[0][0] if orderbook.asks else None
        last = (bid + ask) / 2 if bid and ask else None
        
        
        return Ticker(
            pair=pair,
            bid=bid,
            ask=ask,
            last=last,
            timestamp=time.time()
        )
    
    async def get_balances(self) -> Dict[str, Balance]:
        """
        Get account balances that are available for trading in the orderbook

        Uses GetOrderbookBalances() API which returns tradeable amounts,
        not Channel balances which include locked/in-transit funds.
        """
        if not self.client:
            return {}

        balances = {}

        # Create reverse mapping from (network_id, asset_id) to symbol
        network_asset_to_symbol = {}

        # First, add mappings from configured pairs
        for pair_config in self.pair_mappings.values():
            base_key = (pair_config['base']['network_id'], pair_config['base']['asset_id'])
            quote_key = (pair_config['quote']['network_id'], pair_config['quote']['asset_id'])
            base_symbol = pair_config.get('base_symbol', pair_config['base']['asset_id'][:8])
            quote_symbol = pair_config.get('quote_symbol', pair_config['quote']['asset_id'][:8])

            network_asset_to_symbol[base_key] = base_symbol
            network_asset_to_symbol[quote_key] = quote_symbol

        # Add common native asset mappings for known networks
        common_mappings = {
            # Ethereum Sepolia
            ('11155111', '0x0000000000000000000000000000000000000000'): 'ETH',
            # Arbitrum Sepolia
            ('421614', '0x0000000000000000000000000000000000000000'): 'ETH',
            # Bitcoin Signet
            ('0a03cf40', '0x0000000000000000000000000000000000000000000000000000000000000000'): 'BTC',
            # Common USDC on Ethereum Sepolia
            ('11155111', 'ERC20:0x8cd0da3d001b013336918b8bc4e56d9dda1347e0'): 'USDC',
        }

        # Add common mappings if not already present
        for key, symbol in common_mappings.items():
            if key not in network_asset_to_symbol:
                network_asset_to_symbol[key] = symbol

        try:
            # Get orderbook balances (tradeable amounts only)
            orderbook_balances = self.client.get_orderbook_balances()

            for orderbook_balance in orderbook_balances:
                # Extract currency info
                currency = orderbook_balance.currency
                balance = orderbook_balance.balance

                network_id = currency.network_id
                asset_id = currency.asset_id

                # Map (network_id, asset_id) to proper symbol
                network_asset_key = (network_id, asset_id)
                asset_symbol = network_asset_to_symbol.get(network_asset_key, asset_id[:8] + "..." if len(asset_id) > 8 else asset_id)

                # Add network info to distinguish between same assets on different chains
                network_names = {
                    '11155111': 'Ethereum Sepolia',
                    '421614': 'Arbitrum Sepolia',
                    '0a03cf40': 'Bitcoin Signet',
                }
                network_name = network_names.get(network_id, f'Network {network_id[:8]}')

                # Create display key: if we already have this symbol, add network suffix
                display_key = asset_symbol
                if asset_symbol in balances:
                    display_key = f"{asset_symbol} ({network_name})"

                # Extract balances from CurrencyBalance
                # "sending" = available for trading (what we can send in orders)
                # "in_use_sending" = locked in existing orders
                free_balance = decimal_to_float(balance.sending) if balance.sending else 0
                locked_balance = decimal_to_float(balance.in_use_sending) if balance.in_use_sending else 0

                # Also include receiving amounts (can receive in trades)
                receiving = decimal_to_float(balance.receiving) if balance.receiving else 0
                in_use_receiving = decimal_to_float(balance.in_use_receiving) if balance.in_use_receiving else 0

                # Total tradeable = what we can send + what we can receive
                total_balance = free_balance + locked_balance + receiving + in_use_receiving

                if total_balance > 0:  # Only include assets with balance
                    balances[display_key] = Balance(
                        asset=display_key,
                        free=free_balance,  # Available to trade
                        locked=locked_balance,  # Locked in orders
                        total=total_balance
                    )

        except Exception as e:
            print(f"❌ Error getting orderbook balances: {e}")
            import traceback
            traceback.print_exc()

        return balances
    
    async def place_order(
        self,
        pair: TradingPair,
        side: OrderSide,
        type: OrderType,
        amount: float,
        price: Optional[float] = None,
        params: Optional[Dict] = None
    ) -> Optional[Order]:
        """Place an order on Hydra DEX"""
        if not self.client:
            return None

        # Use cached currencies/precision if available, otherwise discover and cache
        cache_key = pair.symbol
        if cache_key not in self._market_cache:
            currencies = self._discover_currencies_for_pair(pair)
            if not currencies:
                return None
            base_currency, quote_currency = currencies

            # Initialize market and cache the result
            base_precision = 8  # fallback
            quote_precision = 6  # fallback
            try:
                market_info = await asyncio.to_thread(
                    self.client.init_market, base_currency, quote_currency
                )
                if market_info:
                    # Protobuf uint32 defaults to 0, so only use if non-zero
                    if getattr(market_info, 'base_precision', 0) > 0:
                        base_precision = int(market_info.base_precision)
                    if getattr(market_info, 'quote_precision', 0) > 0:
                        quote_precision = int(market_info.quote_precision)
                    print(f"✅ Market {pair.symbol} cached: base_precision={base_precision}, quote_precision={quote_precision}")
                else:
                    print(f"⚠️  Market initialization returned no info for {pair.symbol}, using fallback precision")
            except Exception as e:
                print(f"⚠️  Market initialization failed: {e}, using fallback precision")

            self._market_cache[cache_key] = {
                'base_currency': base_currency,
                'quote_currency': quote_currency,
                'base_precision': base_precision,
                'quote_precision': quote_precision,
            }

        cached = self._market_cache[cache_key]
        base_currency = cached['base_currency']
        quote_currency = cached['quote_currency']
        base_precision = cached['base_precision']
        quote_precision = cached['quote_precision']

        try:
            # For market orders, get current price only if not already provided
            if type == OrderType.MARKET and price is None:
                orderbook = await self.get_orderbook(pair)

                if not orderbook:
                    return None

                if side == OrderSide.BUY:
                    price = orderbook.asks[0][0] if orderbook.asks else None
                else:
                    price = orderbook.bids[0][0] if orderbook.bids else None

                if not price:
                    print(f"❌ No liquidity available for {side.value} orders on {pair.symbol}")
                    return None

            # Place order using Hydra's new Limit/Market order structure
            # - OrderAmount.Quote (quote currency amount) = BUY (buying base with quote)
            # - OrderAmount.Base (base currency amount) = SELL (selling base for quote)

            if side == OrderSide.BUY:
                # BUY: Use quote amount (buying base currency with quote currency)
                base_amount = primitives_pb2.DecimalString()  # Empty for buy orders
                quote_amount = float_to_decimal(amount * price, precision=quote_precision)
            else:
                # SELL: Use base amount (selling base currency for quote currency)
                base_amount = float_to_decimal(amount, precision=base_precision)
                quote_amount = primitives_pb2.DecimalString()  # Empty for sell orders

            # Set price (same for both limit and market orders)
            order_price = float_to_decimal(price, precision=quote_precision)

            # Run in thread to avoid blocking the event loop (gRPC call is synchronous).
            # params: time_in_force ('post_only'|'ioc'|'fok'), client_order_id, self_trade_prevention.
            p = params or {}
            order_id, refusal = await asyncio.to_thread(
                self.client.place_limit_order_ex,
                base_currency=base_currency,
                quote_currency=quote_currency,
                base_amount=base_amount,
                quote_amount=quote_amount,
                min_buy_price=order_price,
                mid_price=order_price,
                max_sell_price=order_price,
                remove_on_fill=(type == OrderType.MARKET),
                time_in_force=p.get('time_in_force'),
                client_order_id=p.get('client_order_id'),
                self_trade_prevention=p.get('self_trade_prevention'),
            )
            # why the last placement on this market/side was refused (read by the strategy)
            self.last_refusal = getattr(self, 'last_refusal', {})
            self.last_refusal[(pair.symbol, side)] = refusal

            if order_id:
                return Order(
                    id=order_id,
                    pair=pair,
                    side=side,
                    type=type,
                    amount=amount,
                    price=price,
                    filled=0.0,
                    remaining=amount,
                    status=OrderStatus.OPEN,
                    timestamp=time.time()
                )

        except Exception as e:
            print(f"❌ Error placing order: {e}")
            import traceback
            traceback.print_exc()

            print(f"\n🛑 STOPPING BOT DUE TO ORDER FAILURE")
            from lib.crash_notifier import notify_and_exit
            notify_and_exit(f"Order failure on {pair.symbol}: {e}")

        return None
    
    async def cancel_order(self, order_id: str, pair: TradingPair) -> bool:
        """Cancel an order"""
        if not self.client:
            return False
        
        try:
            from hydra_pb import orderbook_pb2
            response = self.client.orderbook_stub.CancelOrder(
                orderbook_pb2.CancelOrderRequest(order_id=order_id)
            )
            return response.removed if response else False
            
        except Exception as e:
            print(f"❌ Error canceling order {order_id}: {e}")
            return False
    
    async def get_order(self, order_id: str, pair: TradingPair) -> Optional[Order]:
        """Get order information"""
        if not self.client:
            return None
        
        currencies = self._discover_currencies_for_pair(pair)
        if not currencies:
            return None
        
        base_currency, quote_currency = currencies
        
        try:
            orders = self.client.get_own_orders(base_currency, quote_currency)
            if order_id in orders:
                hydra_order = orders[order_id]
                # Convert Hydra order to standardized format
                # This would require parsing the order structure
                # Implementation depends on Hydra order format
                pass
        
        except Exception as e:
            print(f"❌ Error getting order {order_id}: {e}")
        
        return None
    
    async def get_orders(self, pair: TradingPair, status: Optional[OrderStatus] = None) -> List[Order]:
        """Get own orders for a trading pair with remaining amounts."""
        if not self.client:
            return []

        currencies = self._get_currencies_for_pair(pair)
        if not currencies:
            return []

        try:
            base_currency, quote_currency = currencies
            hydra_orders = await asyncio.to_thread(
                self.client.get_own_orders, base_currency, quote_currency
            )

            orders = []
            for order_id, order_info in hydra_orders.items():
                try:
                    # Extract price and remaining amount from the Hydra order
                    price = 0.0
                    remaining = 0.0
                    side = OrderSide.BUY

                    if hasattr(order_info, 'pair_order') and order_info.pair_order:
                        po = order_info.pair_order
                        lo = getattr(po, 'limit_order', None) or getattr(po, 'market_order', None)
                        if lo:
                            if hasattr(lo, 'price') and lo.price:
                                price = decimal_to_float(lo.price)
                            # Side is on OrderSideVariant.variant (oneof: buy / sell).
                            # Older proto had a flat enum 'side' — handle both for safety.
                            variant = getattr(lo, 'variant', None)
                            if variant is not None and hasattr(variant, 'WhichOneof'):
                                which = variant.WhichOneof('side')
                                if which == 'sell':
                                    side = OrderSide.SELL
                                elif which == 'buy':
                                    side = OrderSide.BUY
                            elif hasattr(lo, 'side') and lo.side:
                                side = OrderSide.SELL if lo.side == 2 else OrderSide.BUY
                            # remaining_amount is an OrderAmount oneof (base | quote).
                            # SELL orders carry it in base; BUY orders carry it in
                            # quote. Always normalize to BASE units so the grid
                            # strategy can compare amounts consistently.
                            ra = getattr(lo, 'remaining_amount', None)
                            if ra is not None:
                                which_amt = ra.WhichOneof('amount') if hasattr(ra, 'WhichOneof') else None
                                if which_amt == 'base':
                                    remaining = decimal_to_float(ra.base.amount)
                                elif which_amt == 'quote':
                                    quote_remaining = decimal_to_float(ra.quote.amount)
                                    # Convert quote→base using the order price
                                    remaining = (quote_remaining / price) if price > 0 else 0.0
                                else:
                                    # Fallback for older proto shapes
                                    if hasattr(ra, 'base') and ra.base and ra.base.amount:
                                        remaining = decimal_to_float(ra.base.amount)
                                    elif hasattr(ra, 'quote') and ra.quote and ra.quote.amount and price > 0:
                                        remaining = decimal_to_float(ra.quote.amount) / price

                    orders.append(Order(
                        id=order_id,
                        pair=pair,
                        side=side,
                        type=OrderType.LIMIT,
                        amount=remaining,
                        price=price,
                        filled=0.0,
                        remaining=remaining,
                        status=OrderStatus.OPEN,
                        timestamp=time.time()
                    ))
                except Exception:
                    continue

            return orders

        except Exception as e:
            print(f"❌ Error getting orders for {pair}: {e}")
            return []
    
    async def get_trades(self, pair: TradingPair, limit: int = 100) -> List[Trade]:
        """Get recent trades"""
        if not self.client:
            return []
        
        currencies = self._get_currencies_for_pair(pair)
        if not currencies:
            return []
        
        try:
            base_currency, quote_currency = currencies
            hydra_trades = self.client.get_trade_history(base_currency, quote_currency)
            
            trades = []
            for hydra_trade in hydra_trades[:limit]:
                # Convert to standardized format
                price = decimal_to_float(hydra_trade.price)
                amount = decimal_to_float(hydra_trade.amount) if hasattr(hydra_trade, 'amount') else 0
                
                trades.append(Trade(
                    id=str(hydra_trade.timestamp.seconds),  # Use timestamp as ID
                    order_id="",  # Not available from trade history
                    pair=pair,
                    side=OrderSide.BUY,  # Would need to determine from trade data
                    amount=amount,
                    price=price,
                    fee=0.0,  # Calculate from trading fees
                    timestamp=hydra_trade.timestamp.seconds
                ))
            
            return trades
            
        except Exception as e:
            print(f"❌ Error getting trades for {pair}: {e}")
            return []