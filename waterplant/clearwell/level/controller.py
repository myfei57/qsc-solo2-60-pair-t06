"""The clear well level closed loop.

Every tick walks four phases in a fixed order so the timing dependency is
explicit and auditable:

    READ_INPUTS -> CONFIRM_PUMPS -> DECIDE_VALVES -> DISPATCH

Pump status (including STARTING/STOPPING timers) is always confirmed *before*
a valve target is computed; a pump transition freezes level regulation behind
a one-shot feed-forward. A level-transmitter outage is a partial failure:
flows and pumps keep working, regulation falls back to a conservative
estimated level with a growing uncertainty band, auto pump starts are
inhibited and alarms are edge-triggered. Recovering re-adopts the measured
level (bumpless, idempotent).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from waterplant.store.epoch import load_float, save_float
from waterplant.store.store import Store

from . import balance
from .auditor import LevelAuditor
from .config import INTEGRAL_CLAMP_FRACTION, REQUEST_DEBOUNCE_TICKS, LevelConfig, clamp
from .estimator import LevelEstimator
from .model import (
    Action,
    ControlMode,
    Decision,
    Phase,
    PumpAction,
    PumpState,
    Reading,
    Reason,
)
from .pumps import PumpGroup
from .valves import ValveGateway

TARGET_KEY = "clearwell:level:target"
INTEGRAL_KEY = "clearwell:level:integral"
LAST_LEVEL_KEY = "clearwell:level:last-measured"
MODE_KEY = "clearwell:level:mode"
INLET_FLOW_KEY = "clearwell:level:inlet-flow"
OUTLET_FLOW_KEY = "clearwell:level:outlet-flow"


@dataclass
class _TickContext:
    phases: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    suppressed: list[str] = field(default_factory=list)


class LevelLoop:
    def __init__(self, store: Store, config: LevelConfig | None = None) -> None:
        self._store = store
        self.config = config or LevelConfig()
        self.band = self.config.band(self._load_target())
        self.pumps = PumpGroup(self.config)
        self.valves = ValveGateway(self.config)
        self.auditor = LevelAuditor(store)
        last, present = load_float(store, LAST_LEVEL_KEY)
        if not present:
            last = self.band.target_m
            save_float(store, LAST_LEVEL_KEY, last)
        self.estimator = LevelEstimator(last)
        self._mode = ControlMode(load_mode(store))
        self._integral, _ = load_float(store, INTEGRAL_KEY)
        self._last_level = last
        self._volatility_prev: float | None = None
        # Re-adopt the last commanded valve openings so a service restart does
        # not look to the travel limiter like a cold 0% -> target slam.
        inlet_flow, inlet_present = load_float(store, INLET_FLOW_KEY)
        outlet_flow, outlet_present = load_float(store, OUTLET_FLOW_KEY)
        if inlet_present and outlet_present:
            self.adopt_field(inlet_flow, outlet_flow)
            self._cold_start = False
        else:
            # No persisted valve position: adopt the very first meter reading
            # as the field baseline so a freshly commissioned loop (or one that
            # lost its state) does not slam the valves from a cold 0%.
            self._cold_start = True

    # -- configuration -----------------------------------------------------

    def _load_target(self) -> float:
        value, present = load_float(self._store, TARGET_KEY)
        return value if present else self.config.default_target_m

    def set_target(self, target_m: float) -> float:
        """Shift usage may move the target, never the fixed interlocks."""

        target = self.config.validate_target(target_m)
        save_float(self._store, TARGET_KEY, target)
        self.band = self.config.band(target)
        return target

    def adopt_field(self, inlet_flow_m3h: float, outlet_flow_m3h: float) -> None:
        """Align the valve gateway with measured field flows without commands.

        Called at startup or after field feedback so the travel limiter starts
        from the actual opening instead of cold 0%.
        """

        self.valves.adopt("inlet", inlet_flow_m3h)
        self.valves.adopt("outlet", outlet_flow_m3h)

    @property
    def mode(self) -> ControlMode:
        return self._mode

    # -- the four-phase tick ----------------------------------------------

    def tick(self, reading: Reading) -> Decision:
        ctx = _TickContext()

        # Phase 1: read inputs and resolve the level we are allowed to act on.
        ctx.phases.append(Phase.READ_INPUTS.value)
        level_used, measured_ok, uncertainty, safe_hold = self._read_inputs(reading, ctx)
        level_raw = reading.level.level_m if reading.level.ok else level_used

        if self._cold_start and reading.flow.inflow_ok and reading.flow.outflow_ok:
            # Treat the first meter pair as the existing field baseline.
            self.adopt_field(reading.flow.inflow_m3h, reading.flow.outflow_m3h)
            self._cold_start = False
            ctx.notes.append("cold start: adopted current valve/flow baseline")

        # Phase 2: confirm pump group state BEFORE computing valve moves.
        # This is the only confirm() call in the tick: it advances the
        # STARTING/STOPPING timers, so calling it more than once would speed
        # the pump sequencer up. The result is threaded through every branch.
        ctx.phases.append(Phase.CONFIRM_PUMPS.value)
        selection = self.pumps.confirm()
        demand = (
            reading.outlet_demand_m3h
            if reading.outlet_demand_m3h is not None
            else reading.flow.outflow_m3h
        )
        volatile = self._is_volatile(level_used, measured_ok)

        # Phase 3: decide valve (and possibly pump) actions.
        ctx.phases.append(Phase.DECIDE_VALVES.value)
        decision = self._decide(
            reading=reading,
            selection=selection,
            level_raw=level_raw,
            level_used=level_used,
            measured_ok=measured_ok,
            uncertainty=uncertainty,
            safe_hold=safe_hold,
            demand=demand,
            volatile=volatile,
            ctx=ctx,
        )

        # Phase 4: dispatch already happened through the idempotent gateways;
        # persist loop state and append the single atomic audit record.
        ctx.phases.append(Phase.DISPATCH.value)
        decision.phase_log.extend(ctx.phases)
        decision.suppressed.extend(ctx.suppressed)
        decision.notes.extend(ctx.notes)
        self._persist(level_used, measured_ok)
        entry = self.auditor.append(decision.as_dict())
        return replace(decision, seq=entry.seq, at=entry.at)

    # -- phase helpers -----------------------------------------------------

    def _read_inputs(
        self, reading: Reading, ctx: _TickContext
    ) -> tuple[float, bool, float, bool]:
        net = reading.flow.imbalance_m3h
        if reading.level.ok and not reading.level.stale:
            if self._mode is not ControlMode.NORMAL:
                ctx.notes.append("level transmitter recovered; bumpless re-adopt")
            level_used, uncertainty, _ = self.estimator.adopt_measurement(reading.level)
            self._mode = ControlMode.NORMAL
            save_mode(self._store, self._mode)
            return level_used, True, uncertainty, False

        # Partial failure: level unreadable/stale. Continue conservatively.
        if self._mode is ControlMode.NORMAL:
            ctx.notes.append("level transmitter unreadable: partial failure, conservative mode")
        self._mode = ControlMode.DEGRADED
        save_mode(self._store, self._mode)
        level_used, uncertainty, safe_hold = self.estimator.integrate(
            reading.level, net, self.config.area_m2, reading.tick_s
        )
        if safe_hold:
            self._mode = ControlMode.SAFE_HOLD
            save_mode(self._store, self._mode)
            ctx.notes.append("estimate uncertainty exhausted: safe hold")
        return level_used, False, uncertainty, safe_hold

    def _decide(
        self,
        *,
        reading: Reading,
        selection,
        level_raw: float,
        level_used: float,
        measured_ok: bool,
        uncertainty: float,
        safe_hold: bool,
        demand: float,
        volatile: bool,
        ctx: _TickContext,
    ) -> Decision:
        band = self.band
        inflow_now = reading.flow.inflow_m3h if reading.flow.inflow_ok else 0.0
        outflow_now = reading.flow.outflow_m3h if reading.flow.outflow_ok else 0.0
        current_net = inflow_now - outflow_now

        base = dict(
            seq=0,
            at="",
            phase_log=[],
            level_m=level_raw if measured_ok else level_used,
            level_used_m=level_used,
            level_estimated=not measured_ok,
            uncertainty_m=uncertainty,
            volume_m3=self.config.volume_for_level(level_used),
            target_m=band.target_m,
            inflow_m3h=inflow_now,
            outflow_m3h=outflow_now,
            net_inflow_m3h=current_net,
            desired_net_m3h=0.0,
            slew_budget_m3h=0.0,
            pump_group=list(selection.statuses),
        )

        # Contradictory trips (both HH and LL) cannot be a real level: freeze.
        if level_used >= band.hh_m and level_used <= band.ll_m:
            return self._build(base, ControlMode.SAFE_HOLD, Action.SAFE_HOLD, Reason.BOTH_TRIPS, ctx)

        # Hard interlocks always win, even on an estimated level (conservative).
        if level_used >= band.hh_m:
            return self._trip(
                base, selection, reading, high=True, inflow_now=inflow_now,
                outflow_now=outflow_now, ctx=ctx,
            )
        if level_used <= band.ll_m:
            return self._trip(
                base, selection, reading, high=False, inflow_now=inflow_now,
                outflow_now=outflow_now, ctx=ctx,
            )

        if safe_hold:
            return self._build(
                base, ControlMode.SAFE_HOLD, Action.SAFE_HOLD, Reason.UNCERTAINTY_HOLD, ctx
            )

        if selection.transition:
            return self._transition(
                base,
                selection=selection,
                reading=reading,
                current_net=current_net,
                inflow_now=inflow_now,
                outflow_now=outflow_now,
                demand=demand,
                volatile=volatile,
                ctx=ctx,
            )

        if not measured_ok:
            return self._degraded(
                base,
                selection=selection,
                reading=reading,
                level_used=level_used,
                current_net=current_net,
                inflow_now=inflow_now,
                outflow_now=outflow_now,
                demand=demand,
                volatile=volatile,
                ctx=ctx,
            )

        return self._normal(
            base,
            selection=selection,
            reading=reading,
            level_used=level_used,
            current_net=current_net,
            inflow_now=inflow_now,
            outflow_now=outflow_now,
            demand=demand,
            volatile=volatile,
            ctx=ctx,
        )

    # -- operating modes ---------------------------------------------------

    def _normal(
        self,
        base: dict,
        *,
        selection,
        reading: Reading,
        level_used: float,
        current_net: float,
        inflow_now: float,
        outflow_now: float,
        demand: float,
        volatile: bool,
        ctx: _TickContext,
    ) -> Decision:
        desired, level_in_deadband = balance.required_net_inflow(
            level_m=level_used,
            target_m=self.band.target_m,
            integral_m=self._integral,
            current_net_m3h=current_net,
            area_m2=self.config.area_m2,
            degraded=False,
        )
        max_inflow, min_outflow, zone, zone_reason = balance.zone_flow_clamps(
            level_m=level_used,
            band=self.band,
            config=self.config,
            inflow_now=inflow_now,
            outflow_now=outflow_now,
        )
        in_protection_zone = zone is not None
        slew, slew_reason = self.config.slew_budget(
            trip=False, protection=in_protection_zone, volatile=volatile
        )

        result = balance.allocate(
            current_net_m3h=current_net,
            desired_net_m3h=desired,
            slew_budget_m3h=slew,
            inflow_now=inflow_now,
            outflow_now=outflow_now,
            demand_m3h=demand,
            max_inflow=max_inflow,
            min_outflow=min_outflow,
            pump_min_outflow=selection.min_outflow_m3h,
            pump_max_outflow=selection.max_outflow_m3h,
            degraded=False,
            volatile=volatile,
        )

        # Always dispatch: the gateway suppresses identical commands, so a
        # settled loop sends nothing. Label HOLD afterwards only when no move
        # was actually required. This lets the outlet keep travelling toward
        # demand after a pump reaches full flow even while the level is happy.
        commands = self._dispatch_flows(result, zone_reason or result.reason, ctx)
        reason = zone_reason or result.reason
        action = Action.PROTECT_HIGH if zone == "high" else (
            Action.PROTECT_LOW if zone == "low" else Action.REGULATE
        )
        outlet_caught_up = abs(result.outlet_flow_m3h - demand) <= balance.BALANCE_TOL
        flows_balanced = abs(result.net_command_m3h) <= balance.BALANCE_TOL
        if (
            level_in_deadband
            and zone is None
            and flows_balanced
            and outlet_caught_up
            and not commands
        ):
            reason = Reason.AT_TARGET
            action = Action.HOLD

        # Pump capacity is evaluated every tick (not gated by the level
        # deadband): demand exceeding the running envelope must start a pump
        # before the level sags, even while the level sits on target.
        pump_id, pump_action, preason = self.pumps.request(
            residual_m3h=result.residual_m3h,
            current_net_m3h=current_net,
            outlet_demand_m3h=demand,
            delivered_outflow_m3h=outflow_now,
            pump_min_outflow=selection.min_outflow_m3h,
            pump_max_outflow=selection.max_outflow_m3h,
            outlet_valve_flow_m3h=result.outlet_flow_m3h,
            allow_start=True,
            level_in_high_zone=zone == "high",
            level_in_low_zone=zone == "low",
        )
        pump_command = None
        if pump_action is not PumpAction.NONE and pump_id:
            if self.pumps.apply(pump_id, pump_action):
                pump_command = (pump_id, pump_action, preason)
            else:
                ctx.suppressed.append(f"pump {pump_id} {pump_action.value} already in progress")
        if result.trim_capped:
            ctx.notes.append(
                f"outlet trim capped by pump envelope; residual={result.residual_m3h:.2f} m3/h"
            )

        if volatile:
            ctx.notes.append("level swing detected: slew speed suppressed, limits unchanged")
        if result.slew_clamped:
            ctx.notes.append("adjustment limited by tank-volume slew budget")

        return self._build(
            base,
            ControlMode.NORMAL,
            action,
            reason,
            ctx,
            desired_net=result.desired_net_m3h,
            slew=slew,
            valve_commands=commands,
            pump_command=pump_command,
        )

    def _degraded(
        self,
        base: dict,
        *,
        selection,
        reading: Reading,
        level_used: float,
        current_net: float,
        inflow_now: float,
        outflow_now: float,
        demand: float,
        volatile: bool,
        ctx: _TickContext,
    ) -> Decision:
        """Conservative partial-failure control.

        Balance flows with half-gain regulation on the estimated level, trim
        the outlet only within a tiny envelope, and never auto-start a pump.
        """

        # In degraded mode we always balance flows from the meter difference
        # plus a half-gain nudge on the estimated level; auto pump start/stop
        # stays inhibited.
        desired, _ = balance.required_net_inflow(
            level_m=level_used,
            target_m=self.band.target_m,
            integral_m=self._integral,
            current_net_m3h=current_net,
            area_m2=self.config.area_m2,
            degraded=True,
        )
        slew, _ = self.config.slew_budget(trip=False, protection=False, volatile=True)
        result = balance.allocate(
            current_net_m3h=current_net,
            desired_net_m3h=desired,
            slew_budget_m3h=slew,
            inflow_now=inflow_now,
            outflow_now=outflow_now,
            demand_m3h=demand,
            max_inflow=self.config.inlet_max_m3h,
            min_outflow=0.0,
            pump_min_outflow=selection.min_outflow_m3h,
            pump_max_outflow=selection.max_outflow_m3h,
            degraded=True,
            volatile=True,
        )
        commands = self._dispatch_flows(result, Reason.LEVEL_FAULT, ctx)
        ctx.notes.append("auto pump start/stop inhibited while level is estimated")
        return self._build(
            base,
            ControlMode.DEGRADED,
            Action.DEGRADED_BALANCE,
            Reason.LEVEL_FAULT,
            ctx,
            desired_net=result.desired_net_m3h,
            slew=slew,
            valve_commands=commands,
        )

    def _transition(
        self,
        base: dict,
        *,
        selection,
        reading: Reading,
        current_net: float,
        inflow_now: float,
        outflow_now: float,
        demand: float,
        volatile: bool,
        ctx: _TickContext,
    ) -> Decision:
        """Freeze level regulation; one-shot feed-forward while pumps ramp.

        The outlet moves toward demand inside the envelope the group will have
        once the ramp completes, and the inlet tracks the outlet (net 0) so the
        well neither fills nor drains. Re-ticks recompute the same targets
        (deterministic); the pre-bias is announced in the audit only once.
        """

        slew, _ = self.config.slew_budget(trip=False, protection=False, volatile=volatile)
        starting = selection.transition_action is PumpAction.START
        prospective_count = selection.running_count + (1 if starting else 0)
        prospective_min, prospective_max = self.config.pump_capacity(prospective_count)
        ff_reason = Reason.PUMP_START_FF if starting else Reason.PUMP_STOP_FF
        if selection.feedforward_needed:
            self.pumps.mark_feedforward_applied()
            ctx.notes.append(
                f"pump {selection.transition_pump_id} {selection.transition_action.value}: "
                "feed-forward pre-bias applied once"
            )
        inlet_flow, outlet_flow, target_net = balance.feedforward_flows(
            inflow_now=inflow_now,
            outflow_now=outflow_now,
            demand_m3h=demand,
            prospective_pump_max=prospective_max,
            prospective_pump_min=prospective_min,
            starting=starting,
            slew_budget_m3h=slew,
            config=self.config,
        )
        dispatches = (
            self.valves.dispatch("inlet", inlet_flow, ff_reason),
            self.valves.dispatch("outlet", outlet_flow, ff_reason),
        )
        commands = [d.command for d in dispatches if d.command and d.sent]
        for dispatch in dispatches:
            if dispatch.travel_limited:
                ctx.suppressed.append(Reason.TRAVEL_LIMITED.value)
        return self._build(
            base,
            ControlMode.NORMAL,
            Action.PUMP_TRANSITION,
            ff_reason,
            ctx,
            desired_net=target_net,
            slew=slew,
            valve_commands=commands,
        )

    def _trip(
        self,
        base: dict,
        selection,
        reading: Reading,
        *,
        high: bool,
        inflow_now: float,
        outflow_now: float,
        ctx: _TickContext,
    ) -> Decision:
        """Hard protection outside HH/LL. Fast, safe-direction override."""

        inlet_flow, outlet_flow = balance.trip_flows(
            high=high,
            config=self.config,
            inflow_now=inflow_now,
            outflow_now=outflow_now,
            pump_max_outflow=selection.max_outflow_m3h,
        )
        slew = self.config.trip_slew_m3h()
        in_dispatch = self.valves.dispatch(
            "inlet", inlet_flow, Reason.HH_TRIP if high else Reason.LL_TRIP, allow_fast=True
        )
        out_dispatch = self.valves.dispatch(
            "outlet", outlet_flow, Reason.HH_TRIP if high else Reason.LL_TRIP, allow_fast=True
        )
        commands = [d.command for d in (in_dispatch, out_dispatch) if d.command and d.sent]
        for dispatch in (in_dispatch, out_dispatch):
            if dispatch.travel_limited:
                ctx.suppressed.append(Reason.TRAVEL_LIMITED.value)

        pump_command = None
        if high and selection.running_count < self.config.pump_count:
            # Hard HH: force the next pump on regardless of debounce, provided
            # one is available. The outlet interlock valve is already open.
            stopped = [
                pump_id
                for pump_id in self.pumps.pump_ids
                if self.pumps.states[pump_id] is PumpState.STOPPED
                and self.pumps.available[pump_id]
            ]
            if stopped:
                pump_id = sorted(stopped)[0]
                # Reset the debounce counter so this forced edge commits once.
                self.pumps._start_votes = REQUEST_DEBOUNCE_TICKS
                if self.pumps.apply(pump_id, PumpAction.START):
                    pump_command = (pump_id, PumpAction.START, Reason.HH_TRIP)
        if not high:
            pump_id, pump_action, preason = self.pumps.force_stop_lowest()
            if pump_id and self.pumps.apply(pump_id, pump_action):
                pump_command = (pump_id, pump_action, preason)

        action = Action.PROTECT_HIGH if high else Action.PROTECT_LOW
        reason = Reason.HH_TRIP if high else Reason.LL_TRIP
        ctx.notes.append("hard interlock override: fast move toward safety")
        return self._build(
            base,
            self._mode if self._mode is ControlMode.DEGRADED else ControlMode.NORMAL,
            action,
            reason,
            ctx,
            desired_net=inlet_flow - outlet_flow,
            slew=slew,
            valve_commands=commands,
            pump_command=pump_command,
        )

    # -- dispatch / build helpers ------------------------------------------

    def _dispatch_flows(self, result: balance.BalanceResult, reason: Reason, ctx: _TickContext):
        dispatches = (
            self.valves.dispatch("inlet", result.inlet_flow_m3h, reason),
            self.valves.dispatch("outlet", result.outlet_flow_m3h, reason),
        )
        commands = []
        for dispatch in dispatches:
            if dispatch.command and dispatch.sent:
                commands.append(dispatch.command)
            if dispatch.travel_limited:
                ctx.suppressed.append(Reason.TRAVEL_LIMITED.value)
            # IDENTICAL_COMMAND is the normal idempotent path on a repeated
            # tick; it is already implied by the command being absent.
        return commands

    def _build(
        self,
        base: dict,
        mode: ControlMode,
        action: Action,
        reason: Reason,
        ctx: _TickContext,
        *,
        desired_net: float = 0.0,
        slew: float = 0.0,
        valve_commands: list | None = None,
        pump_command: tuple[str, PumpAction, Reason] | None = None,
    ) -> Decision:
        pump_commands = []
        if pump_command is not None:
            from .model import PumpCommand

            pump_id, pump_action, preason = pump_command
            pump_commands.append(PumpCommand(pump_id, pump_action, preason))
        return Decision(
            seq=int(base["seq"]),
            at=str(base["at"]),
            phase_log=[],
            mode=mode,
            action=action,
            reason=reason,
            level_m=base["level_m"],
            level_used_m=base["level_used_m"],
            level_estimated=base["level_estimated"],
            uncertainty_m=base["uncertainty_m"],
            volume_m3=base["volume_m3"],
            target_m=base["target_m"],
            inflow_m3h=base["inflow_m3h"],
            outflow_m3h=base["outflow_m3h"],
            net_inflow_m3h=base["net_inflow_m3h"],
            desired_net_m3h=desired_net,
            slew_budget_m3h=slew,
            valves=list(valve_commands or []),
            pumps=pump_commands,
            pump_group=base["pump_group"],
            suppressed=[],
            notes=[],
        )

    # -- state bookkeeping --------------------------------------------------

    def _is_volatile(self, level_used: float, measured_ok: bool) -> bool:
        if not measured_ok or self._volatility_prev is None:
            self._volatility_prev = level_used
            return False
        delta = abs(level_used - self._volatility_prev)
        self._volatility_prev = level_used
        return delta >= self.config.volatility_delta_m()

    def _persist(self, level_used: float, measured_ok: bool) -> None:
        # Anti-windup: clamp the integrator to a fraction of the level span and
        # freeze it entirely while estimating or during big swings.
        if measured_ok:
            error = self.band.target_m - level_used
            limit = self.config.span_m * INTEGRAL_CLAMP_FRACTION
            self._integral = clamp(self._integral + error, -limit, limit)
            self._last_level = level_used
            save_float(self._store, LAST_LEVEL_KEY, level_used)
        save_float(self._store, INTEGRAL_KEY, self._integral)
        save_float(self._store, INLET_FLOW_KEY, self.valves.inlet.flow_for_pct(self.valves.inlet.position_pct))
        save_float(self._store, OUTLET_FLOW_KEY, self.valves.outlet.flow_for_pct(self.valves.outlet.position_pct))

    def status(self) -> dict[str, object]:
        band = self.band
        return {
            "mode": self._mode.value,
            "band": band.as_dict(),
            "effective_volume_m3": self.config.effective_volume_m3,
            "last_measured_level_m": self._last_level,
            "estimated_level_m": self.estimator.level_m,
            "uncertainty_m": self.estimator.uncertainty_m,
            "estimating": self.estimator.estimating,
            "integral_m": self._integral,
            "valves": {
                "inlet_pct": self.valves.inlet.position_pct,
                "outlet_pct": self.valves.outlet.position_pct,
            },
            "pump_group": [status.as_dict() for status in self.pumps.snapshot().statuses],
            "pump_transition": self.pumps.in_transition(),
            "policy": {
                "limits_basis": "fixed fractions of effective tank volume",
                "shift_can_move": "target level only, never interlocks",
                "volatility_response": "suppress slew speed, never widen limits",
            },
        }


def save_mode(store: Store, mode: ControlMode) -> None:
    store.put(MODE_KEY, mode.value)


def load_mode(store: Store) -> str:
    raw, present = store.get(MODE_KEY)
    return raw if present and raw in {m.value for m in ControlMode} else ControlMode.NORMAL.value
