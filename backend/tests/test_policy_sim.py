import pytest

from app.core.policy_sim import (
    ExperimentConfig, Fault, Trace, generate_trace, metrics, run_comparison, run_sensitivity, simulate,
    trace_signature,
)
from app.core.stats import paired_summary, percentile


def small_cfg(**kw):
    d = {"seeds": [1, 2, 3], "days": 10}
    d.update(kw)
    return ExperimentConfig.from_dict(d)


def test_both_policies_replay_identical_trace():
    cfg = small_cfg(technicians=1, fault_rate_per_charger_day=0.4)
    trace = generate_trace(cfg, 7)
    sig = trace_signature(trace)
    fifo = simulate(trace, cfg, "fifo")
    prio = simulate(trace, cfg, "priority")
    assert trace_signature(trace) == sig                     # replay never mutates the trace
    key = lambda f: (f.id, f.charger, f.category, f.detected, f.reset_success, f.travel, f.repair)
    assert [key(f) for f in fifo] == [key(f) for f in prio] == [key(f) for f in trace.faults]
    assert {f.id for f in fifo if not f.by_technician and f.resolved} == {f.id for f in prio if not f.by_technician and f.resolved}


def test_trace_generation_is_reproducible_and_seed_dependent():
    cfg = small_cfg()
    assert trace_signature(generate_trace(cfg, 3)) == trace_signature(generate_trace(cfg, 3))
    assert trace_signature(generate_trace(cfg, 3)) != trace_signature(generate_trace(cfg, 4))


def test_run_comparison_is_reproducible():
    cfg = small_cfg(technicians=1, fault_rate_per_charger_day=0.3)
    a, b = run_comparison(cfg), run_comparison(cfg)
    assert a["per_seed"] == b["per_seed"] and a["summary"] == b["summary"]


def _hand_trace():
    # one site, two chargers; one technician; two faults overlapping in time
    sites = [{"code": "S", "daily_sessions": 100, "travel_min": [10, 10], "chargers_kw": [60, 60]}]
    cfg = ExperimentConfig.from_dict({"seeds": [0], "days": 1, "technicians": 1, "sites": sites,
                                      "remote_reset_success": {k: 0 for k in ("comm_loss", "payment_reader")},
                                      "utilisation_fraction": {"S": 0.5}, "margin_per_kwh_inr": 10})
    faults = [
        Fault(1, 0, "S", "payment_reader", detected=0, reset_success=False, travel=10, repair=50),
        Fault(2, 1, "S", "ground_fault", detected=5, reset_success=False, travel=10, repair=40),
        Fault(3, 0, "S", "payment_reader", detected=10, reset_success=False, travel=10, repair=20),
    ]
    return cfg, Trace(0, 24 * 60, [("S", 60.0), ("S", 60.0)], faults)


def test_metric_calculations_on_hand_built_trace():
    cfg, trace = _hand_trace()
    out = simulate(trace, cfg, "fifo")
    by = {f.id: f for f in out}
    # FIFO: F1 at 0 -> done 60; F2 (queued 5) at 60 -> done 110; F3 at 110 -> done 140
    assert (by[1].assigned, by[1].resolved) == (0, 60)
    assert (by[2].assigned, by[2].resolved) == (60, 110)
    assert (by[3].assigned, by[3].resolved) == (110, 140)
    m = metrics(trace, out, cfg)
    # charger 0 down [0,60) ∪ [10,140) = 140 min; charger 1 down [5,110) = 105 min
    assert m["downtime_h"] == pytest.approx((140 + 105) / 60)
    assert m["availability_pct"] == pytest.approx(100 * (1 - 245 / (2 * 1440)))
    assert m["high_sev_wait_mean_min"] == pytest.approx(55)          # only F2 is critical
    assert m["low_sev_wait_max_min"] == pytest.approx(100)           # F3 waited 100
    assert m["mtta_min"] == pytest.approx((0 + 55 + 100) / 3)
    assert m["mttr_min"] == pytest.approx((60 + 105 + 130) / 3)
    assert m["energy_not_delivered_kwh"] == pytest.approx(245 / 60 * 60 * 0.5)
    assert m["downtime_cost_inr"] == pytest.approx(m["energy_not_delivered_kwh"] * 10)


def test_priority_serves_critical_fault_first():
    cfg, trace = _hand_trace()
    by = {f.id: f for f in simulate(trace, cfg, "priority")}
    assert by[1].assigned == 0                  # only fault in queue at t=0
    assert by[2].assigned == 60                 # critical F2 beats earlier-queued... F3 arrived later
    assert by[3].assigned == 110


def test_priority_reorders_queue_vs_fifo():
    sites = [{"code": "S", "daily_sessions": 100, "travel_min": [10, 10], "chargers_kw": [60, 60, 60]}]
    cfg = ExperimentConfig.from_dict({"seeds": [0], "days": 1, "technicians": 1, "sites": sites,
                                      "remote_reset_success": {k: 0 for k in ("comm_loss", "payment_reader")}})
    faults = [
        Fault(1, 0, "S", "overcurrent", 0, False, 10, 60),
        Fault(2, 1, "S", "payment_reader", 1, False, 10, 30),   # queued first
        Fault(3, 2, "S", "ground_fault", 2, False, 10, 30),     # critical, queued second
    ]
    trace = Trace(0, 1440, [("S", 60.0)] * 3, faults)
    fifo = {f.id: f for f in simulate(trace, cfg, "fifo")}
    prio = {f.id: f for f in simulate(trace, cfg, "priority")}
    assert fifo[2].assigned < fifo[3].assigned
    assert prio[3].assigned < prio[2].assigned


def test_unresolved_faults_censored_at_horizon():
    sites = [{"code": "S", "daily_sessions": 10, "travel_min": [10, 10], "chargers_kw": [60]}]
    cfg = ExperimentConfig.from_dict({"seeds": [0], "days": 1, "technicians": 1, "sites": sites})
    trace = Trace(0, 100, [("S", 60.0)], [Fault(1, 0, "S", "ground_fault", 50, False, 10, 500)])
    out = simulate(trace, cfg, "fifo")
    m = metrics(trace, out, cfg)
    assert m["unresolved_at_horizon"] == 1
    assert m["downtime_h"] == pytest.approx(50 / 60)


def test_sensitivity_recomputes_each_variant():
    base = small_cfg(technicians=2, fault_rate_per_charger_day=0.3)
    res = run_sensitivity(base, ["baseline", "technicians_1", "repair_x2"])
    hashes = [v["config_hash"] for v in res["variants"]]
    assert len(set(hashes)) == 3
    assert hashes[0] == base.hash()
    d0 = res["variants"][0]["summary"]["downtime_h"]["mean_a"]
    d2 = res["variants"][2]["summary"]["downtime_h"]["mean_a"]
    assert d2 > d0                               # doubling repair time must increase downtime


def test_config_validation():
    with pytest.raises(ValueError):
        ExperimentConfig.from_dict({"technicians": 0})
    with pytest.raises(ValueError):
        ExperimentConfig.from_dict({"not_a_param": 1})
    with pytest.raises(ValueError):
        ExperimentConfig.from_dict({"priority": {"weights": {"severity": -1}}})


def test_paired_summary_and_percentile():
    s = paired_summary([10, 12, 14, 16], [9, 10, 13, 13])
    assert s["mean_diff"] == pytest.approx(-1.75)
    assert s["ci95_high"] < 0 and s["significant"] is True
    assert s["relative_diff"] == pytest.approx(-1.75 / 13)
    flat = paired_summary([1, 1, 1], [1, 1, 1])
    assert flat["mean_diff"] == 0 and flat["significant"] is False
    assert percentile([1, 2, 3, 4], 50) == pytest.approx(2.5)
    assert percentile([], 95) is None
