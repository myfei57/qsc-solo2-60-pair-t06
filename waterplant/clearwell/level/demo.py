"""Deterministic clear-well plant used to demonstrate the level loop offline.

This is a tiny numeric plant, not part of the control logic: one rectangular
well, two linear valves and a pump group whose STARTING/STOPPING states ramp
their delivered flow. It is deliberately free of any console/store imports.

Physical coupling per tick:

* inflow   = inlet valve opening * inlet source capacity
* outflow  = min(outlet valve opening, demand, flow the running pumps deliver)
* level   += (inflow - outflow) * dt / footprint

So a demand surge first exhausts the outlet trim envelope; the debounced
pump sequencer then starts another pump, and the inlet follows to balance the
well. A level-transmitter outage simply feeds ``ok=False`` to the loop.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import LevelConfig
from .controller import LevelLoop
from .model import FlowReading, LevelReading, PumpState, Reading

# First-order lag of physical flows behind valve/pump commands, so the demo
# does not introduce an artificial zero-inertia pure delay.
FLOW_TAU = 0.4


@dataclass
class PlantState:
    level_m: float
    inflow_m3h: float
    outflow_m3h: float
    level_ok: bool = True
    demand_m3h: float = 0.0


class ScriptedPlant:
    """Integrates the well and feeds readings to the loop each tick."""

    def __init__(self, config: LevelConfig, level_m: float | None = None) -> None:
        self.config = config
        self.state = PlantState(
            level_m=config.default_target_m if level_m is None else level_m,
            inflow_m3h=config.pump_unit_m3h,
            outflow_m3h=config.pump_unit_m3h,
            level_ok=True,
            demand_m3h=config.pump_unit_m3h,
        )
        self._pump_flow: dict[str, float] = {
            pump_id: (config.pump_unit_m3h if i == 0 else 0.0)
            for i, pump_id in enumerate([f"P{i + 1}" for i in range(config.pump_count)])
        }
        self._adopted = False

    def step(self, loop: LevelLoop, *, level_ok: bool, demand_m3h: float, tick_s: float) -> dict:
        st = self.state
        if not self._adopted:
            loop.adopt_field(st.inflow_m3h, st.outflow_m3h)
            self._adopted = True
        st.level_ok = level_ok
        st.demand_m3h = demand_m3h

        decision = loop.tick(
            Reading(
                level=LevelReading(st.level_m, ok=level_ok),
                flow=FlowReading(st.inflow_m3h, st.outflow_m3h),
                outlet_demand_m3h=demand_m3h,
                tick_s=tick_s,
            )
        )

        inlet_cmd = st.inflow_m3h
        outlet_cmd = st.outflow_m3h
        for command in decision.valves:
            if command.valve == "inlet":
                inlet_cmd = command.flow_m3h
            else:
                outlet_cmd = command.flow_m3h

        # Ramp delivered pump flow according to the confirmed group state.
        selection = loop.pumps.snapshot()
        ramp = self.config.pump_unit_m3h / 3.0
        delivered = 0.0
        for status in selection.statuses:
            current = self._pump_flow.get(status.pump_id, 0.0)
            if status.state is PumpState.RUNNING:
                target = self.config.pump_unit_m3h
            elif status.state is PumpState.STARTING:
                target = min(current + ramp, self.config.pump_unit_m3h)
            elif status.state is PumpState.STOPPING:
                target = max(current - ramp, 0.0)
            else:
                target = 0.0
            self._pump_flow[status.pump_id] = target
            delivered += target

        st.inflow_m3h += (inlet_cmd - st.inflow_m3h) * FLOW_TAU
        target_outflow = max(0.0, min(outlet_cmd, demand_m3h, delivered))
        st.outflow_m3h += (target_outflow - st.outflow_m3h) * FLOW_TAU

        net = st.inflow_m3h - st.outflow_m3h
        st.level_m = min(
            self.config.max_m,
            max(0.0, st.level_m + net * (tick_s / 3600.0) / self.config.area_m2),
        )
        record = decision.as_dict()
        record["plant_inflow_m3h"] = round(st.inflow_m3h, 3)
        record["plant_outflow_m3h"] = round(st.outflow_m3h, 3)
        record["plant_level_m"] = round(st.level_m, 4)
        return record
