import math
from datetime import datetime, timedelta

import pytest

from app.core.priority import PriorityConfig, PriorityInput, PriorityWeights, compute, rank

T0 = datetime(2026, 10, 1, 8, 0)


def inp(ref="T1", cat="ground_fault", sess=120, max_sess=120, kw=60, other=60, age=0.0, det=T0):
    return PriorityInput(ticket_ref=ref, fault_category=cat, site_daily_sessions=sess,
                         max_site_daily_sessions=max_sess, charger_kw=kw, other_working_kw_at_site=other,
                         age_hours=age, detected_at=det)


def test_weighted_score_matches_formula():
    r = compute(inp(sess=60, max_sess=120, kw=60, other=180, age=6))
    # S=1, U=0.5, R=60/240=0.25, A=6/24=0.25
    expected = 0.4 * 1 + 0.3 * 0.5 + 0.2 * 0.25 + 0.1 * 0.25
    assert r.score == pytest.approx(expected)
    vals = {f.key: f.value for f in r.factors}
    assert vals == pytest.approx({"severity": 1, "utilisation": 0.5, "capacity": 0.25, "age": 0.25})


def test_displayed_contributions_sum_to_total():
    for kwargs in [dict(), dict(cat="payment_reader", sess=10, age=30), dict(kw=3.3, other=200, age=1)]:
        r = compute(inp(**kwargs))
        assert sum(f.contribution for f in r.factors) == pytest.approx(r.score)
        for f in r.factors:
            assert f.contribution == pytest.approx(f.weight * f.value)


def test_normalisation_boundaries():
    assert {f.key: f.value for f in compute(inp(sess=0, max_sess=0)).factors}["utilisation"] == 0
    r = compute(inp(sess=200, max_sess=100))
    assert {f.key: f.value for f in r.factors}["utilisation"] == 1.0
    assert any("clamped" in w for w in r.warnings)
    assert {f.key: f.value for f in compute(inp(other=0)).factors}["capacity"] == 1.0   # last working charger
    assert {f.key: f.value for f in compute(inp(age=100)).factors}["age"] == 1.0
    for r in (compute(inp(sess=0, kw=0, age=0, cat="payment_reader")), compute(inp(age=500))):
        assert 0.0 <= r.score <= 1.0


@pytest.mark.parametrize("bad", [None, float("nan"), -5, float("inf")])
def test_missing_or_invalid_inputs_are_safe(bad):
    r = compute(inp(sess=bad, kw=bad, other=bad, age=bad))
    assert not math.isnan(r.score)
    assert 0.0 <= r.score <= 1.0
    assert r.warnings


def test_unknown_category_treated_as_critical_and_flagged():
    r = compute(inp(cat="teleporter_failure"))
    assert r.severity_level == "critical"
    assert any("Unknown fault category" in w for w in r.warnings)


def test_weights_validation_and_normalisation():
    w = PriorityWeights(2, 1, 1, 0).normalised()
    assert w.severity == pytest.approx(0.5) and sum(w.as_dict().values()) == pytest.approx(1)
    with pytest.raises(ValueError):
        PriorityWeights(-1, 1, 1, 1).normalised()
    with pytest.raises(ValueError):
        PriorityWeights(0, 0, 0, 0).normalised()
    with pytest.raises(ValueError):
        PriorityConfig.from_dict({"weights": {"severity": 1}, "age_cap_hours": 0})


def test_alternative_weights_change_score():
    sev_only = PriorityConfig(weights=PriorityWeights(1, 0, 0, 0))
    r = compute(inp(cat="payment_reader", sess=120), sev_only)
    assert r.score == pytest.approx(0.6)


def test_ageing_increases_score_and_escalates():
    young = compute(inp(age=1))
    old = compute(inp(age=12))
    assert old.score > young.score
    assert not old.escalated
    assert compute(inp(age=24)).escalated


def test_escalation_prevents_starvation_in_ranking():
    low_old = compute(inp("LOW", cat="payment_reader", sess=5, kw=3.3, other=200, age=30, det=T0))
    high_new = compute(inp("HIGH", cat="ground_fault", sess=120, other=0, age=0.1, det=T0 + timedelta(hours=30)))
    assert high_new.score > low_old.score
    ordered = rank([high_new, low_old])
    assert [r.ticket_ref for r in ordered] == ["LOW", "HIGH"]
    assert "Escalated" in ordered[0].explanation


def test_tie_handling_is_deterministic():
    a = compute(inp("B-later", det=T0 + timedelta(minutes=5)))
    b = compute(inp("A-earlier", det=T0))
    c = compute(inp("C-same-time", det=T0))
    ordered = rank([a, c, b])
    assert [r.ticket_ref for r in ordered] == ["A-earlier", "C-same-time", "B-later"]
    assert "detected earlier" in ordered[2].above[0]["reason"] or "same score" in ordered[2].above[0]["reason"]


def test_explanation_uses_calculated_values_and_lists_tickets_above():
    hi = compute(inp("HI", sess=120))
    lo = compute(inp("LO", sess=30))
    ordered = rank([lo, hi])
    assert ordered[0].ticket_ref == "HI" and ordered[0].rank == 1
    assert f"{hi.score:.2f}" in ordered[0].explanation
    above = ordered[1].above
    assert above[0]["ticket_ref"] == "HI"
    assert "Site utilisation" in above[0]["reason"]
    assert above[0]["score_gap"] == pytest.approx(round(hi.score - lo.score, 4), abs=1e-4)
