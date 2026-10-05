"""Plain text control report."""

from __future__ import annotations

from waterplant.ns import Stage, treatment_line

from .describe import collect as collect_describe
from .runtime import Runtime
from .telemetry import collect as collect_telemetry


def text_report(rt: Runtime) -> str:
    """Render the operator report that mirrors the JSON snapshot."""

    describe = collect_describe(rt)
    telemetry = collect_telemetry(rt)
    line = treatment_line()
    rows = [
        "waterplant control report",
        f"pipeline: {describe['pipeline']}",
        f"pipeline stages={line.count()} contains_filter={line.contains(Stage.FILTER)}",
        f"store: {describe['store']}",
        f"intake: {describe['intake']}",
        f"coagulant: {describe['coag']}",
        f"chlorine: {describe['chlor']}",
        f"filter: {describe['filter']}",
        f"backwash: {describe['backwash']}",
        f"turbidity: {describe['turbidity']}",
        f"flow: {describe['flow']}",
        f"clearwell: {describe['clearwell']}",
        (
            f"level_loop mode={rt.level_loop.mode.value} target={rt.level_loop.band.target_m:.3f}m "
            f"band=[{rt.level_loop.band.ll_m:.3f},{rt.level_loop.band.l_m:.3f},"
            f"{rt.level_loop.band.h_m:.3f},{rt.level_loop.band.hh_m:.3f}]m "
            f"volume={rt.level_loop.config.effective_volume_m3:.1f}m3"
        ),
        (
            f"level_loop valves inlet={rt.level_loop.valves.inlet.position_pct:.1f}% "
            f"outlet={rt.level_loop.valves.outlet.position_pct:.1f}% "
            f"pumps_running={rt.level_loop.pumps.snapshot().running_count} "
            f"transition={rt.level_loop.pumps.in_transition()}"
        ),
        f"quota: {describe['quota']}",
        f"audit: {describe['audit']}",
        f"audit kinds={rt.auditor.kinds()}",
        (
            f"filter beds={telemetry.filter_beds} active={telemetry.filter_active} "
            f"total_load={telemetry.filter_load:.4f}"
        ),
        f"filter bed_ids={rt.bank.bed_ids()}",
        f"backwash queue={telemetry.backwash_queue} drains={telemetry.backwash_drains}",
        f"backwash commands={rt.backwash.command_list()}",
        f"quota value={telemetry.quota_value:.4f} clearwell level={telemetry.clearwell_level:.4f}",
        f"coag ratio={telemetry.coag_ratio:.4f} chlorine target={telemetry.chlor_target:.4f}",
        (
            f"ph value={telemetry.ph_value:.4f} stable={telemetry.ph_stable} "
            f"scheduled={telemetry.schedule_due}"
        ),
        f"intake trend samples={telemetry.trend_samples}",
        (
            f"inventory balance={telemetry.inventory_balance:.4f} "
            f"alerts={telemetry.inventory_alerts}"
        ),
    ]
    return "\n".join(rows) + "\n"
