"""Clear well projections and validation rules."""

from __future__ import annotations

from dataclasses import dataclass

# Operating limits are fixed fractions of pool capacity.  They are deliberately
# not derived from shift usage, which changes continuously and would make the
# protection limits non-reproducible.
LEVEL_MIN_RATIO = 0.10
LEVEL_HIGH_RATIO = 0.90
DEFAULT_TARGET_RATIO = 0.50
DEADBAND_RATIO = 0.02
VOLATILITY_RATIO = 0.05
NORMAL_FLOW_SLEW_RATIO = 0.10
VOLATILE_FLOW_SLEW_RATIO = 0.05
PROTECTION_FLOW_SLEW_RATIO = 0.20
OUTLET_TRIM_RATIO = 0.20
MIN_CORRECTION_SECONDS = 300.0
DEFAULT_CAPACITY = 1_000.0
DEFAULT_INLET_MAX = 100.0
DEFAULT_HORIZON_SECONDS = 300.0


@dataclass(frozen=True)
class WellState:
    """Stored level and residual target."""

    level: float
    residual_target: float

    def as_dict(self) -> dict[str, float]:
        return {"level": self.level, "residual_target": self.residual_target}


@dataclass(frozen=True)
class LevelConfig:
    """Fixed physical limits and tuning parameters for one clear well."""

    capacity: float = DEFAULT_CAPACITY
    target_level: float = DEFAULT_CAPACITY * DEFAULT_TARGET_RATIO
    inlet_max: float = DEFAULT_INLET_MAX
    horizon_seconds: float = DEFAULT_HORIZON_SECONDS

    @property
    def min_level(self) -> float:
        return self.capacity * LEVEL_MIN_RATIO

    @property
    def high_level(self) -> float:
        return self.capacity * LEVEL_HIGH_RATIO

    @property
    def deadband(self) -> float:
        return self.capacity * DEADBAND_RATIO

    @property
    def volatility_threshold(self) -> float:
        return self.capacity * VOLATILITY_RATIO

    def as_dict(self) -> dict[str, float]:
        return {
            "capacity": self.capacity,
            "target_level": self.target_level,
            "min_level": self.min_level,
            "high_level": self.high_level,
            "inlet_max": self.inlet_max,
            "horizon_seconds": self.horizon_seconds,
            "deadband": self.deadband,
            "volatility_threshold": self.volatility_threshold,
            "limit_basis": "pool_capacity",
        }


def validate_level(level: float) -> None:
    """Reject levels outside the supported range."""

    if level < 0:
        raise ValueError("level must be non-negative")
    if level > 1_000_000:
        raise ValueError("level exceeds the supported range")


def validate_capacity(capacity: float) -> None:
    if capacity <= 0:
        raise ValueError("capacity must be positive")
    if capacity > 1_000_000:
        raise ValueError("capacity exceeds the supported range")


def validate_config(config: LevelConfig) -> None:
    validate_capacity(config.capacity)
    validate_level(config.target_level)
    if config.target_level < config.min_level or config.target_level > config.high_level:
        raise ValueError("target level must remain inside fixed capacity protection limits")
    if config.inlet_max < 0:
        raise ValueError("inlet maximum must be non-negative")
    if config.horizon_seconds <= 0:
        raise ValueError("correction horizon must be positive")
