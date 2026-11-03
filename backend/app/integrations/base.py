"""Connector interface, health tracking and secret redaction."""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime

from sqlalchemy.orm import Session

from ..clock import wall_now
from ..config import get_settings
from ..models import ConnectorHealth
from .schema import ExternalEvent

log = logging.getLogger("chargesight.integrations")

# Health states. "connected" is set ONLY after a verified successful response.
HEALTH_STATES = ("not_configured", "configured_unverified", "connected", "error", "disabled")


class ConnectorError(Exception):
    """Raised for connector failures. Messages are redacted before storage."""


def redact(text: str) -> str:
    out = str(text)
    for secret in get_settings().secret_values():
        out = out.replace(secret, "***REDACTED***")
    return out


class RedactingFilter(logging.Filter):
    """Logging filter that removes configured secrets from every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # pragma: no cover
            return True
        clean = redact(msg)
        if clean != msg:
            record.msg = clean
            record.args = ()
        return True


PLATFORM_CONNECTORS = ("google_maps", "open_charge_map", "osm", "google_places")   # shared public-listing providers


def _health_org(db: Session, connector: str) -> int | None:
    from ..tenancy import effective_org

    return None if connector in PLATFORM_CONNECTORS else effective_org(db)


def get_health(db: Session, connector: str) -> ConnectorHealth | None:
    """Health row of a connector: platform-level for public-listing providers, otherwise the
    organisation of the session (the legacy demo workspace for system sessions)."""
    from sqlalchemy import select

    org = _health_org(db, connector)
    q = select(ConnectorHealth).where(ConnectorHealth.connector == connector)
    q = q.where(ConnectorHealth.org_id.is_(None)) if org is None else q.where(ConnectorHealth.org_id == org)
    with db.no_autoflush:
        row = db.scalar(q.limit(1))
    if row is None:
        row = next((o for o in db.new if isinstance(o, ConnectorHealth) and o.connector == connector and o.org_id == org), None)
    return row


def list_health(db: Session) -> list[ConnectorHealth]:
    from sqlalchemy import or_, select

    from ..tenancy import effective_org

    org = effective_org(db)
    return list(db.scalars(select(ConnectorHealth).where(or_(
        (ConnectorHealth.org_id == org) & ConnectorHealth.connector.not_in(PLATFORM_CONNECTORS),
        ConnectorHealth.org_id.is_(None) & ConnectorHealth.connector.in_(PLATFORM_CONNECTORS))).order_by(ConnectorHealth.connector)))


def ensure_health(db: Session, connector: str, state: str = "not_configured") -> ConnectorHealth:
    row = get_health(db, connector)
    if row is None:
        row = ConnectorHealth(connector=connector, org_id=_health_org(db, connector), state=state)
        db.add(row)
    return row


def set_health(db: Session, connector: str, state: str, *, error: str = "", detail: dict | None = None,
               success: bool = False) -> ConnectorHealth:
    if state not in HEALTH_STATES:
        raise ValueError(state)
    row = ensure_health(db, connector)
    now = wall_now()
    row.state = state
    row.last_checked_at = now
    if success:
        row.last_success_at = now
        row.last_error = ""
    if error:
        row.last_error = redact(error)[:500]
    if detail is not None:
        row.detail = {k: (redact(v) if isinstance(v, str) else v) for k, v in detail.items()}
    return row


class ChargerDataConnector(ABC):
    """Adapter for one external system. Implementations must not leak secrets."""

    name: str = "base"
    source: str = "operator_api"   # provenance tag written to every event

    @abstractmethod
    def is_configured(self) -> bool: ...

    @abstractmethod
    def health_check(self) -> dict:
        """Return {'ok': bool, ...}. Raise ConnectorError on failure."""

    @abstractmethod
    def fetch_events(self, since: datetime | None = None) -> list[ExternalEvent]:
        """Return normalised events. Raise ConnectorError on failure."""
