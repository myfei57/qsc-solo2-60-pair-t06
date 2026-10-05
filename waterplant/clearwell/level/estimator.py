"""Conservative level estimator used while the transmitter is unreadable.

The estimate is a dead-reckoning mass balance: integrate net inflow over the
tank footprint. Every failed tick widens an uncertainty band; when the band
exceeds the configured bound the loop must stop acting on the estimate and
safe-hold. On recovery the measured level is re-adopted directly (bumpless):
the integrator never "catches up" on the stale error, so a recovered
transmitter cannot itself trigger a large swing.
"""

from __future__ import annotations

from .config import MAX_ESTIMATE_UNCERTAINTY_M
from .model import LevelReading

UNCERTAINTY_GROWTH_PER_TICK_M = 0.05
UNCERTAINTY_FLOOR_M = 0.02


class LevelEstimator:
    def __init__(self, initial_level_m: float) -> None:
        self._level_m = float(initial_level_m)
        self._uncertainty_m = 0.0
        self._estimating = False
        self._ticks_without_signal = 0

    def adopt_measurement(self, reading: LevelReading) -> tuple[float, float, bool]:
        """Re-adopt a good reading and clear the uncertainty (bumpless)."""

        self._level_m = float(reading.level_m)
        self._uncertainty_m = 0.0
        self._estimating = False
        self._ticks_without_signal = 0
        return self._level_m, 0.0, False

    def integrate(
        self,
        reading: LevelReading,
        net_inflow_m3h: float,
        area_m2: float,
        tick_s: float,
    ) -> tuple[float, float, bool]:
        """Advance the estimate from net inflow and grow the uncertainty.

        Returns (level_used_m, uncertainty_m, safe_hold_required).
        """

        self._estimating = True
        self._ticks_without_signal += 1
        self._level_m += net_inflow_m3h * (tick_s / 3600.0) / max(area_m2, 1e-9)
        self._uncertainty_m = (
            UNCERTAINTY_FLOOR_M + self._ticks_without_signal * UNCERTAINTY_GROWTH_PER_TICK_M
        )
        safe_hold = self._uncertainty_m >= MAX_ESTIMATE_UNCERTAINTY_M
        return self._level_m, self._uncertainty_m, safe_hold

    @property
    def estimating(self) -> bool:
        return self._estimating

    @property
    def level_m(self) -> float:
        return self._level_m

    @property
    def uncertainty_m(self) -> float:
        return self._uncertainty_m
