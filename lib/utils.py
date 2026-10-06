"""Utility functions for the Hydra Dashboard"""

import datetime
from typing import Dict, Any, Optional
from .hydra_pb import primitives_pb2, currency_pb2, balance_pb2, fee_pb2


def decimal_to_float(decimal_string: primitives_pb2.DecimalString) -> float:
    """Convert protobuf DecimalString to float"""
    if not decimal_string or not decimal_string.value:
        return 0.0
    
    try:
        return float(decimal_string.value)
    except (ValueError, TypeError):
        return 0.0


def float_to_decimal(value: float, precision: int = 8) -> primitives_pb2.DecimalString:
    """Convert float to protobuf DecimalString with limited precision"""
    from decimal import Decimal, ROUND_HALF_UP
    decimal_string = primitives_pb2.DecimalString()
    # Use Decimal for precise string conversion with limited precision
    decimal_value = Decimal(str(value))
    # Round to specified precision to avoid excessive decimal places
    quantized_value = decimal_value.quantize(Decimal('0.' + '0' * precision), rounding=ROUND_HALF_UP)
    # Strip trailing zeros so Hydra doesn't reject for excess precision
    # (e.g. "40.000" -> "40", "1.50" -> "1.5")
    # Use format(, 'f') to avoid scientific notation from normalize()
    decimal_string.value = format(quantized_value.normalize(), 'f')
    return decimal_string


def create_send_amount(amount: float) -> balance_pb2.Amount:
    """Create an Amount protobuf message from a float amount"""
    send_amount = balance_pb2.Amount()
    decimal_amount = float_to_decimal(amount)
    send_amount.exact.amount.CopyFrom(decimal_amount)
    return send_amount


def create_send_amount_all() -> balance_pb2.Amount:
    """Create an Amount protobuf message for sending all available funds"""
    send_amount = balance_pb2.Amount()
    send_amount.all.CopyFrom(balance_pb2.Amount.All())
    return send_amount


def create_fee_option_low() -> fee_pb2.FeeOption:
    """Create a FeeOption protobuf message for low fee"""
    fee_option = fee_pb2.FeeOption()
    fee_option.low.CopyFrom(fee_pb2.FeeOption.Low())
    return fee_option


def create_fee_option_medium() -> fee_pb2.FeeOption:
    """Create a FeeOption protobuf message for medium fee"""
    fee_option = fee_pb2.FeeOption()
    fee_option.medium.CopyFrom(fee_pb2.FeeOption.Medium())
    return fee_option


def create_fee_option_high() -> fee_pb2.FeeOption:
    """Create a FeeOption protobuf message for high fee"""
    fee_option = fee_pb2.FeeOption()
    fee_option.high.CopyFrom(fee_pb2.FeeOption.High())
    return fee_option


def create_fee_option_custom(base_fee_per_unit: str, max_tip_fee_per_unit: str, max_price_per_unit: str) -> fee_pb2.FeeOption:
    """Create a FeeOption protobuf message for custom fee rate

    Args:
        base_fee_per_unit: Base fee per unit as string
        max_tip_fee_per_unit: Max tip fee per unit as string
        max_price_per_unit: Max price per unit as string
    """
    fee_option = fee_pb2.FeeOption()
    fee_rate = fee_pb2.FeeRate()
    fee_rate.base_fee_per_unit.value = base_fee_per_unit
    fee_rate.max_tip_fee_per_unit.value = max_tip_fee_per_unit
    fee_rate.max_price_per_unit.value = max_price_per_unit
    fee_option.custom.fee_rate.CopyFrom(fee_rate)
    return fee_option


def format_balance(balance: balance_pb2.Balance) -> Dict[str, Any]:
    """Format balance for display"""
    if not balance:
        return {"onchain": {"usable": 0, "pending": 0}, "offchain": {}}
    
    formatted = {
        "onchain": {
            "usable": decimal_to_float(balance.onchain.usable) if balance.onchain else 0,
            "pending": decimal_to_float(balance.onchain.pending) if balance.onchain else 0
        },
        "offchain": {}
    }
    
    if balance.offchain:
        formatted["offchain"] = {
            "free_local": decimal_to_float(balance.offchain.free_local),
            "free_remote": decimal_to_float(balance.offchain.free_remote),
            "pending_local": decimal_to_float(balance.offchain.pending_local),
            "pending_remote": decimal_to_float(balance.offchain.pending_remote),
            "unavailable_local": decimal_to_float(balance.offchain.unavailable_local),
            "unavailable_remote": decimal_to_float(balance.offchain.unavailable_remote),
        }
    
    return formatted


def format_transaction_status(status: int) -> str:
    """Format transaction status"""
    status_map = {
        0: "Unknown",
        1: "In Mempool",
        2: "Pending Confirmations",
        3: "Completed",
        4: "Failed"
    }
    return status_map.get(status, "Unknown")


def get_asset_display_name(asset_id: str, network: primitives_pb2.Network, client: 'HydraGRPCClient') -> str:
    """Get readable asset name from asset ID using gRPC"""
    if not client:
        return asset_id
        
    try:
        # Try to get asset info from the gRPC service
        asset_info = client.get_asset(network, asset_id)
        if asset_info and asset_info.symbol:
            return asset_info.symbol
        elif asset_info and asset_info.name:
            return asset_info.name
        else:
            # Fallback: return the original asset_id if no readable name found
            return asset_id
    except Exception as e:
        # If anything fails, return the original asset_id
        return asset_id


def get_network_display_name(network: primitives_pb2.Network) -> str:
    """Get display name for a network"""
    if network.protocol == 1:  # PROTOCOL_BITCOIN
        if network.id == "0a03cf40":
            return "Bitcoin Signet"
        elif network.id == "f9beb4d9":
            return "Bitcoin Mainnet"
        elif network.id == "0b110907":
            return "Bitcoin Testnet3"
        else:
            return f"Bitcoin {network.id}"
    else:  # EVM
        if network.id == "11155111":
            return "Ethereum Sepolia"
        elif network.id == "421614":
            return "Arbitrum Sepolia"
        elif network.id == "1":
            return "Ethereum Mainnet"
        elif network.id == "42161":
            return "Arbitrum One"
        elif network.id == "10":
            return "Optimism Mainnet"
        elif network.id == "137":
            return "Polygon Mainnet"
        else:
            return f"EVM {network.id}"


def add_log_entry(message: str, level: str = "INFO", session_state=None):
    """Add a log entry to the session state"""
    if session_state is None:
        return
        
    if 'logs' not in session_state:
        session_state.logs = []
    
    session_state.logs.append({
        'timestamp': datetime.datetime.now().strftime("%H:%M:%S"),
        'level': level,
        'message': message
    })
    
    # Keep only last 1000 logs to prevent memory issues
    if len(session_state.logs) > 1000:
        session_state.logs = session_state.logs[-1000:]


def calculate_total_portfolio_value(balances: Dict[str, Any], network: primitives_pb2.Network, client) -> float:
    """Calculate total USD value of all balances"""
    total_usd = 0.0
    
    if not balances or not client:
        return total_usd
    
    for asset_id, balance in balances.items():
        try:
            formatted = format_balance(balance)
            total_balance = (formatted["onchain"]["usable"] + 
                           formatted["onchain"]["pending"] + 
                           formatted["offchain"].get("free_local", 0) + 
                           formatted["offchain"].get("free_remote", 0))
            
            if total_balance > 0:
                usd_price = None
                if hasattr(client, 'get_asset_fiat_price'):
                    usd_price = client.get_asset_fiat_price(network, asset_id)
                else:
                    # Add the method to existing client instance if not present
                    try:
                        from hydra_pb import pricing_pb2, pricing_pb2_grpc
                        
                        def get_asset_fiat_price_fallback(self, network, asset_id, fiat_currency=0):
                            try:
                                if not hasattr(self, 'pricing_stub'):
                                    self.pricing_stub = pricing_pb2_grpc.PricingServiceStub(self.channel)
                                
                                request = pricing_pb2.GetAssetFiatPriceRequest(
                                    network=network, 
                                    asset_id=asset_id,
                                    fiat_currency=fiat_currency
                                )
                                response = self.pricing_stub.GetAssetFiatPrice(request)
                                if response.price:
                                    return decimal_to_float(response.price)
                            except Exception:
                                return None
                            return None
                        
                        # Add method to client instance
                        import types
                        client.get_asset_fiat_price = types.MethodType(get_asset_fiat_price_fallback, client)
                        usd_price = client.get_asset_fiat_price(network, asset_id)
                    except Exception:
                        pass  # If anything fails, just skip pricing
                        
                if usd_price:
                    total_usd += total_balance * usd_price
        except Exception:
            continue  # Skip assets that fail price lookup
    
    return total_usd


def generate_asset_pairs_from_balances(balances: Dict[str, Any], network) -> list:
    """Generate all possible asset pairs from available balances"""
    assets = list(balances.keys())
    pairs = []
    
    # Generate all combinations of assets (both directions)
    for i, base_asset in enumerate(assets):
        for quote_asset in assets[i+1:]:
            # Create OrderbookCurrency objects
            from .hydra_pb import currency_pb2
            
            base_currency = currency_pb2.OrderbookCurrency(
                protocol=network.protocol,
                network_id=network.id,
                asset_id=base_asset
            )
            
            quote_currency = currency_pb2.OrderbookCurrency(
                protocol=network.protocol,
                network_id=network.id,
                asset_id=quote_asset
            )
            
            # Add both directions: BASE/QUOTE and QUOTE/BASE
            pairs.append({
                'pair_name': f"{base_asset}/{quote_asset}",
                'base_currency': base_currency,
                'quote_currency': quote_currency,
                'base_asset_id': base_asset,
                'quote_asset_id': quote_asset
            })
            
            pairs.append({
                'pair_name': f"{quote_asset}/{base_asset}",
                'base_currency': quote_currency,
                'quote_currency': base_currency,
                'base_asset_id': quote_asset,
                'quote_asset_id': base_asset
            })
    
    return pairs


def get_asset_pairs_with_names(balances: Dict[str, Any], network, client) -> list:
    """Generate asset pairs with resolved asset names"""
    pairs = generate_asset_pairs_from_balances(balances, network)
    
    # Try to resolve asset names for better display
    for pair in pairs:
        try:
            base_name = get_asset_display_name(pair['base_asset_id'], network, client)
            quote_name = get_asset_display_name(pair['quote_asset_id'], network, client)
            
            if base_name and quote_name:
                pair['display_name'] = f"{base_name}/{quote_name}"
            else:
                # Fallback to shortened asset IDs
                base_short = pair['base_asset_id'][:8] + "..." if len(pair['base_asset_id']) > 8 else pair['base_asset_id']
                quote_short = pair['quote_asset_id'][:8] + "..." if len(pair['quote_asset_id']) > 8 else pair['quote_asset_id']
                pair['display_name'] = f"{base_short}/{quote_short}"
        except:
            # Final fallback
            pair['display_name'] = pair['pair_name']
    
    return pairs


def generate_cross_network_pairs(currencies: list, client) -> list:
    """Generate all possible cross-network trading pairs from DEX currencies"""
    pairs = []
    
    # Generate all combinations of currencies (cross-network pairs)
    for i, base_currency_info in enumerate(currencies):
        for quote_currency_info in currencies[i+1:]:
            base_currency = base_currency_info['currency']
            quote_currency = quote_currency_info['currency']
            
            # Create display names
            base_name = get_asset_display_name(
                base_currency_info['asset_id'], 
                base_currency_info['network'], 
                client
            ) or base_currency_info['asset_id'][:8]
            
            quote_name = get_asset_display_name(
                quote_currency_info['asset_id'], 
                quote_currency_info['network'], 
                client
            ) or quote_currency_info['asset_id'][:8]
            
            # Add network info for cross-network pairs
            base_network_name = f"{base_currency_info['protocol_name']}-{base_currency_info['network'].id[:8]}"
            quote_network_name = f"{quote_currency_info['protocol_name']}-{quote_currency_info['network'].id[:8]}"
            
            # Create pair info with network context
            pair_name = f"{base_currency_info['asset_id']}/{quote_currency_info['asset_id']}"
            display_name = f"{base_name} ({base_network_name}) / {quote_name} ({quote_network_name})"
            
            # Add only one direction since markets are non-directional
            pairs.append({
                'pair_name': pair_name,
                'display_name': display_name,
                'base_currency': base_currency,
                'quote_currency': quote_currency,
                'base_asset_id': base_currency_info['asset_id'],
                'quote_asset_id': quote_currency_info['asset_id'],
                'base_network': base_currency_info['network'],
                'quote_network': quote_currency_info['network'],
                'base_protocol': base_currency_info['protocol_name'],
                'quote_protocol': quote_currency_info['protocol_name'],
                'is_cross_network': (base_currency_info['network'].id != quote_currency_info['network'].id)
            })
    
    # Sort pairs: same-network first, then cross-network
    pairs.sort(key=lambda x: (x['is_cross_network'], x['display_name']))
    
    return pairs