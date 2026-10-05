"""Value objects shared by the level loop, pumps, valves and audit."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Action(str, Enum):
    """What the loop decided to do this tick."""

    HOLD = "hold"
    REGULATE = "regulate"
    PROTECT_HIGH = "protect_high"
    PROTECT_LOW = "protect_low"
    PUMP_TRANSITION = "pump_transition"
    DEGRADED_BALANCE = "degraded_balance"
    SAFE_HOLD = "safe_hold"


class ControlMode(str, Enum):
    """Confidence the loop has in the level measurement."""

    NORMAL = "normal"
    DEGRADED = "degraded"
    SAFE_HOLD = "safe_hold"


class Phase(str, Enum):
    """Sequencing phase; pump state is always resolved before valve moves."""

    READ_INPUTS = "read_inputs"
    CONFIRM_PUMPS = "confirm_pumps"
    DECIDE_VALVES = "decide_valves"
    DISPATCH = "dispatch"


class Reason(str, Enum):
    """Stable reason codes recorded next to every action and command."""

    AT_TARGET = "at_target"
    MASS_BALANCE = "mass_balance"
    HIGH_LEVEL_CLAMP = "high_level_clamp"
    HH_TRIP = "hh_trip"
    LOW_LEVEL_CLAMP = "low_level_clamp"
    LL_TRIP = "ll_trip"
    SLEW_LIMITED = "slew_limited"
    VOLATILITY_SUPPRESSED = "volatility_suppressed"
    OUTLET_TRIM_CAPPED = "outlet_trim_capped"
    PUMP_CAPACITY_SHORT = "pump_capacity_short"
    PUMP_START_FF = "pump_start_feedforward"
    PUMP_STOP_FF = "pump_stop_feedforward"
    PUMP_INHIBITED = "pump_inhibited"
    PUMP_REQUEST_START = "pump_request_start"
    PUMP_REQUEST_STOP = "pump_request_stop"
    LEVEL_FAULT = "level_fault"
    LEVEL_RECOVERED = "level_recovered"
    UNCERTAINTY_HOLD = "uncertainty_hold"
    BOTH_TRIPS = "both_trips"
    IDENTICAL_COMMAND = "identical_command"
    TRAVEL_LIMITED = "travel_limited"


class PumpAction(str, Enum):
    """Requested change to the pump group."""

    NONE = "none"
    START = "start"
    STOP = "stop"


@dataclass(frozen=True)
class LevelReading:
    """One level transmitter sample.

    ``ok=False`` means the transmitter could not be read. That is a partial
    failure: flow meters and pumps may still be healthy.
    """

    level_m: float
    ok: bool = True
    stale: bool = False

    def as_dict(self) -> dict[str, object]:
        return {"level_m": self.level_m, "ok": self.ok, "stale": self.stale}


@dataclass(frozen=True)
class FlowReading:
    """Inflow and outflow flow meter samples in m3/h."""

    inflow_m3h: float
    outflow_m3h: float
    inflow_ok: bool = True
    outflow_ok: bool = True

    @property
    def imbalance_m3h(self) -> float:
        return self.inflow_m3h - self.outflow_m3h

    def as_dict(self) -> dict[str, object]:
        return {
            "inflow_m3h": self.inflow_m3h,
            "outflow_m3h": self.outflow_m3h,
            "inflow_ok": self.inflow_ok,
            "outflow_ok": self.outflow_ok,
            "imbalance_m3h": self.imbalance_m3h,
        }


@dataclass(frozen=True)
class Reading:
    """Everything the loop reads at the top of a tick (phase READ_INPUTS)."""

    level: LevelReading
    flow: FlowReading
    # Outflow demand seen at the pump header, used as a pressure proxy. When it
    # cannot be read it defaults to the measured outflow.
    outlet_demand_m3h: float | None = None
    tick_s: float = 1.0

    def as_dict(self) -> dict[str, object]:
        return {
            "level": self.level.as_dict(),
            "flow": self.flow.as_dict(),
            "outlet_demand_m3h": self.outlet_demand_m3h,
            "tick_s": self.tick_s,
        }


class PumpState(str, Enum):
    RUNNING = "running"
    STOPPED = "stopped"
    STARTING = "starting"
    STOPPING = "stopping"
    FAILED = "failed"

    @property
    def delivers(self) -> bool:
        return self in (PumpState.RUNNING, PumpState.STOPPING)

    @property
    def in_transition(self) -> bool:
        return self in (PumpState.STARTING, PumpState.STOPPING)


@dataclass(frozen=True)
class PumpStatus:
    """Confirmed field status of one pump (phase CONFIRM_PUMPS)."""

    pump_id: str
    state: PumpState
    actual_flow_m3h: float
    available: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "pump_id": self.pump_id,
            "state": self.state.value,
            "actual_flow_m3h": self.actual_flow_m3h,
            "available": self.available,
            "delivers": self.state.delivers,
            "in_transition": self.state.in_transition,
        }


@dataclass(frozen=True)
class ValveCommand:
    """One ordered valve move.

    The idempotency key is the valve plus the ordered opening: re-ticking the
    loop (e.g. after a level-transmitter recovery) that converges on the same
    opening transmits nothing twice, even though the reason code may differ.
    The reason is recorded alongside for the audit, not used for de-dup.
    """

    valve: str
    target_pct: float
    reason: Reason
    flow_m3h: float

    def key(self) -> str:
        return f"{self.valve}:{self.target_pct:.3f}"

    def as_dict(self) -> dict[str, object]:
        return {
            "valve": self.valve,
            "target_pct": round(self.target_pct, 3),
            "reason": self.reason.value,
            "flow_m3h": round(self.flow_m3h, 3),
        }


@dataclass(frozen=True)
class PumpCommand:
    """One ordered pump start/stop. Idempotent on (pump, action, reason)."""

    pump_id: str
    action: PumpAction
    reason: Reason

    def key(self) -> str:
        return f"{self.pump_id}:{self.action.value}:{self.reason.value}"

    def as_dict(self) -> dict[str, object]:
        return {"pump_id": self.pump_id, "action": self.action.value, "reason": self.reason.value}


@dataclass(frozen=True)
class Decision:
    """One closed-loop tick: action, the level it was based on and the reason.

    This object is both dispatched to the field and appended verbatim to the
    audit log, then served to the console. Action, level and reason therefore
    always come from the same atomic record.
    """

    seq: int
    at: str
    phase_log: list[str]
    mode: ControlMode
    action: Action
    reason: Reason
    level_m: float
    level_used_m: float
    level_estimated: bool
    uncertainty_m: float
    volume_m3: float
    target_m: float
    inflow_m3h: float
    outflow_m3h: float
    net_inflow_m3h: float
    desired_net_m3h: float
    slew_budget_m3h: float
    valves: list[ValveCommand] = field(default_factory=list)
    pumps: list[PumpCommand] = field(default_factory=list)
    pump_group: list[PumpStatus] = field(default_factory=list)
    suppressed: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "seq": self.seq,
            "at": self.at,
            "phase_log": list(self.phase_log),
            "mode": self.mode.value,
            "action": self.action.value,
            "reason": self.reason.value,
            "level_m": round(self.level_m, 4),
            "level_used_m": round(self.level_used_m, 4),
            "level_estimated": self.level_estimated,
            "uncertainty_m": round(self.uncertainty_m, 4),
            "volume_m3": round(self.volume_m3, 3),
            "target_m": round(self.target_m, 4),
            "inflow_m3h": round(self.inflow_m3h, 3),
            "outflow_m3h": round(self.outflow_m3h, 3),
            "net_inflow_m3h": round(self.net_inflow_m3h, 3),
            "desired_net_m3h": round(self.desired_net_m3h, 3),
            "slew_budget_m3h": round(self.slew_budget_m3h, 3),
            "valves": [command.as_dict() for command in self.valves],
            "pumps": [command.as_dict() for command in self.pumps],
            "pump_group": [status.as_dict() for status in self.pump_group],
            "suppressed": list(self.suppressed),
            "notes": list(self.notes),
        }
