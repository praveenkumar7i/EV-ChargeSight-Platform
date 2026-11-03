"""Discrete-event maintenance simulation: FIFO vs explainable-priority repair.

Fairness design
---------------
* For each seed one **fault trace** is generated: arrival time, charger, category,
  remote-reset outcome, travel time and repair time are all drawn up front.
* The *same* trace is replayed under both policies (common random numbers), so
  the only difference between the two runs is the order in which technicians
  pick faults from the queue.
* Sensitivity variants that scale repair or travel time reuse the same base
  draws and multiply them, so variants differ only in the changed parameter.

Model (all values are ASSUMPTIONS, configurable in ExperimentConfig)
------------------------------------------------------------------
* Faults arrive per charger as a Poisson process (rate per charger-day).
* Faults are independent of charger state, so a charger can carry more than
  one open fault; it is down while any of its faults is unresolved.
* Remote-resettable categories get a remote Reset `reset_delay_min` after
  detection. Success resolves the fault without a technician (same outcome in
  both policies). Failure puts it in the technician queue at that moment.
* Technicians handle one job at a time: travel (one-way) + repair. Return
  travel is not modelled.
* No new assignments start after the horizon. Faults still open at the horizon
  are censored: their downtime is counted up to the horizon and they are
  reported as unresolved.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import asdict, dataclass, field, replace
from statistics import mean

from .catalog import DEMO_SITES, FAULT_TYPES
from .priority import PriorityConfig, PriorityInput, compute, sort_key
from .stats import paired_summary, percentile


@dataclass
class SiteModel:
    code: str
    daily_sessions: float
    travel_min: tuple[float, float]
    chargers_kw: list[float]


def default_sites() -> list[SiteModel]:
    return [
        SiteModel(s.code, s.daily_sessions, s.travel_min, [kw for _, kw in s.chargers])
        for s in DEMO_SITES
    ]


@dataclass
class ExperimentConfig:
    days: float = 30.0
    seeds: list[int] = field(default_factory=lambda: list(range(1, 31)))
    technicians: int = 2
    fault_rate_per_charger_day: float = 0.05
    category_mix: dict[str, float] = field(
        default_factory=lambda: {k: v.default_share for k, v in FAULT_TYPES.items()}
    )
    remote_reset_success: dict[str, float] = field(
        default_factory=lambda: {k: v.remote_reset_success for k, v in FAULT_TYPES.items()}
    )
    repair_mean_min: dict[str, float] = field(
        default_factory=lambda: {k: v.repair_mean_min for k, v in FAULT_TYPES.items()}
    )
    repair_sigma: float = 0.5                 # lognormal shape
    repair_multiplier: float = 1.0
    travel_multiplier: float = 1.0
    reset_delay_min: float = 10.0
    priority: dict = field(default_factory=lambda: PriorityConfig().to_dict())
    utilisation_fraction: dict[str, float] = field(default_factory=lambda: {
        "KRM": 0.25, "WFD": 0.22, "HSR": 0.18, "ECY": 0.15, "HBL": 0.13, "YLK": 0.07})
    margin_per_kwh_inr: float = 8.0           # for the optional downtime-cost estimate
    sites: list[dict] | None = None           # override the default demo fleet
    fleet_multiplier: int = 1                 # replicate every site's chargers (and demand) N times

    def validate(self) -> None:
        errs = []
        if not 0 < self.days <= 366:
            errs.append("days must be in (0, 366].")
        if not self.seeds or len(self.seeds) > 200:
            errs.append("seeds must contain 1 to 200 values.")
        if not 1 <= int(self.technicians) <= 20:
            errs.append("technicians must be 1..20.")
        if not 0 < self.fault_rate_per_charger_day <= 5:
            errs.append("fault_rate_per_charger_day must be in (0, 5].")
        if not 1 <= int(self.fleet_multiplier) <= 5:
            errs.append("fleet_multiplier must be 1..5.")
        if self.repair_multiplier <= 0 or self.travel_multiplier <= 0:
            errs.append("multipliers must be > 0.")
        mix = {k: v for k, v in self.category_mix.items() if v > 0}
        if not mix or any(k not in FAULT_TYPES for k in mix):
            errs.append("category_mix must use known categories with positive shares.")
        for k, p in self.remote_reset_success.items():
            if not 0 <= p <= 1:
                errs.append(f"remote_reset_success[{k}] must be within 0..1.")
        for k, m in self.repair_mean_min.items():
            if m <= 0:
                errs.append(f"repair_mean_min[{k}] must be > 0.")
        try:
            PriorityConfig.from_dict(self.priority)
        except ValueError as e:
            errs.append(str(e))
        if errs:
            raise ValueError(" ".join(errs))

    def site_models(self) -> list[SiteModel]:
        if self.sites:
            base = [SiteModel(s["code"], float(s["daily_sessions"]), tuple(s["travel_min"]),
                              [float(k) for k in s["chargers_kw"]]) for s in self.sites]
        else:
            base = default_sites()
        m = int(self.fleet_multiplier or 1)
        if m == 1:
            return base
        return [SiteModel(s.code, s.daily_sessions * m, s.travel_min, s.chargers_kw * m) for s in base]

    def to_dict(self) -> dict:
        return asdict(self)

    def hash(self) -> str:
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True).encode()).hexdigest()

    @classmethod
    def from_dict(cls, d: dict | None) -> "ExperimentConfig":
        d = dict(d or {})
        base = cls()
        for key in ("category_mix", "remote_reset_success", "repair_mean_min", "utilisation_fraction"):
            merged = {k: float(v) for k, v in getattr(base, key).items()}
            if key in d and d[key] is not None:
                merged.update({k: float(v) for k, v in d[key].items()})
            d[key] = merged
        for key in ("days", "fault_rate_per_charger_day", "repair_sigma", "repair_multiplier",
                    "travel_multiplier", "reset_delay_min", "margin_per_kwh_inr"):
            if key in d and d[key] is not None:
                d[key] = float(d[key])
        if "priority" in d and d["priority"] is not None:
            d["priority"] = PriorityConfig.from_dict(d["priority"]).to_dict()
        allowed = set(base.to_dict())
        unknown = set(d) - allowed
        if unknown:
            raise ValueError(f"Unknown experiment parameter(s): {', '.join(sorted(unknown))}.")
        cfg = replace(base, **{k: v for k, v in d.items() if v is not None})
        cfg.seeds = [int(s) for s in cfg.seeds]
        cfg.technicians = int(cfg.technicians)
        cfg.fleet_multiplier = int(cfg.fleet_multiplier)
        cfg.validate()
        return cfg


@dataclass
class Fault:
    id: int
    charger: int            # index into flattened charger list
    site: str
    category: str
    detected: float         # minutes from start
    reset_success: bool
    travel: float           # minutes (already scaled)
    repair: float           # minutes (already scaled)
    # filled by the simulation
    queue_entry: float | None = None
    assigned: float | None = None
    resolved: float | None = None
    by_technician: bool = False


@dataclass
class Trace:
    seed: int
    horizon_min: float
    chargers: list[tuple[str, float]]      # (site code, kW)
    faults: list[Fault]


def generate_trace(cfg: ExperimentConfig, seed: int) -> Trace:
    """Draw one fault trace. Repair/travel draws are taken before scaling so the
    same seed yields the same underlying faults across sensitivity variants."""
    rng = random.Random(seed)
    sites = cfg.site_models()
    chargers = [(s.code, kw) for s in sites for kw in s.chargers_kw]
    site_by_code = {s.code: s for s in sites}
    horizon = cfg.days * 24 * 60
    rate_per_min = cfg.fault_rate_per_charger_day / (24 * 60)
    mix = {k: v for k, v in cfg.category_mix.items() if v > 0}
    cats, weights = list(mix), list(mix.values())

    faults: list[Fault] = []
    for ci, (site_code, _kw) in enumerate(chargers):
        t = 0.0
        while True:
            t += rng.expovariate(rate_per_min)
            if t >= horizon:
                break
            cat = rng.choices(cats, weights)[0]
            reset_u = rng.random()
            mean_r = cfg.repair_mean_min.get(cat, FAULT_TYPES[cat].repair_mean_min)
            mu = math.log(mean_r) - cfg.repair_sigma ** 2 / 2
            repair = rng.lognormvariate(mu, cfg.repair_sigma)
            lo, hi = site_by_code[site_code].travel_min
            travel = rng.uniform(lo, hi)
            faults.append(Fault(
                id=0, charger=ci, site=site_code, category=cat, detected=t,
                reset_success=reset_u < cfg.remote_reset_success.get(cat, 0.0),
                travel=travel * cfg.travel_multiplier,
                repair=repair * cfg.repair_multiplier,
            ))
    faults.sort(key=lambda f: (f.detected, f.charger))
    for i, f in enumerate(faults):
        f.id = i + 1
    return Trace(seed, horizon, chargers, faults)


def _copy_faults(trace: Trace) -> list[Fault]:
    return [replace(f) for f in trace.faults]


def simulate(trace: Trace, cfg: ExperimentConfig, policy: str, stats: dict | None = None) -> list[Fault]:
    """Replay a trace under 'fifo' or 'priority'. Returns faults with timings filled.
    If `stats` is given it receives dispatch counts: `decisions` (technician
    assignments) and `contended` (assignments made while more than one fault was
    waiting, i.e. the only moments where the two policies can differ)."""
    if policy not in ("fifo", "priority"):
        raise ValueError("policy must be 'fifo' or 'priority'")
    pcfg = PriorityConfig.from_dict(cfg.priority)
    sites = {s.code: s for s in cfg.site_models()}
    max_sessions = max((s.daily_sessions for s in sites.values()), default=0)
    faults = _copy_faults(trace)

    for f in faults:
        if cfg.remote_reset_success.get(f.category, 0) > 0:
            if f.reset_success:
                f.resolved = f.detected + cfg.reset_delay_min
                continue
            f.queue_entry = f.detected + cfg.reset_delay_min
        else:
            f.queue_entry = f.detected

    pending = sorted((f for f in faults if f.queue_entry is not None), key=lambda f: (f.queue_entry, f.id))
    queue: list[Fault] = []
    tech_free = [0.0] * cfg.technicians
    t = 0.0
    pi = 0

    def down_at(time: float) -> set[int]:
        return {
            f.charger for f in faults
            if f.detected <= time and (f.resolved is None or f.resolved > time)
        }

    while True:
        # admit arrivals up to t
        while pi < len(pending) and pending[pi].queue_entry <= t:
            queue.append(pending[pi])
            pi += 1
        if t < trace.horizon_min:
            free = [i for i, ft in enumerate(tech_free) if ft <= t]
            while free and queue:
                if stats is not None:
                    stats["decisions"] = stats.get("decisions", 0) + 1
                    if len(queue) > 1:
                        stats["contended"] = stats.get("contended", 0) + 1
                if policy == "fifo":
                    nxt = min(queue, key=lambda f: (f.queue_entry, f.id))
                else:
                    down = down_at(t)
                    results = []
                    for f in queue:
                        site = sites[f.site]
                        other_kw = sum(
                            kw for ci, (sc, kw) in enumerate(trace.chargers)
                            if sc == f.site and ci != f.charger and ci not in down
                        )
                        r = compute(PriorityInput(
                            ticket_ref=f"F{f.id:05d}", fault_category=f.category,
                            site_daily_sessions=site.daily_sessions, max_site_daily_sessions=max_sessions,
                            charger_kw=trace.chargers[f.charger][1], other_working_kw_at_site=other_kw,
                            age_hours=(t - f.detected) / 60.0, detected_at=f.detected,
                        ), pcfg)
                        results.append((r, f))
                    nxt = min(results, key=lambda rf: sort_key(rf[0]))[1]
                queue.remove(nxt)
                tech = free.pop(0)
                nxt.assigned = t
                nxt.resolved = t + nxt.travel + nxt.repair
                nxt.by_technician = True
                tech_free[tech] = nxt.resolved
        # next decision time
        candidates = []
        if pi < len(pending):
            candidates.append(pending[pi].queue_entry)
        busy = [ft for ft in tech_free if ft > t]
        if queue and busy:
            candidates.append(min(busy))
        candidates = [c for c in candidates if c > t]
        if not candidates or t >= trace.horizon_min:
            break
        t = min(candidates)
    return faults


def _union_minutes(intervals: list[tuple[float, float]]) -> float:
    total, cur_s, cur_e = 0.0, None, None
    for s, e in sorted(intervals):
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                total += cur_e - cur_s
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_e is not None:
        total += cur_e - cur_s
    return total


METRIC_DEFS = {
    "availability_pct": ("Fleet availability", "%",
                         "100 x (1 - total charger downtime / (number of chargers x horizon)). Downtime is the union of each charger's open-fault intervals, clipped to the horizon."),
    "downtime_h": ("Total charger downtime", "charger-hours",
                   "Sum over chargers of the union of [detected, resolved) intervals, clipped to the horizon; unresolved faults count until the horizon."),
    "weighted_downtime_h": ("Utilisation-weighted downtime", "weighted charger-hours",
                            "Sum of each charger's downtime x (its site's expected daily sessions / mean expected daily sessions per charger)."),
    "high_sev_wait_mean_min": ("Mean wait, high-severity faults", "minutes",
                               "Mean of (assigned - detected) over technician-dispatched faults with severity = critical (S = 1)."),
    "high_sev_wait_p95_min": ("95th-percentile wait, high-severity faults", "minutes",
                              "Linear-interpolated 95th percentile of the same waits."),
    "low_sev_wait_max_min": ("Maximum wait, lower-severity faults", "minutes",
                             "Maximum (assigned - detected) over technician-dispatched faults with S < 1; unassigned faults count with wait = horizon - detected."),
    "mtta_min": ("MTTA (mean time to acknowledge)", "minutes",
                 "Mean (assigned - detected) over technician-dispatched faults that were assigned before the horizon."),
    "mttr_min": ("MTTR (mean time to resolve)", "minutes",
                 "Mean (resolved - detected) over all faults resolved within the horizon, including remote resets."),
    "energy_not_delivered_kwh": ("Estimated energy not delivered", "kWh (ESTIMATED)",
                                 "Sum of downtime_h x charger kW x site utilisation fraction (ASSUMPTION)."),
    "downtime_cost_inr": ("Estimated downtime cost", "INR (ESTIMATED)",
                          "energy_not_delivered_kwh x margin per kWh (ASSUMPTION). Lost margin only; excludes penalties and reputation."),
    "faults": ("Faults in trace", "count", "Number of faults generated for this seed."),
    "remote_resolved": ("Resolved by remote reset", "count", "Faults cleared by a successful remote Reset."),
    "unresolved_at_horizon": ("Unresolved at horizon", "count", "Faults not resolved before the end of the observation period."),
}


def metrics(trace: Trace, faults: list[Fault], cfg: ExperimentConfig) -> dict:
    H = trace.horizon_min
    n = len(trace.chargers)
    sites = {s.code: s for s in cfg.site_models()}
    mean_sessions_per_charger_site = mean(sites[sc].daily_sessions for sc, _ in trace.chargers) or 1.0

    per_charger: dict[int, list[tuple[float, float]]] = {}
    for f in faults:
        end = min(f.resolved if f.resolved is not None else H, H)
        if end > f.detected:
            per_charger.setdefault(f.charger, []).append((f.detected, end))
    downtime_min = {c: _union_minutes(iv) for c, iv in per_charger.items()}
    total_down_h = sum(downtime_min.values()) / 60.0
    weighted = sum(
        (m / 60.0) * (sites[trace.chargers[c][0]].daily_sessions / mean_sessions_per_charger_site)
        for c, m in downtime_min.items()
    )
    energy_lost = sum(
        (m / 60.0) * trace.chargers[c][1] * cfg.utilisation_fraction.get(trace.chargers[c][0], 0.0)
        for c, m in downtime_min.items()
    )

    dispatched = [f for f in faults if f.by_technician and f.assigned is not None]
    high = [f.assigned - f.detected for f in dispatched if FAULT_TYPES[f.category].severity == "critical"]
    low_waits = [f.assigned - f.detected for f in dispatched if FAULT_TYPES[f.category].severity != "critical"]
    low_waits += [H - f.detected for f in faults
                  if f.queue_entry is not None and f.assigned is None and FAULT_TYPES[f.category].severity != "critical"]
    resolved = [f for f in faults if f.resolved is not None and f.resolved <= H]

    return {
        "availability_pct": 100.0 * (1 - (total_down_h * 60) / (n * H)) if n and H else None,
        "downtime_h": total_down_h,
        "weighted_downtime_h": weighted,
        "high_sev_wait_mean_min": mean(high) if high else None,
        "high_sev_wait_p95_min": percentile(high, 95),
        "low_sev_wait_max_min": max(low_waits) if low_waits else None,
        "mtta_min": mean(f.assigned - f.detected for f in dispatched) if dispatched else None,
        "mttr_min": mean(f.resolved - f.detected for f in resolved) if resolved else None,
        "energy_not_delivered_kwh": energy_lost,
        "downtime_cost_inr": energy_lost * cfg.margin_per_kwh_inr,
        "faults": len(faults),
        "remote_resolved": sum(1 for f in faults if f.resolved is not None and not f.by_technician),
        "unresolved_at_horizon": sum(1 for f in faults if f.resolved is None or f.resolved > H),
    }


def run_comparison(cfg: ExperimentConfig, include_traces: bool = False) -> dict:
    cfg.validate()
    per_seed = []
    for seed in cfg.seeds:
        trace = generate_trace(cfg, seed)
        st: dict = {}
        fifo = simulate(trace, cfg, "fifo", st)
        prio = simulate(trace, cfg, "priority")
        row = {
            "seed": seed,
            "trace_signature": trace_signature(trace),
            "dispatch_decisions": st.get("decisions", 0),
            "contended_decisions": st.get("contended", 0),
            "fifo": metrics(trace, fifo, cfg),
            "priority": metrics(trace, prio, cfg),
        }
        if include_traces:
            row["trace"] = [asdict(f) for f in trace.faults]
        per_seed.append(row)

    summary = {}
    for key, (label, unit, definition) in METRIC_DEFS.items():
        a = [r["fifo"][key] for r in per_seed]
        b = [r["priority"][key] for r in per_seed]
        s = paired_summary(a, b)
        s.update({"label": label, "unit": unit, "definition": definition})
        summary[key] = s
    decisions = sum(r["dispatch_decisions"] for r in per_seed)
    contended = sum(r["contended_decisions"] for r in per_seed)
    contention_pct = 100.0 * contended / decisions if decisions else 0.0
    identical = all(r["fifo"] == r["priority"] for r in per_seed)
    if decisions == 0:
        why = "No fault needed a technician in this configuration, so the dispatch policy never mattered."
    elif contention_pct < 10:
        why = (f"Only {contention_pct:.1f}% of technician assignments ({contended} of {decisions}) were made while more than one "
               "fault was waiting. FIFO and priority can only differ at those moments, so their results are similar. "
               "Increase the fault rate or fleet size, or reduce technicians, to create queues.")
    else:
        why = (f"{contention_pct:.1f}% of technician assignments ({contended} of {decisions}) were made while several faults "
               "were waiting, so the ordering policy had room to change outcomes.")
    return {
        "config": cfg.to_dict(),
        "config_hash": cfg.hash(),
        "data_source": "simulation",
        "label": "SIMULATED",
        "queueing": {"dispatch_decisions": decisions, "contended_decisions": contended,
                     "contention_pct": round(contention_pct, 2), "identical_results": identical,
                     "explanation": why,
                     "definition": "A contended decision is a technician assignment made while more than one fault was waiting (counted in the FIFO replay)."},
        "per_seed": per_seed,
        "summary": summary,
        "metric_definitions": {k: {"label": v[0], "unit": v[1], "definition": v[2]} for k, v in METRIC_DEFS.items()},
        "notes": [
            "Both policies replay the identical fault trace for each seed (see trace_signature).",
            "Differences are priority minus FIFO; a negative difference in downtime or wait is an improvement.",
            "All parameters are assumptions, not measured field data.",
        ],
    }


def trace_signature(trace: Trace) -> str:
    payload = [(f.charger, f.category, round(f.detected, 6), f.reset_success, round(f.travel, 6), round(f.repair, 6))
               for f in trace.faults]
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()[:16]


SENSITIVITY_VARIANTS: dict[str, dict] = {
    "baseline": {},
    "technicians_1": {"technicians": 1},
    "technicians_3": {"technicians": 3},
    "repair_x0.5": {"repair_multiplier": 0.5},
    "repair_x2": {"repair_multiplier": 2.0},
    "travel_x0.5": {"travel_multiplier": 0.5},
    "travel_x2": {"travel_multiplier": 2.0},
    "fault_rate_0.02": {"fault_rate_per_charger_day": 0.02},
    "fault_rate_0.10": {"fault_rate_per_charger_day": 0.10},
    "weights_equal": {"priority": {"weights": {"severity": 0.25, "utilisation": 0.25, "capacity": 0.25, "age": 0.25}}},
    "weights_severity_only": {"priority": {"weights": {"severity": 1, "utilisation": 0, "capacity": 0, "age": 0}}},
}

SENSITIVITY_METRICS = ("availability_pct", "downtime_h", "weighted_downtime_h",
                       "high_sev_wait_mean_min", "low_sev_wait_max_min")


def run_sensitivity(base: ExperimentConfig, variants: list[str] | None = None) -> dict:
    variants = variants or list(SENSITIVITY_VARIANTS)
    rows = []
    for name in variants:
        if name not in SENSITIVITY_VARIANTS:
            raise ValueError(f"Unknown sensitivity variant '{name}'.")
        changes = dict(SENSITIVITY_VARIANTS[name])
        d = base.to_dict()
        if "priority" in changes:
            p = dict(d["priority"])
            p.update(changes.pop("priority"))
            changes["priority"] = p
        d.update(changes)
        cfg = ExperimentConfig.from_dict(d)
        res = run_comparison(cfg)
        rows.append({
            "variant": name,
            "changes": SENSITIVITY_VARIANTS[name],
            "config_hash": cfg.hash(),
            "summary": {k: res["summary"][k] for k in SENSITIVITY_METRICS},
        })
    return {"base_config": base.to_dict(), "base_config_hash": base.hash(), "variants": rows,
            "label": "SIMULATED", "data_source": "simulation"}
