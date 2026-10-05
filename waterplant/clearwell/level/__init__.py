"""Clear well level closed loop.

The sub-package implements a mass-balance level controller for the clear well:

* regulation moves are computed from the inflow/outflow imbalance, the tank
  volume and the envelope the pump group can actually carry;
* the inlet valve is the primary actuator, the outlet valve only trims;
* pump status is always confirmed *before* valve moves are computed, and pump
  start/stop transitions freeze the level loop behind feed-forward balancing;
* protection limits are fixed fractions of the effective tank volume. Shift
  usage may move the target level, never the interlocks;
* large level swings shrink the allowed slew rate, they never widen the limits;
* a level transmitter outage is a partial failure handled conservatively, and
  recovery is reconciled idempotently.

Every decision is appended to a hash chained audit log and is the single
document the console displays, so action, level and reason can never diverge.
"""

from .auditor import LevelAuditor, LevelEntry
from .config import LevelConfig
from .controller import LevelLoop
from .demo import ScriptedPlant
from .estimator import LevelEstimator
from .model import (
    Action,
    ControlMode,
    Decision,
    LevelReading,
    Phase,
    PumpAction,
    PumpCommand,
    PumpState,
    PumpStatus,
    Reading,
    Reason,
    ValveCommand,
    PumpCommand,
    FlowReading,
)
from .pumps import PumpGroup
from .valves import Valve, ValveGateway

__all__ = [
    "Action",
    "ControlMode",
    "Decision",
    "FlowReading",
    "LevelAuditor",
    "LevelConfig",
    "LevelEntry",
    "LevelEstimator",
    "LevelLoop",
    "Phase",
    "PumpAction",
    "PumpCommand",
    "PumpGroup",
    "PumpState",
    "PumpStatus",
    "Reading",
    "Reason",
    "ScriptedPlant",
    "Valve",
    "ValveCommand",
    "ValveGateway",
]
