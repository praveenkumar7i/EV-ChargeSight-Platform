"""Transparent charging-site investment model (SiteSure-inspired module).

Definitions (all money in INR, all values ESTIMATED from user-supplied inputs):

  initial_investment   = n_chargers * equipment_cost_per_charger
                         + installation_cost + grid_connection_cost + site_preparation_cost
  sessions_year(y)     = sessions_per_day * 365 * (1 + growth)^(y-1)
  energy_year(y)       = sessions_year(y) * kwh_per_session                         [kWh]
  revenue(y)           = energy_year(y) * price_per_kwh                             (gross revenue, NOT profit)
  electricity_cost(y)  = energy_year(y) * electricity_cost_per_kwh
  variable_cost(y)     = energy_year(y) * variable_cost_per_kwh
                         + revenue(y) * payment_fee_pct
  gross_margin(y)      = revenue - electricity_cost - variable_cost
  fixed_opex(y)        = 12 * (rent_monthly + fixed_opex_monthly)
                         + n_chargers * maintenance_per_charger_annual
  operating_contribution(y) = gross_margin(y) - fixed_opex(y)
  cash_flow(0)         = -initial_investment
  cash_flow(y)         = operating_contribution(y)          (pre-tax; no depreciation, financing or
                                                              replacement capex are modelled)
  NPV                  = sum_y cash_flow(y) / (1 + r)^y      for y = 0..analysis_years
  simple payback       = first month in which cumulative undiscounted cash flow >= 0
                         (months within a year receive 1/12 of that year's cash flow)
  break-even sessions/day (year 1) = fixed_opex(1) / (365 * margin_per_session)
                         where margin_per_session = kwh_per_session * (price - electricity
                         - variable_per_kwh - price * payment_fee_pct)
  implied utilisation  = energy_year(1) / (n_chargers * power_kw * 8760)
  IRR                  = rate where NPV = 0, found by bisection when cash flows change sign once.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, replace


class FinanceInputError(ValueError):
    pass


@dataclass
class InvestmentInputs:
    site_name: str = "Proposed site"
    lat: float | None = None
    lng: float | None = None
    n_chargers: int = 2
    charger_type: str = "DC"
    power_kw: float = 60.0
    equipment_cost_per_charger: float = 1_400_000
    installation_cost: float = 300_000
    grid_connection_cost: float = 800_000
    site_preparation_cost: float = 200_000
    rent_monthly: float = 25_000
    electricity_cost_per_kwh: float = 7.5
    price_per_kwh: float = 18.0
    sessions_per_day: float = 24.0          # whole site, year 1
    kwh_per_session: float = 22.0
    variable_cost_per_kwh: float = 0.5
    payment_fee_pct: float = 0.02           # fraction of revenue
    maintenance_per_charger_annual: float = 60_000
    fixed_opex_monthly: float = 30_000
    annual_growth_pct: float = 0.08         # fraction
    analysis_years: int = 8
    discount_rate_pct: float = 0.12         # fraction

    ASSUMPTION_NOTE = (
        "Default values are illustrative ASSUMPTIONS for demonstration only. "
        "They are not verified Bengaluru market prices or tariffs."
    )

    @classmethod
    def from_dict(cls, d: dict) -> "InvestmentInputs":
        known = {f.name for f in fields(cls)}
        unknown = set(d) - known
        if unknown:
            raise FinanceInputError(f"Unknown input field(s): {', '.join(sorted(unknown))}.")
        base = cls()
        vals = {}
        for f in fields(cls):
            if f.name not in d or d[f.name] is None:
                if f.name in ("lat", "lng"):
                    vals[f.name] = None
                    continue
                vals[f.name] = getattr(base, f.name)
                continue
            vals[f.name] = d[f.name]
        obj = cls(**vals)
        obj.validate()
        return obj

    def validate(self) -> None:
        errs = []
        num_fields = [f.name for f in fields(self) if f.name not in ("site_name", "charger_type", "lat", "lng")]
        for name in num_fields:
            v = getattr(self, name)
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                errs.append(f"{name} must be a number.")
            elif v != v:  # NaN
                errs.append(f"{name} must be a number.")
            elif v < 0 and name not in ("annual_growth_pct",):
                errs.append(f"{name} cannot be negative.")
        if errs:
            raise FinanceInputError(" ".join(errs))
        if int(self.n_chargers) != self.n_chargers or self.n_chargers < 1:
            errs.append("n_chargers must be a whole number of at least 1.")
        if self.power_kw <= 0:
            errs.append("power_kw must be greater than 0.")
        if int(self.analysis_years) != self.analysis_years or not 1 <= self.analysis_years <= 30:
            errs.append("analysis_years must be a whole number from 1 to 30.")
        if not -0.5 <= self.annual_growth_pct <= 1.0:
            errs.append("annual_growth_pct must be between -0.5 and 1.0 (a fraction, e.g. 0.08 = 8%).")
        if self.discount_rate_pct > 1.0:
            errs.append("discount_rate_pct must be a fraction (e.g. 0.12 = 12%).")
        if self.payment_fee_pct >= 1.0:
            errs.append("payment_fee_pct must be a fraction below 1.")
        if self.lat is not None and not -90 <= self.lat <= 90:
            errs.append("lat must be between -90 and 90.")
        if self.lng is not None and not -180 <= self.lng <= 180:
            errs.append("lng must be between -180 and 180.")
        if errs:
            raise FinanceInputError(" ".join(errs))

    def to_dict(self) -> dict:
        return asdict(self)


def initial_investment(i: InvestmentInputs) -> dict:
    equip = i.n_chargers * i.equipment_cost_per_charger
    parts = {
        "equipment": equip,
        "installation": i.installation_cost,
        "grid_connection": i.grid_connection_cost,
        "site_preparation": i.site_preparation_cost,
    }
    parts["total"] = sum(parts.values())
    return parts


def _year(i: InvestmentInputs, y: int) -> dict:
    g = (1 + i.annual_growth_pct) ** (y - 1)
    sessions = i.sessions_per_day * 365 * g
    energy = sessions * i.kwh_per_session
    revenue = energy * i.price_per_kwh
    electricity = energy * i.electricity_cost_per_kwh
    variable = energy * i.variable_cost_per_kwh + revenue * i.payment_fee_pct
    gross_margin = revenue - electricity - variable
    fixed = 12 * (i.rent_monthly + i.fixed_opex_monthly) + i.n_chargers * i.maintenance_per_charger_annual
    contribution = gross_margin - fixed
    return {
        "year": y,
        "sessions": sessions,
        "energy_kwh": energy,
        "revenue": revenue,
        "electricity_cost": electricity,
        "variable_cost": variable,
        "gross_margin": gross_margin,
        "fixed_opex": fixed,
        "operating_contribution": contribution,
        "cash_flow": contribution,
    }


def npv(cash_flows: list[float], rate: float) -> float:
    return sum(cf / (1 + rate) ** t for t, cf in enumerate(cash_flows))


def irr(cash_flows: list[float]) -> float | None:
    """IRR by bisection; None when there is no single sign change or no root in range."""
    signs = [cf > 0 for cf in cash_flows if cf != 0]
    changes = sum(1 for a, b in zip(signs, signs[1:]) if a != b)
    if changes != 1:
        return None
    lo, hi = -0.99, 10.0
    f_lo, f_hi = npv(cash_flows, lo), npv(cash_flows, hi)
    if f_lo * f_hi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = npv(cash_flows, mid)
        if abs(f_mid) < 1e-6:
            break
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2


def evaluate(i: InvestmentInputs) -> dict:
    i.validate()
    inv = initial_investment(i)
    years = [_year(i, y) for y in range(1, int(i.analysis_years) + 1)]
    cash_flows = [-inv["total"]] + [y["cash_flow"] for y in years]

    cumulative = -inv["total"]
    discounted_cum = -inv["total"]
    for y in years:
        cumulative += y["cash_flow"]
        discounted_cum += y["cash_flow"] / (1 + i.discount_rate_pct) ** y["year"]
        y["cumulative_cash_flow"] = cumulative
        y["discounted_cash_flow"] = y["cash_flow"] / (1 + i.discount_rate_pct) ** y["year"]
        y["cumulative_discounted"] = discounted_cum

    # Monthly view and payback month.
    monthly = []
    cum = -inv["total"]
    payback_month = None
    for y in years:
        m_cf = y["cash_flow"] / 12
        for m in range(1, 13):
            month_index = (y["year"] - 1) * 12 + m
            cum += m_cf
            monthly.append({"month": month_index, "cash_flow": m_cf, "cumulative": cum})
            if payback_month is None and cum >= 0:
                payback_month = month_index

    y1 = years[0]
    margin_per_session = i.kwh_per_session * (
        i.price_per_kwh - i.electricity_cost_per_kwh - i.variable_cost_per_kwh - i.price_per_kwh * i.payment_fee_pct
    )
    if margin_per_session > 0:
        breakeven_sessions = y1["fixed_opex"] / (365 * margin_per_session)
    else:
        breakeven_sessions = None

    capacity_kwh = i.n_chargers * i.power_kw * 8760
    utilisation = y1["energy_kwh"] / capacity_kwh if capacity_kwh else 0.0

    flags = []
    if margin_per_session <= 0:
        flags.append({"level": "critical", "code": "negative_unit_margin",
                      "message": "Each session loses money: price does not cover electricity and variable costs."})
    if y1["operating_contribution"] <= 0:
        flags.append({"level": "critical", "code": "loss_making_year1",
                      "message": "Year-1 operating contribution is zero or negative; revenue does not cover operating costs."})
    if payback_month is None:
        flags.append({"level": "warning", "code": "payback_not_reached",
                      "message": f"Simple payback is not reached within the {int(i.analysis_years)}-year analysis period."})
    if utilisation > 1.0:
        flags.append({"level": "critical", "code": "impossible_utilisation",
                      "message": f"Implied utilisation is {utilisation:.0%}, which exceeds the physical capacity of the chargers."})
    elif utilisation > 0.5:
        flags.append({"level": "warning", "code": "high_utilisation",
                      "message": f"Implied utilisation of {utilisation:.0%} is very high for public charging; check the demand assumption."})

    npv_val = npv(cash_flows, i.discount_rate_pct)
    irr_val = irr(cash_flows)

    return {
        "inputs": i.to_dict(),
        "labels": {"inputs": "ASSUMPTION", "outputs": "ESTIMATED", "validation": "NOT YET VALIDATED"},
        "assumption_note": InvestmentInputs.ASSUMPTION_NOTE,
        "initial_investment": inv,
        "year1": {
            "sessions_per_day": i.sessions_per_day,
            "energy_kwh": y1["energy_kwh"],
            "revenue": y1["revenue"],
            "electricity_cost": y1["electricity_cost"],
            "variable_cost": y1["variable_cost"],
            "gross_margin": y1["gross_margin"],
            "fixed_opex": y1["fixed_opex"],
            "operating_contribution": y1["operating_contribution"],
            "monthly_operating_contribution": y1["operating_contribution"] / 12,
            "implied_utilisation": utilisation,
            "margin_per_session": margin_per_session,
        },
        "years": years,
        "monthly": monthly,
        "cash_flows": cash_flows,
        "npv": npv_val,
        "irr": irr_val,
        "payback_months": payback_month,
        "payback_years": payback_month / 12 if payback_month else None,
        "breakeven_sessions_per_day": breakeven_sessions,
        "flags": flags,
        "formulas": __doc__,
    }


SCENARIOS = {
    "conservative": {"sessions_per_day": 0.7, "electricity_cost_per_kwh": 1.10, "annual_growth_pct": 0.5},
    "base": {},
    "optimistic": {"sessions_per_day": 1.25, "electricity_cost_per_kwh": 0.95, "annual_growth_pct": 1.25},
}


def scenarios(i: InvestmentInputs, multipliers: dict | None = None) -> dict:
    """Conservative / base / optimistic cases by scaling selected inputs.
    The multipliers are ASSUMPTIONS and are returned with the results."""
    multipliers = multipliers or SCENARIOS
    out = {}
    for name, mult in multipliers.items():
        changes = {k: getattr(i, k) * v for k, v in mult.items()}
        case = replace(i, **changes)
        r = evaluate(case)
        out[name] = {
            "multipliers": mult,
            "sessions_per_day": case.sessions_per_day,
            "initial_investment": r["initial_investment"]["total"],
            "year1_revenue": r["year1"]["revenue"],
            "year1_operating_contribution": r["year1"]["operating_contribution"],
            "npv": r["npv"],
            "irr": r["irr"],
            "payback_years": r["payback_years"],
            "flags": [f["code"] for f in r["flags"]],
            "cumulative_by_year": [round(y["cumulative_cash_flow"], 2) for y in r["years"]]
            if r.get("years") and "cumulative_cash_flow" in r["years"][0] else None,
            "monthly_cumulative": [round(m["cumulative"], 2) for m in r.get("monthly", [])],
        }
    return out


def compare(sites: list[tuple[str, InvestmentInputs]]) -> list[dict]:
    rows = []
    for ref, inp in sites:
        r = evaluate(inp)
        risks = [f["message"] for f in r["flags"]]
        risks.append("Demand (sessions/day) is user-supplied, not measured.")
        rows.append({
            "ref": ref,
            "site_name": inp.site_name,
            "lat": inp.lat,
            "lng": inp.lng,
            "n_chargers": inp.n_chargers,
            "capacity_kw": inp.n_chargers * inp.power_kw,
            "initial_investment": r["initial_investment"]["total"],
            "year1_revenue": r["year1"]["revenue"],
            "year1_operating_contribution": r["year1"]["operating_contribution"],
            "npv": r["npv"],
            "irr": r["irr"],
            "payback_years": r["payback_years"],
            "breakeven_sessions_per_day": r["breakeven_sessions_per_day"],
            "implied_utilisation": r["year1"]["implied_utilisation"],
            "key_risks": risks,
        })
    return rows
