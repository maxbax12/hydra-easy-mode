"""
Decentralized Price Oracle
===========================

Multi-source price oracle that fetches from:
1. Pyth Network (Hermes API) — decentralized, no API key
2. CoinGecko (fallback) — centralized but free, no API key, supports HDN

Accepts both ticker symbols (BTC, HDN) and on-chain asset IDs
(ERC20:0x67E6a7..., 0x0000...0000). Asset ID mapping is loaded
from config/exchanges/hydra.yaml at startup.
"""

import requests
import statistics
import time
import yaml
from typing import Optional, Dict
import logging


# Pyth price feed IDs (hex) — from https://pyth.network/price-feeds
PYTH_FEED_IDS = {
    'BTC': 'e62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43',
    'ETH': 'ff61491a931112ddf1bd8147cd1b641375f79f5825126d665480874634fd0ace',
    'SOL': 'ef0d8b6fda2ceba41da15d4095d1da392a0d2f8ed0c6c7bc0f4cfac8c280b56d',
    'USDC': 'eaa020c61cc479712813461ce153894a96a6c00b21ed0cfc2798d1f9a9e9c94a',
    'USDT': '2b89b9dc8fdf9f34709a5b106b472f0f39bb6ca9ce04b0fd7f2e971688e2e53b',
}

# CoinGecko coin IDs
COINGECKO_IDS = {
    'BTC': 'bitcoin',
    'ETH': 'ethereum',
    'SOL': 'solana',
    'HDN': 'hydranet',
    'USDC': 'usd-coin',
    'USDT': 'tether',
}

# Normalize aliases
SYMBOL_MAP = {
    'BITCOIN': 'BTC',
    'ETHEREUM': 'ETH',
    'HYDRA': 'HDN',
    'USDC2': 'USDC',
    # bridged / per-chain USDC variants as named in hydra.yaml
    'USDC.ARB': 'USDC',
    'USDC.ETH': 'USDC',
    'USDC.E': 'USDC',
}

PYTH_HERMES_URL = "https://hermes.pyth.network/v2/updates/price/latest"
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"

HYDRA_CONFIG_PATH = "config/exchanges/hydra.yaml"


def _load_asset_id_map(config_path: str = HYDRA_CONFIG_PATH) -> Dict[str, str]:
    """Build asset_id -> symbol mapping from hydra.yaml trading pairs."""
    mapping = {}
    try:
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        for pair in cfg.get('trading_pairs', []):
            for side in ('base', 'quote'):
                side_cfg = pair.get(side, {})
                asset_id = side_cfg.get('asset_id', '')
                symbol_key = f'{side}_symbol'
                symbol = pair.get(symbol_key, '')
                if asset_id and symbol:
                    mapping[asset_id.lower()] = symbol.upper()
    except Exception:
        pass
    return mapping


class PriceOracle:
    """Multi-source price oracle: Pyth (decentralized) -> CoinGecko (fallback)

    Accepts ticker symbols (BTC, ETH, HDN) or on-chain asset IDs.
    """

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger(__name__)
        self.price_cache: Dict[str, tuple] = {}  # symbol -> (price, timestamp)
        self.cache_ttl = 60
        self.asset_id_map = _load_asset_id_map()
        if self.asset_id_map:
            self.logger.info(f"Oracle: loaded {len(self.asset_id_map)} asset ID mappings")

    def _resolve_symbol(self, identifier: str) -> str:
        """Resolve an identifier (symbol or asset ID) to a canonical symbol."""
        # Already a short symbol
        upper = identifier.upper()
        if upper in SYMBOL_MAP:
            return SYMBOL_MAP[upper]

        # Check asset ID map (case-insensitive)
        lower = identifier.lower()
        if lower in self.asset_id_map:
            sym = self.asset_id_map[lower]
            if sym in SYMBOL_MAP:
                return SYMBOL_MAP[sym]
            return sym

        return upper

    def get_price(self, identifier: str) -> Optional[float]:
        """Get USD price for a symbol or asset ID. Tries Pyth first, then CoinGecko."""
        symbol = self._resolve_symbol(identifier)

        # Stablecoins
        if symbol in ('USDC', 'USDT', 'USD', 'DAI', 'BUSD'):
            return 1.0

        # Check cache
        if symbol in self.price_cache:
            price, ts = self.price_cache[symbol]
            if time.time() - ts < self.cache_ttl:
                return price

        # Try Pyth first
        price = self._fetch_pyth(symbol)

        # Fallback to CoinGecko
        if price is None:
            price = self._fetch_coingecko(symbol)

        if price is not None:
            self.price_cache[symbol] = (price, time.time())
            self.logger.info(f"Oracle: {symbol} = ${price:.6f}")

        return price

    def get_pair_price(self, pair: str) -> Optional[float]:
        """Get price for a trading pair (e.g. 'HDN/USDC' or 'asset_id/asset_id')"""
        if '/' not in pair:
            return None

        base, quote = pair.split('/', 1)
        base = self._resolve_symbol(base.strip())
        quote = self._resolve_symbol(quote.strip())

        # Stablecoin quote — just return base price
        if quote in ('USDC', 'USDC2', 'USDT', 'USD', 'DAI', 'BUSD'):
            return self.get_price(base)

        # Cross pair — get both USD prices and compute ratio
        base_price = self.get_price(base)
        quote_price = self.get_price(quote)

        if base_price and quote_price:
            pair_price = base_price / quote_price
            self.logger.info(f"Oracle: {pair} = {pair_price:.8f}")
            return pair_price

        return None

    # Exchanges quoting the pair directly. No cross rate, no API key, and — unlike
    # Pyth (401 since it began requiring auth) and CoinGecko's free tier (429 when
    # several bots share an IP) — they answer reliably.
    _EXCHANGE_ALIASES = {'kraken': {'BTC': 'XBT'}}

    def _fetch_binance_pair(self, base: str, quote: str) -> Optional[float]:
        try:
            resp = requests.get("https://api.binance.com/api/v3/ticker/price",
                                params={'symbol': f"{base}{quote}"}, timeout=5)
            resp.raise_for_status()
            return float(resp.json()['price'])
        except Exception as e:
            self.logger.debug(f"Binance fetch failed for {base}{quote}: {e}")
            return None

    def _fetch_kraken_pair(self, base: str, quote: str) -> Optional[float]:
        alias = self._EXCHANGE_ALIASES['kraken']
        name = f"{alias.get(base, base)}{alias.get(quote, quote)}"
        try:
            resp = requests.get("https://api.kraken.com/0/public/Ticker",
                                params={'pair': name}, timeout=5)
            resp.raise_for_status()
            result = resp.json().get('result') or {}
            return float(next(iter(result.values()))['c'][0]) if result else None
        except Exception as e:
            self.logger.debug(f"Kraken fetch failed for {name}: {e}")
            return None

    def get_market_pair_price(self, pair: str, max_spread_pct: float = 1.0) -> Optional[float]:
        """Outside-market price for a pair, from sources that quote it directly.

        Median of whichever of Binance / Kraken / the USD cross rate respond. When
        they disagree by more than max_spread_pct the answer is None: a guard built
        on a bad reference is worse than no guard.
        """
        if '/' not in pair:
            return None
        base, quote = (self._resolve_symbol(x.strip()) for x in pair.split('/', 1))
        if base == quote:
            return 1.0          # the same asset on two chains (e.g. USDC.arb/USDC.eth)

        key = f"pair:{base}/{quote}"
        if key in self.price_cache:
            price, ts = self.price_cache[key]
            if time.time() - ts < 30:
                return price

        quotes = [q for q in (self._fetch_binance_pair(base, quote),
                              self._fetch_kraken_pair(base, quote)) if q]
        if len(quotes) < 2:                     # only then spend a CoinGecko call
            cross = self.get_pair_price(pair)
            if cross:
                quotes.append(cross)
        if not quotes:
            return None
        quotes.sort()
        if (quotes[-1] / quotes[0] - 1) * 100 > max_spread_pct:
            self.logger.warning(f"Oracle: sources disagree on {pair}: {quotes} — ignoring")
            return None
        price = statistics.median(quotes)
        self.price_cache[key] = (price, time.time())
        return price

    def _fetch_pyth(self, symbol: str) -> Optional[float]:
        """Fetch price from Pyth Hermes API"""
        feed_id = PYTH_FEED_IDS.get(symbol)
        if not feed_id:
            return None

        try:
            resp = requests.get(
                PYTH_HERMES_URL,
                params={'ids[]': feed_id},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

            for entry in data.get('parsed', []):
                price_data = entry.get('price', {})
                price_val = int(price_data.get('price', 0))
                expo = int(price_data.get('expo', 0))
                if price_val:
                    price = price_val * (10 ** expo)
                    self.logger.debug(f"Pyth: {symbol} = ${price:.6f}")
                    return price

        except Exception as e:
            self.logger.debug(f"Pyth fetch failed for {symbol}: {e}")

        return None

    def _fetch_coingecko(self, symbol: str) -> Optional[float]:
        """Fetch price from CoinGecko API"""
        coin_id = COINGECKO_IDS.get(symbol)
        if not coin_id:
            self.logger.warning(f"No CoinGecko ID for {symbol}")
            return None

        try:
            resp = requests.get(
                COINGECKO_URL,
                params={'ids': coin_id, 'vs_currencies': 'usd'},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

            if coin_id in data and 'usd' in data[coin_id]:
                price = data[coin_id]['usd']
                self.logger.debug(f"CoinGecko: {symbol} = ${price:.6f}")
                return price

        except Exception as e:
            self.logger.debug(f"CoinGecko fetch failed for {symbol}: {e}")

        return None
