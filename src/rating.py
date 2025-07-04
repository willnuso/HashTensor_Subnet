# src/rating.py
# Computes normalized ratings for Bittensor hotkeys based on miner metrics

from datetime import timedelta
from typing import Dict, List
import math

from .metrics import MinerMetrics
from fiber.utils import get_logger


logger = get_logger(__name__)

import time


class RatingCalculator:
    def __init__(
        self,
        uptime_alpha: float = 2.0,
        window: timedelta = timedelta(hours=1),
        ndigits: int = 8,
        max_difficulty: float = 16384.0,
        # NEW PARAMETER: Factor to control penalty strength for invalid shares
        invalid_shares_penalty_factor: float = 0.5, # Adjust this value (0.0 to 1.0) to control severity
    ):
        self.uptime_alpha = float(uptime_alpha)
        self.window_seconds = (
            window.total_seconds()
        )  # length of the window (e.g., 1 hour)
        self.ndigits = ndigits
        self.max_difficulty = max_difficulty
        self.invalid_shares_penalty_factor = invalid_shares_penalty_factor # Store the new parameter

    def compute_effective_work(self, metrics: List[MinerMetrics]) -> float:
        """
        Sum of valid_shares * difficulty, with difficulty clamped to self.max_difficulty,
        and apply a per-worker penalty if difficulty exceeds max_difficulty.
        """
        return sum(
            m.valid_shares * min(m.difficulty, self.max_difficulty) * self.penalty_exponential(m.difficulty)
            for m in metrics
        )

    def compute_fractional_uptime(self, start_timestamp: float) -> float:
        """Convert the worker's unix start timestamp to a fractional uptime for the window."""
        now = time.time()
        window_start = now - self.window_seconds

        # If the start timestamp is in the future, consider the worker as not started in this window
        if start_timestamp > now:
            return 0.0

        # If the start is before the window, the worker was online for the entire window
        if start_timestamp < window_start:
            return 1.0

        # Otherwise, the worker started within the window, so was online from start to now:
        uptime_seconds = now - start_timestamp  # always < window_seconds
        return uptime_seconds / self.window_seconds

    def compute_avg_uptime(self, metrics: List[MinerMetrics]) -> float:
        """
        For each worker, take the uptime fraction (less than 1),
        then average across all workers for a single hotkey.
        """
        if not metrics:
            return 0.0
        # Convert each unix timestamp to a fraction
        uptimes = [self.compute_fractional_uptime(m.uptime) for m in metrics]
        # Clamp fractions to [0.0, 1.0] just in case
        uptimes = [max(0.0, min(1.0, u)) for u in uptimes]
        return sum(uptimes) / len(uptimes)

    def penalty_exponential(self, difficulty: float) -> float:
        """
        Applies an exponential penalty if difficulty exceeds max_difficulty.
        Returns a penalty factor in [0, 1].
        """
        if difficulty > self.max_difficulty:
            return math.exp(-(difficulty - self.max_difficulty) / self.max_difficulty)
        return 1.0

    # NEW METHOD: Compute share quality based on valid vs. invalid shares
    def compute_share_quality(self, metrics: List[MinerMetrics]) -> float:
        """
        Calculates the ratio of valid shares to total shares for a hotkey.
        Returns a value between 0.0 and 1.0.
        A quality of 1.0 means all shares are valid. A quality of 0.0 means all shares are invalid.
        """
        total_valid_shares = sum(m.valid_shares for m in metrics)
        total_invalid_shares = sum(m.invalid_shares for m in metrics)
        total_shares = total_valid_shares + total_invalid_shares

        if total_shares == 0:
            # If no shares were submitted, we can't determine quality.
            # Returning 1.0 for no shares means no penalty is applied due to invalid shares.
            # This makes sense as there are no invalid shares either.
            return 1.0

        quality = total_valid_shares / total_shares
        return quality


    def rate_all(
        self, metrics: Dict[str, List[MinerMetrics]]
    ) -> Dict[str, float]:
        """
        First, compute effective work (valid_shares * difficulty),
        normalize, apply uptime penalty, and clamp the result.
        """
        # 1. Compute effective work for all hotkeys
        work = {
            hotkey: self.compute_effective_work(ms)
            for hotkey, ms in metrics.items()
        }
        max_work = max(work.values(), default=1.0) # Ensure no division by zero

        # 2. Normalization and application of all penalties
        scores: Dict[str, float] = {}
        for hotkey, ms in metrics.items():
            # Calculate normalized score based on effective work
            norm_score = 0.0 if max_work == 0 else work[hotkey] / max_work
            
            # Get average uptime for the hotkey's workers
            avg_uptime = self.compute_avg_uptime(ms)  # Value between 0.0 and 1.0

            # Get share quality (valid shares / total shares) for the hotkey's workers
            share_quality = self.compute_share_quality(ms) # Value between 0.0 and 1.0

            # Apply uptime penalty: score * (avg_uptime ^ uptime_alpha)
            # Higher uptime_alpha penalizes lower uptime more heavily.
            penalized_by_uptime = norm_score * (avg_uptime**self.uptime_alpha)

            # NEW: Apply invalid shares penalty
            # We want share_quality of 1.0 to result in a multiplier of 1.0 (no penalty).
            # We want share_quality of 0.0 to result in a multiplier of (1.0 - invalid_shares_penalty_factor).
            # So, if invalid_shares_penalty_factor is 0.5:
            #   - share_quality = 1.0 -> multiplier = 1.0 - (1.0 - 1.0) * 0.5 = 1.0
            #   - share_quality = 0.5 -> multiplier = 1.0 - (1.0 - 0.5) * 0.5 = 1.0 - 0.25 = 0.75
            #   - share_quality = 0.0 -> multiplier = 1.0 - (1.0 - 0.0) * 0.5 = 1.0 - 0.5 = 0.5
            invalid_shares_multiplier = 1.0 - (1.0 - share_quality) * self.invalid_shares_penalty_factor
            
            # Combine all penalties to get the final score before clamping
            final_score_before_clamping = penalized_by_uptime * invalid_shares_multiplier

            # 3. Clamp the final score to [0.0, 1.0] and round
            scores[hotkey] = round(max(0.0, min(1.0, final_score_before_clamping)), self.ndigits)
        return scores
