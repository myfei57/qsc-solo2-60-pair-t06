"""Component health checks."""

from __future__ import annotations

from .runtime import Runtime


def run_checks(rt: Runtime) -> list[dict[str, str]]:
    """Return one check row per mutable component."""

    checks = [{"name": "store", "status": "ok", "detail": f"keys={rt.store.count()}"}]
    if rt.calibration.current() <= 0:
        checks.append({"name": "calibration", "status": "fail", "detail": "factor must be positive"})
    else:
        checks.append({"name": "calibration", "status": "ok", "detail": "factor set"})
    if rt.well.residual_target() <= 0:
        checks.append({"name": "residual", "status": "fail", "detail": "target must be positive"})
    else:
        checks.append({"name": "residual", "status": "ok", "detail": "target set"})
    if rt.accumulator.value() < 0:
        checks.append({"name": "quota", "status": "fail", "detail": "accumulator is negative"})
    else:
        checks.append({"name": "quota", "status": "ok", "detail": "accumulator set"})
    if rt.flow_repository.load_flow()[1]:
        checks.append({"name": "intake", "status": "ok", "detail": "flow reading present"})
    else:
        checks.append({"name": "intake", "status": "warn", "detail": "no flow reading yet"})
    if rt.stabilizer.is_stable():
        checks.append({"name": "ph", "status": "ok", "detail": "ph in the stable band"})
    else:
        checks.append({"name": "ph", "status": "warn", "detail": "ph outside the stable band"})
    alerts = rt.inventory.needs_reorder()
    if alerts:
        checks.append(
            {"name": "inventory", "status": "warn", "detail": "reorder " + ", ".join(alerts)}
        )
    else:
        checks.append({"name": "inventory", "status": "ok", "detail": "stock above reorder level"})
    loop = rt.level_loop
    state = loop.status()
    if state["mode"] == "normal":
        checks.append({"name": "level_loop", "status": "ok", "detail": "level measured, closed loop active"})
    elif state["mode"] == "degraded":
        checks.append(
            {
                "name": "level_loop",
                "status": "warn",
                "detail": "level transmitter failed; conservative flow balance, no auto pump start",
            }
        )
    else:
        checks.append(
            {
                "name": "level_loop",
                "status": "fail",
                "detail": "safe hold: level estimate uncertainty exhausted, operator action required",
            }
        )
    chain_ok, broken = loop.auditor.verify()
    if chain_ok:
        checks.append({"name": "level_audit", "status": "ok", "detail": "hash chain intact"})
    else:
        checks.append(
            {"name": "level_audit", "status": "fail", "detail": f"hash chain broken at seq {broken}"}
        )
    return checks
