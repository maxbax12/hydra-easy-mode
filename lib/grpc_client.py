import grpc
import re
from typing import Optional, Dict, Any
from pathlib import Path
from dotenv import load_dotenv
import datetime

# Import generated proto files
from lib.hydra_pb import app_pb2, app_pb2_grpc
from lib.hydra_pb import wallet_pb2, wallet_pb2_grpc
from lib.hydra_pb import client_pb2, client_pb2_grpc
from lib.hydra_pb import node_pb2, node_pb2_grpc
from lib.hydra_pb import blockchain_pb2, blockchain_pb2_grpc
from lib.hydra_pb import primitives_pb2, currency_pb2, balance_pb2, fee_pb2, transaction_pb2
from lib.hydra_pb import event_pb2, event_pb2_grpc
from lib.hydra_pb import watch_only_node_pb2, watch_only_node_pb2_grpc
from lib.hydra_pb import asset_pb2, asset_pb2_grpc
from lib.hydra_pb import pricing_pb2, pricing_pb2_grpc
from lib.hydra_pb import orderbook_pb2, orderbook_pb2_grpc
from lib.hydra_pb import liquidity_pb2, liquidity_pb2_grpc

load_dotenv(Path(__file__).resolve().parent.parent / '.env')

class HydraGRPCClient:
    def __init__(self, host: str = "localhost", port: int = 5008, log_callback=None):
        """Initialize the gRPC client"""
        self.host = host
        self.port = port
        self.channel = None
        self.log_callback = log_callback
        self.connect()
    
    def _log(self, level: str, message: str):
        """Internal logging method"""
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        if self.log_callback:
            self.log_callback({
                'timestamp': timestamp,
                'level': level,
                'message': message
            })
        else:
            print(f"[{level}] {timestamp} - {message}")
    
    def connect(self):
        """Establish connection to gRPC server"""
        server_address = f"{self.host}:{self.port}"
        self.channel = grpc.insecure_channel(server_address)
        
        # Initialize service stubs
        self.app_stub = app_pb2_grpc.AppServiceStub(self.channel)
        self.wallet_stub = wallet_pb2_grpc.WalletServiceStub(self.channel)
        self.client_stub = client_pb2_grpc.ClientServiceStub(self.channel)
        self.node_stub = node_pb2_grpc.NodeServiceStub(self.channel)
        self.blockchain_stub = blockchain_pb2_grpc.BlockchainServiceStub(self.channel)
        self.event_stub = event_pb2_grpc.EventServiceStub(self.channel)
        self.watch_only_node_stub = watch_only_node_pb2_grpc.WatchOnlyNodeServiceStub(self.channel)
        self.asset_stub = asset_pb2_grpc.AssetServiceStub(self.channel)
        
        # Initialize pricing stub if available
        try:
            self.pricing_stub = pricing_pb2_grpc.PricingServiceStub(self.channel)
        except Exception as e:
            self._log("DEBUG", f"Pricing service not available: {e}")
            self.pricing_stub = None
            
        # Initialize orderbook stub if available
        try:
            self.orderbook_stub = orderbook_pb2_grpc.OrderbookServiceStub(self.channel)
        except Exception as e:
            self._log("DEBUG", f"Orderbook service not available: {e}")
            self.orderbook_stub = None
            
        # Initialize liquidity stub if available
        try:
            self.liquidity_stub = liquidity_pb2_grpc.LiquidityServiceStub(self.channel)
        except Exception as e:
            self._log("DEBUG", f"Liquidity service not available: {e}")
            self.liquidity_stub = None
    
    def test_connection(self) -> bool:
        """Test if the connection is actually working"""
        try:
            self._log("DEBUG", f"Testing connection to {self.host}:{self.port}")
            # Try to get networks with a short timeout
            request = app_pb2.GetNetworksRequest()
            self.app_stub.GetNetworks(request, timeout=2.0)
            self._log("DEBUG", "Connection test successful")
            return True
        except grpc.RpcError as e:
            self._log("ERROR", f"Connection test failed: {e.code()} - {e.details()}")
            return False
    
    def close(self):
        """Close the gRPC channel"""
        if self.channel:
            self.channel.close()
    
    # App Service Methods
    def get_networks(self) -> list:
        """Get list of active networks"""
        try:
            request = app_pb2.GetNetworksRequest()
            response = self.app_stub.GetNetworks(request)
            return response.networks
        except grpc.RpcError as e:
            self._log("ERROR", f"Error getting networks: {e.code()} - {e.details()}")
            return []
    
    def get_public_key(self) -> bytes:
        """Get the public key of the running application"""
        try:
            request = app_pb2.GetPublicKeyRequest()
            response = self.app_stub.GetPublicKey(request)
            return response.public_key
        except grpc.RpcError as e:
            print(f"Error getting public key: {e}")
            return b""
    
    # Wallet Service Methods
    def get_balances(self, network: primitives_pb2.Network) -> Dict[str, Any]:
        """Get all balances for a network"""
        try:
            request = wallet_pb2.GetBalancesRequest(network=network)
            response = self.wallet_stub.GetBalances(request)
            return response.balances
        except grpc.RpcError as e:
            print(f"Error getting balances: {e}")
            return {}
    
    def get_balance(self, network: primitives_pb2.Network, asset_id: str) -> Optional[balance_pb2.Balance]:
        """Get balance for a specific asset"""
        try:
            request = wallet_pb2.GetBalanceRequest(network=network, asset_id=asset_id)
            response = self.wallet_stub.GetBalance(request)
            return response.balance
        except grpc.RpcError as e:
            print(f"Error getting balance: {e}")
            return None
    
    def get_transactions(self, network: primitives_pb2.Network) -> list:
        """Get all transactions for a network"""
        try:
            request = wallet_pb2.GetTransactionsRequest(network=network)
            response = self.wallet_stub.GetTransactions(request)
            return response.transactions
        except grpc.RpcError as e:
            print(f"Error getting transactions: {e}")
            return []
    
    def get_transaction(self, network: primitives_pb2.Network, txid: str) -> Optional[transaction_pb2.Transaction]:
        """Get a specific transaction"""
        try:
            request = wallet_pb2.GetTransactionRequest(network=network, txid=txid)
            response = self.wallet_stub.GetTransaction(request)
            return response.transaction
        except grpc.RpcError as e:
            print(f"Error getting transaction: {e}")
            return None
    
    # Client Service Methods
    def get_deposit_address(self, network: primitives_pb2.Network) -> str:
        """Get deposit address for a network"""
        try:
            request = client_pb2.GetDepositAddressRequest(network=network)
            response = self.client_stub.GetDepositAddress(request)
            return response.address
        except grpc.RpcError as e:
            print(f"Error getting deposit address: {e}")
            return ""
    
    # Node Service Methods (for write operations)
    def connect_to_peer(self, network: primitives_pb2.Network, peer_url: str) -> bool:
        """Connect to a peer"""
        try:
            # Import the node_pb2 for the request
            from hydra_pb import node_pb2
            request = node_pb2.ConnectToPeerRequest(network=network, peer_url=peer_url)
            self.node_stub.ConnectToPeer(request)
            return True
        except grpc.RpcError as e:
            self._log("ERROR", f"Error connecting to peer: {e.code()} - {e.details()}")
            return False
    
    def start_event_stream(self, network: primitives_pb2.Network, event_callback=None):
        """Start streaming node events for real-time updates"""
        try:
            self._log("EVENT", f"Starting event stream for network: protocol={network.protocol}, id={network.id}")
            for event in self.subscribe_to_node_events(network):
                self._log("EVENT", f"Received raw event: {event}")
                
                if event_callback:
                    event_callback(event)
                
                # Handle all possible events with detailed logging
                if event.HasField("syncing"):
                    self._log("EVENT", "Node syncing")
                elif event.HasField("synced"):
                    self._log("EVENT", "Node synced")
                elif event.HasField("peer_connected"):
                    self._log("EVENT", f"Peer connected: {event.peer_connected.node_id}")
                elif event.HasField("peer_disconnected"):
                    self._log("EVENT", f"Peer disconnected: {event.peer_disconnected.node_id}")
                elif event.HasField("watchtower_connected"):
                    self._log("EVENT", f"Watchtower connected: {event.watchtower_connected.node_id}")
                elif event.HasField("watchtower_disconnected"):
                    self._log("EVENT", f"Watchtower disconnected: {event.watchtower_disconnected.node_id}")
                elif event.HasField("channel_update"):
                    self._log("EVENT", f"Channel updated: {event.channel_update.channel.id}")
                elif event.HasField("channel_closed"):
                    self._log("EVENT", f"Channel closed: {event.channel_closed.channel_id}")
                elif event.HasField("asset_channel_update"):
                    self._log("EVENT", f"Asset channel updated: {event.asset_channel_update.channel_id}/{event.asset_channel_update.asset_id}")
                elif event.HasField("asset_channel_closed"):
                    self._log("EVENT", f"Asset channel closed: {event.asset_channel_closed.channel_id}/{event.asset_channel_closed.asset_id}")
                elif event.HasField("payment_update"):
                    self._log("EVENT", f"Payment updated: {event.payment_update.payment.id}")
                else:
                    self._log("EVENT", f"Unknown event type: {event}")
                    
        except grpc.RpcError as e:
            self._log("ERROR", f"Error in event stream: {e.code()} - {e.details()}")
    
    def get_connected_peers(self, network: primitives_pb2.Network) -> list:
        """Get list of connected peer node IDs"""
        try:
            request = node_pb2.GetConnectedPeersRequest(network=network)
            response = self.node_stub.GetConnectedPeers(request)
            # Convert RepeatedScalarContainer to a regular Python list
            return list(response.node_ids)
        except grpc.RpcError as e:
            print(f"Error getting connected peers: {e}")
            return []
    
    # Watch-Only Node Service Methods (for read operations)
    def get_node_id(self, network: primitives_pb2.Network) -> str:
        """Get node ID (public key)"""
        try:
            request = watch_only_node_pb2.GetNodeIdRequest(network=network)
            response = self.watch_only_node_stub.GetNodeId(request)
            return response.node_id
        except grpc.RpcError as e:
            self._log("ERROR", f"Error getting node ID: {e.code()} - {e.details()}")
            if "not found" in str(e).lower() or "unimplemented" in str(e).lower():
                self._log("ERROR", "WatchOnlyNodeService may not be available on this server")
            return ""
    
    def get_channels(self, network: primitives_pb2.Network) -> list:
        """Get all channels for a network"""
        try:
            request = watch_only_node_pb2.GetChannelsRequest(network=network)
            response = self.watch_only_node_stub.GetChannels(request)
            
            channels = response.channels if response.channels else []

            return channels
        except grpc.RpcError as e:
            self._log("ERROR", f"Error getting channels: {e.code()} - {e.details()}")
            self._log("ERROR", f"Full gRPC error: {e}")
            # Try to determine if this service method exists
            if "not found" in str(e).lower() or "unimplemented" in str(e).lower():
                self._log("ERROR", "GetChannels method may not be implemented on WatchOnlyNodeService")
                self._log("ERROR", "This could mean channels need to be fetched from a different service")
            return []
    
    def get_payments(self, network: primitives_pb2.Network) -> list:
        """Get all payments for a network"""
        try:
            request = watch_only_node_pb2.GetPaymentsRequest(network=network)
            response = self.watch_only_node_stub.GetPayments(request)
            return response.payments
        except grpc.RpcError as e:
            self._log("ERROR", f"Error getting payments: {e.code()} - {e.details()}")
            return []
    
    def test_watch_only_service(self, network: primitives_pb2.Network):
        """Test if WatchOnlyNodeService is working"""
        
        # Test GetNodeId first (simpler call)
        try:
            node_id = self.get_node_id(network)
            if node_id:
                self._log("DEBUG", f"WatchOnlyNodeService.GetNodeId works - Node ID: {node_id}")
                return True
            else:
                self._log("DEBUG", "WatchOnlyNodeService.GetNodeId returned empty")
                return False
        except Exception as e:
            self._log("ERROR", f"WatchOnlyNodeService test failed: {e}")
            return False
    
    # Blockchain Service Methods
    def get_block_height(self, network: primitives_pb2.Network) -> int:
        """Get current block height"""
        try:
            request = blockchain_pb2.GetBlockNumberRequest(network=network)
            response = self.blockchain_stub.GetBlockNumber(request)
            return response.block_number
        except grpc.RpcError as e:
            print(f"Error getting block height: {e}")
            return 0
    
    def get_fee_estimate(self, network: primitives_pb2.Network) -> Optional[fee_pb2.FeeEstimate]:
        """Get fee estimate for a network"""
        try:
            request = blockchain_pb2.GetFeeEstimatesRequest(network=network)
            response = self.blockchain_stub.GetFeeEstimates(request)
            return response.fee_estimate
        except grpc.RpcError as e:
            print(f"Error getting fee estimate: {e}")
            return None
    
    # Asset Service Methods
    def get_asset(self, network: primitives_pb2.Network, asset_id: str) -> Optional[primitives_pb2.Asset]:
        """Get asset information by ID"""
        try:
            request = asset_pb2.GetAssetRequest(network=network, asset_id=asset_id)
            response = self.asset_stub.GetAsset(request)
            return response.asset
        except grpc.RpcError as e:
            self._log("DEBUG", f"Could not get asset info for {asset_id}: {e.code()}")
            return None
    
    def get_assets(self, network: primitives_pb2.Network) -> list:
        """Get all available assets for a network"""
        try:
            self._log("DEBUG", f"Getting all assets for network {network.id}")
            request = asset_pb2.GetAssetsRequest(network=network)
            response = self.asset_stub.GetAssets(request)
            self._log("DEBUG", f"Found {len(response.assets)} assets")
            return response.assets
        except grpc.RpcError as e:
            self._log("DEBUG", f"Error getting assets: {e.code()}")
            return []
    
    def get_native_asset(self, network: primitives_pb2.Network) -> Optional[primitives_pb2.Asset]:
        """Get the native asset for a network"""
        try:
            request = asset_pb2.GetNativeAssetRequest(network=network)
            response = self.asset_stub.GetNativeAsset(request)
            return response.asset
        except grpc.RpcError as e:
            self._log("DEBUG", f"Error getting native asset: {e.code()}")
            return None
    
    # Pricing Service Methods
    def get_asset_fiat_price(self, network: primitives_pb2.Network, asset_id: str, fiat_currency: int = 1) -> Optional[float]:
        """Get the fiat price of an asset (default: FIAT_CURRENCY_USD=1)"""
        try:
            # Check if pricing service is available
            if not hasattr(self, 'pricing_stub') or self.pricing_stub is None:
                self._log("DEBUG", f"Pricing service not available")
                return None
                
            from utils import decimal_to_float
            self._log("DEBUG", f"Getting USD price for {asset_id} on network {network.id}")
            request = pricing_pb2.GetAssetFiatPriceRequest(
                network=network, 
                asset_id=asset_id,
                fiat_currency=fiat_currency  # FIAT_CURRENCY_USD = 1
            )
            response = self.pricing_stub.GetAssetFiatPrice(request)
            if response.price:
                price = decimal_to_float(response.price)
                self._log("DEBUG", f"USD price for {asset_id}: ${price:.2f}")
                return price
            return None
        except grpc.RpcError as e:
            self._log("DEBUG", f"Could not get USD price for {asset_id}: {e.code()}")
            return None
        except Exception as e:
            self._log("DEBUG", f"Error getting USD price for {asset_id}: {e}")
            return None
    
    # Event streaming
    def subscribe_to_client_events(self, network: primitives_pb2.Network):
        """Subscribe to client events"""
        try:
            request = event_pb2.SubscribeClientEventsRequest(network=network)
            for event in self.event_stub.SubscribeClientEvents(request):
                yield event
        except grpc.RpcError as e:
            print(f"Error subscribing to client events: {e}")
    
    def subscribe_to_node_events(self, network: primitives_pb2.Network):
        """Subscribe to node events"""
        try:
            request = event_pb2.SubscribeNodeEventsRequest(network=network)
            for event in self.event_stub.SubscribeNodeEvents(request):
                yield event
        except grpc.RpcError as e:
            print(f"Error subscribing to node events: {e}")
    
    def subscribe_to_dex_events(self):
        """
        Subscribe to DexEvents for real-time order, balance, and trade updates.
        
        Returns a generator that yields DexEvent messages containing:
        - BalanceUpdate: Changes to currency balances
        - OrderUpdate: Order creation, updates, completion, cancellation  
        - MatchedOrder: When orders are matched for trading
        - SwapUpdate: Swap initialization, completion, failures
        - MarketTradeUpdate: Trade execution details
        - SwapTradeUpdate: Swap trade details
        """
        try:
            if not hasattr(self, 'orderbook_stub') or self.orderbook_stub is None:
                self._log("ERROR", "Orderbook service not available for dex events")
                return iter([])
                
            self._log("INFO", "Creating DexEvents subscription request...")
            request = orderbook_pb2.SubscribeDexEventsRequest()
            
            self._log("INFO", "Starting DexEvents stream...")
            stream = self.orderbook_stub.SubscribeDexEvents(request)
            
            self._log("INFO", "DexEvents stream established, waiting for events...")
            for dex_event in stream:
                yield dex_event
                
            self._log("INFO", "DexEvents stream ended normally")
                
        except grpc.RpcError as e:
            self._log("ERROR", f"gRPC error in dex events: {e.code()} - {e.details()}")
            if e.code() == grpc.StatusCode.UNIMPLEMENTED:
                self._log("ERROR", "SubscribeDexEvents is not implemented on the server")
            elif e.code() == grpc.StatusCode.UNAVAILABLE:
                self._log("ERROR", "Server is unavailable for streaming")
            elif e.code() == grpc.StatusCode.CANCELLED:
                self._log("INFO", "Stream was cancelled")
            return iter([])
        except Exception as e:
            self._log("ERROR", f"Unexpected error in dex events: {type(e).__name__}: {e}")
            import traceback
            self._log("DEBUG", f"Traceback: {traceback.format_exc()}")
            return iter([])
    
    # Compatibility method for existing code
    def subscribe_to_private_updates(self):
        """Compatibility wrapper for the old method name"""
        self._log("WARNING", "subscribe_to_private_updates is deprecated, use subscribe_to_dex_events")
        return self.subscribe_to_dex_events()

    def subscribe_to_market_updates(self, base_currency: currency_pb2.OrderbookCurrency, quote_currency: currency_pb2.OrderbookCurrency):
        """
        Subscribe to MarketUpdates for real-time market data.

        This is a compatibility wrapper for subscribe_market_events.

        Returns a generator that yields MarketEvent messages containing:
        - OrderbookUpdate: Changes to bid/ask levels
        - Trade: Public trade executions
        - MarketDailyStats: 24h volume, price changes
        - CandlestickUpdate: OHLCV candlestick data
        """
        self._log("WARNING", "subscribe_to_market_updates is deprecated, use subscribe_market_events")
        return self.subscribe_market_events(base_currency, quote_currency)
    
    def subscribe_market_events(self, base_currency: currency_pb2.OrderbookCurrency, quote_currency: currency_pb2.OrderbookCurrency):
        """
        Subscribe to MarketEvents for real-time market data.

        Returns a generator that yields MarketEvent messages containing:
        - OrderbookUpdate: Changes to bid/ask levels
        - Trade: Public trade executions
        - MarketDailyStats: 24h volume, price changes
        - CandlestickUpdate: OHLCV candlestick data
        """
        try:
            if not hasattr(self, 'orderbook_stub') or self.orderbook_stub is None:
                self._log("ERROR", "Orderbook service not available for market events")
                return iter([])

            self._log("DEBUG", f"Subscribing to market events for {base_currency.asset_id}/{quote_currency.asset_id}")
            request = orderbook_pb2.SubscribeMarketEventsRequest(
                base=base_currency,
                quote=quote_currency
            )

            for market_event in self.orderbook_stub.SubscribeMarketEvents(request):
                yield market_event

        except grpc.RpcError as e:
            self._log("ERROR", f"Error subscribing to market events: {e.code()} - {e.details()}")
            return iter([])
        except Exception as e:
            self._log("ERROR", f"Unexpected error in market events: {e}")
            return iter([])
    
    # Orderbook Service Methods
    def init_market(self, first_currency: currency_pb2.OrderbookCurrency, other_currency: currency_pb2.OrderbookCurrency) -> Optional[Any]:
        """Initialize a market with two currencies"""
        try:
            if not hasattr(self, 'orderbook_stub') or self.orderbook_stub is None:
                self._log("DEBUG", "Orderbook service not available")
                return None
                
            request = orderbook_pb2.InitMarketRequest(
                first_currency=first_currency,
                other_currency=other_currency
            )
            response = self.orderbook_stub.InitMarket(request)
            self._log("DEBUG", f"Market initialized: {first_currency.asset_id}/{other_currency.asset_id}")
            return response.market_info if response.market_info else None
        except grpc.RpcError as e:
            self._log("ERROR", f"Error initializing market: {e.code()} - {e.details()}")
            return None
        except Exception as e:
            self._log("ERROR", f"Error initializing market: {e}")
            return None

    def get_orderbook_balances(self) -> list:
        """Get orderbook balances for all currencies"""
        try:
            if not hasattr(self, 'orderbook_stub') or self.orderbook_stub is None:
                self._log("DEBUG", "Orderbook service not available")
                return []
                
            request = orderbook_pb2.GetOrderbookBalancesRequest()
            response = self.orderbook_stub.GetOrderbookBalances(request)
            return response.balances
        except grpc.RpcError:
            return []
        except Exception:
            return []
    
    def get_initialized_markets(self) -> list:
        """Get list of initialized markets"""
        try:
            if not hasattr(self, 'orderbook_stub') or self.orderbook_stub is None:
                self._log("DEBUG", "Orderbook service not available")
                return []
                
            request = orderbook_pb2.GetInitializedMarketsRequest()
            response = self.orderbook_stub.GetInitializedMarkets(request)
            return response.markets
        except grpc.RpcError as e:
            self._log("ERROR", f"Error getting initialized markets: {e.code()} - {e.details()}")
            return []
        except Exception as e:
            self._log("ERROR", f"Error getting initialized markets: {e}")
            return []
    
    def get_markets_info(self) -> list:
        """Get info for all available markets"""
        try:
            if not hasattr(self, 'orderbook_stub') or self.orderbook_stub is None:
                self._log("DEBUG", "Orderbook service not available")
                return []
                
            request = orderbook_pb2.GetMarketsInfoRequest()
            response = self.orderbook_stub.GetMarketsInfo(request)
            return response.markets
        except grpc.RpcError as e:
            self._log("ERROR", f"Error getting markets info: {e.code()} - {e.details()}")
            return []
        except Exception as e:
            self._log("ERROR", f"Error getting markets info: {e}")
            return []
    
    def get_market_info(self, first_currency: currency_pb2.OrderbookCurrency, other_currency: currency_pb2.OrderbookCurrency) -> Optional[Any]:
        """Get info for a specific market"""
        try:
            if not hasattr(self, 'orderbook_stub') or self.orderbook_stub is None:
                self._log("DEBUG", "Orderbook service not available")
                return None
                
            request = orderbook_pb2.GetMarketInfoRequest(
                first_currency=first_currency,
                other_currency=other_currency
            )
            response = self.orderbook_stub.GetMarketInfo(request)
            return response.market_info if response.market_info else None
        except grpc.RpcError as e:
            self._log("ERROR", f"Error getting market info: {e.code()} - {e.details()}")
            return None
        except Exception as e:
            self._log("ERROR", f"Error getting market info: {e}")
            return None

    def get_orderbook(self, base_currency: currency_pb2.OrderbookCurrency, quote_currency: currency_pb2.OrderbookCurrency) -> Optional[Any]:
        """Get orderbook for a specific market"""
        try:
            if not hasattr(self, 'orderbook_stub') or self.orderbook_stub is None:
                self._log("ERROR", "Orderbook stub not available")
                return None

            self._log("DEBUG", f"GetOrderbook: {base_currency.network_id}:{base_currency.asset_id[:10]}... / {quote_currency.network_id}:{quote_currency.asset_id[:10]}...")

            request = orderbook_pb2.GetOrderbookRequest(
                base=base_currency,
                quote=quote_currency
            )

            response = self.orderbook_stub.GetOrderbook(request)

            if response.orderbook:
                self._log("INFO", f"GetOrderbook returned orderbook with {len(response.orderbook.orders)} orders")
                return response.orderbook
            else:
                self._log("WARNING", "GetOrderbook returned no orderbook")
                return None
        except grpc.RpcError as e:
            self._log("ERROR", f"Error getting orderbook: {e.code()} - {e.details()}")
            return None
        except Exception as e:
            self._log("ERROR", f"Error getting orderbook: {e}")
            return None

    def estimate_order(self, order_variant) -> Optional[Any]:
        """Estimate the order matching for a specific order"""
        try:
            if not hasattr(self, 'orderbook_stub') or self.orderbook_stub is None:
                self._log("DEBUG", "Orderbook service not available")
                return None
                
            # Reduced verbosity: only log errors, not full request/response
            request = orderbook_pb2.EstimateOrderRequest(order_variant=order_variant)
            response = self.orderbook_stub.EstimateOrder(request)
            
            if response.order_match:
                return response.order_match
            else:
                self._log("DEBUG", "EstimateOrder: No order match in response")
                return None
        except grpc.RpcError as e:
            self._log("ERROR", f"Error estimating order: {e.code()} - {e.details()}")
            return None
        except Exception as e:
            self._log("ERROR", f"Error estimating order: {e}")
            return None

    def get_all_own_orders(self) -> dict:
        """Get all own orders across all markets"""
        try:
            if not hasattr(self, 'orderbook_stub') or self.orderbook_stub is None:
                self._log("DEBUG", "Orderbook service not available")
                return {}

            request = orderbook_pb2.GetAllOwnOrdersRequest()
            response = self.orderbook_stub.GetAllOwnOrders(request)
            return dict(response.orders)
        except grpc.RpcError as e:
            self._log("ERROR", f"Error getting all own orders: {e.code()} - {e.details()}")
            return {}
        except Exception as e:
            self._log("ERROR", f"Error getting all own orders: {e}")
            return {}

    def get_own_orders(self, base_currency: currency_pb2.OrderbookCurrency, quote_currency: currency_pb2.OrderbookCurrency) -> dict:
        """Get own orders for a specific market"""
        try:
            if not hasattr(self, 'orderbook_stub') or self.orderbook_stub is None:
                self._log("DEBUG", "Orderbook service not available")
                return {}

            request = orderbook_pb2.GetOwnOrdersRequest(
                base=base_currency,
                quote=quote_currency
            )
            response = self.orderbook_stub.GetOwnOrders(request)
            return dict(response.orders)
        except grpc.RpcError as e:
            self._log("ERROR", f"Error getting own orders: {e.code()} - {e.details()}")
            return {}
        except Exception as e:
            self._log("ERROR", f"Error getting own orders: {e}")
            return {}

    def get_trade_history(self, base_currency: currency_pb2.OrderbookCurrency, quote_currency: currency_pb2.OrderbookCurrency) -> list:
        """Get trade history for a specific market"""
        try:
            if not hasattr(self, 'orderbook_stub') or self.orderbook_stub is None:
                self._log("DEBUG", "Orderbook service not available")
                return []
                
            request = orderbook_pb2.GetTradeHistoryRequest(
                base=base_currency,
                quote=quote_currency
            )
            response = self.orderbook_stub.GetTradeHistory(request)
            return response.trade_history
        except grpc.RpcError as e:
            self._log("ERROR", f"Error getting trade history: {e.code()} - {e.details()}")
            return []
        except Exception as e:
            self._log("ERROR", f"Error getting trade history: {e}")
            return []

    def get_dex_currencies(self) -> list:
        """Get all DEX currencies across all networks and protocols"""
        currencies = []
        
        try:
            # Get all networks
            networks = self.get_networks()
            if not networks:
                self._log("DEBUG", "No networks available")
                return currencies
            
            for network in networks:
                try:
                    # Get assets for this network
                    assets = self.get_balances(network)
                    if not assets:
                        self._log("DEBUG", f"No assets found for network {network.id}")
                        continue
                    
                    # Create OrderbookCurrency for each asset
                    for asset_id in assets.keys():
                        currency = currency_pb2.OrderbookCurrency(
                            protocol=network.protocol,
                            network_id=network.id,
                            asset_id=asset_id
                        )
                        currencies.append({
                            'currency': currency,
                            'network': network,
                            'asset_id': asset_id,
                            'protocol_name': 'Bitcoin' if network.protocol == 1 else 'EVM'
                        })
                        
                except Exception as e:
                    self._log("DEBUG", f"Error getting assets for network {network.id}: {e}")
                    continue
            
            return currencies
            
        except Exception as e:
            self._log("ERROR", f"Error getting DEX currencies: {e}")
            return currencies
    
    # Liquidity Service Methods (formerly Rental)
    def get_rental_node_info(self):
        """Get liquidity node information including min/max amounts and fees"""
        try:
            if not hasattr(self, 'liquidity_stub') or self.liquidity_stub is None:
                self._log("DEBUG", "Liquidity service not available")
                return None

            request = liquidity_pb2.GetRentalNodeInfoRequest()
            response = self.liquidity_stub.GetRentalNodeInfo(request)
            return response
        except grpc.RpcError as e:
            self._log("ERROR", f"Error getting rental node info: {e.code()} - {e.details()}")
            return None
    
    def get_rentable_asset_info(self, network: primitives_pb2.Network, asset_id: str):
        """Get rental info for a specific asset"""
        try:
            if not hasattr(self, 'liquidity_stub') or self.liquidity_stub is None:
                self._log("DEBUG", "Liquidity service not available")
                return None

            request = liquidity_pb2.GetRentableAssetInfoRequest(
                network=network,
                asset_id=asset_id
            )
            response = self.liquidity_stub.GetRentableAssetInfo(request)
            return response
        except grpc.RpcError as e:
            self._log("ERROR", f"Error getting rentable asset info: {e.code()} - {e.details()}")
            return None
    
    def estimate_rent_channel_fee(self, network: primitives_pb2.Network, asset_id: str,
                                  lifetime_seconds: int, amount, rental_option):
        """Estimate rental fee for a channel"""
        try:
            if not hasattr(self, 'liquidity_stub') or self.liquidity_stub is None:
                self._log("ERROR", "Liquidity service not available - liquidity_stub is None")
                return None
                
            from utils import float_to_decimal
            decimal_amount = float_to_decimal(amount) if isinstance(amount, float) else amount
            
            # Enhanced debug logging for fee estimation
            self._log("DEBUG", f"=== EstimateRentChannelFee Debug Info ===")
            self._log("DEBUG", f"Network: protocol={network.protocol}, id={network.id}")
            self._log("DEBUG", f"Asset ID: {asset_id}")
            self._log("DEBUG", f"Amount: {amount} (type: {type(amount)})")
            self._log("DEBUG", f"Lifetime: {lifetime_seconds} seconds ({lifetime_seconds/86400:.1f} days)")
            self._log("DEBUG", f"Decimal amount: {decimal_amount}")
            self._log("DEBUG", f"Rental option type: {type(rental_option)}")
            
            # Debug rental option details
            if rental_option and hasattr(rental_option, 'payment'):
                payment = rental_option.payment
                self._log("DEBUG", f"Payment details:")
                self._log("DEBUG", f"  - Payment network: protocol={payment.payment_network.protocol}, id={payment.payment_network.id}")
                self._log("DEBUG", f"  - Payment asset: {payment.payment_asset_id}")
                self._log("DEBUG", f"  - Payment method: {payment.payment_method}")
                
                if hasattr(payment, 'rental_tx_fee_rate') and payment.rental_tx_fee_rate:
                    fee_rate = payment.rental_tx_fee_rate
                    self._log("DEBUG", f"  - Fee rate: base={fee_rate.base_fee_per_unit.lo}, max={fee_rate.max_price_per_unit.lo}")
                else:
                    self._log("DEBUG", f"  - Fee rate: NOT SET")
            
            # Create request with detailed logging
            self._log("DEBUG", "Creating EstimateRentChannelFeeRequest...")
            try:
                request = liquidity_pb2.EstimateRentChannelFeeRequest(
                    network=network,
                    asset_id=asset_id,
                    lifetime_seconds=lifetime_seconds,
                    amount=decimal_amount,
                    rental_option=rental_option
                )
                self._log("DEBUG", f"Request created successfully, size: {len(str(request))} chars")
            except Exception as req_error:
                self._log("ERROR", f"Failed to create request: {req_error}")
                raise req_error
            
            # Send request with timing
            import time
            self._log("DEBUG", "Sending EstimateRentChannelFee request to liquidity service...")
            start_time = time.time()
            
            try:
                response = self.liquidity_stub.EstimateRentChannelFee(request)
                elapsed = time.time() - start_time
                self._log("DEBUG", f"✅ Received response in {elapsed:.2f}s: {response}")
                return response
            except Exception as call_error:
                elapsed = time.time() - start_time
                self._log("ERROR", f"❌ Request failed after {elapsed:.2f}s: {call_error}")
                raise call_error
        except grpc.RpcError as e:
            self._log("ERROR", f"gRPC Error estimating rental fee: {e.code()} - {e.details()}")
            return None
        except Exception as e:
            self._log("ERROR", f"Exception estimating rental fee: {type(e).__name__} - {str(e)}")
            return None
    
    def rent_channel(self, network: primitives_pb2.Network, asset_id: str,
                     lifetime_seconds: int, amount, rental_option):
        """Rent a channel for inbound liquidity"""
        try:
            if not hasattr(self, 'liquidity_stub') or self.liquidity_stub is None:
                self._log("DEBUG", "Liquidity service not available")
                return None

            from utils import float_to_decimal
            decimal_amount = float_to_decimal(amount) if isinstance(amount, float) else amount

            request = liquidity_pb2.RentChannelRequest(
                network=network,
                asset_id=asset_id,
                lifetime_seconds=lifetime_seconds,
                amount=decimal_amount,
                rental_option=rental_option
            )
            response = self.liquidity_stub.RentChannel(request)
            self._log("INFO", f"Channel rented: {response.channel_id}, txid: {response.txid}")
            return response
        except grpc.RpcError as e:
            self._log("ERROR", f"gRPC Error renting channel: {e.code()} - {e.details()}")
            # Re-raise the exception so the UI can catch it
            raise e
        except Exception as e:
            self._log("ERROR", f"Unexpected error renting channel: {type(e).__name__} - {str(e)}")
            raise e
    
    # Node Channel Management Methods  
    def open_channel(self, network: primitives_pb2.Network, node_id: str, asset_amounts: dict, fee_option=None):
        """Open a new channel with a peer

        Args:
            network: The network to open the channel on
            node_id: The peer's node ID (public key)
            asset_amounts: Dict mapping asset_id -> SendAmount or asset_id -> float
            fee_option: Optional FeeOption (Low/Medium/High/Custom) for the transaction
        """
        try:
            # Validate inputs
            if network is None:
                raise ValueError("network cannot be None")

            request = node_pb2.OpenChannelRequest()
            request.network.CopyFrom(network)

            # Ensure node_id is a valid string
            if not node_id:
                raise ValueError("node_id cannot be empty")
            request.node_id = str(node_id)

            # Add asset amounts to the request
            for asset_id, amount in asset_amounts.items():
                if isinstance(amount, (int, float)):
                    # Convert float/int to SendAmount
                    from utils import create_send_amount
                    send_amount = create_send_amount(float(amount))
                    request.asset_amounts[asset_id].CopyFrom(send_amount)
                elif hasattr(amount, 'exact') or hasattr(amount, 'all'):
                    # Already a SendAmount protobuf message
                    request.asset_amounts[asset_id].CopyFrom(amount)
                else:
                    self._log("ERROR", f"Invalid amount type for asset {asset_id}: {type(amount)}")
                    raise ValueError(f"Invalid amount type for asset {asset_id}: {type(amount)}")

            if fee_option:
                request.fee_option.CopyFrom(fee_option)
            
            self._log("DEBUG", f"Opening channel with node {node_id} on network {network.protocol}:{network.id}")
            self._log("DEBUG", f"Asset amounts: {list(asset_amounts.keys())}")
            
            response = self.node_stub.OpenChannel(request)
            self._log("INFO", f"Channel opened successfully: channel_id={response.channel_id}, txid={response.txid}")
            return response
        except grpc.RpcError as e:
            self._log("ERROR", f"gRPC error opening channel: {e.code()} - {e.details()}")
            # Log more details for debugging
            if e.code() == grpc.StatusCode.INVALID_ARGUMENT:
                self._log("ERROR", "Invalid arguments provided to OpenChannel. Check asset amounts and network.")
            elif e.code() == grpc.StatusCode.FAILED_PRECONDITION:
                self._log("ERROR", "Failed precondition for opening channel. Check balances and peer connection.")
            elif e.code() == grpc.StatusCode.UNAVAILABLE:
                self._log("ERROR", "Node service unavailable. Check if the node is running.")
            return None
        except Exception as e:
            self._log("ERROR", f"Unexpected error opening channel: {type(e).__name__}: {e}")
            return None
    
    def deposit_channel(self, network: primitives_pb2.Network, channel_id: str, asset_amounts: dict, fee_option=None):
        """Deposit assets into an existing channel"""
        try:
            from hydra_pb import node_pb2
            request = node_pb2.DepositChannelRequest()
            request.network.CopyFrom(network)
            request.channel_id = channel_id

            # Add asset amounts to the request
            for asset_id, send_amount in asset_amounts.items():
                request.asset_amounts[asset_id].CopyFrom(send_amount)

            if fee_option:
                request.fee_option.CopyFrom(fee_option)
                
            response = self.node_stub.DepositChannel(request)
            self._log("INFO", f"Channel deposit: txid: {response.txid}")
            return response
        except grpc.RpcError as e:
            self._log("ERROR", f"Error depositing to channel: {e.code()} - {e.details()}")
            return None
    
    def withdraw_channel(self, network: primitives_pb2.Network, channel_id: str, asset_amounts: dict, fee_option=None):
        """Withdraw assets from an existing channel"""
        try:
            from hydra_pb import node_pb2
            request = node_pb2.WithdrawChannelRequest()
            request.network.CopyFrom(network)
            request.channel_id = channel_id

            # Add asset amounts to the request
            for asset_id, withdraw_amount in asset_amounts.items():
                request.asset_amounts[asset_id].CopyFrom(withdraw_amount)

            if fee_option:
                request.fee_option.CopyFrom(fee_option)
                
            response = self.node_stub.WithdrawChannel(request)
            self._log("INFO", f"Channel withdrawal: txid: {response.txid}")
            return response
        except grpc.RpcError as e:
            self._log("ERROR", f"Error withdrawing from channel: {e.code()} - {e.details()}")
            return None
    
    def close_channel(self, network: primitives_pb2.Network, channel_id: str, fee_option=None):
        """Close a channel cooperatively"""
        try:
            from hydra_pb import node_pb2
            request = node_pb2.CloseChannelRequest()
            request.network.CopyFrom(network)
            request.channel_id = channel_id
            if fee_option:
                request.fee_option.CopyFrom(fee_option)
                
            response = self.node_stub.CloseChannel(request)
            self._log("INFO", f"Channel closing: txid: {response.txid}")
            return response
        except grpc.RpcError as e:
            self._log("ERROR", f"Error closing channel: {e.code()} - {e.details()}")
            return None
    
    def force_close_channel(self, network: primitives_pb2.Network, channel_id: str, fee_option=None):
        """Force close a channel unilaterally"""
        try:
            from hydra_pb import node_pb2
            request = node_pb2.ForceCloseChannelRequest()
            request.network.CopyFrom(network)
            request.channel_id = channel_id
            if fee_option:
                request.fee_option.CopyFrom(fee_option)
                
            response = self.node_stub.ForceCloseChannel(request)
            self._log("INFO", f"Channel force closing: txid: {response.txid}")
            return response
        except grpc.RpcError as e:
            self._log("ERROR", f"Error force closing channel: {e.code()} - {e.details()}")
            return None
    
    def _refusal(self, value):
        """The last placement refusal, per thread (markets place orders from parallel threads)."""
        import threading
        if not hasattr(self, "_tl"):
            self._tl = threading.local()
        self._tl.refusal = value
        self.last_refusal = value

    def place_limit_order_ex(self, **kw):
        """place_limit_order, returning (order_id or None, refusal prefix or None) from the same thread."""
        oid = self.place_limit_order(**kw)
        return oid, (getattr(self._tl, "refusal", None) if hasattr(self, "_tl") else None)

    def place_limit_order(
        self, 
        base_currency: currency_pb2.OrderbookCurrency,
        quote_currency: currency_pb2.OrderbookCurrency,
        base_amount: primitives_pb2.DecimalString,
        quote_amount: primitives_pb2.DecimalString,
        min_buy_price: primitives_pb2.DecimalString,
        mid_price: primitives_pb2.DecimalString,
        max_sell_price: primitives_pb2.DecimalString,
        remove_on_fill: bool = False,
        time_in_force: Optional[str] = None,
        client_order_id: Optional[str] = None,
        self_trade_prevention: Optional[str] = None,
    ) -> Optional[str]:
        """Place a limit order on the orderbook.

        time_in_force: None (GTC, field left unset) | 'post_only' | 'ioc' | 'fok'
        self_trade_prevention: None (unset) | 'cancel_taker' | 'cancel_maker' | 'cancel_both'
        client_order_id: names the order for the hub's retention period (24 h), so a resend
            after a lost answer returns that order instead of placing a second one — a
            DEADLINE_EXCEEDED / UNAVAILABLE answer is retried once under the same id.
        The refusal of the last failed placement is kept in `last_refusal` (its prefix,
        e.g. 'post_only_would_cross', or the gRPC code name).
        """
        self._refusal(None)
        try:
            if not hasattr(self, 'orderbook_stub') or self.orderbook_stub is None:
                self._log("ERROR", "Orderbook service not available")
                return None
            
            # Create OrderAmount using the working dashboard approach
            order_amount = orderbook_pb2.OrderAmount()
            
            if base_amount and base_amount is not None and base_amount.value and base_amount.value != "":
                order_amount.base.amount.CopyFrom(base_amount)
                order_side = 2  # ORDER_SIDE_SELL when providing base amount
            elif quote_amount and quote_amount is not None and quote_amount.value and quote_amount.value != "":
                order_amount.quote.amount.CopyFrom(quote_amount)
                order_side = 1  # ORDER_SIDE_BUY when providing quote amount
            else:
                self._log("ERROR", "Either base_amount or quote_amount must be provided")
                return None
            
            # Use MarketOrder variant for instant-fill orders
            if remove_on_fill:
                market_order = orderbook_pb2.OrderVariant.MarketOrder(
                    base=base_currency,
                    quote=quote_currency,
                    amount=order_amount,
                    side=order_side
                )
                order_variant = orderbook_pb2.OrderVariant(market_order=market_order)
            else:
                # Create LimitOrder variant (NEW in latest Hydra - traditional CEX style)
                # Hydra deprecated AddLiquidity in favor of standard Limit Orders
                limit_order = orderbook_pb2.OrderVariant.LimitOrder(
                    base=base_currency,
                    quote=quote_currency,
                    side=order_side,
                    price=mid_price,  # Use mid_price as the limit price
                    amount=order_amount
                )
                if time_in_force:      # leave unset for GTC: a hub that predates the field refuses explicit values
                    limit_order.time_in_force = orderbook_pb2.TimeInForce.Value(f"TIME_IN_FORCE_{time_in_force.upper()}")

                order_variant = orderbook_pb2.OrderVariant(limit_order=limit_order)

            # Create the request with explicit channel-based settlement on both legs.
            # Bot orders are regular channel swaps: we SEND and RECEIVE via Lightning
            # channels, never on-chain HTLCs. LEG_SETTLEMENT_CHANNEL is the zero value
            # (so an unset field already means channel), but we set it explicitly so
            # intent is unambiguous and survives any future change to the default.
            # NOTE: resting maker (limit) orders are channel-only per the orderbook;
            # route_filter must stay unset for makers.
            settlement = currency_pb2.OrderSettlement(
                sending=currency_pb2.LEG_SETTLEMENT_CHANNEL,
                receiving=currency_pb2.LEG_SETTLEMENT_CHANNEL,
            )
            request = orderbook_pb2.CreateOrderRequest(
                order_variant=order_variant,
                settlement=settlement,
            )
            if client_order_id:
                request.client_order_id = client_order_id[:64]
            if self_trade_prevention:
                request.self_trade_prevention = orderbook_pb2.SelfTradePrevention.Value(
                    f"SELF_TRADE_PREVENTION_{self_trade_prevention.upper()}")

            # Debug logging for order structure
            self._log("DEBUG", f"Submitting order with structure:")
            if order_variant.HasField('limit_order'):
                lo = order_variant.limit_order
                side_str = "BUY" if lo.side == 1 else "SELL"
                self._log("DEBUG", f"  LimitOrder:")
                self._log("DEBUG", f"    base: {lo.base.network_id}:{lo.base.asset_id[:10]}...")
                self._log("DEBUG", f"    quote: {lo.quote.network_id}:{lo.quote.asset_id[:10]}...")
                self._log("DEBUG", f"    side: {side_str}")
                self._log("DEBUG", f"    price: {lo.price.value}")
                if lo.amount.HasField('base'):
                    self._log("DEBUG", f"    amount: base {lo.amount.base.amount.value}")
                elif lo.amount.HasField('quote'):
                    self._log("DEBUG", f"    amount: quote {lo.amount.quote.amount.value}")
            elif order_variant.HasField('market_order'):
                mo = order_variant.market_order
                side_str = "BUY" if mo.side == 1 else "SELL"
                self._log("DEBUG", f"  MarketOrder:")
                self._log("DEBUG", f"    base: {mo.base.network_id}:{mo.base.asset_id[:10]}...")
                self._log("DEBUG", f"    quote: {mo.quote.network_id}:{mo.quote.asset_id[:10]}...")
                self._log("DEBUG", f"    side: {side_str}")
                if mo.amount.HasField('base'):
                    self._log("DEBUG", f"    amount: base {mo.amount.base.amount.value}")
                elif mo.amount.HasField('quote'):
                    self._log("DEBUG", f"    amount: quote {mo.amount.quote.amount.value}")
            
            self._log("DEBUG", f"    settlement: sending=CHANNEL receiving=CHANNEL")

            # Submit the order (one retry under the same client_order_id when the answer may be lost)
            for attempt in (1, 2):
                try:
                    response = self.orderbook_stub.CreateOrder(request, timeout=30)
                    break
                except grpc.RpcError as e:
                    retry = (client_order_id and attempt == 1 and
                             e.code() in (grpc.StatusCode.DEADLINE_EXCEEDED, grpc.StatusCode.UNAVAILABLE))
                    if not retry:
                        raise
                    self._log("WARNING", f"CreateOrder {e.code().name} — resending under client_order_id {client_order_id}")
            if response.HasField("released"):
                self._log("INFO", f"Order {response.order_id}: part not placed — {str(response.released).strip()[:160]}")
            self._log("INFO", f"Order placed successfully with ID: {response.order_id}")
            return response.order_id

        except grpc.RpcError as e:
            details = e.details() or ""
            m = re.match(r"\s*([a-z_]+):", details)
            refusal = m.group(1) if m else e.code().name
            self._refusal(refusal)
            level = "INFO" if refusal in ("post_only_would_cross", "self_trade_prevented",
                                                    "nothing_to_take", "fill_or_kill_unfilled") else "ERROR"
            self._log(level, f"Error placing limit order on HYDRA: {e.code()} - {details}")
            return None
        except Exception as e:
            self._log("ERROR", f"Error placing limit order on HYDRA: {e}")
            return None