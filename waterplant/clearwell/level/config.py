"""Fixed level loop configuration.

Two policy decisions are intentionally hard-coded here:

1. Interlock limits (LL/L/H/HH) are fixed fractions of the *effective tank
   volume*. Shift usage may shift the target level (``set_target``), it can
   never move the protection limits. There is deliberately no setter for the
   fractions or the tank geometry.

2. When the level swings hard, the loop *suppresses slew speed*; it never
   widens the limits. ``slew_budget`` is derived from the tank volume, and
   volatile ticks get a fraction of it. Protection trips may only move faster
   toward safety.
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import Reason

# Fixed fractions of the effective (usable) tank volume / level span.
HH_FRACTION = 0.95
H_FRACTION = 0.85
L_FRACTION = 0.15
LL_FRACTION = 0.05
DEFAULT_TARGET_FRACTION = 0.50

# Slew policy, all expressed against the tank volume.
NOMINAL_DRAIN_HOURS = 4.0
PROTECTION_DRAIN_HOURS = 1.0
VOLATILITY_FRACTION = 0.5
VOLATILITY_DELTA_FRACTION = 0.02
TRIP_DRAIN_HOURS = 0.25

# Regulation gains.
DEADBAND_M = 0.02
KP = 1.2
KI = 0.10
INTEGRAL_CLAMP_FRACTION = 0.25
# Degraded mode uses half the proportional gain and a half-speed outlet trim.
DEGRADED_GAIN_FRACTION = 0.5

# Pump sequencer (outflow demand is used as a header pressure proxy).
# A group is stopped down to n-1 pumps only when demand falls below this
# fraction of the n-1 group's capacity (stop-side hysteresis).
STOP_DEMAND_FRACTION = 0.60
REQUEST_DEBOUNCE_TICKS = 2
TRANSITION_TICKS = 3
TRANSITION_SETTLE_TICKS = 1
UNMET_FLOW_M3H = 1.0

# Estimated level is abandoned for a safe hold once the uncertainty band
# reaches this width.
MAX_ESTIMATE_UNCERTAINTY_M = 0.30


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass(frozen=True)
class LevelBand:
    """Protection band in both metres and cubic metres."""

    ll_m: float
    l_m: float
    target_m: float
    h_m: float
    hh_m: float
    ll_m3: float
    l_m3: float
    target_m3: float
    h_m3: float
    hh_m3: float

    def as_dict(self) -> dict[str, float]:
        return {
            "ll_m": round(self.ll_m, 4),
            "l_m": round(self.l_m, 4),
            "target_m": round(self.target_m, 4),
            "h_m": round(self.h_m, 4),
            "hh_m": round(self.hh_m, 4),
            "ll_m3": round(self.ll_m3, 3),
            "l_m3": round(self.l_m3, 3),
            "target_m3": round(self.target_m3, 3),
            "h_m3": round(self.h_m3, 3),
            "hh_m3": round(self.hh_m3, 3),
        }


class LevelConfig:
    """Tank geometry, valve/pump envelope and the fixed protection policy."""

    def __init__(
        self,
        area_m2: float = 100.0,
        dead_m: float = 0.0,
        max_m: float = 4.0,
        inlet_max_m3h: float = 120.0,
        outlet_max_m3h: float = 120.0,
        valve_travel_pct_per_tick: float = 25.0,
        pump_unit_m3h: float = 50.0,
        pump_min_fraction: float = 0.6,
        pump_count: int = 3,
    ) -> None:
        if area_m2 <= 0 or max_m <= 0:
            raise ValueError("tank geometry must be positive")
        if dead_m < 0 or dead_m >= max_m:
            raise ValueError("dead level must sit below the maximum level")
        if inlet_max_m3h <= 0 or outlet_max_m3h <= 0:
            raise ValueError("valve capacities must be positive")
        if not 0 < valve_travel_pct_per_tick <= 100:
            raise ValueError("valve travel per tick must be within (0, 100]")
        if pump_unit_m3h <= 0 or pump_count <= 0:
            raise ValueError("pump envelope must be positive")
        if not 0 < pump_min_fraction <= 1:
            raise ValueError("pump minimum fraction must be within (0, 1]")
        self.area_m2 = float(area_m2)
        self.dead_m = float(dead_m)
        self.max_m = float(max_m)
        self.inlet_max_m3h = float(inlet_max_m3h)
        self.outlet_max_m3h = float(outlet_max_m3h)
        self.valve_travel_pct_per_tick = float(valve_travel_pct_per_tick)
        self.pump_unit_m3h = float(pump_unit_m3h)
        self.pump_min_fraction = float(pump_min_fraction)
        self.pump_count = int(pump_count)

    # -- volume geometry ---------------------------------------------------

    @property
    def span_m(self) -> float:
        return self.max_m - self.dead_m

    @property
    def effective_volume_m3(self) -> float:
        """Usable volume between dead level and max level; basis for limits."""

        return self.span_m * self.area_m2

    def volume_for_level(self, level_m: float) -> float:
        return max(0.0, level_m - self.dead_m) * self.area_m2

    def level_for_volume(self, volume_m3: float) -> float:
        return self.dead_m + clamp(volume_m3, 0.0, self.effective_volume_m3) / self.area_m2

    # -- fixed protection band --------------------------------------------

    def band(self, target_m: float | None = None) -> LevelBand:
        target = self.default_target_m if target_m is None else target_m
        if not self.ll_m < target < self.hh_m:
            raise ValueError("target level must stay strictly inside the interlocks")
        return LevelBand(
            ll_m=self.ll_m,
            l_m=self.l_m,
            target_m=target,
            h_m=self.h_m,
            hh_m=self.hh_m,
            ll_m3=self.volume_for_level(self.ll_m),
            l_m3=self.volume_for_level(self.l_m),
            target_m3=self.volume_for_level(target),
            h_m3=self.volume_for_level(self.h_m),
            hh_m3=self.volume_for_level(self.hh_m),
        )

    @property
    def ll_m(self) -> float:
        return self._level(LL_FRACTION)

    @property
    def l_m(self) -> float:
        return self._level(L_FRACTION)

    @property
    def h_m(self) -> float:
        return self._level(H_FRACTION)

    @property
    def hh_m(self) -> float:
        return self._level(HH_FRACTION)

    @property
    def default_target_m(self) -> float:
        return self._level(DEFAULT_TARGET_FRACTION)

    def _level(self, fraction: float) -> float:
        return self.dead_m + self.span_m * fraction

    def validate_target(self, target_m: float) -> float:
        """Accept a shift-supplied target only inside the fixed interlocks."""

        if not self.ll_m < target_m < self.hh_m:
            raise ValueError(
                f"target {target_m:.3f}m must stay inside the fixed interlocks "
                f"({self.ll_m:.3f}m, {self.hh_m:.3f}m); shift usage cannot widen them"
            )
        return float(target_m)

    # -- slew budget (tank volume based; never widened on volatility) ------

    def nominal_slew_m3h(self) -> float:
        return self.effective_volume_m3 / NOMINAL_DRAIN_HOURS

    def protection_slew_m3h(self) -> float:
        return self.effective_volume_m3 / PROTECTION_DRAIN_HOURS

    def trip_slew_m3h(self) -> float:
        return self.effective_volume_m3 / TRIP_DRAIN_HOURS

    def volatility_delta_m(self) -> float:
        return self.span_m * VOLATILITY_DELTA_FRACTION

    def slew_budget(self, *, trip: bool, protection: bool, volatile: bool) -> tuple[float, Reason]:
        """Maximum |net inflow change| this tick.

        Volatility only shrinks the budget. Trips may speed moves up, but only
        toward safety.
        """

        if trip:
            return self.trip_slew_m3h(), Reason.SLEW_LIMITED
        budget = self.protection_slew_m3h() if protection else self.nominal_slew_m3h()
        if volatile:
            return budget * VOLATILITY_FRACTION, Reason.VOLATILITY_SUPPRESSED
        return budget, Reason.SLEW_LIMITED

    # -- pump envelope -----------------------------------------------------

    def pump_capacity(self, running_count: int) -> tuple[float, float]:
        """Return (min, max) outflow the running pumps can carry."""

        count = int(clamp(running_count, 0, self.pump_count))
        return count * self.pump_min_fraction * self.pump_unit_m3h, count * self.pump_unit_m3h
