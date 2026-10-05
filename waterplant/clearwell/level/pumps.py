"""Pump group model: confirmation, timed transitions and feed-forward.

Timing dependency with the valves is owned here:

* ``confirm`` is called before any valve move is computed (phase
  CONFIRM_PUMPS). It advances STARTING/STOPPING timers from confirmed field
  status and reports whether the group is mid-transition.
* During a transition the level loop freezes PI regulation and balances flows
  with a one-shot feed-forward (pre-bias the inlet against the pump that is
  coming on / dropping off). Repeating a transition tick never re-applies the
  pre-bias.
* Start/stop requests are debounced on sustained outlet demand so a single
  noisy tick cannot cycle a pump.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import (
    REQUEST_DEBOUNCE_TICKS,
    STOP_DEMAND_FRACTION,
    TRANSITION_SETTLE_TICKS,
    TRANSITION_TICKS,
    UNMET_FLOW_M3H,
    LevelConfig,
)
from .balance import BALANCE_TOL
from .model import PumpAction, PumpState, PumpStatus
from .model import Reason


@dataclass(frozen=True)
class PumpSelection:
    """Result of confirming the group at the top of a tick."""

    statuses: list[PumpStatus]
    running_count: int
    transition: bool
    transition_pump_id: str
    transition_action: PumpAction
    feedforward_applied: bool
    min_outflow_m3h: float
    max_outflow_m3h: float
    actual_outflow_m3h: float

    @property
    def feedforward_needed(self) -> bool:
        return self.transition and not self.feedforward_applied


class PumpGroup:
    def __init__(self, config: LevelConfig) -> None:
        self.config = config
        self.pump_ids = [f"P{i + 1}" for i in range(config.pump_count)]
        self.states: dict[str, PumpState] = {
            pump_id: PumpState.RUNNING if i == 0 else PumpState.STOPPED
            for i, pump_id in enumerate(self.pump_ids)
        }
        self.actual_flow: dict[str, float] = {
            pump_id: (config.pump_unit_m3h if i == 0 else 0.0)
            for i, pump_id in enumerate(self.pump_ids)
        }
        self.available: dict[str, bool] = {pump_id: True for pump_id in self.pump_ids}
        self.transition_left = 0
        self.transition_pump_id = ""
        self.transition_action = PumpAction.NONE
        self._ff_applied = False
        self._settle_left = 0
        self._start_votes = 0
        self._stop_votes = 0
        self._last_request: dict[str, PumpAction] = {}

    # -- field confirmation (runs before valve maths) ----------------------

    def snapshot(self) -> PumpSelection:
        """Read-only view used by the console; must not advance timers."""

        statuses = [
            PumpStatus(
                pump_id=pump_id,
                state=self.states[pump_id],
                actual_flow_m3h=self.actual_flow[pump_id],
                available=self.available[pump_id],
            )
            for pump_id in self.pump_ids
        ]
        running = [s for s in statuses if s.state.delivers]
        count = len(running)
        pmin, pmax = self.config.pump_capacity(count)
        transition = self.transition_left > 0
        return PumpSelection(
            statuses=statuses,
            running_count=count,
            transition=transition,
            transition_pump_id=self.transition_pump_id if transition else "",
            transition_action=self.transition_action if transition else PumpAction.NONE,
            feedforward_applied=self._ff_applied,
            min_outflow_m3h=pmin,
            max_outflow_m3h=pmax,
            actual_outflow_m3h=sum(s.actual_flow_m3h for s in running),
        )

    def confirm(self) -> PumpSelection:
        """Advance transition timers from confirmed field status."""

        if self.transition_left > 0:
            self.transition_left -= 1
            if self.transition_left == 0:
                self._complete_transition()
                self._settle_left = TRANSITION_SETTLE_TICKS
        elif self._settle_left > 0:
            self._settle_left -= 1

        statuses = [
            PumpStatus(
                pump_id=pump_id,
                state=self.states[pump_id],
                actual_flow_m3h=self.actual_flow[pump_id],
                available=self.available[pump_id],
            )
            for pump_id in self.pump_ids
        ]
        running = [s for s in statuses if s.state.delivers]
        count = len(running)
        pmin, pmax = self.config.pump_capacity(count)
        transition = self.transition_left > 0
        return PumpSelection(
            statuses=statuses,
            running_count=count,
            transition=transition,
            transition_pump_id=self.transition_pump_id if transition else "",
            transition_action=self.transition_action if transition else PumpAction.NONE,
            feedforward_applied=self._ff_applied,
            min_outflow_m3h=pmin,
            max_outflow_m3h=pmax,
            actual_outflow_m3h=sum(s.actual_flow_m3h for s in running),
        )

    def _complete_transition(self) -> None:
        pump_id = self.transition_pump_id
        if self.transition_action is PumpAction.START:
            self.states[pump_id] = PumpState.RUNNING
            self.actual_flow[pump_id] = self.config.pump_unit_m3h
        elif self.transition_action is PumpAction.STOP:
            self.states[pump_id] = PumpState.STOPPED
            self.actual_flow[pump_id] = 0.0
        self.transition_pump_id = ""
        self.transition_action = PumpAction.NONE
        self._ff_applied = False

    # -- requests -----------------------------------------------------------

    def request(
        self,
        *,
        residual_m3h: float,
        current_net_m3h: float,
        outlet_demand_m3h: float,
        delivered_outflow_m3h: float,
        pump_min_outflow: float,
        pump_max_outflow: float,
        outlet_valve_flow_m3h: float,
        allow_start: bool,
        level_in_high_zone: bool = False,
        level_in_low_zone: bool = False,
    ) -> tuple[str | None, PumpAction, Reason]:
        """Debounced start/stop decision driven by demand vs pump envelope.

        * start: sustained demand above the capacity the running group can
          carry (with a small hysteresis margin). The outlet trim cannot
          conjure flow the pumps do not deliver, so this fires on demand,
          before the level sags. Refused in the high-level zone, where
          starting another pump would add outflow to an already filling well;
        * stop: sustained demand below the minimum stable flow of the running
          group, i.e. one fewer pump can still carry it. Refused in the
          low-level zone, which is already shedding outflow.

        Auto starts are also refused when ``allow_start`` is false (degraded
        level measurement). Two consecutive ticks are required, so one noisy
        sample cannot cycle a pump.
        """

        running = [p for p in self.pump_ids if self.states[p].delivers]
        stopped_available = [
            p
            for p in self.pump_ids
            if self.states[p] is PumpState.STOPPED and self.available[p]
        ]

        # Start when the group cannot serve demand. Two independent signals,
        # either sustained for two ticks:
        #   * demand exceeds the envelope of the running group, or
        #   * the outlet valve is already wide against that envelope yet the
        #     measured outflow still undershoots demand (physical shortage,
        #     e.g. pumps ramping slower than the valve).
        # The high-level interlock blocks starts there (more outflow into a
        # filling well would be wrong).
        unmet = outlet_demand_m3h - delivered_outflow_m3h
        envelope_short = outlet_demand_m3h > pump_max_outflow + BALANCE_TOL
        # The outlet valve is open as far as the running envelope allows but
        # the water physically delivered still undershoots demand.
        valve_wide_short = (
            unmet > UNMET_FLOW_M3H
            and outlet_valve_flow_m3h >= pump_max_outflow - BALANCE_TOL
        )
        want_start = (
            len(running) < self.config.pump_count
            and bool(stopped_available)
            and not level_in_high_zone
            and (envelope_short or valve_wide_short or residual_m3h < -BALANCE_TOL)
        )

        if len(running) > 1:
            _, fewer_max = self.config.pump_capacity(len(running) - 1)
            # One fewer pump must be able to carry the demand; the 0.6 factor
            # is the stop-side hysteresis against the start line. The positive
            # residual says the inlet is already against a limit shed-off pump
            # would relieve.
            stop_line = fewer_max * STOP_DEMAND_FRACTION
        else:
            stop_line = -1.0
        want_stop = (
            len(running) > 1
            and not level_in_low_zone
            and (outlet_demand_m3h <= stop_line or residual_m3h > BALANCE_TOL)
        )

        self._start_votes = self._start_votes + 1 if want_start else 0
        self._stop_votes = self._stop_votes + 1 if want_stop else 0

        if self._start_votes >= REQUEST_DEBOUNCE_TICKS and stopped_available and allow_start:
            pump_id = self._pick_start(stopped_available)
            return pump_id, PumpAction.START, Reason.PUMP_REQUEST_START
        if self._stop_votes >= REQUEST_DEBOUNCE_TICKS and running:
            pump_id = self._pick_stop(running)
            return pump_id, PumpAction.STOP, Reason.PUMP_REQUEST_STOP
        return None, PumpAction.NONE, Reason.PUMP_INHIBITED

    def _pick_start(self, candidates: list[str]) -> str:
        return sorted(candidates)[0]

    def _pick_stop(self, running: list[str]) -> str:
        return sorted(running)[-1]

    # -- command application (idempotent transitions) ----------------------

    def apply(self, pump_id: str, action: PumpAction) -> bool:
        """Begin a transition. Returns False when the state is already right.

        Repeating the same start/stop while the transition is live or complete
        is a no-op, which makes recovery-triggered re-ticks idempotent.
        """

        if self._last_request.get(pump_id) is action and (
            self.transition_pump_id == pump_id
            or (action is PumpAction.START and self.states[pump_id] is PumpState.RUNNING)
            or (action is PumpAction.STOP and self.states[pump_id] is PumpState.STOPPED)
        ):
            return False
        if action is PumpAction.START:
            if self.states[pump_id] is not PumpState.STOPPED or not self.available[pump_id]:
                return False
            self.states[pump_id] = PumpState.STARTING
        elif action is PumpAction.STOP:
            if not self.states[pump_id].delivers:
                return False
            self.states[pump_id] = PumpState.STOPPING
        else:
            return False
        self.transition_pump_id = pump_id
        self.transition_action = action
        self.transition_left = TRANSITION_TICKS
        self._ff_applied = False
        self._settle_left = 0
        self._last_request[pump_id] = action
        self._start_votes = 0
        self._stop_votes = 0
        return True

    def mark_feedforward_applied(self) -> None:
        self._ff_applied = True

    def in_transition(self) -> bool:
        return self.transition_left > 0 or self._settle_left > 0

    # -- feed-forward target used during transitions -----------------------

    def force_stop_lowest(self) -> tuple[str | None, PumpAction, Reason]:
        """LL protection: stop delivering pumps, lowest priority last."""

        running = [p for p in self.pump_ids if self.states[p].delivers]
        if not running:
            return None, PumpAction.NONE, Reason.LL_TRIP
        return self._pick_stop(running), PumpAction.STOP, Reason.LL_TRIP
