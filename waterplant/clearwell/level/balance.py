"""Mass balance maths: required adjustment and inlet-first allocation.

All functions are pure so the policy can be unit tested without pumps, valves
or the store. Volumes are m3, flows are m3/h.

Sign convention used everywhere: net inflow ``Q_in - Q_out`` is positive when
the well is filling. The allocation residual is (achieved net) - (commanded
net): positive means the inlet is at capacity and the group could shed a
pump's worth of outflow; negative means outflow the running pumps cannot
deliver is required, i.e. another pump is needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .config import (
    DEADBAND_M,
    DEGRADED_GAIN_FRACTION,
    KI,
    KP,
    LevelConfig,
    clamp,
)
from .model import Reason

if TYPE_CHECKING:  # pragma: no cover - imported only for type checkers
    from .config import LevelBand

BALANCE_TOL = 1e-6


@dataclass(frozen=True)
class BalanceResult:
    """How the required net inflow is split across the two valves."""

    desired_net_m3h: float
    net_command_m3h: float
    inlet_flow_m3h: float
    outlet_flow_m3h: float
    residual_m3h: float
    slew_clamped: bool
    trim_capped: bool
    reason: Reason

    @property
    def need_more_outflow(self) -> bool:
        return self.residual_m3h < -BALANCE_TOL

    @property
    def need_less_outflow(self) -> bool:
        return self.residual_m3h > BALANCE_TOL


def required_net_inflow(
    *,
    level_m: float,
    target_m: float,
    integral_m: float,
    current_net_m3h: float,
    area_m2: float,
    degraded: bool,
) -> tuple[float, bool]:
    """Required net inflow: flow-balance feed-forward plus a PI level term.

    * **balance feed-forward** — demand ``-current_net``: whatever outflow is
      running, the inlet is asked to match it. A pump switching on appears in
      the flow difference before the level moves, so the inlet follows at once;
    * **level feedback** — a PI correction (gain halved on an estimated level)
      that restores the target level and clears standing error.

    Returns (desired_net_m3h, in_deadband). The deadband means "level on
    target *and* flows already balanced".
    """

    error_m = target_m - level_m
    level_in_deadband = abs(error_m) <= DEADBAND_M
    if level_in_deadband:
        return -current_net_m3h, True
    gain = KP * (DEGRADED_GAIN_FRACTION if degraded else 1.0)
    level_term = (gain * error_m + KI * integral_m) * area_m2
    return -current_net_m3h + level_term, False


def zone_flow_clamps(
    *,
    level_m: float,
    band: "LevelBand",
    config: LevelConfig,
    inflow_now: float,
    outflow_now: float,
) -> tuple[float, float, str | None, Reason | None]:
    """Return (max_inflow, min_outflow, zone, reason) near the limits.

    Near H the inflow is squeezed in proportion to the remaining headroom;
    near L the outflow is squeezed in proportion to remaining depth. Outside
    HH/LL the caller trips first.
    """

    max_inflow = config.inlet_max_m3h
    min_outflow = 0.0
    zone: str | None = None
    reason: Reason | None = None
    if band.h_m <= level_m < band.hh_m:
        zone = "high"
        reason = Reason.HIGH_LEVEL_CLAMP
        headroom = clamp((band.hh_m - level_m) / max(band.hh_m - band.h_m, 1e-9), 0.0, 1.0)
        max_inflow = inflow_now * headroom
    elif band.ll_m < level_m <= band.l_m:
        zone = "low"
        reason = Reason.LOW_LEVEL_CLAMP
        depth = clamp((level_m - band.ll_m) / max(band.l_m - band.ll_m, 1e-9), 0.0, 1.0)
        min_outflow = outflow_now * depth
    return max(0.0, max_inflow), min_outflow, zone, reason


def allocate(
    *,
    current_net_m3h: float,
    desired_net_m3h: float,
    slew_budget_m3h: float,
    inflow_now: float,
    outflow_now: float,
    demand_m3h: float,
    max_inflow: float,
    min_outflow: float,
    pump_min_outflow: float,
    pump_max_outflow: float,
    degraded: bool,
    volatile: bool,
) -> BalanceResult:
    """Split the required move with the inlet as primary level actuator.

    Responsibilities:

    * the **outlet valve** only trims: it creeps toward process demand inside a
      small per-tick envelope and never beyond what the running pumps carry;
    * the **inlet valve** does the balancing and level correction. Once the
      outlet picks its trim, the inlet is set to ``outlet + net_command`` so
      the mass balance is delivered on the primary actuator;
    * the net change per tick is limited by the tank-volume slew budget;
    * anything the inlet cannot cover (at capacity/closed) is the residual
      that drives the debounced pump start/stop sequencer.
    """

    wanted_change = desired_net_m3h - current_net_m3h
    slew_clamped = abs(wanted_change) > slew_budget_m3h + BALANCE_TOL
    if slew_clamped:
        wanted_change = clamp(wanted_change, -slew_budget_m3h, slew_budget_m3h)
    net_command = current_net_m3h + wanted_change

    # Outlet: pump-envelope-limited tracking toward demand. The valve gateway
    # applies the physical per-tick travel limit (so valves always ramp, never
    # slam); the net-flow slew budget is not applied here because the outlet is
    # only tracking demand, not driving the level. The inlet tracks it
    # one-for-one below, so the net stays on the level target. Degraded mode
    # and volatile ticks deliberately under-shoot demand (conservative trim).
    outlet_want = min(demand_m3h, pump_max_outflow)
    if degraded:
        outlet_want = outflow_now + (outlet_want - outflow_now) * DEGRADED_GAIN_FRACTION
    if volatile:
        outlet_want = outflow_now + (outlet_want - outflow_now) * 0.5
    low_out = max(min_outflow, pump_min_outflow)
    high_out = pump_max_outflow
    outlet_target = clamp(outlet_want, low_out, high_out)

    # Inlet: balance + level correction on the primary actuator. Its speed is
    # what the tank-volume slew budget governs, measured as a per-tick change
    # of the commanded flow from where it sits now.
    inlet_target = clamp(net_command + outlet_target, 0.0, max_inflow)
    inlet_target = clamp(
        inlet_target,
        max(0.0, inflow_now - slew_budget_m3h),
        min(max_inflow, inflow_now + slew_budget_m3h),
    )
    residual = (inlet_target - outlet_target) - net_command
    trim_capped = abs(residual) > BALANCE_TOL

    reason = Reason.OUTLET_TRIM_CAPPED if trim_capped else Reason.MASS_BALANCE
    return BalanceResult(
        desired_net_m3h=desired_net_m3h,
        net_command_m3h=net_command,
        inlet_flow_m3h=inlet_target,
        outlet_flow_m3h=outlet_target,
        residual_m3h=residual,
        slew_clamped=slew_clamped,
        trim_capped=trim_capped,
        reason=reason,
    )


def trip_flows(
    *,
    high: bool,
    config: LevelConfig,
    inflow_now: float,
    outflow_now: float,
    pump_max_outflow: float,
) -> tuple[float, float]:
    """Hard override flows outside HH/LL: shut one valve, open the other."""

    if high:
        return 0.0, min(config.outlet_max_m3h, max(pump_max_outflow, outflow_now))
    return min(config.inlet_max_m3h, max(inflow_now, config.inlet_max_m3h)), 0.0


def feedforward_flows(
    *,
    inflow_now: float,
    outflow_now: float,
    demand_m3h: float,
    prospective_pump_max: float,
    prospective_pump_min: float,
    starting: bool,
    slew_budget_m3h: float,
    config: LevelConfig,
) -> tuple[float, float, float]:
    """Inlet/outlet targets while a pump ramps.

    The level loop is frozen (net target 0). The outlet moves toward demand
    inside the envelope the group will have once the ramp completes, and the
    inlet tracks the outlet so the well neither fills nor drains. Moves are
    still slew limited. Returns (inlet_target, outlet_target, net_target=0).
    """

    if starting:
        outlet_target = clamp(demand_m3h, outflow_now, prospective_pump_max)
    else:
        outlet_target = clamp(demand_m3h, prospective_pump_min, outflow_now)
    outlet_target = clamp(
        outlet_target,
        outflow_now - slew_budget_m3h,
        outflow_now + slew_budget_m3h,
    )
    outlet_target = clamp(outlet_target, 0.0, config.outlet_max_m3h)
    inlet_target = clamp(outlet_target, 0.0, config.inlet_max_m3h)
    return inlet_target, outlet_target, 0.0
