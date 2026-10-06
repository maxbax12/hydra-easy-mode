"""
Exchange Factory
================

Factory class for creating and managing exchange connectors.
Handles dynamic loading of exchange types and configuration management.
"""

import importlib
import os
import re
import yaml
from pathlib import Path
from typing import Dict, List, Optional, Any
import logging
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root (next to this file's parent directory)
load_dotenv(Path(__file__).resolve().parent.parent / '.env')


class ExchangeFactory:
    """Factory for creating and managing exchange connectors"""
    
    def __init__(self, config_dir: Path):
        """
        Initialize exchange factory
        
        Args:
            config_dir: Directory containing exchange configuration files
        """
        self.config_dir = config_dir
        self.exchanges: Dict[str, Any] = {}
        self.logger = logging.getLogger("exchange_factory")
        
        # Registry of available exchange types
        self.exchange_types = {
            'HydraExchange': 'connectors.hydra_exchange.HydraExchange',
            'BinanceExchange': 'connectors.binance_exchange.BinanceExchange',
            'MockExchange': 'connectors.mock_exchange.MockExchange',
            # Add more exchanges here as they're implemented
        }
    
    def register_exchange_type(self, name: str, module_path: str):
        """Register a new exchange type"""
        self.exchange_types[name] = module_path
        self.logger.info(f"Registered exchange type: {name} -> {module_path}")
    
    async def load_exchanges(self) -> Dict[str, Any]:
        """
        Load all exchange configurations and create connectors
        
        Returns:
            Dictionary of exchange name -> connector instance
        """
        config_files = self.config_dir.glob("*.yaml")
        
        for config_file in config_files:
            try:
                with open(config_file, 'r') as f:
                    config = yaml.safe_load(f)
                
                if config.get('enabled', False):
                    exchange = await self._create_exchange(config)
                    if exchange:
                        self.exchanges[config['name']] = exchange
                        self.logger.info(f"Loaded exchange: {config['name']}")
                    else:
                        self.logger.error(f"Failed to create exchange: {config['name']}")
                else:
                    self.logger.info(f"Exchange disabled: {config.get('name', config_file.stem)}")
                    
            except Exception as e:
                self.logger.error(f"Error loading exchange config {config_file}: {e}")
        
        return self.exchanges
    
    def _substitute_env_vars(self, config: Dict) -> Dict:
        """
        Substitute environment variables in configuration
        
        Replaces ${VAR_NAME} patterns with actual environment variable values
        """
        def replace_env_var(value):
            if isinstance(value, str):
                # Pattern to match ${VAR_NAME}
                pattern = r'\$\{([^}]+)\}'
                
                def replacer(match):
                    var_name = match.group(1)
                    env_value = os.getenv(var_name)
                    if env_value is None:
                        self.logger.warning(f"Environment variable {var_name} not found")
                        return ''  # Return empty string if not found
                    return env_value
                
                return re.sub(pattern, replacer, value)
            elif isinstance(value, dict):
                return {k: replace_env_var(v) for k, v in value.items()}
            elif isinstance(value, list):
                return [replace_env_var(item) for item in value]
            else:
                return value
        
        return replace_env_var(config)
    
    async def _create_exchange(self, config: Dict) -> Optional[Any]:
        """Create an exchange connector from configuration"""
        exchange_type = config.get('type')
        if not exchange_type:
            self.logger.error(f"No exchange type specified in config: {config.get('name')}")
            return None
        
        if exchange_type not in self.exchange_types:
            self.logger.error(f"Unknown exchange type: {exchange_type}")
            return None
        
        try:
            # Process environment variable substitutions in config
            config = self._substitute_env_vars(config)
            
            # Dynamically import and create exchange
            module_path = self.exchange_types[exchange_type]
            module_name, class_name = module_path.rsplit('.', 1)
            
            module = importlib.import_module(module_name)
            exchange_class = getattr(module, class_name)
            
            # Create exchange instance
            exchange = exchange_class(config)
            
            # Connect to exchange
            if await exchange.connect():
                self.logger.info(f"Connected to {config['name']} exchange")
                return exchange
            else:
                self.logger.error(f"Failed to connect to {config['name']} exchange")
                return None
                
        except ImportError as e:
            self.logger.error(f"Failed to import exchange {exchange_type}: {e}")
            return None
        except Exception as e:
            self.logger.error(f"Error creating exchange {exchange_type}: {e}")
            return None
    
    async def connect_all(self) -> bool:
        """Connect to all loaded exchanges"""
        success = True
        
        for name, exchange in self.exchanges.items():
            try:
                if not exchange.connected:
                    connected = await exchange.connect()
                    if not connected:
                        self.logger.error(f"Failed to connect to {name}")
                        success = False
            except Exception as e:
                self.logger.error(f"Error connecting to {name}: {e}")
                success = False
        
        return success
    
    async def disconnect_all(self):
        """Disconnect from all exchanges"""
        for name, exchange in self.exchanges.items():
            try:
                await exchange.disconnect()
                self.logger.info(f"Disconnected from {name}")
            except Exception as e:
                self.logger.error(f"Error disconnecting from {name}: {e}")
    
    def get_exchange(self, name: str) -> Optional[Any]:
        """Get exchange by name"""
        return self.exchanges.get(name)
    
    def get_all_exchanges(self) -> Dict[str, Any]:
        """Get all loaded exchanges"""
        return self.exchanges.copy()
    
    def list_available_exchanges(self) -> List[str]:
        """List available exchange types"""
        return list(self.exchange_types.keys())
    
    def list_loaded_exchanges(self) -> List[str]:
        """List loaded exchange names"""
        return list(self.exchanges.keys())
    
    async def get_all_trading_pairs(self) -> Dict[str, List]:
        """Get trading pairs from all exchanges"""
        all_pairs = {}
        
        for name, exchange in self.exchanges.items():
            try:
                pairs = await exchange.get_trading_pairs()
                all_pairs[name] = [str(pair) for pair in pairs]
            except Exception as e:
                self.logger.error(f"Error getting pairs from {name}: {e}")
                all_pairs[name] = []
        
        return all_pairs
    
    async def validate_exchange_configs(self) -> Dict[str, bool]:
        """Validate all exchange configurations"""
        results = {}
        
        config_files = self.config_dir.glob("*.yaml")
        
        for config_file in config_files:
            try:
                with open(config_file, 'r') as f:
                    config = yaml.safe_load(f)
                
                exchange_name = config.get('name', config_file.stem)
                
                # Basic validation
                if not config.get('type'):
                    results[exchange_name] = False
                    self.logger.error(f"{exchange_name}: No exchange type specified")
                    continue
                
                if config.get('type') not in self.exchange_types:
                    results[exchange_name] = False
                    self.logger.error(f"{exchange_name}: Unknown exchange type {config.get('type')}")
                    continue
                
                # Exchange-specific validation
                if config.get('type') == 'BinanceExchange':
                    if not config.get('api_key') or not config.get('secret'):
                        results[exchange_name] = False
                        self.logger.warning(f"{exchange_name}: Missing API credentials (will use public API only)")
                
                results[exchange_name] = True
                self.logger.info(f"{exchange_name}: Configuration valid")
                
            except Exception as e:
                exchange_name = config_file.stem
                results[exchange_name] = False
                self.logger.error(f"{exchange_name}: Configuration error - {e}")
        
        return results


# Utility functions for exchange management

def create_exchange_factory(config_dir: Optional[Path] = None) -> ExchangeFactory:
    """
    Create exchange factory with default configuration directory
    
    Args:
        config_dir: Custom config directory (optional)
        
    Returns:
        ExchangeFactory instance
    """
    if config_dir is None:
        config_dir = Path(__file__).parent.parent / "config" / "exchanges"
    
    return ExchangeFactory(config_dir)


async def load_all_exchanges(config_dir: Optional[Path] = None) -> Dict[str, Any]:
    """
    Convenience function to load all exchanges
    
    Args:
        config_dir: Custom config directory (optional)
        
    Returns:
        Dictionary of loaded exchanges
    """
    factory = create_exchange_factory(config_dir)
    return await factory.load_exchanges()