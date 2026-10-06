#!/usr/bin/env python3
"""
Multi-Exchange Trading Bot CLI
==============================

A command-line trading bot for automated liquidity provision, market making, and arbitrage
across multiple exchanges using a standardized connector architecture.

Features:
- Multi-exchange support (Hydra DEX, Binance, etc.)
- Pluggable strategy architecture
- Real-time event handling
- Unified trading interface

Usage:
    python trading_bot_cli.py --config config/bot_config.yaml
    python trading_bot_cli.py --daemon --strategy grid_btc_usdc
"""

import argparse
import asyncio
import copy
import logging
import readline
import signal
import sys
import time
import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime
import yaml
from dotenv import load_dotenv

# Load environment variables from .env file (explicit path)
load_dotenv(Path(__file__).resolve().parent / '.env')

# Import multi-exchange architecture
from connectors.exchange_factory import ExchangeFactory, create_exchange_factory
from strategies.base.base_strategy import BaseStrategy, StrategyManager, StrategyConfig, StrategyStatus
from order_tracker import OrderTracker, OrderEvent, OrderEventType, format_order_event
from connectors.base_exchange import Order, OrderSide


@dataclass
class BotConfig:
    """Configuration for the trading bot"""
    exchange_config_dir: str = "config/exchanges"
    strategies: Optional[List[Dict]] = None
    max_position_size: float = 1.0
    max_daily_trades: int = 1000
    cancel_orders_on_exit: bool = True  # Cancel all orders when bot exits
    cmc_api_key: str = ""  # Deprecated — oracle no longer needs an API key
    telegram: Optional[Dict] = None  # Telegram crash notification config
    log_level: str = "INFO"
    log_file: str = "trading_bot.log"


class TradingBotCLI:
    """Main trading bot CLI application"""
    
    def __init__(self, config: BotConfig):
        self.config = config
        self.exchange_factory: Optional[ExchangeFactory] = None
        self.strategy_manager: Optional[StrategyManager] = None
        self.exchanges: Dict[str, any] = {}
        self.running = False
        
        # Order tracking
        self.order_trackers: Dict[str, OrderTracker] = {}

        # Price oracle
        self.price_oracle = None

        # Setup logging
        self._setup_logging()
        self.logger = logging.getLogger(__name__)

        # Initialize Telegram crash notifier
        if self.config.telegram:
            from lib.crash_notifier import init_crash_notifier
            token = self.config.telegram.get("bot_token", "")
            chat = self.config.telegram.get("chat_id", "")
            if token and chat:
                init_crash_notifier(token, str(chat))
                self.logger.info("📡 Telegram crash notifier initialized")

        # Initialize price oracle (Pyth + CoinGecko, no API key needed)
        try:
            from lib.price_oracle import PriceOracle
            self.price_oracle = PriceOracle(self.logger)
            self.logger.info("📊 Price oracle initialized (Pyth + CoinGecko)")
        except Exception as e:
            self.logger.warning(f"⚠️  Failed to initialize price oracle: {e}")

        # Console output control
        self.quiet_mode = False
        self.console_handler = None

        # Hot reload (touch state/reload, or the `reload` command)
        self.config_path: Optional[Path] = None
        self._strategy_entries: Dict[str, Dict] = {}   # name -> the yaml entry it runs with
        self._removed: Dict[str, BaseStrategy] = {}    # stopped by a reload, callbacks still attached
        self._reload_lock: Optional[asyncio.Lock] = None
        self._reload_task: Optional[asyncio.Task] = None
        self._closing = False
        self._shutdown_task: Optional[asyncio.Task] = None
        
    def _setup_logging(self):
        """Configure logging"""
        log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

        # Create handlers
        file_handler = logging.FileHandler(self.config.log_file)
        file_handler.setLevel(getattr(logging, self.config.log_level))
        file_handler.setFormatter(logging.Formatter(log_format))

        self.console_handler = logging.StreamHandler(sys.stdout)
        self.console_handler.setLevel(getattr(logging, self.config.log_level))
        self.console_handler.setFormatter(logging.Formatter(log_format))

        # Setup root logger
        root_logger = logging.getLogger()
        root_logger.setLevel(getattr(logging, self.config.log_level))
        root_logger.addHandler(file_handler)
        root_logger.addHandler(self.console_handler)

    def set_quiet_mode(self, enabled: bool):
        """Enable or disable quiet mode for console output"""
        self.quiet_mode = enabled
        if self.console_handler:
            if enabled:
                # In quiet mode, only show WARNING and above on console
                self.console_handler.setLevel(logging.WARNING)
                print("🔇 Quiet mode enabled - only warnings/errors shown on console")
                print("   (Full logs still written to file)")
            else:
                # Restore normal console logging level
                self.console_handler.setLevel(getattr(logging, self.config.log_level))
                print("🔊 Quiet mode disabled - all logs shown on console")

    async def initialize(self) -> bool:
        """Initialize the bot - load exchanges and strategies"""
        try:
            self.logger.info("🚀 Initializing Multi-Exchange Trading Bot...")
            
            # Create exchange factory
            config_dir = Path(self.config.exchange_config_dir)
            self.exchange_factory = create_exchange_factory(config_dir)
            
            # Load and connect to all exchanges
            self.exchanges = await self.exchange_factory.load_exchanges()
            if not self.exchanges:
                self.logger.error("❌ No exchanges loaded successfully")
                return False
                
            self.logger.info(f"✅ Loaded {len(self.exchanges)} exchanges: {list(self.exchanges.keys())}")
            
            # Extract Hydra client for EstimateOrder validation
            self.hydra_client = None
            if 'hydra' in self.exchanges:
                hydra_exchange = self.exchanges['hydra']
                if hasattr(hydra_exchange, 'client') and hydra_exchange.client:
                    self.hydra_client = hydra_exchange.client
                    self.logger.info("✅ Hydra client extracted for EstimateOrder validation")
                else:
                    self.logger.warning("⚠️  Hydra exchange found but no client available")
            else:
                self.logger.info("ℹ️  No Hydra exchange found - EstimateOrder validation disabled")
            
            # Validate exchange connections
            connected = await self.exchange_factory.connect_all()
            if not connected:
                self.logger.warning("⚠️  Some exchanges failed to connect")
            
            # Initialize order trackers for each exchange
            await self._setup_order_trackers()
            
            # Initialize strategy manager
            self.strategy_manager = StrategyManager()
            
            # Load configured strategies
            await self._load_strategies()
            
            return True
            
        except Exception as e:
            self.logger.error(f"❌ Initialization failed: {e}")
            return False
    
    async def _setup_order_trackers(self):
        """Initialize order trackers for each connected exchange"""
        for exchange_name, exchange in self.exchanges.items():
            try:
                # Only create trackers for exchanges that have a client (like HydraExchange)
                if hasattr(exchange, 'client') and exchange.client:
                    tracker = OrderTracker(exchange.client, self.logger, exchange)

                    # Configure max_swap_failures from strategy configs
                    # Use the highest max_swap_failures from ENABLED strategies on this exchange
                    max_failures = 1  # Default: stop on first failure
                    for strategy_config in self.config.strategies:
                        strategy_exchange = strategy_config.get('exchange', 'hydra')
                        strategy_enabled = strategy_config.get('enabled', True)
                        if strategy_exchange == exchange_name and strategy_enabled:
                            # Check for max_swap_failures in config
                            if 'max_swap_failures' in strategy_config:
                                max_failures = max(max_failures, strategy_config.get('max_swap_failures', 1))

                    tracker.max_swap_failures = max_failures
                    tracker.stop_on_swap_failure = True  # Ensure it's enabled (strategies can override later)
                    self.logger.info(f"⚙️  Configured order tracker for {exchange_name}: max_swap_failures={max_failures}, stop_on_swap_failure={tracker.stop_on_swap_failure}")

                    # Add event callback to display order updates
                    tracker.add_event_callback(self._handle_order_event)

                    # Add shutdown callback for swap failures
                    tracker.shutdown_callback = self._trigger_shutdown

                    # Start the tracker
                    tracker.start()

                    self.order_trackers[exchange_name] = tracker
                    self.logger.info(f"📡 Order tracking enabled for {exchange_name}")

            except Exception as e:
                self.logger.warning(f"⚠️  Failed to setup order tracking for {exchange_name}: {e}")
    
    def _handle_order_event(self, event: OrderEvent):
        """Handle order status events"""
        # Format and display the event
        message = format_order_event(event)
        print(f"\n{message}")

        # Log important events
        if event.event_type.value in ['filled', 'cancelled', 'failed']:
            self.logger.info(message)

        # Show comprehensive overview after fills (important for grid strategies)
        if event.event_type == OrderEventType.FILLED and event.order:
            # Schedule the async overview in the main event loop
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._show_post_fill_overview(event.order))
            except RuntimeError:
                # No running loop, skip overview
                pass

    def _trigger_shutdown(self):
        """Trigger graceful shutdown from any thread (e.g., swap failure)"""
        self.logger.info("🛑 Shutdown triggered, initiating graceful exit...")
        # Stop event loop and trigger shutdown
        try:
            loop = asyncio.get_running_loop()
            # Schedule shutdown in the main event loop
            asyncio.run_coroutine_threadsafe(self.shutdown(), loop)
        except RuntimeError:
            # No event loop, use notify_and_exit as fallback
            self.logger.error("❌ No event loop available, forcing exit")
            from lib.crash_notifier import notify_and_exit
            notify_and_exit("Shutdown triggered but no event loop available")

    async def _load_strategies(self):
        """Load and register strategies from configuration"""
        if not self.config.strategies:
            self.logger.info("No strategies configured")
            return

        for strategy_config in self.config.strategies:
            try:
                # Check if strategy is enabled
                if not strategy_config.get('enabled', True):
                    strategy_name = strategy_config.get('name', 'unnamed')
                    self.logger.info(f"⏩ Skipping disabled strategy: {strategy_name}")
                    continue

                strategy_type = strategy_config.get('type')
                if not strategy_type:
                    self.logger.error(f"No strategy type specified in config: {strategy_config}")
                    continue

                # Load strategy instance
                strategy = await self._create_strategy(strategy_type, strategy_config)
                if strategy:
                    self.strategy_manager.register_strategy(strategy)
                    self._strategy_entries[strategy.name] = copy.deepcopy(strategy_config)
                    self.logger.info(f"Strategy loaded: {strategy_type} - {strategy_config.get('name', 'unnamed')}")
                else:
                    self.logger.error(f"Failed to create strategy: {strategy_type}")

            except Exception as e:
                self.logger.error(f"Error loading strategy {strategy_config}: {e}")

    async def _create_strategy(self, strategy_type: str, strategy_config: dict) -> Optional[BaseStrategy]:
        """Create a strategy instance based on type"""
        try:
            strategy_name = strategy_config.get('name', f"{strategy_type}_strategy")
            exchange_name = strategy_config.get('exchange', 'hydra')
            
            # Get exchange instance
            exchange = self.exchanges.get(exchange_name)
            if not exchange:
                self.logger.error(f"Exchange '{exchange_name}' not available for strategy {strategy_name}")
                return None
            
            # Create strategy instance based on type
            if strategy_type.lower() == 'volume_maker':
                from strategies.volume_maker import VolumeMakerStrategy
                strategy = VolumeMakerStrategy(strategy_name, strategy_config)

            elif strategy_type.lower() in ['grid', 'grid_strategy']:
                from strategies.grid_strategy import GridStrategy
                strategy = GridStrategy(strategy_name, strategy_config)

                # Pass order tracker to strategy for automatic order tracking
                if exchange_name in self.order_trackers:
                    strategy.order_tracker = self.order_trackers[exchange_name]

                # Pass price oracle to strategy if available
                if self.price_oracle:
                    strategy.price_oracle = self.price_oracle

            elif strategy_type.lower() == 'market_maker':
                from strategies.market_maker import MarketMakerStrategy
                strategy = MarketMakerStrategy(strategy_name, strategy_config)
                if exchange_name in self.order_trackers:
                    strategy.order_tracker = self.order_trackers[exchange_name]
                if self.price_oracle:
                    strategy.price_oracle = self.price_oracle

            elif strategy_type.lower() == 'enhanced_grid':
                from strategies.enhanced_grid_strategy import EnhancedGridStrategy
                # Enhanced grid needs orderbook manager
                if not hasattr(self, 'orderbook_manager'):
                    from orderbook_manager import OrderbookManager
                    # OrderbookManager expects params to be at the top level, not nested under 'params'
                    ob_config = {'params': strategy_config.get('params', {})}
                    self.orderbook_manager = OrderbookManager(logger=self.logger, hydra_client=self.hydra_client, config=ob_config)
                strategy = EnhancedGridStrategy(strategy_name, strategy_config, self.orderbook_manager)

                # Same wiring the plain grid gets. Without the order tracker the
                # strategy never learns about fills and the grid goes inert after
                # the first one.
                if exchange_name in self.order_trackers:
                    strategy.order_tracker = self.order_trackers[exchange_name]

                if self.price_oracle:
                    strategy.price_oracle = self.price_oracle
                
            elif strategy_type.lower() == 'market_order_strategy':
                from strategies.market_order_strategy import MarketOrderStrategy
                strategy = MarketOrderStrategy(strategy_name, strategy_config)
                
            elif strategy_type.lower() == 'arbitrage':
                from strategies.arbitrage import ArbitrageDetector, ArbitrageExecutor
                from orderbook_manager import OrderbookManager
                
                # Check if this strategy wants to use shared OrderbookManager
                use_shared_manager = strategy_config.get('shared_orderbook_manager', False)
                
                if use_shared_manager:
                    # Use or create shared OrderbookManager for multi-pair arbitrage
                    if not hasattr(self, 'shared_arbitrage_orderbook_manager'):
                        # Create shared OrderbookManager with optimized config for multi-pair monitoring
                        ob_config = {'params': strategy_config.get('params', {})}
                        print(f"🔧 Creating shared OrderbookManager for multi-pair arbitrage with config: {ob_config}")
                        self.shared_arbitrage_orderbook_manager = OrderbookManager(
                            logger=self.logger,
                            hydra_client=self.hydra_client,
                            config=ob_config
                        )

                    # Use the shared manager
                    orderbook_manager = self.shared_arbitrage_orderbook_manager

                    # Register this pair BEFORE connecting exchanges (if enabled)
                    if strategy_config.get('enabled', True):
                        pair = strategy_config.get('pair', '')
                        orderbook_manager.register_active_strategy_pair(pair)
                        print(f"📌 Registered {pair} as active strategy in shared OrderbookManager")

                        # Connect all exchanges to shared manager once (on first enabled strategy)
                        if not hasattr(self, '_shared_manager_exchanges_connected'):
                            for ex_name, ex_connector in self.exchanges.items():
                                print(f"📡 Connecting {ex_name} to shared OrderbookManager")
                                orderbook_manager.connect_exchange(ex_name, ex_connector)
                            self._shared_manager_exchanges_connected = True

                    print(f"🔗 {strategy_name} using shared OrderbookManager for pair: {strategy_config.get('pair', 'UNKNOWN')}")
                    
                else:
                    # Create individual OrderbookManager (legacy behavior)
                    ob_config = {'params': strategy_config.get('params', {})}
                    print(f"🔧 Creating individual OrderbookManager for {strategy_type} with config: {ob_config}")
                    orderbook_manager = OrderbookManager(logger=self.logger, hydra_client=self.hydra_client, config=ob_config)
                    
                    # Connect exchanges to individual manager
                    if strategy_config.get('enabled', True):
                        for ex_name, ex_connector in self.exchanges.items():
                            orderbook_manager.connect_exchange(ex_name, ex_connector)
                    
                # Create a simple arbitrage strategy wrapper with EstimateOrder validation
                from strategies.arbitrage_strategy_wrapper import ArbitrageStrategyWrapper
                strategy = ArbitrageStrategyWrapper(strategy_name, strategy_config,
                                                  orderbook_manager, self.exchange_factory,
                                                  self.hydra_client)  # Pass Hydra client for EstimateOrder

                # Pass all exchanges to the arbitrage strategy
                strategy.all_exchanges = self.exchanges
                
            else:
                self.logger.error(f"Unknown strategy type: {strategy_type}")
                return None
            
            # Set exchange in exchanges dict
            strategy.exchanges[exchange_name] = exchange
            strategy.exchange = exchange
            
            # Add order event callback for tracking
            if exchange_name in self.order_trackers:
                tracker = self.order_trackers[exchange_name]
                
                def order_event_callback(event, strat=strategy):
                    from order_tracker import OrderEventType
                    if event.event_type == OrderEventType.FILLED:
                        strat.on_order_filled(event.order_id)
                    elif event.event_type == OrderEventType.CANCELLED:
                        strat.on_order_cancelled(event.order_id)
                    elif event.event_type == OrderEventType.PARTIALLY_FILLED and hasattr(strat, 'on_order_partial'):
                        strat.on_order_partial(event.order_id)
                
                tracker.add_event_callback(order_event_callback)
                strategy._tracker_callback = order_event_callback   # detached on a reload

            return strategy
            
        except ImportError as e:
            self.logger.error(f"Failed to import strategy {strategy_type}: {e}")
            return None
        except Exception as e:
            self.logger.error(f"Error creating strategy {strategy_type}: {e}")
            return None

    # ------------------------------------------------------------ hot reload
    RELOAD_TRIGGER = Path("state/reload")

    def start_reload_watch(self):
        """Poll for the reload trigger file (touch state/reload) while the bot runs."""
        if self._reload_task is None:
            self._reload_task = asyncio.get_running_loop().create_task(self._reload_watcher())
            self.logger.info(f"🔄 hot reload armed: edit {self.config_path}, then `touch {self.RELOAD_TRIGGER}`")

    async def _reload_watcher(self):
        while not self._closing:
            await asyncio.sleep(2)
            try:
                if self.RELOAD_TRIGGER.exists():
                    self.RELOAD_TRIGGER.unlink()
                    await self.reload_config(str(self.RELOAD_TRIGGER))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger.error(f"❌ reload watcher: {type(e).__name__}: {e}")

    async def reload_config(self, source: str = "command") -> bool:
        """Re-read the config file and apply strategy changes without a restart.

        All or nothing: the new file is parsed and every enabled market maker's
        config validated first; any error keeps the running config untouched.
        Then, per strategy:
          unchanged                 -> left alone (keeps quoting)
          market maker, new params  -> swapped in at the start of its next pass;
                                       reconcile moves only the quotes that differ
          pair/exchange/state_dir   -> that one strategy is stopped and rebuilt
          disabled / removed        -> stopped (its orders cancelled)
          new / enabled             -> created and started
          other types, changed      -> reported; they need a restart
        """
        if self._closing or self.strategy_manager is None or self.config_path is None:
            return False
        if self._reload_lock is None:
            self._reload_lock = asyncio.Lock()
        async with self._reload_lock:
            from strategies.market_maker import MarketMakerStrategy as MM
            try:
                with open(self.config_path) as f:
                    raw = yaml.safe_load(f) or {}
                if not isinstance(raw, dict):
                    raise ValueError("the file is not a mapping")
                entries = raw.get('strategies') or []
                if not isinstance(entries, list):
                    raise ValueError("strategies must be a list")
                new: Dict[str, Dict] = {}
                for e in entries:
                    if not isinstance(e, dict) or not e.get('name') or not e.get('type'):
                        raise ValueError(f"strategy entry without name/type: {str(e)[:80]}")
                    if e['name'] in new:
                        raise ValueError(f"duplicate strategy name {e['name']}")
                    new[e['name']] = e
                for n, e in new.items():
                    if e.get('enabled', True) and str(e['type']).lower() == 'market_maker':
                        MM.build_config(n, e)
                        if e.get('exchange', 'hydra') not in self.exchanges:
                            raise ValueError(f"{n}: exchange {e.get('exchange', 'hydra')} is not loaded")
            except Exception as e:
                self.logger.warning(f"⚠️  reload rejected ({source}): {e} — the running config is unchanged")
                return False

            notes: List[str] = []
            try:
                want = {n: e for n, e in new.items() if e.get('enabled', True)}
                for n in list(self.strategy_manager.strategies):
                    if n not in want:
                        await self._remove_strategy(n)
                        notes.append(f"{n} stopped")
                for n, e in want.items():
                    strat = self.strategy_manager.strategies.get(n)
                    if strat is None:
                        ok = await self._add_strategy(e)
                        notes.append(f"{n} {'started' if ok else 'FAILED to start'}")
                        continue
                    if self._strategy_entries.get(n) == e:
                        continue
                    if str(e['type']).lower() != 'market_maker' or not hasattr(strat, 'queue_config'):
                        notes.append(f"{n} changed, but only market makers hot-reload — restart to apply")
                        continue
                    unknown = MM.unknown_params(e)
                    if unknown:
                        self.logger.warning(f"⚠️  {n}: ignoring unknown params {', '.join(unknown)}")
                    new_cfg = MM.build_config(n, e)
                    if any(getattr(strat.cfg, k) != getattr(new_cfg, k) for k in MM.RESTART_KEYS):
                        keep = (strat.cfg.pair == new_cfg.pair and strat.cfg.state_dir == new_cfg.state_dir)
                        recent = dict(getattr(strat, '_recent', {})) if keep else None
                        await self._remove_strategy(n)
                        ok = await self._add_strategy(e, recent)
                        notes.append(f"{n} rebuilt ({strat.cfg.pair} -> {new_cfg.pair})"
                                     + ("" if ok else " but FAILED to start"))
                        continue
                    changes = strat.queue_config(new_cfg)
                    self._strategy_entries[n] = copy.deepcopy(e)
                    notes.append(f"{n}: " + (", ".join(changes) if changes else "no effective change")
                                 + (" (applied on its next pass)" if changes else ""))
            except Exception as e:
                self.logger.error(f"❌ reload ({source}) failed part-way: {type(e).__name__}: {e} — "
                                  f"done so far: {'; '.join(notes) or 'nothing'}")
                return False

            restart = [k for k, v in raw.items() if k != 'strategies' and hasattr(self.config, k)
                       and getattr(self.config, k) != v]
            if restart:
                notes.append(f"{', '.join(restart)} changed — needs a restart")
            self.config.strategies = entries
            self.logger.info(f"🔄 reload ({source}): " + ("; ".join(notes) if notes else "no changes"))
            return True

    def _detach(self, strategy):
        """Stop a strategy instance from receiving tracker events (copy-on-write:
        the tracker threads iterate these lists)."""
        cb = getattr(strategy, '_tracker_callback', None)
        on_fail = getattr(strategy, '_on_swap_failure', None)
        for tracker in self.order_trackers.values():
            if cb is not None:
                tracker.event_callbacks = [c for c in tracker.event_callbacks if c is not cb]
            if on_fail is not None and hasattr(tracker, 'swap_failure_callbacks'):
                tracker.swap_failure_callbacks = [c for c in tracker.swap_failure_callbacks if c != on_fail]
        strategy._detached = True

    async def _remove_strategy(self, name: str):
        strat = self.strategy_manager.strategies.get(name)
        if strat is None:
            return
        await self.strategy_manager.stop_strategy(name)     # cancels its orders, saves its state
        self.strategy_manager.unregister_strategy(name)
        self._strategy_entries.pop(name, None)
        # Keep it listening: a fill confirmed after the cancel is still booked into
        # its state file. Detached only if a new instance takes over that file.
        self._removed[name] = strat

    async def _add_strategy(self, entry: Dict, recent: Optional[Dict] = None) -> bool:
        name = entry['name']
        old = self._removed.pop(name, None)
        if old is not None:
            self._detach(old)
            if recent is None and getattr(old, 'cfg', None) is not None and \
                    getattr(old.cfg, 'pair', None) == (entry.get('pair')):
                recent = dict(getattr(old, '_recent', {}))
        strat = await self._create_strategy(str(entry['type']), entry)
        if strat is None:
            return False
        if recent and hasattr(strat, '_recent'):
            strat._recent = recent            # late fills of the old instance's orders land here
        self.strategy_manager.register_strategy(strat)
        self._strategy_entries[name] = copy.deepcopy(entry)
        await self.strategy_manager.start_strategy(name)
        return bool(getattr(strat, '_active', True))

    async def _validate_exchange_pairs(self) -> Dict[str, List[str]]:
        """Validate trading pairs across all exchanges"""
        all_pairs = await self.exchange_factory.get_all_trading_pairs()
        
        for exchange_name, pairs in all_pairs.items():
            self.logger.info(f"✅ {exchange_name}: {len(pairs)} trading pairs available")
            if pairs:
                # Show first few pairs as examples
                example_pairs = pairs[:3]
                self.logger.info(f"   Examples: {', '.join(example_pairs)}")
        
        return all_pairs
    
    def _request_shutdown(self):
        """SIGTERM/SIGINT in daemon mode: run the full shutdown (cancel every order)
        as a task the daemon loop waits for — never exit with orders left resting."""
        if self._shutdown_task is None:
            self.logger.info("🛑 Received stop signal, shutting down...")
            self._shutdown_task = asyncio.ensure_future(self.shutdown())

    async def start_daemon_mode(self):
        """Start the bot in daemon mode with all configured strategies"""
        self.logger.info("🤖 Starting daemon mode...")
        self.running = True
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._request_shutdown)   # replaces the signal.signal handler
            except (NotImplementedError, RuntimeError):
                pass

        try:
            # Start all configured strategies
            await self.strategy_manager.start_all()
            
            strategy_count = len(self.strategy_manager.list_strategies())
            self.logger.info(f"✅ Started {strategy_count} trading strategies")
            self.start_reload_watch()
            
            # Main daemon loop
            while self.running:
                await self._daemon_loop()
                await asyncio.sleep(1)
            if self._shutdown_task is not None:
                await self._shutdown_task
                
        except KeyboardInterrupt:
            self.logger.info("🛑 Received interrupt signal, shutting down...")
            await self.shutdown()
        except Exception as e:
            self.logger.error(f"❌ Daemon error: {e}")
            await self.shutdown()
    
    async def _handle_strategy_event(self, event_data: Dict):
        """Handle events from strategies"""
        self.logger.info(f"📊 Strategy Event: {event_data}")
        
        # Log significant events
        event_type = event_data.get('event', '')
        if event_type == 'order_completed':
            self.logger.info(f"🎯 Order filled: {event_data.get('side', '')} {event_data.get('amount', '')} at {event_data.get('price', '')}")
        elif event_type == 'order_placed':
            self.logger.info(f"📈 Order placed: {event_data.get('side', '')} {event_data.get('amount', '')} at {event_data.get('price', '')}")
        elif event_type == 'error':
            self.logger.error(f"❌ Strategy error: {event_data.get('message', '')}")
    
    async def _daemon_loop(self):
        """Main daemon loop - monitor strategies and handle events"""
        # Check strategy health
        strategy_statuses = self.strategy_manager.get_all_status()
        
        for strategy_name, status in strategy_statuses.items():
            if not status.running:
                self.logger.warning(f"⚠️  Strategy {strategy_name} stopped running")
                if status.last_error:
                    self.logger.error(f"   Last error: {status.last_error}")
        
        # TODO: Check for arbitrage opportunities across exchanges
        # TODO: Risk management checks
        # TODO: Performance monitoring
        # TODO: Exchange health monitoring

    def _setup_readline(self):
        """Setup readline for command history and arrow key navigation"""
        # Set up history file
        history_file = os.path.expanduser("~/.trading_bot_history")

        # Create history file if it doesn't exist
        if not os.path.exists(history_file):
            try:
                open(history_file, 'w').close()
            except IOError:
                pass  # Ignore if we can't create it

        # Load command history
        try:
            readline.read_history_file(history_file)
            # Set max history size
            readline.set_history_length(1000)
        except FileNotFoundError:
            pass  # No history file yet
        except Exception as e:
            self.logger.debug(f"Could not load command history: {e}")

        # Setup readline to save history on exit
        import atexit
        atexit.register(lambda: self._save_history(history_file))

        # Enable tab completion (optional - we could add custom completions later)
        readline.parse_and_bind("tab: complete")

        self.logger.debug("✓ Command history and arrow key support enabled")

    def _save_history(self, history_file):
        """Save command history to file"""
        try:
            readline.write_history_file(history_file)
        except Exception as e:
            # Don't log errors during shutdown
            pass

    async def interactive_mode(self):
        """Start interactive command-line mode"""
        self.logger.info("💬 Starting interactive mode...")

        # Setup readline for command history and arrow key support
        self._setup_readline()

        # Enable quiet mode by default in interactive mode
        # This prevents strategy logs from interfering with user input
        self.set_quiet_mode(True)

        # Auto-start enabled strategies in interactive mode
        enabled_strategies = []
        for strategy_name, strategy in self.strategy_manager.strategies.items():
            if hasattr(strategy, 'config'):
                # Handle both dict and dataclass config types
                if hasattr(strategy.config, 'enabled'):
                    enabled = strategy.config.enabled
                elif hasattr(strategy.config, 'get'):
                    enabled = strategy.config.get('enabled', True)
                else:
                    enabled = True  # Default to enabled
                
                if enabled:
                    enabled_strategies.append(strategy_name)
        
        if enabled_strategies:
            self.logger.info(f"🚀 Auto-starting {len(enabled_strategies)} enabled strategies...")
            for strategy_name in enabled_strategies:
                await self.strategy_manager.start_strategy(strategy_name)
                self.logger.info(f"✅ Started strategy: {strategy_name}")
        
        self.start_reload_watch()
        self.logger.info("Type 'help' for available commands, 'exit' to quit")
        
        try:
            while True:
                try:
                    # Use asyncio to read input without blocking the event loop
                    loop = asyncio.get_event_loop()
                    command = await loop.run_in_executor(None, input, "trading-bot> ")
                    command = command.strip()
                    if not command:
                        continue
                        
                    if command.lower() in ['exit', 'quit']:
                        break
                    elif command.lower() == 'help':
                        self._print_help()
                    else:
                        await self._execute_command(command)
                        
                except KeyboardInterrupt:
                    print("\nUse 'exit' to quit")
                    continue
                    
        finally:
            await self.shutdown()
    
    def _print_help(self):
        """Print available commands"""
        help_text = """
🤖 Multi-Exchange Trading Bot Commands:

Exchanges:
  exchanges                      - List loaded exchanges
  exchange-status <name>         - Show exchange status
  pairs <exchange>               - List trading pairs for exchange
  prices <exchange> [pair]       - Show current prices for all markets or specific pair
  orderbook <exchange> <pair> [depth] - Show orderbook depth for arbitrage analysis
  validate-orderbook <exchange> <pair> [level] - Validate orderbook calculation using EstimateOrder
  trades <exchange> [pair]       - Show recent trades/transactions for exchange or specific pair
  
Strategies:
  strategies                     - List active strategies
  start-strategy <name>          - Start a strategy
  stop-strategy <name>           - Stop a strategy
  strategy-status <name>         - Show strategy status
  reload                         - Re-read the config file and apply strategy changes live
                                   (same as `touch state/reload` from a shell)

Market Orders:
  buy <exchange> <amount> <pair> - Place market buy order
  sell <exchange> <amount> <pair> - Place market sell order
  
Limit Orders:
  limit-buy <exchange> <amount> <pair> <price> - Place limit buy order
  limit-sell <exchange> <amount> <pair> <price> - Place limit sell order
  
Order Tracking:
  orders                         - Show all active orders
  order-status <id>              - Show status of specific order
  order-events                   - Show recent order events
  
Information:
  status                         - Show overall bot status
  balances <exchange>            - Show account balances
  marketinfo <exchange> <pair>   - Check market base/quote orientation (Hydra only)

Output Control:
  quiet                          - Enable quiet mode (only warnings/errors shown)
  verbose                        - Disable quiet mode (show all logs)

Control:
  help                           - Show this help
  exit                           - Exit the bot
        """
        print(help_text)
    
    async def _execute_command(self, command: str):
        """Execute a user command"""
        parts = command.split()
        if not parts:
            return
            
        cmd = parts[0].lower()
        args = parts[1:]
        
        try:
            if cmd == "exchanges":
                await self._cmd_list_exchanges()
            elif cmd == "exchange-status" and len(args) >= 1:
                await self._cmd_exchange_status(args[0])
            elif cmd == "pairs" and len(args) >= 1:
                await self._cmd_list_pairs(args[0])
            elif cmd == "prices" and len(args) >= 1:
                pair = args[1] if len(args) >= 2 else None
                await self._cmd_show_prices(args[0], pair)
            elif cmd == "orderbook" and len(args) >= 2:
                depth = int(args[2]) if len(args) >= 3 and args[2].isdigit() else 10
                await self._cmd_show_orderbook(args[0], args[1], depth)
            elif cmd == "trades" and len(args) >= 1:
                pair = args[1] if len(args) >= 2 else None
                await self._cmd_show_trades(args[0], pair)
            elif cmd == "strategies":
                await self._cmd_list_strategies()
            elif cmd == "reload":
                await self.reload_config("command")
            elif cmd == "start-strategy" and len(args) >= 1:
                await self._cmd_start_strategy(args[0])
            elif cmd == "stop-strategy" and len(args) >= 1:
                await self._cmd_stop_strategy(args[0])
            elif cmd == "strategy-status" and len(args) >= 1:
                await self._cmd_strategy_status(args[0])
            elif cmd == "balances" and len(args) >= 1:
                await self._cmd_show_balances(args[0])
            elif cmd == "status":
                await self._cmd_show_status()
            elif cmd == "quiet":
                self.set_quiet_mode(True)
            elif cmd == "verbose":
                self.set_quiet_mode(False)
            elif cmd == "buy" and len(args) >= 3:
                await self._cmd_market_buy(args[0], args[1], args[2])
            elif cmd == "sell" and len(args) >= 3:
                await self._cmd_market_sell(args[0], args[1], args[2])
            elif cmd == "limit-buy" and len(args) >= 4:
                await self._cmd_limit_buy(args[0], args[1], args[2], args[3])
            elif cmd == "limit-sell" and len(args) >= 4:
                await self._cmd_limit_sell(args[0], args[1], args[2], args[3])
            elif cmd == "orders":
                await self._cmd_show_orders()
            elif cmd == "orders-all":
                await self._cmd_show_all_orders()
            elif cmd == "order-status" and len(args) >= 1:
                await self._cmd_order_status(args[0])
            elif cmd == "order-events":
                await self._cmd_order_events()
            elif cmd == "validate-orderbook" and len(args) >= 2:
                level = int(args[2]) if len(args) >= 3 and args[2].isdigit() else 0
                await self._cmd_validate_orderbook(args[0], args[1], level)
            elif cmd == "marketinfo" and len(args) >= 2:
                await self._cmd_marketinfo(args[0], args[1])
            elif cmd == "wait" and len(args) >= 1:
                await self._cmd_wait(float(args[0]))
            else:
                print(f"❌ Unknown command: {command}")
                print("Type 'help' for available commands")
                
        except Exception as e:
            print(f"❌ Command error: {e}")
    
    async def _cmd_list_exchanges(self):
        """List loaded exchanges"""
        print("🏢 Loaded Exchanges:")
        for name, exchange in self.exchanges.items():
            status = "✅ Connected" if exchange.connected else "❌ Disconnected"
            print(f"  {name}: {status}")
            
    async def _cmd_exchange_status(self, exchange_name: str):
        """Show exchange status"""
        if exchange_name not in self.exchanges:
            print(f"❌ Exchange '{exchange_name}' not found")
            return
            
        exchange = self.exchanges[exchange_name]
        print(f"🏢 Exchange Status: {exchange_name}")
        print(f"  Connected: {'✅ Yes' if exchange.connected else '❌ No'}")
        print(f"  Type: {exchange.__class__.__name__}")
            
    async def _cmd_list_pairs(self, exchange_name: str):
        """List available trading pairs for an exchange"""
        if exchange_name not in self.exchanges:
            print(f"❌ Exchange '{exchange_name}' not found")
            return
            
        exchange = self.exchanges[exchange_name]
        try:
            pairs = await exchange.get_trading_pairs()
            print(f"📊 Trading Pairs on {exchange_name}:")
            
            for i, pair in enumerate(pairs[:20], 1):  # Show first 20 pairs
                print(f"  {i}. {pair.symbol} ({pair.base}/{pair.quote})")
                
            if len(pairs) > 20:
                print(f"  ... and {len(pairs) - 20} more pairs")
                
        except Exception as e:
            print(f"❌ Error getting pairs: {e}")

    async def _cmd_show_prices(self, exchange_name: str, pair: str = None):
        """Show current prices for all markets or a specific pair on an exchange"""
        if exchange_name not in self.exchanges:
            print(f"❌ Exchange '{exchange_name}' not found")
            return
        
        try:
            exchange = self.exchanges[exchange_name]
            
            if pair:
                # Show price for specific pair
                await self._show_single_pair_price(exchange, exchange_name, pair)
            else:
                # Show prices for all pairs
                await self._show_all_prices(exchange, exchange_name)
                
        except Exception as e:
            print(f"❌ Error getting prices: {e}")

    async def _show_single_pair_price(self, exchange, exchange_name: str, pair_str: str):
        """Show price for a specific trading pair"""
        try:
            # Parse the pair
            if '/' not in pair_str:
                print(f"❌ Invalid pair format. Use format like: BTC/USDC")
                return
                
            base, quote = pair_str.split('/', 1)
            from connectors.base_exchange import TradingPair
            trading_pair = TradingPair(base=base, quote=quote, symbol=pair_str)
            
            # Get orderbook
            orderbook = await exchange.get_orderbook(trading_pair)
            if not orderbook or not orderbook.bids or not orderbook.asks:
                print(f"❌ No orderbook data available for {pair_str}")
                return
            
            # Parse prices based on format
            if isinstance(orderbook.bids[0], (tuple, list)):
                # Handle both tuple and list formats: [price, size] or (price, size)
                best_bid = orderbook.bids[0][0]
                best_ask = orderbook.asks[0][0]
                bid_size = orderbook.bids[0][1] if len(orderbook.bids[0]) > 1 else 0
                ask_size = orderbook.asks[0][1] if len(orderbook.asks[0]) > 1 else 0
            elif isinstance(orderbook.bids[0], dict):
                best_bid = orderbook.bids[0]['price']
                best_ask = orderbook.asks[0]['price']
                bid_size = orderbook.bids[0].get('amount', 0)
                ask_size = orderbook.asks[0].get('amount', 0)
            else:
                # Object with attributes
                best_bid = orderbook.bids[0].price
                best_ask = orderbook.asks[0].price
                bid_size = getattr(orderbook.bids[0], 'amount', 0)
                ask_size = getattr(orderbook.asks[0], 'amount', 0)
            
            mid_price = (best_bid + best_ask) / 2.0
            spread_pct = ((best_ask - best_bid) / mid_price) * 100
            
            print(f"💰 {pair_str} Price on {exchange_name.upper()}:")
            print(f"  Best Bid:    {best_bid:.8f} (size: {bid_size:.6f})")
            print(f"  Best Ask:    {best_ask:.8f} (size: {ask_size:.6f})")
            print(f"  Mid Price:   {mid_price:.8f}")
            print(f"  Spread:      {spread_pct:.3f}%")
            
        except Exception as e:
            print(f"❌ Error getting price for {pair_str}: {e}")

    async def _show_all_prices(self, exchange, exchange_name: str):
        """Show prices for all available pairs"""
        try:
            pairs = await exchange.get_trading_pairs()
            
            if not pairs:
                print(f"💰 No trading pairs available on {exchange_name}")
                return
            
            print(f"💰 Current Prices on {exchange_name.upper()}:")
            print(f"{'Pair':<15} {'Bid':<12} {'Ask':<12} {'Spread':<8} {'Mid Price':<12}")
            print("=" * 65)
            
            prices_shown = 0
            for trading_pair in sorted(pairs, key=lambda p: p.symbol)[:10]:  # Show first 10 pairs
                try:
                    # Get orderbook
                    orderbook = await exchange.get_orderbook(trading_pair)
                    if not orderbook or not orderbook.bids or not orderbook.asks:
                        continue
                    
                    # Parse prices based on format
                    if isinstance(orderbook.bids[0], (tuple, list)):
                        # Handle both tuple and list formats: [price, size] or (price, size)
                        best_bid = orderbook.bids[0][0]
                        best_ask = orderbook.asks[0][0]
                    elif isinstance(orderbook.bids[0], dict):
                        best_bid = orderbook.bids[0]['price']
                        best_ask = orderbook.asks[0]['price']
                    else:
                        # Object with attributes
                        best_bid = orderbook.bids[0].price
                        best_ask = orderbook.asks[0].price
                    
                    if best_bid > 0 and best_ask > 0:
                        mid_price = (best_bid + best_ask) / 2.0
                        spread_pct = ((best_ask - best_bid) / mid_price) * 100
                        
                        print(f"{trading_pair.symbol:<15} {best_bid:<12.2f} {best_ask:<12.2f} {spread_pct:<8.3f}% {mid_price:<12.2f}")
                        prices_shown += 1
                        
                except Exception as e:
                    # Skip pairs that cause errors
                    continue
            
            if prices_shown == 0:
                print("  No price data available")
            elif len(pairs) > 10:
                print(f"\nShowing {prices_shown} markets (out of {len(pairs)} total pairs)")
                print(f"Use 'prices {exchange_name} <pair>' to see details for a specific pair")
                
        except Exception as e:
            print(f"❌ Error getting all prices: {e}")

    async def _cmd_show_orderbook(self, exchange_name: str, pair: str, depth: int = 10):
        """Show orderbook depth for arbitrage analysis"""
        if exchange_name not in self.exchanges:
            print(f"❌ Exchange '{exchange_name}' not found")
            return
            
        exchange = self.exchanges[exchange_name]
        
        # Parse pair - simple format like "BTC/USDT"
        if '/' not in pair:
            print(f"❌ Invalid pair format: {pair}. Use format like BTC/USDT")
            return
            
        base, quote = pair.split('/', 1)
        
        
        try:
            from connectors.base_exchange import TradingPair
            trading_pair = TradingPair(base=base, quote=quote, symbol=pair)
            
            orderbook = await exchange.get_orderbook(trading_pair)
            if not orderbook:
                print(f"❌ No orderbook data available for {pair}")
                if exchange_name == 'hydra':
                    print(f"💡 Tip: Check correct orientation with: marketinfo hydra {pair}")
                return

            if not orderbook.bids and not orderbook.asks:
                print(f"❌ Empty orderbook for {pair}")
                if exchange_name == 'hydra':
                    print(f"💡 Tip: Check correct orientation with: marketinfo hydra {pair}")
                return
            
            # Handle different orderbook formats
            bids = []
            asks = []
            
            for i, bid in enumerate(orderbook.bids[:depth]):
                if isinstance(bid, (tuple, list)):
                    # Handle extended tuple format for Hydra: (price, size, min_price, max_price)
                    if len(bid) >= 4:
                        price, size, min_price, max_price = bid[0], bid[1], bid[2], bid[3]
                        bids.append((price, size, min_price, max_price))
                    else:
                        # Standard tuple format: (price, size)
                        price, size = bid[0], bid[1]
                        bids.append((price, size))
                elif isinstance(bid, dict):
                    price, size = bid['price'], bid.get('amount', 0)
                    bids.append((price, size))
                else:
                    # Object with attributes
                    price, size = bid.price, getattr(bid, 'amount', 0)
                    bids.append((price, size))
                
            for i, ask in enumerate(orderbook.asks[:depth]):
                if isinstance(ask, (tuple, list)):
                    # Handle extended tuple format for Hydra: (price, size, min_price, max_price)
                    if len(ask) >= 4:
                        price, size, min_price, max_price = ask[0], ask[1], ask[2], ask[3]
                        asks.append((price, size, min_price, max_price))
                    else:
                        # Standard tuple format: (price, size)
                        price, size = ask[0], ask[1]
                        asks.append((price, size))
                elif isinstance(ask, dict):
                    price, size = ask['price'], ask.get('amount', 0)
                    asks.append((price, size))
                else:
                    # Object with attributes
                    price, size = ask.price, getattr(ask, 'amount', 0)
                    asks.append((price, size))
            
            # Calculate cumulative volumes
            bid_cum_volume = 0
            ask_cum_volume = 0
            
            # Check if this is Hydra (has range info)
            is_hydra = exchange_name.lower() == 'hydra' and any(len(ask) >= 4 for ask in asks if isinstance(ask, (tuple, list)))
            
            if is_hydra:
                print(f"📈 ASKS (Sell Orders) - Size in {base} - Hydra Liquidity Ranges")
                print("Price Range".rjust(20), f"Size ({base})".rjust(15), f"Cumulative ({base})".rjust(18))
                print("-" * 56)
                
                # Show asks in reverse order (highest price first)
                for ask_data in reversed(asks):
                    if len(ask_data) >= 4:
                        price, size, min_price, max_price = ask_data
                        ask_cum_volume += size
                        print(f"{min_price:>8.2f}-{max_price:>8.2f} {size:>15.6f} {ask_cum_volume:>18.6f}")
                    else:
                        price, size = ask_data[0], ask_data[1]
                        ask_cum_volume += size
                        print(f"{price:>20.2f} {size:>15.6f} {ask_cum_volume:>18.6f}")
            else:
                print(f"📈 ASKS (Sell Orders) - Size in {base}")
                print("Price".rjust(12), f"Size ({base})".rjust(15), f"Cumulative ({base})".rjust(18))
                print("-" * 48)
                
                # Show asks in reverse order (highest price first)
                for ask_data in reversed(asks):
                    price, size = ask_data[0], ask_data[1]
                    ask_cum_volume += size
                    print(f"{price:>12.2f} {size:>15.6f} {ask_cum_volume:>18.6f}")
            
            print()
            if is_hydra:
                print(f"📉 BIDS (Buy Orders) - Size in {base} - Hydra Liquidity Ranges")
                print("Price Range".rjust(20), f"Size ({base})".rjust(15), f"Cumulative ({base})".rjust(18))
                print("-" * 56)
                
                # Show bids in descending price order
                for bid_data in bids:
                    if len(bid_data) >= 4:
                        price, size, min_price, max_price = bid_data
                        bid_cum_volume += size
                        print(f"{min_price:>8.2f}-{max_price:>8.2f} {size:>15.6f} {bid_cum_volume:>18.6f}")
                    else:
                        price, size = bid_data[0], bid_data[1]
                        bid_cum_volume += size
                        print(f"{price:>20.2f} {size:>15.6f} {bid_cum_volume:>18.6f}")
            else:
                print(f"📉 BIDS (Buy Orders) - Size in {base}")
                print("Price".rjust(12), f"Size ({base})".rjust(15), f"Cumulative ({base})".rjust(18))
                print("-" * 48)
                
                # Show bids in descending price order
                for bid_data in bids:
                    price, size = bid_data[0], bid_data[1]
                    bid_cum_volume += size
                    print(f"{price:>12.2f} {size:>15.6f} {bid_cum_volume:>18.6f}")
            
            print()
            
            if bids and asks:
                # Extract price from first element regardless of tuple length
                best_bid = bids[0][0]
                best_ask = asks[0][0]
                spread = best_ask - best_bid
                spread_pct = (spread / best_bid) * 100 if best_bid > 0 else 0
                
                print(f"💰 Best Bid: {best_bid:.2f} {quote}")
                print(f"💰 Best Ask: {best_ask:.2f} {quote}")
                print(f"📏 Spread: {spread:.2f} {quote} ({spread_pct:.3f}%)")
                print(f"📊 Total Bid Volume: {bid_cum_volume:.6f} {base} (wants to buy {base})")
                print(f"📊 Total Ask Volume: {ask_cum_volume:.6f} {base} (for sale)")
                
        except Exception as e:
            print(f"❌ Error getting orderbook: {e}")
            import traceback
            print(traceback.format_exc())

    async def _cmd_show_trades(self, exchange_name: str, pair: str = None):
        """Show recent trades/transactions from the exchange and wallet"""
        if exchange_name not in self.exchanges:
            print(f"❌ Exchange '{exchange_name}' not found")
            return
            
        exchange = self.exchanges[exchange_name]
        
        # Show wallet transactions first if this is Hydra exchange
        if exchange_name.lower() == 'hydra' and hasattr(exchange, 'client') and exchange.client:
            await self._show_wallet_transactions(exchange.client)
            print()  # Add spacing
        
        if pair:
            # Show trades for specific pair
            if '/' not in pair:
                print(f"❌ Invalid pair format: {pair}. Use format like BTC/USDC")
                return
                
            base, quote = pair.split('/', 1)
            print(f"💹 Recent Trades for {pair} on {exchange_name.upper()}:")
            
            try:
                from connectors.base_exchange import TradingPair
                trading_pair = TradingPair(base=base, quote=quote, symbol=pair)
                
                # Try to get trade history for the specific pair
                if hasattr(exchange, 'get_trade_history'):
                    trades = await exchange.get_trade_history(trading_pair, limit=20)
                    if trades:
                        print()
                        print("Time".ljust(12), "Side".ljust(6), "Amount".rjust(15), "Price".rjust(15), "Total".rjust(15))
                        print("-" * 68)
                        
                        for trade in trades:
                            timestamp = datetime.fromtimestamp(trade.timestamp).strftime('%H:%M:%S')
                            side_emoji = "🟢" if trade.side == "buy" else "🔴"
                            total = trade.amount * trade.price
                            
                            print(f"{timestamp.ljust(12)} {side_emoji}{trade.side.upper().ljust(5)} "
                                  f"{trade.amount:>15.6f} {trade.price:>15.2f} {total:>15.2f}")
                    else:
                        print("  No trade history available")
                else:
                    print("  Trade history not supported by this exchange")
                    
            except Exception as e:
                print(f"❌ Error getting trade history: {e}")
        else:
            # Show all recent trades for user
            print(f"💹 Your Recent Trades on {exchange_name.upper()}:")
            
            try:
                # Get all market trades for the user
                if hasattr(exchange, 'get_all_market_trades'):
                    all_trades = await exchange.get_all_market_trades()
                    
                    if all_trades:
                        print()
                        print("Time".ljust(12), "Pair".ljust(12), "Side".ljust(6), "Amount".rjust(15), "Price".rjust(15))
                        print("-" * 65)
                        
                        # Sort by timestamp (most recent first)
                        sorted_trades = []
                        for pair_trades in all_trades:
                            for trade in pair_trades.trades:
                                sorted_trades.append((pair_trades, trade))
                        
                        # Sort by timestamp descending
                        sorted_trades.sort(key=lambda x: x[1].timestamp.seconds, reverse=True)
                        
                        # Show up to 20 most recent trades
                        for pair_trades, trade in sorted_trades[:20]:
                            pair_symbol = f"{pair_trades.base.asset_id[:6]}/{pair_trades.quote.asset_id[:6]}"
                            timestamp = datetime.fromtimestamp(trade.timestamp.seconds).strftime('%H:%M:%S')
                            side_emoji = "🟢" if trade.order_side == 0 else "🔴"  # 0=BUY, 1=SELL
                            side_text = "BUY" if trade.order_side == 0 else "SELL"
                            
                            # Convert protobuf decimals
                            amount = float(trade.base_amount.lo) / (10 ** trade.base_amount.scale)
                            price = float(trade.price.lo) / (10 ** trade.price.scale)
                            
                            print(f"{timestamp.ljust(12)} {pair_symbol.ljust(12)} {side_emoji}{side_text.ljust(5)} "
                                  f"{amount:>15.6f} {price:>15.2f}")
                    else:
                        print("  No trades found")
                else:
                    print("  Trade history not supported by this exchange")
                    
            except Exception as e:
                print(f"❌ Error getting trades: {e}")
                import traceback
                print(traceback.format_exc())
    
    async def _show_wallet_transactions(self, grpc_client):
        """Show wallet transactions from all networks"""
        print("💰 Wallet Transactions:")
        
        try:
            # Get all networks
            networks = grpc_client.get_networks()
            if not networks:
                print("  No networks available")
                return
                
            all_transactions = []
            
            # Get transactions from each network
            for network in networks:
                try:
                    transactions = grpc_client.get_transactions(network)
                    if transactions:
                        for tx in transactions:
                            all_transactions.append((network, tx))
                            
                except Exception as e:
                    print(f"  Error getting transactions for network {network.id}: {e}")
                    continue
            
            if not all_transactions:
                print("  No transactions found")
                return
                
            # Display transactions
            print()
            print("Network".ljust(10), "ID".ljust(12), "Status".ljust(15), "Confirmations".rjust(8), "Fee".rjust(12))
            print("-" * 62)
            
            # Sort by most recent first (if transactions have timestamps)
            for network, tx in all_transactions[-20:]:  # Show last 20
                # Get status name
                status_map = {
                    0: "PENDING",
                    1: "CONFIRMED", 
                    2: "FAILED",
                    3: "CANCELLED"
                }
                status_name = status_map.get(tx.status, f"UNKNOWN({tx.status})")
                
                # Convert fee to float if available
                fee_str = "0"
                if hasattr(tx, 'fee') and tx.fee:
                    try:
                        from lib.utils import decimal_to_float
                        fee_float = decimal_to_float(tx.fee)
                        fee_str = f"{fee_float:.6f}"
                    except:
                        fee_str = "N/A"
                
                network_name = f"{network.protocol}:{network.id[:6]}"
                tx_id = tx.id[:10] if tx.id else "N/A"
                
                print(f"{network_name.ljust(10)} {tx_id.ljust(12)} {status_name.ljust(15)} "
                      f"{str(tx.confirmations).rjust(8)} {fee_str.rjust(12)}")
                
                # Show spent/received amounts if available
                if hasattr(tx, 'spent') and tx.spent:
                    for asset_id, amount in tx.spent.items():
                        try:
                            from lib.utils import decimal_to_float
                            amount_float = decimal_to_float(amount)
                            print(f"  → Spent: {amount_float:.8f} {asset_id[:8]}")
                        except:
                            print(f"  → Spent: [amount] {asset_id[:8]}")
                            
                if hasattr(tx, 'received') and tx.received:
                    for asset_id, amount in tx.received.items():
                        try:
                            from lib.utils import decimal_to_float
                            amount_float = decimal_to_float(amount)
                            print(f"  ← Received: {amount_float:.8f} {asset_id[:8]}")
                        except:
                            print(f"  ← Received: [amount] {asset_id[:8]}")
                
                print()  # Empty line between transactions
                
        except Exception as e:
            print(f"❌ Error getting wallet transactions: {e}")
            import traceback
            print(traceback.format_exc())

    async def _cmd_show_balances(self, exchange_name: str):
        """Show account balances for an exchange"""
        if exchange_name not in self.exchanges:
            print(f"❌ Exchange '{exchange_name}' not found")
            return
            
        exchange = self.exchanges[exchange_name]
        print(f"💰 Account Balances on {exchange_name}:")
        
        try:
            balances = await exchange.get_balances()
            if not balances:
                print("  No balances found (may require API credentials)")
                return
                
            for asset, balance in balances.items():
                if balance.total > 0:
                    print(f"  {asset}:")
                    print(f"    Total: {balance.total:.8f}")
                    print(f"    Free: {balance.free:.8f}")
                    print(f"    Locked: {balance.locked:.8f}")
                    
        except Exception as e:
            print(f"❌ Error fetching balances: {e}")

    async def _cmd_show_status(self):
        """Show overall bot status"""
        print("🤖 Bot Status:")
        print(f"  Running: {'✅ Yes' if self.running else '❌ No'}")
        print(f"  Exchanges: {len(self.exchanges)}")
        
        if self.strategy_manager:
            strategies = self.strategy_manager.list_strategies()
            print(f"  Strategies: {len(strategies)}")
        else:
            print("  Strategies: 0 (not initialized)")

    async def _cmd_list_strategies(self):
        """List active strategies"""
        if not self.strategy_manager:
            print("📊 No strategy manager initialized")
            return
            
        strategies = self.strategy_manager.list_strategies()
        if not strategies:
            print("📊 No active strategies")
            return
            
        print("📊 Active Strategies:")
        statuses = self.strategy_manager.get_all_status()
        
        for strategy_name in strategies:
            status = statuses.get(strategy_name)
            if status:
                print(f"  🤖 {strategy_name}:")
                print(f"    Running: {'✅ Yes' if status.running else '❌ No'}")
                print(f"    Active Positions: {status.active_positions}")
                print(f"    Total Trades: {status.total_trades}")
                print(f"    P&L: {status.profit_loss:+.8f}")
                if status.last_error:
                    print(f"    Last Error: {status.last_error}")

    async def _cmd_start_strategy(self, strategy_name: str):
        """Start a specific strategy"""
        if not self.strategy_manager:
            print("❌ Strategy manager not initialized")
            return
            
        await self.strategy_manager.start_strategy(strategy_name)
        print(f"🚀 Started strategy: {strategy_name}")
    
    async def _cmd_stop_strategy(self, strategy_name: str):
        """Stop a specific strategy"""
        if not self.strategy_manager:
            print("❌ Strategy manager not initialized")
            return
            
        await self.strategy_manager.stop_strategy(strategy_name)
        print(f"🛑 Stopped strategy: {strategy_name}")
        
    async def _cmd_strategy_status(self, strategy_name: str):
        """Show detailed strategy status"""
        if not self.strategy_manager:
            print("❌ Strategy manager not initialized")
            return
            
        statuses = self.strategy_manager.get_all_status()
        status = statuses.get(strategy_name)
        
        if not status:
            print(f"❌ Strategy '{strategy_name}' not found")
            return
            
        print(f"🤖 Strategy Status: {strategy_name}")
        print(f"  Running: {'✅ Yes' if status.running else '❌ No'}")
        print(f"  Active Positions: {status.active_positions}")
        print(f"  Total Trades: {status.total_trades}")
        print(f"  Profit & Loss: {status.profit_loss:+.8f}")
        print(f"  Error Count: {status.error_count}")
        if status.last_error:
            print(f"  Last Error: {status.last_error}")
        print(f"  Last Update: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(status.last_update))}")

    async def _cmd_market_buy(self, exchange_name: str, amount: str, pair: str):
        """Execute market buy order"""
        print(f"🔍 DEBUG: Starting market buy - exchange: {exchange_name}, amount: {amount}, pair: {pair}")
        
        if exchange_name not in self.exchanges:
            print(f"❌ Exchange '{exchange_name}' not found")
            print(f"🔍 DEBUG: Available exchanges: {list(self.exchanges.keys())}")
            return
            
        try:
            print(f"🔍 DEBUG: Parsing amount '{amount}' to float")
            amount_float = float(amount)
            print(f"🔍 DEBUG: Parsed amount: {amount_float}")
            
            exchange = self.exchanges[exchange_name]
            print(f"🔍 DEBUG: Got exchange object: {type(exchange).__name__}")
            print(f"🔍 DEBUG: Exchange connected: {getattr(exchange, 'connected', 'unknown')}")
            
            # Parse pair - simple format like "BTC/USDT"
            if '/' not in pair:
                print(f"❌ Invalid pair format: {pair}. Use format like BTC/USDT")
                return
                
            base, quote = pair.split('/', 1)
            print(f"🔍 DEBUG: Parsed pair - base: '{base}', quote: '{quote}'")
            
            print(f"🛒 Executing market buy on {exchange_name}: {amount} {base} for {quote}")
            
            # Create trading pair object
            from connectors.base_exchange import TradingPair, OrderSide, OrderType
            trading_pair = TradingPair(base=base, quote=quote, symbol=pair)
            print(f"🔍 DEBUG: Created TradingPair object: {trading_pair}")
            
            # Check if exchange can handle this pair
            if hasattr(exchange, '_get_currencies_for_pair'):
                print(f"🔍 DEBUG: Checking currency mapping for pair...")
                currencies = exchange._get_currencies_for_pair(trading_pair)
                if currencies:
                    base_curr, quote_curr = currencies
                    print(f"🔍 DEBUG: Found currency mapping:")
                    print(f"   Base: protocol={base_curr.protocol}, network={base_curr.network_id}, asset={base_curr.asset_id[:10]}...")
                    print(f"   Quote: protocol={quote_curr.protocol}, network={quote_curr.network_id}, asset={quote_curr.asset_id[:10]}...")
                else:
                    print(f"❌ DEBUG: No currency mapping found for pair {pair}")
                    if hasattr(exchange, 'pair_mappings'):
                        print(f"🔍 DEBUG: Available pair mappings: {list(exchange.pair_mappings.keys())}")
                    return
            
            print(f"🔍 DEBUG: About to call exchange.place_order()")
            print(f"   pair: {trading_pair}")
            print(f"   side: {OrderSide.BUY}")
            print(f"   type: {OrderType.MARKET}")
            print(f"   amount: {amount_float}")
            
            # Place market buy order
            order = await exchange.place_order(
                pair=trading_pair,
                side=OrderSide.BUY,
                type=OrderType.MARKET,
                amount=amount_float
            )
            
            print(f"🔍 DEBUG: place_order returned: {order}")
            print(f"🔍 DEBUG: Order type: {type(order) if order else 'None'}")
            
            if order:
                print(f"✅ Market buy order placed!")
                print(f"   Order ID: {order.id}")
                print(f"   Amount: {order.amount}")
                print(f"   Status: {order.status.value}")
                
                # Start tracking this order
                if exchange_name in self.order_trackers:
                    self.order_trackers[exchange_name].track_order(order)
                    print(f"📡 Order tracking enabled - you'll receive real-time updates")
            else:
                print(f"❌ Market buy failed - place_order returned None")
                
        except ValueError as ve:
            print(f"❌ Invalid amount: {amount}")
            print(f"🔍 DEBUG: ValueError details: {ve}")
        except Exception as e:
            print(f"❌ Market buy error: {e}")
            print(f"🔍 DEBUG: Exception type: {type(e).__name__}")
            import traceback
            print(f"🔍 DEBUG: Full traceback:")
            traceback.print_exc()

    async def _cmd_market_sell(self, exchange_name: str, amount: str, pair: str):
        """Execute market sell order"""
        if exchange_name not in self.exchanges:
            print(f"❌ Exchange '{exchange_name}' not found")
            return
            
        try:
            amount_float = float(amount)
            exchange = self.exchanges[exchange_name]
            
            # Parse pair - simple format like "BTC/USDT"
            if '/' not in pair:
                print(f"❌ Invalid pair format: {pair}. Use format like BTC/USDT")
                return
                
            base, quote = pair.split('/', 1)
            
            print(f"💸 Executing market sell on {exchange_name}: {amount} {base} for {quote}")
            
            # Create trading pair object
            from connectors.base_exchange import TradingPair, OrderSide, OrderType
            trading_pair = TradingPair(base=base, quote=quote, symbol=pair)
            
            # Place market sell order
            order = await exchange.place_order(
                pair=trading_pair,
                side=OrderSide.SELL,
                type=OrderType.MARKET,
                amount=amount_float
            )
            
            if order:
                print(f"✅ Market sell order placed!")
                print(f"   Order ID: {order.id}")
                print(f"   Amount: {order.amount}")
                print(f"   Status: {order.status.value}")
                
                # Start tracking this order
                if exchange_name in self.order_trackers:
                    self.order_trackers[exchange_name].track_order(order)
                    print(f"📡 Order tracking enabled - you'll receive real-time updates")
            else:
                print(f"❌ Market sell failed")
                
        except ValueError:
            print(f"❌ Invalid amount: {amount}")
        except Exception as e:
            print(f"❌ Market sell error: {e}")
    
    async def _cmd_limit_buy(self, exchange_name: str, amount: str, pair: str, price: str):
        """Execute limit buy order"""
        if exchange_name not in self.exchanges:
            print(f"❌ Exchange '{exchange_name}' not found")
            return
            
        try:
            amount_float = float(amount)
            price_float = float(price)
            exchange = self.exchanges[exchange_name]
            
            # Parse pair - simple format like "BTC/USDT"
            if '/' not in pair:
                print(f"❌ Invalid pair format: {pair}. Use format like BTC/USDT")
                return
                
            base, quote = pair.split('/', 1)
            
            print(f"📊 Executing limit buy on {exchange_name}: {amount} {base} at {price} {quote}")
            
            # Create trading pair object
            from connectors.base_exchange import TradingPair, OrderSide, OrderType
            trading_pair = TradingPair(base=base, quote=quote, symbol=pair)
            
            # Place limit buy order
            order = await exchange.place_order(
                pair=trading_pair,
                side=OrderSide.BUY,
                type=OrderType.LIMIT,
                amount=amount_float,
                price=price_float
            )
            
            if order:
                print(f"✅ Limit buy order placed!")
                print(f"   Order ID: {order.id}")
                print(f"   Amount: {order.amount}")
                print(f"   Price: {order.price}")
                print(f"   Status: {order.status.value}")
                
                # Start tracking this order
                if exchange_name in self.order_trackers:
                    self.order_trackers[exchange_name].track_order(order)
                    print(f"📡 Order tracking enabled - you'll receive real-time updates")
            else:
                print(f"❌ Limit buy failed")
                
        except ValueError as ve:
            if "amount" in str(ve).lower():
                print(f"❌ Invalid amount: {amount}")
            elif "price" in str(ve).lower():
                print(f"❌ Invalid price: {price}")
            else:
                print(f"❌ Invalid input: {ve}")
        except Exception as e:
            print(f"❌ Limit buy error: {e}")
    
    async def _cmd_limit_sell(self, exchange_name: str, amount: str, pair: str, price: str):
        """Execute limit sell order"""
        if exchange_name not in self.exchanges:
            print(f"❌ Exchange '{exchange_name}' not found")
            return
            
        try:
            amount_float = float(amount)
            price_float = float(price)
            exchange = self.exchanges[exchange_name]
            
            # Parse pair - simple format like "BTC/USDT"
            if '/' not in pair:
                print(f"❌ Invalid pair format: {pair}. Use format like BTC/USDT")
                return
                
            base, quote = pair.split('/', 1)
            
            print(f"📊 Executing limit sell on {exchange_name}: {amount} {base} at {price} {quote}")
            
            # Create trading pair object
            from connectors.base_exchange import TradingPair, OrderSide, OrderType
            trading_pair = TradingPair(base=base, quote=quote, symbol=pair)
            
            # Place limit sell order
            order = await exchange.place_order(
                pair=trading_pair,
                side=OrderSide.SELL,
                type=OrderType.LIMIT,
                amount=amount_float,
                price=price_float
            )
            
            if order:
                print(f"✅ Limit sell order placed!")
                print(f"   Order ID: {order.id}")
                print(f"   Amount: {order.amount}")
                print(f"   Price: {order.price}")
                print(f"   Status: {order.status.value}")
                
                # Start tracking this order
                if exchange_name in self.order_trackers:
                    self.order_trackers[exchange_name].track_order(order)
                    print(f"📡 Order tracking enabled - you'll receive real-time updates")
            else:
                print(f"❌ Limit sell failed")
                
        except ValueError as ve:
            if "amount" in str(ve).lower():
                print(f"❌ Invalid amount: {amount}")
            elif "price" in str(ve).lower():
                print(f"❌ Invalid price: {price}")
            else:
                print(f"❌ Invalid input: {ve}")
        except Exception as e:
            print(f"❌ Limit sell error: {e}")
    
    async def _cmd_wait(self, seconds: float):
        """Wait for specified number of seconds"""
        print(f"⏳ Waiting {seconds} seconds...")
        await asyncio.sleep(seconds)
        print(f"✅ Wait complete")

    async def _cmd_marketinfo(self, exchange_name: str, pair_str: str):
        """Check market orientation on Hydra by searching initialized markets"""
        if exchange_name not in self.exchanges:
            print(f"❌ Exchange '{exchange_name}' not found")
            return

        exchange = self.exchanges[exchange_name]

        if exchange_name != 'hydra':
            print("❌ marketinfo only works with Hydra exchange")
            return

        # Parse pair
        if '/' not in pair_str:
            print(f"❌ Invalid pair format: {pair_str}. Use format like BTC/USDC")
            return

        from connectors.base_exchange import TradingPair
        base_symbol, quote_symbol = pair_str.split('/', 1)

        print(f"\n📊 Searching for {base_symbol}/{quote_symbol} on Hydra...")
        print(f"=" * 70)

        # Get config asset addresses if available for exact matching
        config_base_asset = None
        config_quote_asset = None
        trading_pair = TradingPair(base=base_symbol, quote=quote_symbol, symbol=pair_str)
        config_currencies = exchange._get_currencies_for_pair(trading_pair)
        if config_currencies:
            config_base_asset = config_currencies[0].asset_id
            config_quote_asset = config_currencies[1].asset_id

        # Search initialized markets for these two assets
        initialized_markets = exchange.client.get_initialized_markets()

        matching_markets = []
        for market in initialized_markets:
            # Check if market contains both assets (in any order) - USE CONFIG for exact matching
            has_base = exchange._asset_matches(base_symbol, config_base_asset, market.base.asset_id, market.base)
            has_quote_as_quote = exchange._asset_matches(quote_symbol, config_quote_asset, market.quote.asset_id, market.quote)

            # Also check reversed
            has_base_rev = exchange._asset_matches(base_symbol, config_base_asset, market.quote.asset_id, market.quote)
            has_quote_as_base = exchange._asset_matches(quote_symbol, config_quote_asset, market.base.asset_id, market.base)

            if (has_base and has_quote_as_quote):
                # Found in requested orientation
                matching_markets.append((market, False, base_symbol, quote_symbol))
            elif (has_base_rev and has_quote_as_base):
                # Found in reversed orientation
                matching_markets.append((market, True, quote_symbol, base_symbol))

        if not matching_markets:
            print(f"❌ No market found containing {base_symbol} and {quote_symbol}")
            print(f"\nAvailable assets on Hydra:")
            print(f"   BTC, ETH, USDC, USDT, HDN, DAI")
            return

        # Use first matching market
        market, is_reversed, actual_base, actual_quote = matching_markets[0]

        from lib.hydra_pb import currency_pb2
        currency1 = currency_pb2.OrderbookCurrency(
            protocol=market.base.protocol,
            network_id=market.base.network_id,
            asset_id=market.base.asset_id
        )
        currency2 = currency_pb2.OrderbookCurrency(
            protocol=market.quote.protocol,
            network_id=market.quote.network_id,
            asset_id=market.quote.asset_id
        )

        if is_reversed:
            print(f"\n⚠️  Market found but in REVERSED orientation!")
            print(f"   You requested: {base_symbol}/{quote_symbol}")
            print(f"   Hydra has:     {actual_base}/{actual_quote}")
        else:
            print(f"\n✅ Market found in requested orientation!")
            print(f"   Hydra has:     {actual_base}/{actual_quote}")

        print(f"\n🔍 Hydra Market Details:")
        print(f"   BASE:  {actual_base}")
        print(f"          Protocol: {currency1.protocol} (0=Bitcoin, 1=EVM)")
        print(f"          Network:  {currency1.network_id}")
        print(f"          Asset:    {currency1.asset_id}")
        print(f"   QUOTE: {actual_quote}")
        print(f"          Protocol: {currency2.protocol} (0=Bitcoin, 1=EVM)")
        print(f"          Network:  {currency2.network_id}")
        print(f"          Asset:    {currency2.asset_id}")

        # Verify with init_market
        print(f"\n🔍 Verifying with init_market...")
        try:
            market_info = exchange.client.init_market(currency1, currency2)
            if market_info:
                print(f"   ✅ Confirmed - Market is active on Hydra!")
            else:
                print(f"   ⚠️  Market initialized but might be empty")
        except Exception as e:
            print(f"   ❌ Error: {e}")

        print(f"\n💡 Add to config/exchanges/hydra.yaml:")
        print(f"")
        print(f"  - base_symbol: \"{actual_base}\"")
        print(f"    quote_symbol: \"{actual_quote}\"")
        print(f"    enabled: true")
        print(f"    base:")
        print(f"      protocol: {currency1.protocol}")
        print(f"      network_id: \"{currency1.network_id}\"")
        print(f"      asset_id: \"{currency1.asset_id}\"")
        print(f"    quote:")
        print(f"      protocol: {currency2.protocol}")
        print(f"      network_id: \"{currency2.network_id}\"")
        print(f"      asset_id: \"{currency2.asset_id}\"")
        print(f"")
        print(f"Then use in commands: {actual_base}/{actual_quote}")

    async def _cancel_all_orders(self):
        """Cancel all open orders across all exchanges"""
        self.logger.info("🗂️  Cancelling all open orders...")

        try:
            total_cancelled = 0
            total_failed = 0

            # Cancel orders on each exchange
            for exchange_name, exchange in self.exchanges.items():
                try:
                    # For Hydra, fetch all orders from the exchange directly
                    if hasattr(exchange, 'client') and exchange.client:
                        self.logger.info(f"📋 Fetching all orders from {exchange_name}...")
                        all_orders = exchange.client.get_all_own_orders()

                        if not all_orders:
                            self.logger.info(f"   No orders found on {exchange_name}")
                            continue

                        self.logger.info(f"   Found {len(all_orders)} orders on {exchange_name}")

                        # Cancel each order
                        for order_id in all_orders.keys():
                            try:
                                # Cancel order (pair is not used by Hydra's cancel_order)
                                success = await exchange.cancel_order(order_id, None)
                                if success:
                                    total_cancelled += 1
                                    self.logger.info(f"   ❌ Cancelled: {order_id[:8]}...")
                                else:
                                    total_failed += 1
                                    self.logger.warning(f"   ⚠️  Failed to cancel: {order_id[:8]}...")

                            except Exception as e:
                                total_failed += 1
                                self.logger.error(f"   ❌ Error cancelling order {order_id[:8]}...: {e}")

                    else:
                        # For other exchanges, use the exchange's get_orders method
                        # (Not implemented for now, would need to iterate through all pairs)
                        self.logger.info(f"⏩ Skipping {exchange_name} (no direct all-orders API)")

                except Exception as e:
                    self.logger.error(f"❌ Error processing {exchange_name}: {e}")

            if total_cancelled > 0:
                self.logger.info(f"✅ Cancelled {total_cancelled} orders")
            if total_failed > 0:
                self.logger.warning(f"⚠️  Failed to cancel {total_failed} orders")
            if total_cancelled == 0 and total_failed == 0:
                self.logger.info("✅ No active orders to cancel")

        except Exception as e:
            self.logger.error(f"❌ Error during order cancellation: {e}")

    async def shutdown(self):
        """Graceful shutdown"""
        self.logger.info("🛑 Shutting down trading bot...")
        self.running = False
        self._closing = True
        if self._reload_task is not None:
            self._reload_task.cancel()

        # Orders are cancelled below *before* strategies are stopped. Tell them to
        # stop reacting first, or a strategy that rebuilds cancelled orders
        # (enhanced_grid) re-places what we are about to cancel.
        if self.strategy_manager:
            for strategy in self.strategy_manager.strategies.values():
                prepare = getattr(strategy, 'prepare_for_shutdown', None)
                if prepare:
                    try:
                        prepare()
                    except Exception as e:
                        self.logger.error(f"❌ Error preparing {strategy.name} for shutdown: {e}")

        # Cancel all open orders first (if enabled)
        if self.config.cancel_orders_on_exit:
            await self._cancel_all_orders()
        else:
            self.logger.info("⏩ Skipping order cancellation (cancel_orders_on_exit=false)")
        
        # Stop all strategies
        if self.strategy_manager:
            try:
                await self.strategy_manager.stop_all()
                self.logger.info("✅ Stopped all strategies")
            except Exception as e:
                self.logger.error(f"❌ Error stopping strategies: {e}")
        
        # Disconnect from all exchanges
        if self.exchange_factory:
            try:
                await self.exchange_factory.disconnect_all()
                self.logger.info("✅ Disconnected from all exchanges")
            except Exception as e:
                self.logger.error(f"❌ Error disconnecting exchanges: {e}")
        
        # Stop order trackers
        for exchange_name, tracker in self.order_trackers.items():
            try:
                tracker.stop()
                self.logger.info(f"✅ Stopped order tracking for {exchange_name}")
            except Exception as e:
                self.logger.error(f"❌ Error stopping order tracker for {exchange_name}: {e}")
        
        self.logger.info("✅ Shutdown complete")
    
    async def _cmd_show_orders(self):
        """Show all active orders from order tracker"""
        active_orders = []

        for exchange_name, tracker in self.order_trackers.items():
            orders = tracker.get_active_orders()
            for order in orders:
                active_orders.append((exchange_name, order))

        if not active_orders:
            print("📋 No active orders in tracker")
            print("💡 Tip: Use 'orders-all' to fetch all orders from Hydra")
            return

        print(f"📋 Active Orders in Tracker ({len(active_orders)}):")
        print()

        for exchange_name, order in active_orders:
            print(f"🔹 {exchange_name.upper()} - {order.id[:8]}...")
            print(f"   Pair: {order.pair.symbol}")
            print(f"   Side: {order.side.value.upper()}")
            print(f"   Type: {order.type.value.upper()}")
            print(f"   Amount: {order.amount}")
            print(f"   Price: {order.price}")
            print(f"   Status: {order.status.value.upper()}")
            if order.filled > 0:
                print(f"   Filled: {order.filled}/{order.amount}")
            print()

    async def _cmd_show_all_orders(self):
        """Show ALL orders from Hydra GetAllOwnOrders API"""
        if 'hydra' not in self.exchanges:
            print("❌ Hydra exchange not configured")
            return

        hydra_exchange = self.exchanges['hydra']
        if not hasattr(hydra_exchange, 'client') or not hydra_exchange.client:
            print("❌ Hydra client not available")
            return

        try:
            # Call GetAllOwnOrders
            from lib.hydra_pb import orderbook_pb2
            request = orderbook_pb2.GetAllOwnOrdersRequest()
            response = hydra_exchange.client.orderbook_stub.GetAllOwnOrders(request)

            if not response.orders:
                print("📋 No orders found on Hydra")
                return

            print(f"📋 All Hydra Orders ({len(response.orders)}):")
            print()

            for order_id, order in response.orders.items():
                # Determine order type based on oneof (returns 'pair_order' or 'swap_order')
                order_type = order.WhichOneof('order')

                # Handle pair_order (contains limit_order, market_order, or liquidity_order)
                if order_type == 'pair_order':
                    pair_order = order.pair_order
                    pair_order_type = pair_order.WhichOneof('order')

                    if pair_order_type == 'limit_order':
                        try:
                            limit_order = pair_order.limit_order

                            # Extract side from variant (oneof with 'buy' or 'sell')
                            side_type = limit_order.variant.WhichOneof('side')
                            side = "BUY" if side_type == 'buy' else "SELL"

                            # Extract price
                            price = self._extract_decimal(limit_order.price)

                            # Extract remaining amount (oneof with 'base' or 'quote')
                            amount_type_field = limit_order.remaining_amount.WhichOneof('amount')
                            if amount_type_field == 'base':
                                amount = self._extract_decimal(limit_order.remaining_amount.base.amount)
                                amount_type = "BASE"
                            elif amount_type_field == 'quote':
                                amount = self._extract_decimal(limit_order.remaining_amount.quote.amount)
                                amount_type = "QUOTE"
                            else:
                                amount = 0
                                amount_type = "UNKNOWN"

                            # Get filled amounts from variant
                            bought = 0
                            sold = 0
                            fee = 0
                            try:
                                if side_type == 'buy' and hasattr(limit_order.variant, 'buy'):
                                    bought = self._extract_decimal(limit_order.variant.buy.bought_base_amount)
                                    sold = self._extract_decimal(limit_order.variant.buy.sold_quote_amount)
                                    fee = self._extract_decimal(limit_order.variant.buy.paid_base_fee)
                                elif side_type == 'sell' and hasattr(limit_order.variant, 'sell'):
                                    bought = self._extract_decimal(limit_order.variant.sell.bought_quote_amount)
                                    sold = self._extract_decimal(limit_order.variant.sell.sold_base_amount)
                                    fee = self._extract_decimal(limit_order.variant.sell.paid_quote_fee)
                            except Exception as e:
                                pass  # Filled amounts optional

                            print(f"🔹 LIMIT ORDER - {order_id[:8]}...")
                            print(f"   Side: {side}")
                            print(f"   Price: {price}")
                            print(f"   Remaining: {amount} ({amount_type})")
                            if bought > 0 or sold > 0:
                                print(f"   Filled - Bought: {bought}, Sold: {sold}, Fee: {fee}")

                        except Exception as e:
                            print(f"🔹 LIMIT ORDER - {order_id[:8]}...")
                            print(f"   ⚠️  Error parsing order details: {e}")

                    elif pair_order_type == 'market_order':
                        market_order = pair_order.market_order
                        print(f"🔹 MARKET ORDER - {order_id[:8]}...")
                        print(f"   (details not fully parsed)")

                    elif pair_order_type == 'liquidity_order':
                        print(f"🔹 LIQUIDITY ORDER (deprecated) - {order_id[:8]}...")

                elif order_type == 'swap_order':
                    print(f"🔹 SWAP ORDER - {order_id[:8]}...")
                    print(f"   (swap orders not yet supported)")

                print()

        except Exception as e:
            print(f"❌ Error fetching orders: {e}")
            import traceback
            traceback.print_exc()

    def _extract_decimal(self, decimal_value) -> float:
        """Helper to extract float from DecimalString"""
        try:
            from lib.utils import decimal_to_float
            return decimal_to_float(decimal_value)
        except:
            return float(decimal_value.value) if hasattr(decimal_value, 'value') else 0.0
    
    async def _cmd_order_status(self, order_id: str):
        """Show status of specific order"""
        found = False
        
        for exchange_name, tracker in self.order_trackers.items():
            order = tracker.get_order_status(order_id)
            if order:
                found = True
                print(f"📊 Order Status ({exchange_name.upper()}):")
                print(f"   ID: {order.id}")
                print(f"   Pair: {order.pair.symbol}")
                print(f"   Side: {order.side.value.upper()}")
                print(f"   Type: {order.type.value.upper()}")
                print(f"   Amount: {order.amount}")
                print(f"   Price: {order.price}")
                print(f"   Status: {order.status.value.upper()}")
                print(f"   Filled: {order.filled}")
                print(f"   Remaining: {order.remaining}")
                print(f"   Created: {time.strftime('%H:%M:%S', time.localtime(order.timestamp))}")
                break
        
        if not found:
            print(f"❌ Order {order_id} not found in active orders")
    
    async def _cmd_order_events(self):
        """Show recent order events"""
        all_events = []
        
        for exchange_name, tracker in self.order_trackers.items():
            events = tracker.get_recent_events(20)
            for event in events:
                all_events.append((exchange_name, event))
        
        if not all_events:
            print("📋 No recent order events")
            return
        
        # Sort by timestamp
        all_events.sort(key=lambda x: x[1].timestamp, reverse=True)
        
        print(f"📋 Recent Order Events ({len(all_events[:10])}):")
        print()
        
        for exchange_name, event in all_events[:10]:
            formatted_time = time.strftime('%H:%M:%S', time.localtime(event.timestamp))
            
            print(f"🔹 [{formatted_time}] {exchange_name.upper()}")
            print(f"   {event.message}")
            print()

    async def _cmd_validate_orderbook(self, exchange_name: str, pair: str, level: int = 0):
        """Validate orderbook calculation using EstimateOrder"""
        if exchange_name not in self.exchanges:
            print(f"❌ Exchange '{exchange_name}' not found")
            return
            
        exchange = self.exchanges[exchange_name]
        
        # Only works for Hydra exchange
        if exchange_name.lower() != 'hydra':
            print(f"❌ EstimateOrder validation only available for Hydra exchange")
            return
        
        # Parse pair
        if '/' not in pair:
            print(f"❌ Invalid pair format: {pair}. Use format like BTC/USDC")
            return
            
        base, quote = pair.split('/', 1)
        
        print(f"🔍 Validating orderbook calculations for {pair} on Hydra (level {level})")
        print(f"   Using EstimateOrder API to verify our price calculations...")
        print()
        
        try:
            from connectors.base_exchange import TradingPair
            trading_pair = TradingPair(base=base, quote=quote, symbol=pair)
            
            # Check if this is a Hydra exchange with validation methods
            if not hasattr(exchange, 'validate_orderbook_level'):
                print(f"❌ Exchange {exchange_name} does not support validation")
                return
            
            # Run the validation
            results = await exchange.validate_orderbook_level(trading_pair, level)
            
            # Display results using the exchange's pretty printer
            exchange.print_validation_results(results)
            
        except Exception as e:
            print(f"❌ Error validating orderbook: {e}")
            import traceback
            print(traceback.format_exc())

    async def _show_post_fill_overview(self, filled_order: Order):
        """
        Show comprehensive overview after an order fills.
        Critical for grid strategies - helps track grid state and next actions.
        """
        try:
            print(f"\n{'='*80}")
            print(f"📊 POST-FILL OVERVIEW - Order {filled_order.id[:12]}... Filled")
            print(f"{'='*80}")

            # Show filled order details
            print(f"\n✅ FILLED ORDER:")
            print(f"   Pair: {filled_order.pair.symbol}")
            print(f"   Side: {filled_order.side.value.upper()}")
            print(f"   Type: {filled_order.type.value.upper()}")
            print(f"   Amount: {filled_order.amount} {filled_order.pair.base}")
            print(f"   Price: {filled_order.price:.8f} {filled_order.pair.quote}")
            print(f"   Total: {filled_order.amount * filled_order.price:.2f} {filled_order.pair.quote}")

            # Get exchange (assume Hydra for now)
            exchange_name = 'hydra'
            if exchange_name not in self.exchanges:
                return

            exchange = self.exchanges[exchange_name]

            # Fetch current balances
            print(f"\n💰 CURRENT BALANCES:")
            try:
                balances = await exchange.get_balances()
                if balances:
                    # Show relevant balances
                    base_balance = balances.get(filled_order.pair.base, 0.0)
                    quote_balance = balances.get(filled_order.pair.quote, 0.0)
                    print(f"   {filled_order.pair.base}: {base_balance:.8f}")
                    print(f"   {filled_order.pair.quote}: {quote_balance:.2f}")
                else:
                    print(f"   ⚠️  Unable to fetch balances")
            except Exception as e:
                print(f"   ⚠️  Error fetching balances: {e}")

            # Fetch ALL orders from Hydra to see current grid state
            print(f"\n📋 REMAINING ORDERS ON {filled_order.pair.symbol}:")
            try:
                if hasattr(exchange, 'client') and exchange.client:
                    from lib.hydra_pb import orderbook_pb2
                    request = orderbook_pb2.GetAllOwnOrdersRequest()
                    response = exchange.client.orderbook_stub.GetAllOwnOrders(request)

                    # Filter orders for this pair
                    pair_orders = []
                    for order_id, order in response.orders.items():
                        order_type = order.WhichOneof('order')
                        if order_type == 'limit_order':
                            limit_order = order.limit_order
                            # Check if same pair (simple check - could be improved)
                            side_type = limit_order.variant.WhichOneof('side')
                            price = self._extract_decimal(limit_order.price)

                            # Get remaining amount
                            amount_type_field = limit_order.remaining_amount.WhichOneof('amount')
                            if amount_type_field == 'base':
                                amount = self._extract_decimal(limit_order.remaining_amount.base.amount)
                            elif amount_type_field == 'quote':
                                amount = self._extract_decimal(limit_order.remaining_amount.quote.amount)
                            else:
                                amount = 0

                            if amount > 0:  # Only show open orders
                                pair_orders.append({
                                    'id': order_id,
                                    'side': 'BUY' if side_type == 'buy' else 'SELL',
                                    'price': price,
                                    'amount': amount,
                                    'amount_type': amount_type_field
                                })

                    if pair_orders:
                        # Sort by price
                        pair_orders.sort(key=lambda x: x['price'], reverse=True)

                        # Separate buys and sells
                        buy_orders = [o for o in pair_orders if o['side'] == 'BUY']
                        sell_orders = [o for o in pair_orders if o['side'] == 'SELL']

                        if buy_orders:
                            print(f"\n   🟢 BUY ORDERS ({len(buy_orders)}):")
                            for order in buy_orders[:5]:  # Show top 5
                                print(f"      ${order['price']:.2f} - {order['amount']:.8f} ({order['amount_type']})")

                        if sell_orders:
                            print(f"\n   🔴 SELL ORDERS ({len(sell_orders)}):")
                            for order in sell_orders[:5]:  # Show top 5
                                print(f"      ${order['price']:.2f} - {order['amount']:.8f} ({order['amount_type']})")

                        print(f"\n   Total: {len(buy_orders)} buys, {len(sell_orders)} sells")
                    else:
                        print(f"   ℹ️  No other open orders on this pair")

            except Exception as e:
                print(f"   ⚠️  Error fetching orders: {e}")

            # Grid strategy suggestion
            print(f"\n💡 GRID STRATEGY SUGGESTION:")
            if filled_order.side == OrderSide.BUY:
                suggested_sell_price = filled_order.price * 1.01  # 1% above
                print(f"   Buy order filled at ${filled_order.price:.2f}")
                print(f"   → Consider placing SELL order at ${suggested_sell_price:.2f} (+1%)")
                print(f"   → Command: limit-sell hydra {filled_order.amount} {filled_order.pair.symbol} {suggested_sell_price:.2f}")
            else:
                suggested_buy_price = filled_order.price * 0.99  # 1% below
                print(f"   Sell order filled at ${filled_order.price:.2f}")
                print(f"   → Consider placing BUY order at ${suggested_buy_price:.2f} (-1%)")
                print(f"   → Command: limit-buy hydra {filled_order.amount} {filled_order.pair.symbol} {suggested_buy_price:.2f}")

            print(f"\n{'='*80}\n")

        except Exception as e:
            self.logger.error(f"Error showing post-fill overview: {e}")
            import traceback
            traceback.print_exc()


def load_config(config_path: Path) -> BotConfig:
    """Load configuration from YAML file, with .env fallbacks"""
    try:
        with open(config_path, 'r') as f:
            config_data = yaml.safe_load(f)

        # .env fallbacks — only used when the yaml doesn't set a value
        env_defaults = {
            'log_level': os.getenv('LOG_LEVEL'),
            'log_file': os.getenv('LOG_FILE'),
            'max_daily_trades': int(os.getenv('MAX_DAILY_TRADES', 0)) or None,
            'max_position_size': float(os.getenv('MAX_POSITION_SIZE', 0)) or None,
        }
        for key, env_val in env_defaults.items():
            if env_val and key not in config_data:
                config_data[key] = env_val

        return BotConfig(**config_data)
        
    except FileNotFoundError:
        print(f"❌ Config file not found: {config_path}")
        print("Creating default config...")
        
        # Create default config
        default_config = BotConfig(
            strategies=[
                {
                    'name': 'example_grid',
                    'type': 'GridStrategy', 
                    'enabled': False,
                    'exchange': 'hydra',
                    'pair': 'BTC/USDC',
                    'params': {
                        'grid_levels': 10,
                        'grid_spacing': 0.5,
                        'profit_percentage': 1.0,
                        'order_amount': 0.01
                    }
                }
            ]
        )
        
        # Save default config
        with open(config_path, 'w') as f:
            yaml.dump(default_config.__dict__, f, default_flow_style=False)
        
        print(f"✅ Created default config at {config_path}")
        print("Please edit the config file and add your trading strategies.")
        sys.exit(1)
        
    except Exception as e:
        print(f"❌ Error loading config: {e}")
        sys.exit(1)


def setup_signal_handlers(bot: TradingBotCLI):
    """Setup signal handlers for graceful shutdown"""
    def signal_handler(signum, frame):
        print(f"\n🛑 Received signal {signum}, shutting down...")
        # Try to run shutdown in the current event loop
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(bot.shutdown())
            # Give it a moment to clean up before exit
            loop.run_until_complete(asyncio.sleep(0.1))
        except RuntimeError:
            # No running loop, just exit
            print("⚠️  No event loop, exiting immediately")
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)


async def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description="Multi-Exchange Trading Bot CLI")
    parser.add_argument("--config", "-c", default="config/bot_config.yaml", help="Configuration file path")
    parser.add_argument("--daemon", "-d", action="store_true", help="Run in daemon mode")
    parser.add_argument("--strategy", "-s", help="Strategy to run (for daemon mode)")
    
    args = parser.parse_args()
    
    # Load configuration
    config_path = Path(args.config)
    config = load_config(config_path)
    
    # Create bot instance
    bot = TradingBotCLI(config)
    bot.config_path = config_path
    setup_signal_handlers(bot)
    
    # Initialize bot
    if not await bot.initialize():
        print("❌ Bot initialization failed")
        sys.exit(1)
    
    # Run in appropriate mode
    if args.daemon:
        await bot.start_daemon_mode()
    else:
        await bot.interactive_mode()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Interrupted")
        sys.exit(0)