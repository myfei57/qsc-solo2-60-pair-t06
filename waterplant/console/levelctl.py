"""Console endpoints for the clear well level closed loop."""

from __future__ import annotations

from waterplant.clearwell import ScriptedPlant

from .http import Request, Response, json_response
from .runtime import Runtime
from waterplant.clearwell import FlowReading, LevelReading, Reading


def _reading_from_payload(payload: dict[str, object]) -> Reading:
    def number(name: str, default: float) -> float:
        value = payload.get(name, default)
        return float(value)  # Request validation happens at handler boundary.

    level_present = "level" in payload
    level_ok = bool(payload.get("level_ok", True))
    level_stale = bool(payload.get("level_stale", False))
    level = LevelReading(
        level_m=number("level", 0.0), ok=level_present and level_ok, stale=level_stale
    )
    flow = FlowReading(
        inflow_m3h=number("inflow", 0.0),
        outflow_m3h=number("outflow", 0.0),
        inflow_ok=bool(payload.get("inflow_ok", True)),
        outflow_ok=bool(payload.get("outflow_ok", True)),
    )
    demand = payload.get("demand")
    return Reading(
        level=level,
        flow=flow,
        outlet_demand_m3h=None if demand is None else float(demand),
        tick_s=number("tick_s", 1.0),
    )


def level_state(rt: Runtime, request: Request) -> Response:
    return json_response(rt.level_loop.status())


def level_band(rt: Runtime, request: Request) -> Response:
    return json_response(
        {
            "band": rt.level_loop.band.as_dict(),
            "effective_volume_m3": rt.level_loop.config.effective_volume_m3,
        }
    )


def level_target(rt: Runtime, request: Request) -> Response:
    target = request.float_field("target")
    applied = rt.level_loop.set_target(target)
    rt.auditor.record("clearwell_level_target", f"target={applied:.4f}")
    return json_response({"target_m": applied, "band": rt.level_loop.band.as_dict()})


def level_tick(rt: Runtime, request: Request) -> Response:
    reading = _reading_from_payload(request.payload)
    decision = rt.level_loop.tick(reading)
    return json_response(decision.as_dict())


def level_audit(rt: Runtime, request: Request) -> Response:
    entries = rt.level_loop.auditor.entries()
    return json_response({"entries": [entry.as_dict() for entry in entries], "count": len(entries)})


def level_audit_verify(rt: Runtime, request: Request) -> Response:
    ok, broken_seq = rt.level_loop.auditor.verify()
    return json_response({"ok": ok, "first_broken_seq": broken_seq})


def level_demo(rt: Runtime, request: Request) -> Response:
    """Scripted scenario: balance, pump starts, outage, recovery, protection."""

    ticks = max(1, min(request.int_field("ticks", 60), 240))
    loop = rt.level_loop
    plant = ScriptedPlant(loop.config)
    scenario = [
        (6, True, loop.config.pump_unit_m3h, "balanced at target"),
        (20, True, loop.config.pump_unit_m3h * 2.4, "demand surge: trim then start pumps"),
        (18, True, loop.config.pump_unit_m3h * 0.5, "demand eases: feed-forward pump stops"),
        (5, False, loop.config.pump_unit_m3h * 0.6, "level transmitter outage: degraded"),
        (8, True, loop.config.pump_unit_m3h * 0.6, "recovered: bumpless, idempotent re-tick"),
        (10, True, loop.config.pump_unit_m3h * 0.16, "very low demand: low-level balance"),
    ]

    timeline: list[dict[str, object]] = []
    phase_left = 0
    phase_note = ""
    level_ok_flag = True
    demand = loop.config.pump_unit_m3h
    for _ in range(ticks):
        if phase_left == 0 and scenario:
            phase_left, level_ok_flag, demand, phase_note = scenario.pop(0)
        phase_left -= 1
        record = plant.step(loop, level_ok=level_ok_flag, demand_m3h=demand, tick_s=60.0)
        record["scenario"] = phase_note
        record["plant_level_m"] = round(plant.state.level_m, 4)
        timeline.append(record)

    return json_response(
        {"ticks": timeline, "final_status": loop.status(), "audit_ok": loop.auditor.verify()[0]}
    )
