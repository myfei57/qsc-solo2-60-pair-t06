"""Valve model and the idempotent command gateway.

The gateway is the only place commands reach the field. It enforces:

* travel speed limiting (a valve cannot jump more than its configured
  percent-per-tick, so big corrections ramp instead of slamming);
* idempotent dispatch: a command identical to the one already sent for the
  same reason is swallowed (``IDENTICAL_COMMAND``). After a level-transmitter
  recovery the loop re-evaluates and re-ticks; identical valve targets must
  not be transmitted twice.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import LevelConfig, clamp
from .model import Reason, ValveCommand


@dataclass
class Valve:
    """Linear valve: percent open maps linearly to flow capacity."""

    name: str
    capacity_m3h: float
    position_pct: float = 0.0

    def flow_for_pct(self, pct: float) -> float:
        return self.capacity_m3h * clamp(pct, 0.0, 100.0) / 100.0

    def pct_for_flow(self, flow_m3h: float) -> float:
        return clamp(flow_m3h / self.capacity_m3h, 0.0, 1.0) * 100.0


@dataclass(frozen=True)
class DispatchResult:
    command: ValveCommand | None
    sent: bool
    suppressed_reason: Reason | None
    travel_limited: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "command": None if self.command is None else self.command.as_dict(),
            "sent": self.sent,
            "suppressed_reason": None
            if self.suppressed_reason is None
            else self.suppressed_reason.value,
            "travel_limited": self.travel_limited,
        }


class ValveGateway:
    def __init__(self, config: LevelConfig) -> None:
        self.inlet = Valve("inlet", config.inlet_max_m3h, 0.0)
        self.outlet = Valve("outlet", config.outlet_max_m3h, 0.0)
        self.travel = config.valve_travel_pct_per_tick
        self._last_keys: dict[str, str] = {}

    def valve(self, name: str) -> Valve:
        if name == "inlet":
            return self.inlet
        if name == "outlet":
            return self.outlet
        raise KeyError(f"unknown valve {name}")

    def adopt(self, name: str, flow_m3h: float) -> None:
        """Adopt the measured valve position without sending a command.

        Used at startup or after field feedback so the travel limiter starts
        from the real opening rather than 0%.
        """

        valve = self.valve(name)
        valve.position_pct = valve.pct_for_flow(flow_m3h)
        self._last_keys[name] = ""

    def dispatch(
        self,
        name: str,
        target_flow_m3h: float,
        reason: Reason,
        *,
        allow_fast: bool = False,
    ) -> DispatchResult:
        valve = self.valve(name)
        requested_pct = valve.pct_for_flow(target_flow_m3h)
        travel = 100.0 if allow_fast else self.travel
        limited = abs(requested_pct - valve.position_pct) > travel + 1e-9
        target_pct = (
            clamp(requested_pct, valve.position_pct - travel, valve.position_pct + travel)
            if not allow_fast
            else requested_pct
        )
        flow = valve.flow_for_pct(target_pct)
        command = ValveCommand(valve=name, target_pct=target_pct, reason=reason, flow_m3h=flow)

        key = command.key()
        # No movement at all (adopted position or an already-settled command):
        # never transmit, whatever reason this tick attached.
        if abs(target_pct - valve.position_pct) <= 1e-9:
            return DispatchResult(
                command=command,
                sent=False,
                suppressed_reason=Reason.IDENTICAL_COMMAND,
                travel_limited=False,
            )
        valve.position_pct = target_pct
        self._last_keys[name] = key
        return DispatchResult(
            command=command,
            sent=True,
            suppressed_reason=None,
            travel_limited=limited and not allow_fast,
        )
