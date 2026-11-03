"""Internal schema for external charger data, and mappings from common
operator vocabularies (including OCPP 1.6) into it."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from ..clock import to_naive_utc
from ..core.catalog import OCPP_ERROR_TO_CATEGORY

InternalEventType = Literal[
    "boot", "heartbeat", "status_available", "session_start", "session_stop",
    "meter_values", "fault", "offline", "reconnect", "fault_resolved", "status_change",
]

# External status strings -> internal status. Keys are compared lower-case.
STATUS_MAP = {
    "available": "available",
    "preparing": "available",
    "charging": "charging",
    "suspendedev": "charging",
    "suspendedevse": "charging",
    "finishing": "available",
    "reserved": "available",
    "unavailable": "unavailable",
    "faulted": "faulted",
    "fault": "faulted",
    "error": "faulted",
    "offline": "offline",
    "disconnected": "offline",
    "in_use": "charging",
    "occupied": "charging",
}


class ExternalEvent(BaseModel):
    """One normalised event from an operator system or import file."""

    external_event_id: str = Field(min_length=1, max_length=160)
    external_charger_id: str = Field(min_length=1, max_length=128)
    timestamp: datetime
    event_type: InternalEventType
    status: str | None = None              # internal status after the event, if any
    fault_category: str | None = None      # internal fault category for fault events
    site_code: str | None = None
    site_name: str | None = None
    lat: float | None = None
    lng: float | None = None
    power_kw: float | None = None
    connector_type: str | None = None
    meter_kwh: float | None = None
    raw: dict = Field(default_factory=dict)

    @field_validator("timestamp")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamp must include a timezone offset (e.g. 2026-10-01T09:30:00+05:30 or ...Z)")
        return to_naive_utc(v)

    @field_validator("status")
    @classmethod
    def _status(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        mapped = STATUS_MAP.get(v.strip().lower())
        if mapped is None:
            raise ValueError(f"unknown status '{v}'")
        return mapped

    @field_validator("fault_category")
    @classmethod
    def _fault(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        from ..core.catalog import FAULT_TYPES

        if v in FAULT_TYPES:
            return v
        mapped = OCPP_ERROR_TO_CATEGORY.get(v)
        if mapped is None:
            raise ValueError(f"unknown fault code '{v}'")
        return mapped

    @field_validator("lat")
    @classmethod
    def _lat(cls, v):
        if v is not None and not -90 <= v <= 90:
            raise ValueError("lat out of range")
        return v

    @field_validator("lng")
    @classmethod
    def _lng(cls, v):
        if v is not None and not -180 <= v <= 180:
            raise ValueError("lng out of range")
        return v
