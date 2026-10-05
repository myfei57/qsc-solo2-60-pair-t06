"""Closed-loop clear well level control."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from waterplant.audit import Auditor
from waterplant.store.store import Store

from .pumps import PumpGroup, PumpTelemetry
from .report import (
    DEFAULT_HORIZON_SECONDS,
    DEFAULT_INLET_MAX,
    DEFAULT_CAPACITY,
    NORMAL_FLOW_SLEW_RATIO,
    OUTLET_TRIM_RATIO,
    PROTECTION_FLOW_SLEW_RATIO,
    VOLATILE_FLOW_SLEW_RATIO,
    LevelConfig,
    validate_config,
    validate_level,
)

CONFIG_KEY = "clearwell:level-config"
STATE_KEY = "clearwell:loop-state"
HISTORY_KEY = "clearwell:loop-history"
IDEMPOTENCY_KEY = "clearwell:loop-requests"
MAX_HISTORY = 50
MAX_REQUEST_IDS = 100


@dataclass(frozen=True)
class ValveCommand:
    """A bounded valve flow command and the reason it was selected."""

    valve: str
    flow: float
    position: float
    slew_limit: float
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "valve": self.valve,
            "flow": self.flow,
            "position": self.position,
            "slew_limit": self.slew_limit,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class LevelDecision:
    """The complete, auditable result of one level-loop evaluation."""

    request_id: str
    status: str
    sequence: int
    at: str
    sensor_available: bool
    level: float | None
    last_good_level: float | None
    capacity: float
    target_level: float
    min_level: float
    high_level: float
    measured_inflow: float
    measured_outflow: float
    imbalance: float
    projected_level: float | None
    desired_net_flow: float
    volatile: bool
    protection: str
    pump_confirmed: bool
    inlet_command: ValveCommand
    outlet_command: ValveCommand
    pump_command: dict[str, object] | None
    reasons: list[str] = field(default_factory=list)
    idempotent_replay: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "status": self.status,
            "sequence": self.sequence,
            "at": self.at,
            "sensor_available": self.sensor_available,
            "level": self.level,
            "last_good_level": self.last_good_level,
            "capacity": self.capacity,
            "target_level": self.target_level,
            "min_level": self.min_level,
            "high_level": self.high_level,
            "measured_inflow": self.measured_inflow,
            "measured_outflow": self.measured_outflow,
            "imbalance": self.imbalance,
            "projected_level": self.projected_level,
            "desired_net_flow": self.desired_net_flow,
            "volatile": self.volatile,
            "protection": self.protection,
            "pump_confirmed": self.pump_confirmed,
            "inlet_command": self.inlet_command.as_dict(),
            "outlet_command": self.outlet_command.as_dict(),
            "pump_command": self.pump_command,
            "reasons": list(self.reasons),
            "idempotent_replay": self.idempotent_replay,
        }


def _load_json(store: Store, key: str, default: object) -> Any:
    raw, present = store.get(key)
    if not present:
        return default
    try:
        return json.loads(raw)
    except ValueError:
        return default


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _move_towards(current: float, target: float, limit: float) -> float:
    return _clamp(target, current - limit, current + limit)


def _non_negative(name: str, value: float) -> float:
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    if value > 1_000_000:
        raise ValueError(f"{name} exceeds the supported range")
    return value


class LevelLoop:
    """Computes valve and pump actions from level, volume and flow balance."""

    def __init__(self, store: Store, pumps: PumpGroup, auditor: Auditor) -> None:
        self._store = store
        self._pumps = pumps
        self._auditor = auditor

    def config(self) -> LevelConfig:
        payload = _load_json(self._store, CONFIG_KEY, {})
        if not isinstance(payload, dict):
            payload = {}
        return LevelConfig(
            capacity=float(payload.get("capacity", DEFAULT_CAPACITY)),
            target_level=float(payload.get("target_level", DEFAULT_CAPACITY * 0.5)),
            inlet_max=float(payload.get("inlet_max", DEFAULT_INLET_MAX)),
            horizon_seconds=float(payload.get("horizon_seconds", DEFAULT_HORIZON_SECONDS)),
        )

    def set_config(
        self,
        capacity: float | None = None,
        target_level: float | None = None,
        inlet_max: float | None = None,
        horizon_seconds: float | None = None,
    ) -> LevelConfig:
        current = self.config()
        updated = LevelConfig(
            capacity=current.capacity if capacity is None else capacity,
            target_level=current.target_level if target_level is None else target_level,
            inlet_max=current.inlet_max if inlet_max is None else inlet_max,
            horizon_seconds=current.horizon_seconds if horizon_seconds is None else horizon_seconds,
        )
        validate_config(updated)
        self._store.put(CONFIG_KEY, json.dumps(updated.as_dict(), sort_keys=True))
        return updated

    def state(self) -> dict[str, object]:
        payload = _load_json(self._store, STATE_KEY, {})
        if not isinstance(payload, dict):
            payload = {}
        return {
            "config": self.config().as_dict(),
            "pumps": self._pumps.state(),
            "sensor_available": bool(payload.get("sensor_available", False)),
            "last_good_level": payload.get("last_good_level"),
            "last_inlet_flow": payload.get("last_inlet_flow", 0.0),
            "last_outlet_flow": payload.get("last_outlet_flow", 0.0),
            "recent_levels": payload.get("recent_levels", []),
            "last_decision": payload.get("last_decision"),
        }

    def history(self) -> list[dict[str, object]]:
        payload = _load_json(self._store, HISTORY_KEY, [])
        return payload if isinstance(payload, list) else []

    def control(self, request: dict[str, Any]) -> LevelDecision:
        """Evaluate one loop tick.

        Pump telemetry is acknowledged before valve math.  A supplied
        ``request_id`` makes a retried request return the original decision.
        """

        request_id = str(request.get("request_id") or uuid.uuid4().hex)
        replay = self._replay(request_id)
        if replay is not None:
            return replay

        raw_readings = request.get("pumps", [])
        if not isinstance(raw_readings, (list, tuple)):
            raw_readings = []
        parsed_readings: list[dict[str, Any]] = []
        for item in raw_readings:
            if isinstance(item, PumpTelemetry):
                parsed_readings.append(item.as_dict())
            elif isinstance(item, dict):
                parsed_readings.append(item)
        readings = [PumpTelemetry.from_payload(item) for item in parsed_readings]
        if readings:
            self._pumps.confirm(readings)

        inflow = _non_negative("inflow", float(request.get("inflow", 0.0)))
        outflow = _non_negative("outflow", float(request.get("outflow", 0.0)))
        interval = max(0.0, float(request.get("interval", 0.0)))
        sensor_available, level = self._read_level(request)

        if not self._pumps.confirmed():
            decision = self._hold_for_pumps(
                request_id, sensor_available, level, inflow, outflow
            )
        elif not sensor_available:
            decision = self._conservative(
                request_id, level, inflow, outflow, interval
            )
        else:
            decision = self._normal(request_id, level, inflow, outflow, interval)

        self._persist(decision, request_id, sensor_available)
        if sensor_available and decision.level is not None:
            from .well import Well

            Well(self._store).set_level(decision.level)
        self._auditor.record("clearwell", json.dumps(decision.as_dict(), ensure_ascii=False))
        return decision

    def _read_level(self, request: dict[str, Any]) -> tuple[bool, float | None]:
        raw = request.get("level", None)
        available_flag = request.get("level_available")
        if available_flag is False or raw is None:
            return False, None
        try:
            level = float(raw)
        except (TypeError, ValueError):
            return False, None
        config = self.config()
        if level < 0 or level > config.capacity:
            return False, level
        validate_level(level)
        return True, level

    def _normal(
        self,
        request_id: str,
        level: float,
        inflow: float,
        outflow: float,
        interval: float,
    ) -> LevelDecision:
        config = self.config()
        state = self.state()
        recent = [float(item) for item in state.get("recent_levels", []) if isinstance(item, (int, float))]
        recent.append(level)
        recent = recent[-12:]
        volatile = bool(recent) and max(recent) - min(recent) > config.volatility_threshold

        correction_seconds = max(config.horizon_seconds, interval)
        horizon_hours = correction_seconds / 3600.0
        hours = max(interval / 3600.0, config.horizon_seconds / 3600.0)
        imbalance = inflow - outflow
        projected = level + imbalance * horizon_hours

        error = config.target_level - level
        desired_net = 0.0 if abs(error) <= config.deadband else error / horizon_hours
        protection = "none"
        if projected >= config.high_level or level >= config.high_level:
            protection = "high"
            desired_net = min(
                -(max(level, projected) - config.high_level) / horizon_hours,
                -config.volatility_threshold / horizon_hours,
            )
        elif projected <= config.min_level or level <= config.min_level:
            protection = "low"
            desired_net = max(
                (config.min_level - min(level, projected)) / horizon_hours,
                config.volatility_threshold / horizon_hours,
            )

        ratio = VOLATILE_FLOW_SLEW_RATIO if volatile else (
            PROTECTION_FLOW_SLEW_RATIO
            if protection != "none"
            else NORMAL_FLOW_SLEW_RATIO
        )
        slew = config.capacity * ratio * hours
        trim_capacity = config.capacity * OUTLET_TRIM_RATIO * hours
        current_min, current_max = self._pumps.current_range()

        desired_outlet = outflow
        preliminary_inlet = _clamp(outflow + desired_net, 0.0, config.inlet_max)
        residual = outflow + desired_net - preliminary_inlet
        if abs(residual) > 0.000001:
            adjustment = _clamp(residual, -trim_capacity, trim_capacity)
            desired_outlet = _clamp(outflow - adjustment, 0.0, self._pumps.total_capacity())

        pump_command = self._pumps.request_for_outlet(
            desired_outlet,
            request_id,
            f"{protection}_level_protection" if protection != "none" else "flow_balance",
        )
        outlet_target = _clamp(desired_outlet, current_min, current_max)
        outlet_flow = _move_towards(outflow, outlet_target, slew)
        outlet_flow = _clamp(outlet_flow, current_min, current_max)

        inlet_target = _clamp(outlet_flow + desired_net, 0.0, config.inlet_max)
        inlet_flow = _move_towards(inflow, inlet_target, slew)

        reasons = ["inlet_first", "outlet_micro_trim"]
        if volatile:
            reasons.append("volatile_level_slew_rate_pressed")
        if protection != "none":
            reasons.append(f"{protection}_level_protection")
        if pump_command is not None:
            reasons.append(f"pump_{pump_command.action}_requested")
        if abs(desired_net) <= 0.000001:
            reasons.append("level_deadband")

        return self._decision(
            request_id=request_id,
            status="applied",
            sensor=True,
            level=level,
            inflow=inflow,
            outflow=outflow,
            imbalance=imbalance,
            projected=projected,
            desired_net=desired_net,
            volatile=volatile,
            protection=protection,
            inlet_flow=inlet_flow,
            outlet_flow=outlet_flow,
            inlet_slew=slew,
            outlet_slew=slew,
            pump_command=pump_command,
            reasons=reasons,
        )

    def _conservative(
        self,
        request_id: str,
        invalid_level: float | None,
        inflow: float,
        outflow: float,
        interval: float,
    ) -> LevelDecision:
        config = self.config()
        state = self.state()
        last_good = state.get("last_good_level")
        last_good_level = float(last_good) if isinstance(last_good, (int, float)) else None
        hours = max(interval / 3600.0, config.horizon_seconds / 3600.0)
        slew = config.capacity * VOLATILE_FLOW_SLEW_RATIO * hours
        current_min, current_max = self._pumps.current_range()

        bias = "unknown"
        target_outlet = outflow
        if last_good_level is not None and last_good_level >= config.high_level:
            bias = "last_known_high"
            target_outlet = min(current_max, outflow + config.capacity * 0.05 * hours)
        elif last_good_level is not None and last_good_level <= config.min_level:
            bias = "last_known_low"
            target_outlet = max(0.0, outflow - config.capacity * 0.05 * hours)

        outlet_flow = _clamp(_move_towards(outflow, target_outlet, slew), current_min, current_max)
        inlet_bias = 0.0
        if bias == "last_known_high":
            inlet_bias = -config.capacity * 0.05 * hours
        elif bias == "last_known_low":
            inlet_bias = config.capacity * 0.05 * hours
        inlet_flow = _clamp(
            _move_towards(inflow, max(0.0, outlet_flow + inlet_bias), slew),
            0.0,
            config.inlet_max,
        )
        reasons = [
            "level_sensor_partial_failure",
            "conservative_inlet_matches_outlet",
            "pump_start_stop_inhibited",
            f"sensor_bias_{bias}",
        ]
        return self._decision(
            request_id=request_id,
            status="conservative",
            sensor=False,
            level=invalid_level,
            last_good_override=last_good_level,
            inflow=inflow,
            outflow=outflow,
            imbalance=inflow - outflow,
            projected=None,
            desired_net=0.0,
            volatile=True,
            protection="sensor_failure",
            inlet_flow=inlet_flow,
            outlet_flow=outlet_flow,
            inlet_slew=slew,
            outlet_slew=slew,
            pump_command=None,
            reasons=reasons,
        )

    def _hold_for_pumps(
        self,
        request_id: str,
        sensor_available: bool,
        level: float | None,
        inflow: float,
        outflow: float,
    ) -> LevelDecision:
        state = self.state()
        has_previous_command = state.get("last_decision") is not None
        inlet_flow = float(state.get("last_inlet_flow", inflow)) if has_previous_command else inflow
        outlet_flow = float(state.get("last_outlet_flow", outflow)) if has_previous_command else outflow
        last_good = state.get("last_good_level")
        last_good_level = level if sensor_available and level is not None else (
            float(last_good) if isinstance(last_good, (int, float)) else None
        )
        reasons = ["pump_state_unconfirmed", "valves_hold_pending_acknowledgement"]
        if not sensor_available:
            reasons.append("level_sensor_partial_failure")
        return self._decision(
            request_id=request_id,
            status="pumps_unconfirmed",
            sensor=sensor_available,
            level=level,
            last_good_override=last_good_level,
            inflow=inflow,
            outflow=outflow,
            imbalance=inflow - outflow,
            projected=None,
            desired_net=0.0,
            volatile=True,
            protection="pump_unconfirmed",
            inlet_flow=inlet_flow,
            outlet_flow=outlet_flow,
            inlet_slew=0.0,
            outlet_slew=0.0,
            pump_command=None,
            reasons=reasons,
        )

    def _decision(
        self,
        *,
        request_id: str,
        status: str,
        sensor: bool,
        level: float | None,
        inflow: float,
        outflow: float,
        imbalance: float,
        projected: float | None,
        desired_net: float,
        volatile: bool,
        protection: str,
        inlet_flow: float,
        outlet_flow: float,
        inlet_slew: float,
        outlet_slew: float,
        pump_command: object,
        reasons: list[str],
        last_good_override: float | None = None,
    ) -> LevelDecision:
        config = self.config()
        state = self.state()
        stored_good = state.get("last_good_level")
        last_good = last_good_override
        if last_good is None and isinstance(stored_good, (int, float)):
            last_good = float(stored_good)
        if last_good is None and sensor and level is not None:
            last_good = level
        inlet_position = 100.0 * inlet_flow / config.inlet_max if config.inlet_max else 0.0
        outlet_position = 100.0 * outlet_flow / self._pumps.total_capacity()
        return LevelDecision(
            request_id=request_id,
            status=status,
            sequence=len(self.history()) + 1,
            at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            sensor_available=sensor,
            level=level,
            last_good_level=last_good,
            capacity=config.capacity,
            target_level=config.target_level,
            min_level=config.min_level,
            high_level=config.high_level,
            measured_inflow=inflow,
            measured_outflow=outflow,
            imbalance=imbalance,
            projected_level=projected,
            desired_net_flow=desired_net,
            volatile=volatile,
            protection=protection,
            pump_confirmed=self._pumps.confirmed(),
            inlet_command=ValveCommand(
                "inlet",
                inlet_flow,
                _clamp(inlet_position, 0.0, 100.0),
                inlet_slew,
                "primary_flow_balance",
            ),
            outlet_command=ValveCommand(
                "outlet",
                outlet_flow,
                _clamp(outlet_position, 0.0, 100.0),
                outlet_slew,
                "micro_trim_or_protection",
            ),
            pump_command=None if pump_command is None else pump_command.as_dict(),
            reasons=reasons,
        )

    def _persist(
        self, decision: LevelDecision, request_id: str, sensor_available: bool
    ) -> None:
        payload = decision.as_dict()
        recent = [
            float(item)
            for item in self.state().get("recent_levels", [])
            if isinstance(item, (int, float))
        ]
        if sensor_available and decision.level is not None:
            recent.append(decision.level)
        state = {
            "sensor_available": sensor_available,
            "last_good_level": decision.last_good_level,
            "last_inlet_flow": decision.inlet_command.flow,
            "last_outlet_flow": decision.outlet_command.flow,
            "recent_levels": recent[-12:],
            "last_decision": payload,
        }
        self._store.put(STATE_KEY, json.dumps(state, ensure_ascii=False, sort_keys=True))
        history = self.history()
        history.append(payload)
        self._store.put(
            HISTORY_KEY, json.dumps(history[-MAX_HISTORY:], ensure_ascii=False)
        )
        ids_payload = _load_json(self._store, IDEMPOTENCY_KEY, {})
        ids = ids_payload if isinstance(ids_payload, dict) else {}
        ids[request_id] = payload
        trimmed = dict(list(ids.items())[-MAX_REQUEST_IDS:])
        self._store.put(IDEMPOTENCY_KEY, json.dumps(trimmed, ensure_ascii=False))

    def _replay(self, request_id: str) -> LevelDecision | None:
        payload = _load_json(self._store, IDEMPOTENCY_KEY, {})
        if not isinstance(payload, dict) or request_id not in payload:
            return None
        data = payload[request_id]
        if not isinstance(data, dict):
            return None
        inlet = data.get("inlet_command", {})
        outlet = data.get("outlet_command", {})
        decision = LevelDecision(
            request_id=str(data.get("request_id", request_id)),
            status=str(data.get("status", "")),
            sequence=int(data.get("sequence", 0)),
            at=str(data.get("at", "")),
            sensor_available=bool(data.get("sensor_available", False)),
            level=data.get("level"),
            last_good_level=data.get("last_good_level"),
            capacity=float(data.get("capacity", 0.0)),
            target_level=float(data.get("target_level", 0.0)),
            min_level=float(data.get("min_level", 0.0)),
            high_level=float(data.get("high_level", 0.0)),
            measured_inflow=float(data.get("measured_inflow", 0.0)),
            measured_outflow=float(data.get("measured_outflow", 0.0)),
            imbalance=float(data.get("imbalance", 0.0)),
            projected_level=data.get("projected_level"),
            desired_net_flow=float(data.get("desired_net_flow", 0.0)),
            volatile=bool(data.get("volatile", False)),
            protection=str(data.get("protection", "none")),
            pump_confirmed=bool(data.get("pump_confirmed", False)),
            inlet_command=self._valve(inlet),
            outlet_command=self._valve(outlet),
            pump_command=data.get("pump_command"),
            reasons=[str(item) for item in data.get("reasons", [])],
            idempotent_replay=True,
        )
        return decision

    @staticmethod
    def _valve(payload: object) -> ValveCommand:
        data = payload if isinstance(payload, dict) else {}
        return ValveCommand(
            valve=str(data.get("valve", "")),
            flow=float(data.get("flow", 0.0)),
            position=float(data.get("position", 0.0)),
            slew_limit=float(data.get("slew_limit", 0.0)),
            reason=str(data.get("reason", "")),
        )
