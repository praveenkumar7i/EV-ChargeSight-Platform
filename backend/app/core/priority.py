"""Explainable fault-priority engine.

    P = wS*S + wU*U + wR*R + wA*A          (all factors in [0, 1])

S  Severity     critical = 1.0 (cannot charge), major = 0.6 (degraded), minor = 0.2
U  Utilisation  site expected daily sessions / max expected daily sessions over
                the sites being compared. 0 if the max is 0.
R  Capacity     kW of the faulted charger / (kW of the faulted charger + kW of the
                *other* chargers at the same site that can still charge).
                R = 1 when this was the last working charger at the site.
A  Age          min(age_hours / age_cap_hours, 1); age_cap_hours defaults to 24.

Inputs are built by services/priority_service.py. For a user-reported site-wide issue affecting N
chargers, R uses N x the reported charger's kW as the lost capacity (ASSUMPTION: the other affected
chargers have the same rating); N = 1 is exactly the rule above. Severity comes from the existing
definitions (critical = cannot charge, major = degraded, minor = no effect); safety faults are always
critical. `notes` on the result record where each input came from (measured vs reported).

Weights default to 0.4 / 0.3 / 0.2 / 0.1 (a design ASSUMPTION from the research
report, not an industry standard). Weights must be non-negative and are
normalised to sum to 1, so P is always in [0, 1].

Ranking (deterministic, used everywhere: live queue, experiment, retrospective):
  1. Escalated tickets first. A ticket is escalated once it has waited at least
     `starvation_cap_hours` (default 24 h). Escalated tickets are ordered by
     detection time (oldest first). This hard cap guarantees that low-priority
     faults cannot starve: the A factor alone (max 0.1) is too small to do so.
  2. Higher score first.
  3. Ties broken by earlier detection time, then by ticket id.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Sequence

from .catalog import FAULT_TYPES, SEVERITY_MEANING, SEVERITY_VALUES, severity_of

FACTOR_ORDER = ("severity", "utilisation", "capacity", "age")
FACTOR_LABELS = {
    "severity": "Severity (S)",
    "utilisation": "Site utilisation (U)",
    "capacity": "Capacity lost (R)",
    "age": "Fault age (A)",
}


@dataclass(frozen=True)
class PriorityWeights:
    severity: float = 0.4
    utilisation: float = 0.3
    capacity: float = 0.2
    age: float = 0.1

    def as_dict(self) -> dict[str, float]:
        return {k: getattr(self, k) for k in FACTOR_ORDER}

    def normalised(self) -> "PriorityWeights":
        vals = self.as_dict()
        for k, v in vals.items():
            if v is None or not isinstance(v, (int, float)) or math.isnan(v) or v < 0:
                raise ValueError(f"Weight '{k}' must be a non-negative number, got {v!r}.")
        total = sum(vals.values())
        if total <= 0:
            raise ValueError("At least one weight must be greater than zero.")
        return PriorityWeights(**{k: v / total for k, v in vals.items()})

    @classmethod
    def from_dict(cls, d: dict | None) -> "PriorityWeights":
        d = d or {}
        return cls(**{k: float(d.get(k, getattr(cls(), k))) for k in FACTOR_ORDER})


@dataclass(frozen=True)
class PriorityConfig:
    weights: PriorityWeights = field(default_factory=PriorityWeights)
    age_cap_hours: float = 24.0
    starvation_cap_hours: float = 24.0

    @classmethod
    def from_dict(cls, d: dict | None) -> "PriorityConfig":
        d = d or {}
        cfg = cls(
            weights=PriorityWeights.from_dict(d.get("weights")),
            age_cap_hours=float(d.get("age_cap_hours", 24.0)),
            starvation_cap_hours=float(d.get("starvation_cap_hours", 24.0)),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        self.weights.normalised()
        if not (self.age_cap_hours > 0):
            raise ValueError("age_cap_hours must be > 0.")
        if not (self.starvation_cap_hours > 0):
            raise ValueError("starvation_cap_hours must be > 0.")

    def to_dict(self) -> dict:
        return {
            "weights": self.weights.as_dict(),
            "age_cap_hours": self.age_cap_hours,
            "starvation_cap_hours": self.starvation_cap_hours,
        }


@dataclass
class PriorityInput:
    ticket_ref: str                    # ticket code or fault id, used for ties and explanations
    fault_category: str
    site_daily_sessions: float
    max_site_daily_sessions: float
    charger_kw: float
    other_working_kw_at_site: float    # kW of OTHER chargers at the site that can charge now
    age_hours: float
    detected_at: datetime | float      # datetime (live) or minutes (experiment); only used for ordering
    site_name: str = ""
    severity_level: str | None = None  # optional operator override (critical/major/minor)
    notes: list[str] = field(default_factory=list)   # provenance of the inputs (shown with the explanation)


@dataclass
class FactorBreakdown:
    key: str
    label: str
    value: float          # normalised input, 0..1
    weight: float         # normalised weight
    contribution: float   # weight * value
    detail: str           # what the raw input was


@dataclass
class PriorityResult:
    ticket_ref: str
    score: float
    factors: list[FactorBreakdown]
    escalated: bool
    warnings: list[str]
    detected_at: datetime | float
    severity_level: str
    rank: int | None = None
    explanation: str = ""
    above: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        if isinstance(self.detected_at, datetime):
            d["detected_at"] = self.detected_at.isoformat() + "Z"
        d["score"] = round(self.score, 4)
        for f in d["factors"]:
            f["value"] = round(f["value"], 4)
            f["weight"] = round(f["weight"], 4)
            f["contribution"] = round(f["contribution"], 4)
        return d


def _clean(x: float | None, name: str, warnings: list[str]) -> float:
    """Return a finite, non-negative float; record a warning when repaired."""
    if x is None or not isinstance(x, (int, float)) or math.isnan(x) or math.isinf(x):
        warnings.append(f"{name} missing or invalid; treated as 0.")
        return 0.0
    if x < 0:
        warnings.append(f"{name} was negative ({x}); treated as 0.")
        return 0.0
    return float(x)


def _clamp01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def compute(inp: PriorityInput, config: PriorityConfig | None = None) -> PriorityResult:
    config = config or PriorityConfig()
    w = config.weights.normalised()
    warnings: list[str] = []

    level, s_val, sev_warn = severity_of(inp.fault_category)
    warnings += sev_warn
    if inp.severity_level and inp.severity_level != level:
        if inp.severity_level in SEVERITY_VALUES:
            warnings.append(f"Severity overridden from {level} to {inp.severity_level} when the fault was raised.")
            level, s_val = inp.severity_level, SEVERITY_VALUES[inp.severity_level]
        else:
            warnings.append(f"Unknown severity override '{inp.severity_level}' ignored.")

    sessions = _clean(inp.site_daily_sessions, "Site daily sessions", warnings)
    max_sessions = _clean(inp.max_site_daily_sessions, "Max site daily sessions", warnings)
    if max_sessions <= 0:
        u_val = 0.0
        if sessions > 0:
            warnings.append("Max site sessions is 0; utilisation treated as 0.")
    else:
        if sessions > max_sessions:
            warnings.append("Site sessions exceed the comparison maximum; clamped to 1.")
        u_val = _clamp01(sessions / max_sessions)

    kw = _clean(inp.charger_kw, "Charger power", warnings)
    other_kw = _clean(inp.other_working_kw_at_site, "Other working kW at site", warnings)
    if kw <= 0:
        r_val = 0.0
        warnings.append("Charger power is 0; capacity factor treated as 0.")
    else:
        r_val = _clamp01(kw / (kw + other_kw))

    age_h = _clean(inp.age_hours, "Fault age", warnings)
    a_val = _clamp01(age_h / config.age_cap_hours)
    escalated = age_h >= config.starvation_cap_hours

    ft = FAULT_TYPES.get(inp.fault_category)
    factors = [
        FactorBreakdown("severity", FACTOR_LABELS["severity"], s_val, w.severity, w.severity * s_val,
                        f"{ft.label if ft else inp.fault_category}: {level} ({SEVERITY_MEANING[level]})"),
        FactorBreakdown("utilisation", FACTOR_LABELS["utilisation"], u_val, w.utilisation, w.utilisation * u_val,
                        f"{sessions:g} expected sessions/day vs network max {max_sessions:g}"),
        FactorBreakdown("capacity", FACTOR_LABELS["capacity"], r_val, w.capacity, w.capacity * r_val,
                        f"{kw:g} kW of {kw + other_kw:g} kW still able to charge at the site"),
        FactorBreakdown("age", FACTOR_LABELS["age"], a_val, w.age, w.age * a_val,
                        f"waiting {age_h:.1f} h (cap {config.age_cap_hours:g} h)"),
    ]
    score = sum(f.contribution for f in factors)
    return PriorityResult(
        ticket_ref=inp.ticket_ref,
        score=score,
        factors=factors,
        escalated=escalated,
        warnings=warnings,
        detected_at=inp.detected_at,
        severity_level=level,
        notes=list(inp.notes),
    )


def sort_key(r: PriorityResult):
    """Escalated first (oldest first), then score desc, then detection asc, then ref."""
    det = r.detected_at.timestamp() if isinstance(r.detected_at, datetime) else float(r.detected_at)
    if r.escalated:
        return (0, det, 0.0, r.ticket_ref)
    return (1, -round(r.score, 12), det, r.ticket_ref)


def _phrases(r: PriorityResult) -> list[str]:
    f = {x.key: x for x in r.factors}
    out = []
    out.append(
        {"critical": "the charger cannot charge", "major": "charging is degraded",
         "minor": "the fault is minor"}[r.severity_level]
    )
    u = f["utilisation"].value
    out.append("it is at one of the busiest sites" if u >= 0.75 else
               "it is at a moderately busy site" if u >= 0.4 else "it is at a low-traffic site")
    rv = f["capacity"].value
    out.append("it was the last working charger at its site" if rv >= 0.999 else
               f"it removes {rv:.0%} of the site's remaining capacity")
    return out


def rank(results: Sequence[PriorityResult], max_above: int = 3) -> list[PriorityResult]:
    """Order results and attach rank, readable explanation and 'ranked above' comparisons."""
    ordered = sorted(results, key=sort_key)
    n = len(ordered)
    for i, r in enumerate(ordered):
        r.rank = i + 1
        parts = " + ".join(f"{x.label.split(' (')[0]} {x.contribution:.2f}" for x in r.factors)
        top = max(r.factors, key=lambda x: x.contribution)
        text = (
            f"Rank {r.rank} of {n}. Score {r.score:.2f} = {parts}. "
            f"Largest driver: {top.label} ({top.contribution:.2f}). "
            f"Ranked here because {', '.join(_phrases(r))}."
        )
        if r.escalated:
            text += " Escalated: it has waited longer than the starvation cap, so it is served ahead of non-escalated tickets."
        if r.warnings:
            text += " Data warnings: " + " ".join(r.warnings)
        r.explanation = text

        above = []
        for other in ordered[max(0, i - max_above):i]:
            diffs = {
                a.key: a.contribution - b.contribution
                for a, b in zip(other.factors, r.factors)
            }
            if other.escalated and not r.escalated:
                reason = "escalated after waiting longer than the starvation cap"
            elif abs(other.score - r.score) < 1e-9:
                reason = "same score; ranked above because it was detected earlier"
            else:
                k = max(diffs, key=lambda key: diffs[key])
                reason = f"mainly higher {FACTOR_LABELS[k]} (+{diffs[k]:.2f})"
            above.append({
                "ticket_ref": other.ticket_ref,
                "score": round(other.score, 4),
                "score_gap": round(other.score - r.score, 4),
                "factor_gaps": {k: round(v, 4) for k, v in diffs.items()},
                "reason": reason,
            })
        r.above = list(reversed(above))   # nearest first
    return ordered
