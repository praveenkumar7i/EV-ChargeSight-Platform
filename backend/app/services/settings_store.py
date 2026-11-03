"""Runtime configuration persisted in the `settings` table."""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..core.priority import PriorityConfig
from ..models import Setting

DEFAULT_SLA_HOURS = {"critical": 4.0, "major": 12.0, "minor": 48.0}   # ASSUMPTION


# Platform-wide keys (shared by every organisation). All other keys are per organisation: the legacy
# demo workspace keeps the original unprefixed keys, other organisations use "o<id>:<key>".
PLATFORM_KEYS = {"public_snapshot", "billing_policy", "v9_migration", "receipt_counter", "plan_catalogue"}


def scoped_key(db: Session, key: str) -> str:
    from ..tenancy import LEGACY_ORG_ID, effective_org

    org = effective_org(db)
    return key if key in PLATFORM_KEYS or org == LEGACY_ORG_ID else f"o{org}:{key}"


def get(db: Session, key: str, default):
    row = db.get(Setting, scoped_key(db, key))
    return row.value if row is not None else default


def put(db: Session, key: str, value) -> None:
    k = scoped_key(db, key)
    row = db.get(Setting, k)
    if row is None:
        db.add(Setting(key=k, value=value))
    else:
        row.value = value


def priority_config(db: Session) -> PriorityConfig:
    return PriorityConfig.from_dict(get(db, "priority", PriorityConfig().to_dict()))


def set_priority_config(db: Session, d: dict) -> PriorityConfig:
    cfg = PriorityConfig.from_dict(d)          # validates; raises ValueError
    put(db, "priority", cfg.to_dict())
    return cfg


def sla_hours(db: Session) -> dict:
    return get(db, "sla_hours", DEFAULT_SLA_HOURS)


# v8: technician assignment policy (all ASSUMPTIONS, editable via /api/assignment/policy)
DEFAULT_ASSIGNMENT_POLICY = {
    "auto_assign": True,                 # automatic assignment for SIMULATED tickets (subject to the simulator's auto_dispatch)
    "auto_assign_live_sources": False,   # OCPP / operator-API tickets: off until real technicians are registered
    "emergency_override": False,         # allow a qualified technician above capacity for critical/escalated tickets
    "travel_speed_kmh": 20.0,            # only for straight-line travel estimates when both locations are known
    "wait_for_remote_reset": True,       # give a scheduled remote reset a chance before dispatching (existing behaviour)
}


def assignment_policy(db: Session) -> dict:
    return {**DEFAULT_ASSIGNMENT_POLICY, **(get(db, "assignment_policy", {}) or {})}


def set_assignment_policy(db: Session, d: dict) -> dict:
    unknown = set(d) - set(DEFAULT_ASSIGNMENT_POLICY)
    if unknown:
        raise ValueError(f"Unknown policy key(s): {', '.join(sorted(unknown))}.")
    merged = {**assignment_policy(db), **d}
    if not (1.0 <= float(merged["travel_speed_kmh"]) <= 120.0):
        raise ValueError("travel_speed_kmh must be between 1 and 120.")
    for k in ("auto_assign", "auto_assign_live_sources", "emergency_override", "wait_for_remote_reset"):
        merged[k] = bool(merged[k])
    put(db, "assignment_policy", merged)
    return merged
