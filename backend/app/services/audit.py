"""Audit log for account, organisation, subscription, integration and platform-administration actions.
Never stores passwords, tokens or credentials: callers pass only identifiers and non-secret facts."""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..models_saas import AuditEvent

_FORBIDDEN_KEYS = {"password", "token", "secret", "credential", "api_key", "csrf", "authorization"}


def _clean(data: dict | None) -> dict:
    out = {}
    for k, v in (data or {}).items():
        if any(f in k.lower() for f in _FORBIDDEN_KEYS):
            continue
        out[k] = v if isinstance(v, (int, float, bool, type(None))) else str(v)[:500]
    return out


def record(db: Session, action: str, *, org_id: int | None = None, actor=None, actor_label: str | None = None,
           target_type: str = "", target_id: str | int = "", ip: str = "", data: dict | None = None) -> AuditEvent:
    ev = AuditEvent(action=action[:60], org_id=org_id, actor_user_id=getattr(actor, "id", None),
                    actor_label=(actor_label or getattr(actor, "email", None) or "system")[:120],
                    target_type=target_type[:40], target_id=str(target_id)[:80], ip=(ip or "")[:64], data=_clean(data))
    db.add(ev)
    return ev
