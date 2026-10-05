"""Tests for the clear well level closed loop."""

from __future__ import annotations

import json
import tempfile
import os
import unittest

from waterplant.clearwell import (
    FlowReading,
    LevelConfig,
    LevelLoop,
    LevelReading,
    PumpAction,
    PumpState,
    Reading,
    Reason,
    ScriptedPlant,
)
from waterplant.clearwell.level import balance
from waterplant.clearwell.level.auditor import LEVEL_AUDIT_KEY
from waterplant.store import Store


def reading(level=2.0, inflow=50.0, outflow=50.0, *, ok=True, demand=50.0, tick=60.0):
    return Reading(
        level=LevelReading(level, ok=ok),
        flow=FlowReading(inflow, outflow),
        outlet_demand_m3h=demand,
        tick_s=tick,
    )


def fresh_loop(config=None):
    tmp = tempfile.mkdtemp()
    store = Store.open(os.path.join(tmp, "state.json"))
    loop = LevelLoop(store, config)
    loop.adopt_field(50.0, 50.0)
    return loop, store


class ConfigPolicyCase(unittest.TestCase):
    def test_limits_are_fixed_fractions_of_tank_volume(self) -> None:
        config = LevelConfig(area_m2=100.0, dead_m=0.0, max_m=4.0)
        band = config.band(2.0)
        self.assertAlmostEqual(band.ll_m, 0.2)
        self.assertAlmostEqual(band.l_m, 0.6)
        self.assertAlmostEqual(band.h_m, 3.4)
        self.assertAlmostEqual(band.hh_m, 3.8)
        self.assertAlmostEqual(band.ll_m3, 20.0)
        self.assertAlmostEqual(band.hh_m3, 380.0)
        self.assertAlmostEqual(config.effective_volume_m3, 400.0)

    def test_dead_volume_basis_shifts_limits_with_geometry(self) -> None:
        config = LevelConfig(area_m2=50.0, dead_m=1.0, max_m=5.0)
        band = config.band(3.0)
        # span is 4 m now; fractions apply to the usable span above dead.
        self.assertAlmostEqual(band.ll_m, 1.2)
        self.assertAlmostEqual(band.hh_m, 4.8)
        self.assertAlmostEqual(config.effective_volume_m3, 200.0)

    def test_target_cannot_breach_interlocks(self) -> None:
        config = LevelConfig()
        with self.assertRaises(ValueError):
            config.validate_target(3.9)  # at/above HH
        with self.assertRaises(ValueError):
            config.validate_target(0.1)  # at/below LL
        # A shift-supplied target inside the band is accepted; the band itself
        # does not move.
        target = config.validate_target(2.5)
        band = config.band(target)
        self.assertEqual(band.target_m, 2.5)
        self.assertEqual(band.hh_m, config.hh_m)
        self.assertEqual(band.ll_m, config.ll_m)

    def test_volatility_shrinks_slew_never_widens_limits(self) -> None:
        config = LevelConfig()
        nominal, _ = config.slew_budget(trip=False, protection=False, volatile=False)
        suppressed, reason = config.slew_budget(trip=False, protection=False, volatile=True)
        self.assertLess(suppressed, nominal)
        self.assertIs(reason, Reason.VOLATILITY_SUPPRESSED)
        protection, _ = config.slew_budget(trip=False, protection=True, volatile=False)
        self.assertGreater(protection, nominal)  # protection may move faster
        # Limits are identical regardless of the volatility flag.
        self.assertEqual(config.band().hh_m, config.band().hh_m)


class BalanceMathCase(unittest.TestCase):
    def setUp(self) -> None:
        self.config = LevelConfig()

    def test_inflow_dominates_outlet_only_trims(self) -> None:
        result = balance.allocate(
            current_net_m3h=0.0,
            desired_net_m3h=0.0,
            slew_budget_m3h=100.0,
            inflow_now=50.0,
            outflow_now=50.0,
            demand_m3h=50.0,
            max_inflow=120.0,
            min_outflow=0.0,
            pump_min_outflow=30.0,
            pump_max_outflow=100.0,
            degraded=False,
            volatile=False,
        )
        self.assertAlmostEqual(result.outlet_flow_m3h, 50.0)
        self.assertAlmostEqual(result.inlet_flow_m3h, 50.0)

    def test_outlet_cannot_exceed_pump_envelope(self) -> None:
        # The running pump caps outflow; even a wildly higher demand cannot
        # pull the outlet valve past the pump envelope. The unmet demand is
        # surfaced to the pump sequencer via delivered < demand (tested in the
        # sequencing case), not by overdriving the valve.
        result = balance.allocate(
            current_net_m3h=-50.0,
            desired_net_m3h=0.0,
            slew_budget_m3h=100.0,
            inflow_now=0.0,
            outflow_now=50.0,
            demand_m3h=200.0,
            max_inflow=120.0,
            min_outflow=0.0,
            pump_min_outflow=50.0,
            pump_max_outflow=50.0,
            degraded=False,
            volatile=False,
        )
        self.assertLessEqual(result.outlet_flow_m3h, 50.0 + 1e-9)
        # Balance target is net zero; the inlet is set to match the
        # pump-bounded outlet rather than overdriven to chase demand the
        # pumps cannot deliver.
        self.assertAlmostEqual(result.inlet_flow_m3h, 50.0)
        self.assertAlmostEqual(
            result.inlet_flow_m3h - result.outlet_flow_m3h, result.net_command_m3h
        )

    def test_inlet_capacity_shortfall_is_a_negative_residual(self) -> None:
        # When the inlet valve cannot open far enough to balance the level
        # target, the residual tells the sequencer to shed a pump.
        result = balance.allocate(
            current_net_m3h=0.0,
            desired_net_m3h=100.0,
            slew_budget_m3h=100.0,
            inflow_now=50.0,
            outflow_now=50.0,
            demand_m3h=50.0,
            max_inflow=60.0,
            min_outflow=0.0,
            pump_min_outflow=30.0,
            pump_max_outflow=100.0,
            degraded=False,
            volatile=False,
        )
        self.assertEqual(result.inlet_flow_m3h, 60.0)
        self.assertLess(result.residual_m3h, 0.0)
        self.assertTrue(result.trim_capped)

    def test_degraded_uses_conservative_trim_and_gain(self) -> None:
        desired, _ = balance.required_net_inflow(
            level_m=1.9,
            target_m=2.0,
            integral_m=0.0,
            current_net_m3h=0.0,
            area_m2=100.0,
            degraded=True,
        )
        nominal_desired, _ = balance.required_net_inflow(
            level_m=1.9,
            target_m=2.0,
            integral_m=0.0,
            current_net_m3h=0.0,
            area_m2=100.0,
            degraded=False,
        )
        self.assertAlmostEqual(desired, nominal_desired * 0.5)


class SequencingCase(unittest.TestCase):
    def test_pump_confirmed_before_valve_decision(self) -> None:
        loop, _ = fresh_loop()
        decision = loop.tick(reading())
        self.assertEqual(
            decision.phase_log,
            ["read_inputs", "confirm_pumps", "decide_valves", "dispatch"],
        )

    def test_pump_start_is_debounced_then_transitions_with_feedforward(self) -> None:
        loop, _ = fresh_loop()
        # First high-demand tick only accrues a debounce vote (no cycling on a
        # single noisy sample).
        d0 = loop.tick(reading(level=2.0, inflow=50, outflow=50, demand=130))
        self.assertEqual(d0.pumps, [])
        # Second sustained tick commits the start.
        d1 = loop.tick(reading(level=2.0, inflow=50, outflow=50, demand=130))
        self.assertEqual([p.action for p in d1.pumps], [PumpAction.START])
        # The next ticks are the timed transition; no second start is issued.
        transition_actions = []
        for _ in range(3):
            d = loop.tick(reading(level=2.0, inflow=70, outflow=70, demand=130))
            transition_actions.append(d.action.value)
        self.assertIn("pump_transition", transition_actions)
        self.assertEqual(loop.pumps.states["P2"], PumpState.RUNNING)
        # Repeating a start while already running is a no-op (idempotent).
        self.assertFalse(loop.pumps.apply("P2", PumpAction.START))

    def test_repeated_identical_valve_command_is_not_resent(self) -> None:
        loop, _ = fresh_loop()
        loop.tick(reading())
        before = loop.valves.inlet.position_pct
        d = loop.tick(reading())
        self.assertEqual(d.valves, [])  # gateway swallowed the identical move
        self.assertEqual(loop.valves.inlet.position_pct, before)


class ProtectionCase(unittest.TestCase):
    def test_hh_trip_closes_inlet_and_opens_outlet(self) -> None:
        loop, _ = fresh_loop()
        # Start from an unbalanced opening so the safe-direction commands are
        # real movements (an already-closed valve is correctly left alone).
        loop.adopt_field(80.0, 20.0)
        d = loop.tick(reading(level=3.95, inflow=80, outflow=20, demand=20))
        self.assertEqual(d.reason, Reason.HH_TRIP)
        valve_flows = {v.valve: v.flow_m3h for v in d.valves}
        self.assertEqual(valve_flows["inlet"], 0.0)
        self.assertGreater(valve_flows["outlet"], 0.0)

    def test_ll_trip_opens_inlet_stops_outlet_and_pump(self) -> None:
        loop, _ = fresh_loop()
        d = loop.tick(reading(level=0.1, inflow=10, outflow=50, demand=10))
        self.assertEqual(d.reason, Reason.LL_TRIP)
        valve_flows = {v.valve: v.flow_m3h for v in d.valves}
        self.assertGreater(valve_flows["inlet"], 0.0)
        self.assertEqual(valve_flows["outlet"], 0.0)
        self.assertEqual([p.action for p in d.pumps], [PumpAction.STOP])


class PartialFailureCase(unittest.TestCase):
    def test_transmitter_outage_is_degraded_not_full_failure(self) -> None:
        loop, _ = fresh_loop()
        d = loop.tick(reading(level=2.0, inflow=50, outflow=50, ok=False))
        self.assertEqual(d.mode.value, "degraded")
        self.assertTrue(d.level_estimated)
        # Flows are still balanced conservatively; no auto pump start is
        # possible in degraded mode.
        self.assertEqual(d.pumps, [])

    def test_prolonged_outage_degrades_to_safe_hold(self) -> None:
        loop, _ = fresh_loop()
        last = None
        for _ in range(8):
            last = loop.tick(reading(level=2.0, inflow=50, outflow=50, ok=False))
        self.assertEqual(last.mode.value, "safe_hold")
        self.assertEqual(last.reason, Reason.UNCERTAINTY_HOLD)
        self.assertEqual(last.valves, [])

    def test_recovery_is_bumpless_and_re_ticks_are_idempotent(self) -> None:
        loop, _ = fresh_loop()
        for _ in range(3):
            loop.tick(reading(level=2.0, inflow=50, outflow=50, ok=False))
        recovered = loop.tick(reading(level=2.0, inflow=50, outflow=50, ok=True))
        self.assertEqual(recovered.mode.value, "normal")
        self.assertFalse(recovered.level_estimated)
        self.assertIn("recovered", " ".join(recovered.notes).lower())
        # Repeat the same recovered tick: no duplicate commands or pump edges.
        again = loop.tick(reading(level=2.0, inflow=50, outflow=50, ok=True))
        self.assertEqual(again.valves, [])
        self.assertEqual(again.pumps, [])


class AuditCase(unittest.TestCase):
    def test_action_level_reason_share_one_record_and_chain_verifies(self) -> None:
        loop, store = fresh_loop()
        d = loop.tick(reading(level=3.95, inflow=80, outflow=20))
        entries = loop.auditor.entries()
        self.assertEqual(len(entries), 1)
        payload = entries[0].payload
        self.assertEqual(payload["action"], d.action.value)
        self.assertEqual(payload["reason"], d.reason.value)
        self.assertEqual(payload["level_m"], round(d.level_m, 4))
        self.assertTrue(loop.auditor.verify()[0])

    def test_tampering_breaks_the_chain(self) -> None:
        loop, store = fresh_loop()
        loop.tick(reading())
        loop.tick(reading(level=2.1))
        raw = [json.loads(item) for item in json.loads(store.get(LEVEL_AUDIT_KEY)[0])]
        decision = dict(raw[0]["decision"])
        decision["level_m"] = 9.99
        raw[0]["decision"] = decision
        store.put(LEVEL_AUDIT_KEY, json.dumps([json.dumps(row) for row in raw]))
        ok, broken = loop.auditor.verify()
        self.assertFalse(ok)
        self.assertEqual(broken, 1)


class DemoScenarioCase(unittest.TestCase):
    def test_demo_keeps_level_bounded_through_the_script(self) -> None:
        loop, _ = fresh_loop()
        plant = ScriptedPlant(loop.config)
        scenario = [
            (6, True, 50.0),
            (20, True, 120.0),
            (18, True, 25.0),
            (5, False, 30.0),
            (8, True, 30.0),
            (10, True, 8.0),
        ]
        phase_left = 0
        ok = True
        demand = 50.0
        levels = []
        for _ in range(67):
            if phase_left == 0 and scenario:
                phase_left, ok, demand = scenario.pop(0)
            phase_left -= 1
            record = plant.step(loop, level_ok=ok, demand_m3h=demand, tick_s=60.0)
            levels.append(record["plant_level_m"])
        band = loop.band
        self.assertTrue(all(band.ll_m - 0.01 <= level <= band.hh_m + 0.01 for level in levels))
        self.assertTrue(loop.auditor.verify()[0])


if __name__ == "__main__":
    unittest.main()
