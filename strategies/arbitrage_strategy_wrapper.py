#!/usr/bin/env python3
"""
Arbitrage Strategy Wrapper for Trading Bot CLI
===============================================

Wraps the arbitrage components (ArbitrageDetector, ArbitrageExecutor) 
to work with the BaseStrategy interface.
"""

import time
import asyncio
from typing import Dict, List, Optional, Any
from dataclasses import dataclass

from .base.base_strategy import BaseStrategy, StrategyConfig, StrategyStatus
from .arbitrage import ArbitrageDetector, ArbitrageExecutor, ArbitrageOpportunity
from orderbook_manager import OrderbookManager


@dataclass
class ArbitrageConfig(StrategyConfig):
    """Configuration for arbitrage strategy wrapper"""
    min_profit_percentage: float = 0.05
    min_profit_amount: float = 1.0
    max_position_size: float = 100.0
    paper_trading: bool = True
    scan_interval: float = 0.1
    exchange: str = "hydra"
    pair: str = "BTC/USDT"


class ArbitrageStrategyWrapper(BaseStrategy):
    """
    Wrapper to integrate arbitrage components with the trading bot CLI
    """
    
    def __init__(self, name: str, config: Dict[str, Any], 
                 orderbook_manager: OrderbookManager, 
                 exchange_factory, hydra_client=None):
        # Create ArbitrageConfig from dict
        params = config.get('params', {})
        
        arb_config = ArbitrageConfig(
            name=name,
            enabled=config.get('enabled', True),
            risk_limit=config.get('risk_limit', 1.0),
            max_daily_trades=config.get('max_daily_trades', 1000),
            min_profit_percentage=params.get('min_profit_percentage', 0.5),
            min_profit_amount=params.get('min_profit_amount', 1.0),
            max_position_size=params.get('max_position_size', 100.0),
            paper_trading=params.get('paper_trading', True),
            scan_interval=params.get('scan_interval', 0.1),
            exchange=config.get('exchange', 'hydra'),
            pair=config.get('pair', 'BTC/USDT'),
            params=params
        )
        
        super().__init__(name, arb_config, {})
        
        self.arb_config = arb_config
        self.orderbook_manager = orderbook_manager
        self.exchange_factory = exchange_factory
        self.hydra_client = hydra_client  # Store Hydra client for EstimateOrder validation
        
        # Arbitrage components
        self.arbitrage_detector: Optional[ArbitrageDetector] = None
        self.arbitrage_executor: Optional[ArbitrageExecutor] = None
        
        # Statistics
        self.opportunities_detected = 0
        self.opportunities_executed = 0
        self.successful_trades = 0
        self.total_profit = 0.0

        # Cooldown tracking to prevent repeated failed attempts
        self.failed_opportunities = {}  # Key: (pair, buy_exchange, sell_exchange) -> timestamp of last failure
        self.failure_cooldown = 60  # Seconds to wait before retrying after balance failure

        # Execution lock to prevent concurrent trades
        self.execution_in_progress = False
        self.last_execution_time = 0
        self.min_time_between_trades = 5.0  # Minimum 5 seconds between trades

        # Opportunity queue for selecting best opportunity
        self.opportunity_queue = []  # List of recent opportunities
        self.opportunity_window = 0.1  # Seconds to collect opportunities (100ms is enough - callbacks are serial)
        self.opportunity_selection_task = None

        # Exchange reference
        self.exchange = None
    
    async def initialize(self) -> bool:
        """Initialize the arbitrage strategy"""
        try:
            self.logger.info(f"Initializing arbitrage strategy: {self.name}")
            
            # Initialize arbitrage detector
            self.arbitrage_detector = ArbitrageDetector(
                orderbook_manager=self.orderbook_manager,
                min_profit_threshold=self.arb_config.min_profit_percentage,
                logger=self.logger
            )
            
            # Initialize arbitrage executor with EstimateOrder validation
            self.arbitrage_executor = ArbitrageExecutor(
                exchange_factory=self.exchange_factory,
                hydra_client=self.hydra_client,  # Enable EstimateOrder validation
                config={'params': {
                    'max_position_size': self.arb_config.max_position_size,
                    'min_profit_percentage': self.arb_config.min_profit_percentage,
                    'min_profit_amount': self.arb_config.min_profit_amount
                }},  # Pass all relevant config parameters
                logger=self.logger
            )
            
            if self.hydra_client:
                self.logger.info("✅ Enhanced arbitrage with EstimateOrder validation enabled")
            else:
                self.logger.warning("⚠️  Basic arbitrage mode - no EstimateOrder validation")
            
            # Pass available exchanges to the executor if we have them
            if hasattr(self, 'all_exchanges') and self.all_exchanges:
                self.arbitrage_executor.exchanges = self.all_exchanges
            
            # Register directly with orderbook manager for opportunities
            self.orderbook_manager.add_arbitrage_callback(self._on_arbitrage_opportunity)

            # Register this pair as having an active strategy to enable dynamic sizing
            self.orderbook_manager.register_active_strategy_pair(self.arb_config.pair)

            self.logger.info(f"✅ Arbitrage strategy initialized and registered for callbacks")
            return True
            
        except Exception as e:
            self.log_error(f"Failed to initialize arbitrage strategy: {e}")
            return False
    
    def _on_arbitrage_opportunity(self, opportunity: ArbitrageOpportunity):
        """Handle detected arbitrage opportunity - add to queue for selection"""
        # Check if execution is already in progress
        if self.execution_in_progress:
            self.logger.debug(f"⏭️  Skipping {opportunity.pair} - execution already in progress")
            return

        # Check minimum time between trades (let prices settle)
        current_time = time.time()
        time_since_last = current_time - self.last_execution_time
        if time_since_last < self.min_time_between_trades:
            remaining = self.min_time_between_trades - time_since_last
            self.logger.debug(f"⏱️  Skipping {opportunity.pair} - cooling down ({remaining:.1f}s remaining)")
            return

        # Filter opportunities by pair - only handle opportunities for this strategy's assigned pair
        strategy_pair = self.arb_config.pair

        # Normalize pair formats for comparison (handle BTC/USDC vs BTCUSDC, stablecoins, etc.)
        def normalize_pair(pair_str):
            # First normalize stablecoins to USD
            stablecoins = ['USDC', 'USDC2', 'USDT', 'BUSD', 'DAI', 'TUSD', 'USDP']
            normalized = pair_str
            for stable in stablecoins:
                if f'/{stable}' in normalized:
                    normalized = normalized.replace(f'/{stable}', '/USD')
                elif normalized.endswith(stable):
                    normalized = normalized.replace(stable, 'USD')
            # Then remove slashes and uppercase
            return normalized.replace('/', '').upper()

        if normalize_pair(opportunity.pair) != normalize_pair(strategy_pair):
            # This opportunity is for a different pair, ignore it
            self.logger.debug(f"🚫 Ignoring {opportunity.pair} opportunity (strategy handles {strategy_pair})")
            return

        # Check if this opportunity is in cooldown after a recent failure
        cooldown_key = (opportunity.pair, opportunity.buy_exchange, opportunity.sell_exchange)

        if cooldown_key in self.failed_opportunities:
            time_since_failure = current_time - self.failed_opportunities[cooldown_key]
            if time_since_failure < self.failure_cooldown:
                remaining = int(self.failure_cooldown - time_since_failure)
                self.logger.debug(f"⏸️  Skipping {opportunity.pair} - cooldown active ({remaining}s remaining)")
                return
            else:
                # Cooldown expired, remove from tracking
                del self.failed_opportunities[cooldown_key]

        self.opportunities_detected += 1

        # Add opportunity to queue with cooldown key
        self.opportunity_queue.append((opportunity, cooldown_key, current_time))
        self.logger.debug(f"📥 Queued opportunity: {opportunity.pair} {opportunity.buy_exchange}->{opportunity.sell_exchange} ({opportunity.profit_percentage:.2f}%)")

        # Start selection task if not already running
        if self.opportunity_selection_task is None or self.opportunity_selection_task.done():
            self.opportunity_selection_task = asyncio.create_task(self._select_and_execute_best_opportunity())
    
    async def _select_and_execute_best_opportunity(self):
        """Wait for opportunity window, then select and execute the best opportunity"""
        # Wait for the opportunity collection window to gather all opportunities
        await asyncio.sleep(self.opportunity_window)

        if not self.opportunity_queue:
            self.logger.debug("No opportunities in queue")
            return

        # Select best opportunity (highest profit percentage)
        best_opportunity, best_cooldown_key, best_timestamp = max(
            self.opportunity_queue,
            key=lambda x: x[0].profit_percentage
        )

        queue_size = len(self.opportunity_queue)
        self.logger.info(f"🎯 Selected BEST of {queue_size} opportunities:")
        self.logger.info(f"   {best_opportunity.pair} {best_opportunity.buy_exchange}->{best_opportunity.sell_exchange}")
        self.logger.info(f"   📈 BUY on {best_opportunity.buy_exchange} at ${best_opportunity.buy_price:.2f}")
        self.logger.info(f"   📉 SELL on {best_opportunity.sell_exchange} at ${best_opportunity.sell_price:.2f}")
        self.logger.info(f"   💵 Expected profit: {best_opportunity.profit_percentage:.2f}%")

        # Clear the queue now that we've selected
        self.opportunity_queue.clear()

        # Execute the best opportunity - SET LOCK to prevent new opportunities during execution
        if self.running:
            self.execution_in_progress = True
            self.last_execution_time = time.time()
            await self._execute_opportunity(best_opportunity, best_cooldown_key)
        else:
            self.logger.warning(f"⚠️  Strategy is not running, skipping execution")

    async def _execute_opportunity(self, opportunity: ArbitrageOpportunity, cooldown_key: tuple):
        """Execute an arbitrage opportunity"""
        # Lock is already set in _select_and_execute_best_opportunity
        try:
            if self.arb_config.paper_trading:
                # Paper trading - just simulate
                self._execute_paper_trade(opportunity)
            else:
                # Real execution
                result = await self.arbitrage_executor.execute_arbitrage(opportunity)

                # Track execution result and handle failures
                if result.get('success'):
                    self._track_execution_result(result)
                    # Remove from cooldown on success
                    if cooldown_key in self.failed_opportunities:
                        del self.failed_opportunities[cooldown_key]
                else:
                    # Check if this was a balance failure
                    error_msg = result.get('error', '')
                    if 'balance' in error_msg.lower() or 'too small' in error_msg.lower():
                        # Add to cooldown to prevent repeated attempts
                        self.failed_opportunities[cooldown_key] = time.time()
                        self.logger.warning(f"⏸️  Insufficient balance - cooldown active for {self.failure_cooldown}s")

                    self._track_execution_result(result)

        except Exception as e:
            self.logger.error(f"❌ Error executing arbitrage opportunity: {e}")
            import traceback
            self.logger.error(f"❌ Traceback: {traceback.format_exc()}")
        finally:
            # Always clear execution lock when done
            self.execution_in_progress = False

    def _execute_paper_trade(self, opportunity: ArbitrageOpportunity):
        """Execute paper trade (simulation)"""
        position_size = min(opportunity.max_volume, self.arb_config.max_position_size)
        profit = (opportunity.sell_price - opportunity.buy_price) * position_size
        
        self.opportunities_executed += 1
        self.successful_trades += 1
        self.total_profit += profit
        
        self.logger.info(f"📝 Paper trade: ${profit:.2f} profit (Total: ${self.total_profit:.2f})")
    
    def _track_execution_result(self, result: Dict):
        """Track real execution result"""
        self.opportunities_executed += 1
        
        if result['success']:
            self.successful_trades += 1
            actual_profit = result.get('actual_profit', 0)
            self.total_profit += actual_profit
            self.logger.info(f"💵 Real profit: ${actual_profit:.2f} (Total: ${self.total_profit:.2f})")
        else:
            self.logger.warning(f"⚠️  Execution failed: {result.get('error', 'Unknown error')}")
    
    async def start(self) -> bool:
        """Start the arbitrage strategy"""
        self.logger.info("🔧 ArbitrageStrategyWrapper.start() called")
        
        # Set running state
        self.running = True
        
        self.logger.info(f"🚀 Arbitrage strategy started")
        self.logger.info(f"📊 Config: {self.arb_config.pair} | Min profit: {self.arb_config.min_profit_percentage}% | Max size: ${self.arb_config.max_position_size} | Paper: {self.arb_config.paper_trading}")
        
        # Connect exchanges to orderbook manager to start price monitoring
        if hasattr(self, 'all_exchanges') and self.all_exchanges:
            for ex_name, ex_connector in self.all_exchanges.items():
                self.logger.info(f"📡 Connecting {ex_name} to OrderbookManager for price monitoring")
                self.orderbook_manager.connect_exchange(ex_name, ex_connector)
        else:
            self.logger.warning("⚠️  No exchanges available for price monitoring")
        
        # Start periodic status logging
        asyncio.create_task(self._periodic_status_log())
        
        # The strategy is event-driven, so no continuous loop needed
        # Opportunities will be detected via OrderbookUpdate events
        
        return True
    
    async def _periodic_status_log(self):
        """Log periodic status updates"""
        while self.running:
            try:
                # Log every 30 seconds that we're monitoring
                await asyncio.sleep(30)
                if self.running:
                    detector_stats = self.arbitrage_detector.get_statistics() if self.arbitrage_detector else {}
                    self.logger.info(f"📡 Arbitrage monitoring: {self.arb_config.pair} | Detected: {self.opportunities_detected} | Executed: {self.opportunities_executed} | Profit: ${self.total_profit:.2f}")
            except Exception as e:
                self.logger.error(f"Error in periodic status log: {e}")
                break
    
    async def stop(self) -> bool:
        """Stop the arbitrage strategy"""
        self.logger.info("🛑 Stopping arbitrage strategy")

        # Unregister this pair from active strategies
        if hasattr(self, 'orderbook_manager') and self.orderbook_manager:
            self.orderbook_manager.unregister_active_strategy_pair(self.arb_config.pair)

        return await super().stop()
    
    def get_status(self) -> StrategyStatus:
        """Get current strategy status"""
        success_rate = (self.successful_trades / max(self.opportunities_executed, 1)) * 100
        
        return StrategyStatus(
            name=self.name,
            running=self.running,
            active_positions=0,  # Arbitrage doesn't hold positions
            total_trades=self.opportunities_executed,
            profit_loss=self.total_profit,
            last_update=time.time(),
            error_count=self.error_count,
            last_error=self.last_error
        )
    
    # BaseStrategy interface methods (mostly not used for arbitrage)
    async def on_market_data(self, exchange_name: str, data: dict):
        """Handle market data updates (not used - we use OrderbookUpdates)"""
        pass
    
    async def on_order_update(self, exchange_name: str, order: Any):
        """Handle order updates (handled by arbitrage executor)"""
        pass
    
    def on_order_filled(self, order_id: str):
        """Handle order fill notification (handled by arbitrage executor)"""
        pass
    
    def on_order_cancelled(self, order_id: str):
        """Handle order cancellation notification"""
        pass