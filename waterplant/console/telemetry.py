"""Runtime telemetry counters."""

from __future__ import annotations

from dataclasses import dataclass

from .runtime import Runtime


@dataclass(frozen=True)
class Telemetry:
    """A flat snapshot of the numbers an operator watches."""

    store_keys: int
    filter_beds: int
    filter_active: int
    filter_load: float
    backwash_queue: int
    backwash_drains: int
    last_drain: str
    audit_entries: int
    quota_value: float
    clearwell_level: float
    level_sensor_available: bool
    level_protection: str
    level_loop_status: str
    pump_confirmed: bool
    pump_min_flow: float
    pump_max_flow: float
    inlet_command: float
    outlet_command: float
    coag_ratio: float
    chlor_target: float
    turbidity_verdict: float
    ph_value: float
    ph_stable: bool
    schedule_due: int
    trend_samples: int
    inventory_balance: float
    inventory_alerts: int

    def as_dict(self) -> dict[str, object]:
        return {
            "store_keys": self.store_keys,
            "filter_beds": self.filter_beds,
            "filter_active": self.filter_active,
            "filter_load": self.filter_load,
            "backwash_queue": self.backwash_queue,
            "backwash_drains": self.backwash_drains,
            "last_drain": self.last_drain,
            "audit_entries": self.audit_entries,
            "quota_value": self.quota_value,
            "clearwell_level": self.clearwell_level,
            "level_sensor_available": self.level_sensor_available,
            "level_protection": self.level_protection,
            "level_loop_status": self.level_loop_status,
            "pump_confirmed": self.pump_confirmed,
            "pump_min_flow": self.pump_min_flow,
            "pump_max_flow": self.pump_max_flow,
            "inlet_command": self.inlet_command,
            "outlet_command": self.outlet_command,
            "coag_ratio": self.coag_ratio,
            "chlor_target": self.chlor_target,
            "turbidity_verdict": self.turbidity_verdict,
            "ph_value": self.ph_value,
            "ph_stable": self.ph_stable,
            "schedule_due": self.schedule_due,
            "trend_samples": self.trend_samples,
            "inventory_balance": self.inventory_balance,
            "inventory_alerts": self.inventory_alerts,
        }


def collect(rt: Runtime) -> Telemetry:
    """Read every counter without mutating the control state."""

    loop = rt.level_loop.state()
    last_decision = loop.get("last_decision")
    if not isinstance(last_decision, dict):
        last_decision = {}
    inlet = last_decision.get("inlet_command", {})
    outlet = last_decision.get("outlet_command", {})
    pumps = loop.get("pumps", {})

    return Telemetry(
        store_keys=rt.store.count(),
        filter_beds=rt.bank.count(),
        filter_active=rt.bank.active_count(),
        filter_load=rt.bank.total_load(),
        backwash_queue=rt.backwash.pending_count(),
        backwash_drains=rt.backwash.drain_count(),
        last_drain=rt.backwash.last_drain() or "",
        audit_entries=rt.auditor.count(),
        quota_value=rt.accumulator.value(),
        clearwell_level=rt.well.level(),
        level_sensor_available=bool(loop.get("sensor_available", False)),
        level_protection=str(last_decision.get("protection", "none")),
        level_loop_status=str(last_decision.get("status", "idle")),
        pump_confirmed=bool(pumps.get("confirmed", False)),
        pump_min_flow=float(pumps.get("min_flow", 0.0)),
        pump_max_flow=float(pumps.get("max_flow", 0.0)),
        inlet_command=float(inlet.get("flow", 0.0)) if isinstance(inlet, dict) else 0.0,
        outlet_command=float(outlet.get("flow", 0.0)) if isinstance(outlet, dict) else 0.0,
        coag_ratio=rt.coag_doser.current_ratio(),
        chlor_target=rt.chlor_doser.current_target(),
        turbidity_verdict=rt.sampler.state().last_verdict,
        ph_value=rt.stabilizer.current(),
        ph_stable=rt.stabilizer.is_stable(),
        schedule_due=len(rt.scheduler.due(rt.bank)),
        trend_samples=rt.trend.stats().samples,
        inventory_balance=rt.inventory.balance(),
        inventory_alerts=len(rt.inventory.needs_reorder()),
    )
