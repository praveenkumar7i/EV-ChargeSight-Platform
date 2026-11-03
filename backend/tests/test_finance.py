import pytest

from app.core.finance import FinanceInputError, InvestmentInputs, compare, evaluate, irr, npv, scenarios


def simple(**kw):
    base = dict(n_chargers=1, power_kw=60, equipment_cost_per_charger=1_000_000, installation_cost=0,
                grid_connection_cost=0, site_preparation_cost=0, rent_monthly=10_000, fixed_opex_monthly=0,
                maintenance_per_charger_annual=0, sessions_per_day=10, kwh_per_session=20, price_per_kwh=20,
                electricity_cost_per_kwh=8, variable_cost_per_kwh=0, payment_fee_pct=0, annual_growth_pct=0,
                analysis_years=5, discount_rate_pct=0.10)
    base.update(kw)
    return InvestmentInputs.from_dict(base)


def test_revenue_cost_and_contribution():
    r = evaluate(simple())
    y = r["year1"]
    energy = 10 * 365 * 20
    assert y["energy_kwh"] == pytest.approx(energy)
    assert y["revenue"] == pytest.approx(energy * 20)
    assert y["electricity_cost"] == pytest.approx(energy * 8)
    assert y["fixed_opex"] == pytest.approx(120_000)
    assert y["operating_contribution"] == pytest.approx(energy * 12 - 120_000)
    assert y["revenue"] != y["operating_contribution"]           # revenue is never called profit


def test_payback_month():
    r = evaluate(simple())
    monthly = (10 * 365 * 20 * 12 - 120_000) / 12        # 63,000 per month
    expected = -(-1_000_000 // monthly)                  # ceil
    assert r["payback_months"] == expected
    assert r["payback_years"] == pytest.approx(expected / 12)


def test_breakeven_sessions():
    r = evaluate(simple())
    # margin per session = 20 kWh * (20 - 8) = 240; fixed 120,000/yr
    assert r["breakeven_sessions_per_day"] == pytest.approx(120_000 / (365 * 240))


def test_loss_making_case_flags_and_no_payback():
    r = evaluate(simple(sessions_per_day=1))
    codes = {f["code"] for f in r["flags"]}
    assert "loss_making_year1" in codes and "payback_not_reached" in codes
    assert r["payback_months"] is None and r["npv"] < 0


def test_negative_unit_margin():
    r = evaluate(simple(price_per_kwh=7))
    codes = {f["code"] for f in r["flags"]}
    assert "negative_unit_margin" in codes and r["breakeven_sessions_per_day"] is None
    assert r["irr"] is None


def test_zero_cash_flow():
    # contribution exactly zero: 10*365*20*margin = 120,000  -> margin = 120000/73000
    r = evaluate(simple(price_per_kwh=8 + 120_000 / 73_000))
    assert r["year1"]["operating_contribution"] == pytest.approx(0, abs=1e-6)
    assert r["payback_months"] is None
    assert "loss_making_year1" in {f["code"] for f in r["flags"]}


def test_unreachable_payback_within_period():
    r = evaluate(simple(analysis_years=1))
    assert r["payback_months"] is None
    assert "payback_not_reached" in {f["code"] for f in r["flags"]}


def test_impossible_utilisation_flag():
    r = evaluate(simple(sessions_per_day=200, kwh_per_session=20, power_kw=7.4))
    assert "impossible_utilisation" in {f["code"] for f in r["flags"]}


def test_npv_and_irr_consistency():
    r = evaluate(simple())
    assert r["npv"] == pytest.approx(npv(r["cash_flows"], 0.10))
    assert npv(r["cash_flows"], r["irr"]) == pytest.approx(0, abs=1)
    assert irr([100, 100]) is None


@pytest.mark.parametrize("bad", [{"sessions_per_day": -1}, {"n_chargers": 0}, {"power_kw": 0},
                                 {"analysis_years": 0}, {"discount_rate_pct": 12}, {"price_per_kwh": "abc"},
                                 {"price_per_kwh": float("nan")}, {"unknown": 1}, {"lat": 120}])
def test_invalid_inputs_rejected(bad):
    with pytest.raises(FinanceInputError):
        InvestmentInputs.from_dict(bad)


def test_missing_inputs_use_documented_defaults():
    i = InvestmentInputs.from_dict({"sessions_per_day": None})
    assert i.sessions_per_day == InvestmentInputs().sessions_per_day
    assert i.lat is None


def test_scenarios_are_ordered():
    sc = scenarios(simple())
    assert sc["conservative"]["npv"] < sc["base"]["npv"] < sc["optimistic"]["npv"]
    assert sc["base"]["multipliers"] == {}


def test_compare_sites_lists_risks():
    rows = compare([("a", simple(site_name="A")), ("b", simple(site_name="B", sessions_per_day=1))])
    assert rows[0]["npv"] > rows[1]["npv"]
    assert any("user-supplied" in r for r in rows[0]["key_risks"])
    assert any("operating contribution" in r for r in rows[1]["key_risks"])
