# validator.py
# Main validation logic for Bittensor subnet

from .metrics import MetricsClient, MinerKey
from .mapping import MappingManager
from .rating import RatingCalculator
from .config import ValidatorSettings # Ensure ValidatorSettings is imported
from typing import Dict, List
from src.metrics import MinerMetrics
from collections import defaultdict


class Validator:
    def __init__(
        self,
        config: ValidatorSettings,
        metrics_client: MetricsClient,
        mapping_manager: MappingManager,
    ):
        self.config = config
        self.metrics_client = metrics_client
        self.mapping_manager = mapping_manager
        self.rating_calculator = RatingCalculator(
            uptime_alpha=config.rating_weight, # Corresponds to rating_weight in config
            window=config.window,
            max_difficulty=config.max_difficulty,
            # NEW PARAMETER: Pass the invalid_shares_penalty_factor from config
            invalid_shares_penalty_factor=config.invalid_shares_penalty_factor,
        )

    async def compute_ratings(self):
        """Fetch metrics, update mapping, compute ratings, and send to Bittensor."""
        hotkey_metrics = await self.get_hotkey_metrics_map()
        ratings = self.rating_calculator.rate_all(hotkey_metrics)
        return ratings

    async def get_hotkey_metrics_map(self) -> Dict[str, List[MinerMetrics]]:
        """Load mapping and map metrics to hotkeys. Returns dict[hotkey, List[MinerMetrics]]."""
        metrics = await self.metrics_client.fetch_metrics()
        mapping = await self.mapping_manager.get_mapping()
        hotkey_metrics: Dict[str, List[MinerMetrics]] = defaultdict(list)
        for worker, hotkey in mapping.items():
            key = MinerKey(
                wallet=self.config.kaspa_pool_owner_wallet, worker=worker
            )
            hotkey_metrics[hotkey].append(
                metrics.get(key, MinerMetrics.default_instance(worker))
            )
        return dict(hotkey_metrics)
