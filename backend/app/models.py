"""Relational schema. All timestamps are stored as naive UTC datetimes.

Provenance: every Site, Charger, Event and Ticket carries `source`, one of
`simulation`, `operator_api`, `ocpp`, `operator_import` (see core/catalog.py).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base
from .tenancy import TenantMixin

SOURCE_CHECK = "source IN ('simulation','operator_api','ocpp','operator_import')"


def utcnow() -> datetime:
    from .clock import wall_now

    return wall_now()


class Site(TenantMixin, Base):
    __tablename__ = "sites"
    __table_args__ = (CheckConstraint(SOURCE_CHECK, name="ck_site_source"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    lat: Mapped[float] = mapped_column(Float)
    lng: Mapped[float] = mapped_column(Float)
    daily_sessions: Mapped[int] = mapped_column(Integer, default=0)   # ASSUMPTION for demo sites
    travel_min_low: Mapped[int] = mapped_column(Integer, default=30)
    travel_min_high: Mapped[int] = mapped_column(Integer, default=45)
    is_demo_location: Mapped[bool] = mapped_column(Boolean, default=True)
    source: Mapped[str] = mapped_column(String(20), default="simulation")

    chargers: Mapped[list["Charger"]] = relationship(back_populates="site")


class Charger(TenantMixin, Base):
    __tablename__ = "chargers"
    __table_args__ = (
        CheckConstraint(SOURCE_CHECK, name="ck_charger_source"),
        CheckConstraint(
            "status IN ('available','charging','faulted','offline','unavailable')",
            name="ck_charger_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    connector_type: Mapped[str] = mapped_column(String(40))
    power_kw: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(20), default="available", index=True)
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime)
    session_kwh: Mapped[float] = mapped_column(Float, default=0.0)
    total_kwh: Mapped[float] = mapped_column(Float, default=0.0)
    comm_lost: Mapped[bool] = mapped_column(Boolean, default=False)   # simulator-internal: heartbeats suppressed
    source: Mapped[str] = mapped_column(String(20), default="simulation", index=True)
    external_id: Mapped[str | None] = mapped_column(String(128))
    power_now_kw: Mapped[float] = mapped_column(Float, default=0.0)       # instantaneous output
    temp_c: Mapped[float | None] = mapped_column(Float, default=30.0)     # connector temperature (simulated model)
    session_started_at: Mapped[datetime | None] = mapped_column(DateTime)
    test_traffic: Mapped[bool] = mapped_column(Boolean, default=False)   # OCPP traffic from the local simulator
    ocpp_vendor: Mapped[str | None] = mapped_column(String(80))
    ocpp_model: Mapped[str | None] = mapped_column(String(80))

    site: Mapped[Site] = relationship(back_populates="chargers")


class FleetSnapshot(TenantMixin, Base):
    """Fleet state sampled once per simulated minute (history for live charts)."""

    __tablename__ = "fleet_snapshots"
    __table_args__ = (Index("ix_snap_source_ts", "source", "ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime)
    source: Mapped[str] = mapped_column(String(20), default="simulation")
    total: Mapped[int] = mapped_column(Integer, default=0)
    available: Mapped[int] = mapped_column(Integer, default=0)
    charging: Mapped[int] = mapped_column(Integer, default=0)
    faulted: Mapped[int] = mapped_column(Integer, default=0)
    offline: Mapped[int] = mapped_column(Integer, default=0)
    unavailable: Mapped[int] = mapped_column(Integer, default=0)
    uptime_pct: Mapped[float] = mapped_column(Float, default=0.0)
    power_kw: Mapped[float] = mapped_column(Float, default=0.0)
    energy_kwh_total: Mapped[float] = mapped_column(Float, default=0.0)
    sessions_total: Mapped[int] = mapped_column(Integer, default=0)
    open_tickets: Mapped[int] = mapped_column(Integer, default=0)
    critical_open: Mapped[int] = mapped_column(Integer, default=0)


class ChargerReading(TenantMixin, Base):
    """Per-charger power / temperature sample (every 5 simulated minutes)."""

    __tablename__ = "charger_readings"
    __table_args__ = (Index("ix_reading_charger_ts", "charger_id", "ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    charger_id: Mapped[str] = mapped_column(ForeignKey("chargers.id"))
    ts: Mapped[datetime] = mapped_column(DateTime, index=True)
    status: Mapped[str] = mapped_column(String(20))
    power_kw: Mapped[float] = mapped_column(Float, default=0.0)
    temp_c: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(20), default="simulation")


class Event(TenantMixin, Base):
    __tablename__ = "events"
    __table_args__ = (
        CheckConstraint(SOURCE_CHECK, name="ck_event_source"),
        UniqueConstraint("source", "external_event_id", name="uq_event_source_extid"),
        Index("ix_events_charger_ts", "charger_id", "ts"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    charger_id: Mapped[str] = mapped_column(ForeignKey("chargers.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, index=True)          # event time
    seq: Mapped[int] = mapped_column(Integer, default=0)                # order within the same ts
    type: Mapped[str] = mapped_column(String(40), index=True)           # internal event type
    ocpp_action: Mapped[str | None] = mapped_column(String(40))         # OCPP 1.6 action name, if applicable
    status_after: Mapped[str | None] = mapped_column(String(20))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(String(20), default="simulation", index=True)
    external_event_id: Mapped[str | None] = mapped_column(String(160))
    ingested_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Technician(TenantMixin, Base):
    __tablename__ = "technicians"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    zone: Mapped[str] = mapped_column(String(40), default="")
    available: Mapped[bool] = mapped_column(Boolean, default=True)    # kept in sync: active and availability == "available"
    # v8 registry fields
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    availability: Mapped[str] = mapped_column(String(20), default="available")   # available | unavailable | off_duty
    skills: Mapped[list] = mapped_column(JSON, default=list)
    equipment: Mapped[list] = mapped_column(JSON, default=list)       # "ac", "dc"
    safety_certified: Mapped[bool] = mapped_column(Boolean, default=False)
    capacity: Mapped[int] = mapped_column(Integer, default=1)         # concurrent active tickets (existing rule: one job)
    base_lat: Mapped[float | None] = mapped_column(Float)
    base_lng: Mapped[float | None] = mapped_column(Float)
    base_label: Mapped[str] = mapped_column(String(80), default="")
    shift_start: Mapped[str | None] = mapped_column(String(5))        # "HH:MM", optional
    shift_end: Mapped[str | None] = mapped_column(String(5))
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str] = mapped_column(Text, default="")


class Ticket(TenantMixin, Base):
    __tablename__ = "tickets"
    __table_args__ = (
        CheckConstraint(SOURCE_CHECK, name="ck_ticket_source"),
        CheckConstraint(
            "status IN ('open','assigned','en_route','on_site','waiting_parts','waiting_access','resolved','closed')",
            name="ck_ticket_status",
        ),
        # At most one active (not resolved/closed) ticket per charger and fault category.
        Index(
            "uq_ticket_open_per_fault",
            "charger_id",
            "fault_category",
            unique=True,
            sqlite_where=text("status NOT IN ('resolved','closed')"),
            postgresql_where=text("status NOT IN ('resolved','closed')"),
        ),
        Index("uq_ticket_client_request", "client_request_id", unique=True,
              sqlite_where=text("client_request_id IS NOT NULL"), postgresql_where=text("client_request_id IS NOT NULL")),
        Index("ix_ticket_source_status", "source", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    charger_id: Mapped[str] = mapped_column(ForeignKey("chargers.id"), index=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    fault_category: Mapped[str] = mapped_column(String(40))
    severity: Mapped[str] = mapped_column(String(20))
    detected_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)
    technician_id: Mapped[str | None] = mapped_column(ForeignKey("technicians.id"))
    est_travel_min: Mapped[float | None] = mapped_column(Float)
    est_repair_min: Mapped[float | None] = mapped_column(Float)
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime)   # technician accepted (En Route)
    on_site_at: Mapped[datetime | None] = mapped_column(DateTime)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
    resolution_notes: Mapped[str] = mapped_column(Text, default="")
    resolved_by: Mapped[str | None] = mapped_column(String(40))            # technician | remote_reset | reconnect | admin
    remote_reset_attempted: Mapped[bool] = mapped_column(Boolean, default=False)
    reset_due_at: Mapped[datetime | None] = mapped_column(DateTime)
    source: Mapped[str] = mapped_column(String(20), default="simulation", index=True)
    # v8: how the ticket was raised and what was reported
    origin: Mapped[str] = mapped_column(String(20), default="")         # user_reported | simulator | ocpp | operator_api | imported_data
    issue_category: Mapped[str | None] = mapped_column(String(30))      # user-facing category (Raise a Ticket)
    description: Mapped[str] = mapped_column(Text, default="")
    symptoms: Mapped[str] = mapped_column(Text, default="")
    error_code: Mapped[str | None] = mapped_column(String(60))
    impact: Mapped[str | None] = mapped_column(String(30))              # unavailable | reduced_capacity | intermittent | informational
    unable_to_charge: Mapped[bool | None] = mapped_column(Boolean)
    safety_related: Mapped[bool] = mapped_column(Boolean, default=False)
    affected_chargers: Mapped[int] = mapped_column(Integer, default=1)
    reported_by: Mapped[str] = mapped_column(String(80), default="")    # free text (no authentication in this prototype)
    client_request_id: Mapped[str | None] = mapped_column(String(64))   # idempotency key for user submissions
    severity_basis: Mapped[str] = mapped_column(String(200), default="")
    # priority snapshot (the live ranking is recomputed continuously; snapshot changes are audited)
    priority_score: Mapped[float | None] = mapped_column(Float)
    priority_updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    triaged_at: Mapped[datetime | None] = mapped_column(DateTime)
    # SLA and escalation
    sla_due_at: Mapped[datetime | None] = mapped_column(DateTime)
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime)
    escalation_reason: Mapped[str] = mapped_column(String(200), default="")
    dispatch_needed: Mapped[bool] = mapped_column(Boolean, default=False)
    # assignment
    assignment_note: Mapped[str] = mapped_column(Text, default="")     # why assigned / why no technician
    auto_assigned: Mapped[bool] = mapped_column(Boolean, default=False)
    # waiting, verification, reopening
    waiting_reason: Mapped[str] = mapped_column(String(200), default="")
    inspection_passed: Mapped[bool | None] = mapped_column(Boolean)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime)
    closed_by: Mapped[str] = mapped_column(String(80), default="")
    reopen_count: Mapped[int] = mapped_column(Integer, default=0)
    sim_work_due_at: Mapped[datetime | None] = mapped_column(DateTime)  # simulator only: when the current simulated work step ends

    charger: Mapped[Charger] = relationship()
    site: Mapped[Site] = relationship()
    technician: Mapped[Technician | None] = relationship()
    history: Mapped[list["TicketHistory"]] = relationship(
        back_populates="ticket", order_by="TicketHistory.id", cascade="all, delete-orphan"
    )


class TicketHistory(TenantMixin, Base):
    __tablename__ = "ticket_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), index=True)
    from_status: Mapped[str | None] = mapped_column(String(20))
    to_status: Mapped[str] = mapped_column(String(20))
    at: Mapped[datetime] = mapped_column(DateTime)
    actor: Mapped[str] = mapped_column(String(40), default="system")
    note: Mapped[str] = mapped_column(Text, default="")
    override: Mapped[bool] = mapped_column(Boolean, default=False)
    # v8 audit trail: status | priority | assignment | escalation | note | inspection
    kind: Mapped[str] = mapped_column(String(20), default="status")
    data: Mapped[dict] = mapped_column(JSON, default=dict)

    ticket: Mapped[Ticket] = relationship(back_populates="history")


class SimState(Base):
    """Live simulator state, one row per organisation (id = organisation id; 1 = legacy demo workspace)."""

    __tablename__ = "sim_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    seed: Mapped[int] = mapped_column(Integer, default=42)
    step: Mapped[int] = mapped_column(Integer, default=0)
    sim_time: Mapped[datetime] = mapped_column(DateTime)
    running: Mapped[bool] = mapped_column(Boolean, default=False)
    speed: Mapped[int] = mapped_column(Integer, default=1)          # steps per tick
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    ticket_counter: Mapped[int] = mapped_column(Integer, default=0)
    sessions_total: Mapped[int] = mapped_column(Integer, default=0)   # cumulative simulated sessions


class Setting(Base):
    """Key/value store for runtime configuration such as priority weights."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)


class ExperimentRun(TenantMixin, Base):
    __tablename__ = "experiment_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), default="comparison")   # comparison | sensitivity | retrospective
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    config: Mapped[dict] = mapped_column(JSON)
    config_hash: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(20), default="completed")
    results: Mapped[dict] = mapped_column(JSON, default=dict)
    label: Mapped[str] = mapped_column(String(160), default="")
    data_source: Mapped[str] = mapped_column(String(40), default="simulation")


class InvestmentSite(TenantMixin, Base):
    __tablename__ = "investment_sites"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    lat: Mapped[float | None] = mapped_column(Float)
    lng: Mapped[float | None] = mapped_column(Float)
    inputs: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    notes: Mapped[str] = mapped_column(Text, default="")


class FinancialReference(TenantMixin, Base):
    """Actual figures shared by an operator for validating an investment estimate."""

    __tablename__ = "financial_references"

    id: Mapped[int] = mapped_column(primary_key=True)
    investment_site_id: Mapped[int] = mapped_column(ForeignKey("investment_sites.id"), index=True)
    metric: Mapped[str] = mapped_column(String(60))
    reference_value: Mapped[float] = mapped_column(Float)
    period: Mapped[str] = mapped_column(String(60))
    data_source: Mapped[str] = mapped_column(String(160))
    limitations: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PublicStation(Base):
    """Publicly listed charging locations (e.g. Open Charge Map). Never live status."""

    __tablename__ = "public_stations"
    __table_args__ = (UniqueConstraint("provider", "provider_id", name="uq_public_station"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(40))
    provider_id: Mapped[str] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(200))
    operator_name: Mapped[str] = mapped_column(String(160), default="")
    lat: Mapped[float] = mapped_column(Float)
    lng: Mapped[float] = mapped_column(Float)
    address: Mapped[str] = mapped_column(Text, default="")
    connections: Mapped[list] = mapped_column(JSON, default=list)
    data_provider: Mapped[str] = mapped_column(String(200), default="")
    licence: Mapped[str] = mapped_column(String(200), default="")
    retrieved_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # v3 additions (PUBLIC LISTING metadata; none of these is live status)
    source_url: Mapped[str] = mapped_column(String(400), default="")
    usage_type: Mapped[str | None] = mapped_column(String(120))
    usage_cost: Mapped[str | None] = mapped_column(String(200))
    opening_hours: Mapped[str | None] = mapped_column(Text)
    listed_status: Mapped[str | None] = mapped_column(String(200))   # as declared by the listing, never live
    classification: Mapped[str] = mapped_column(String(40), default="PUBLIC LISTING")   # or IMPORTED
    retrieval_method: Mapped[str] = mapped_column(String(300), default="")
    attribution: Mapped[str] = mapped_column(String(300), default="")
    duplicate_of: Mapped[int | None] = mapped_column(Integer)          # id of the primary record when merged
    origin: Mapped[str] = mapped_column(String(20), default="")          # snapshot | api
    absent_from_latest_refresh: Mapped[bool] = mapped_column(Boolean, default=False)


class IngestionRun(TenantMixin, Base):
    __tablename__ = "ingestion_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    connector: Mapped[str] = mapped_column(String(40))
    source: Mapped[str] = mapped_column(String(20))
    dataset_label: Mapped[str] = mapped_column(String(200), default="")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    received: Mapped[int] = mapped_column(Integer, default=0)
    accepted: Mapped[int] = mapped_column(Integer, default=0)
    duplicates: Mapped[int] = mapped_column(Integer, default=0)
    rejected: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[list] = mapped_column(JSON, default=list)
    ok: Mapped[bool] = mapped_column(Boolean, default=False)


class ConnectorHealth(Base):
    """Connection health. org_id NULL = platform-level public-listing providers (Open Charge Map, OSM,
    Google); otherwise the organisation's own connectors (operator API, CSV import, OCPP).
    Read and written only through integrations.base.get_health / set_health."""

    __tablename__ = "connector_health"
    __table_args__ = (UniqueConstraint("org_id", "connector", name="uq_connector_health"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int | None] = mapped_column(ForeignKey("organisations.id"), index=True)
    connector: Mapped[str] = mapped_column(String(40), index=True)
    state: Mapped[str] = mapped_column(String(30), default="not_configured")
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str] = mapped_column(Text, default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class Interview(TenantMixin, Base):
    """Operator / site-owner validation interview. Never seeded with fake data."""

    __tablename__ = "interviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    interview_date: Mapped[str] = mapped_column(String(10))
    participant_ref: Mapped[str] = mapped_column(String(80), default="")   # pseudonym; stored only with consent
    consent_identifiable: Mapped[bool] = mapped_column(Boolean, default=False)
    role: Mapped[str] = mapped_column(String(80))
    organisation_type: Mapped[str] = mapped_column(String(80), default="")
    current_workflow: Mapped[str] = mapped_column(Text, default="")
    prioritisation_today: Mapped[str] = mapped_column(Text, default="")
    difficulties: Mapped[str] = mapped_column(Text, default="")
    data_shareable: Mapped[str] = mapped_column(Text, default="")
    feedback: Mapped[str] = mapped_column(Text, default="")
    disagreements: Mapped[str] = mapped_column(Text, default="")
    follow_ups: Mapped[str] = mapped_column(Text, default="")
    confidentiality_notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ValidationDataset(TenantMixin, Base):
    """Authorised historical ticket records imported for validation."""

    __tablename__ = "validation_datasets"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(200))
    provided_by: Mapped[str] = mapped_column(String(160), default="")
    permission_note: Mapped[str] = mapped_column(Text, default="")
    is_synthetic_sample: Mapped[bool] = mapped_column(Boolean, default=False)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    records: Mapped[list] = mapped_column(JSON, default=list)
    quality: Mapped[dict] = mapped_column(JSON, default=dict)


class ValidationTrack(TenantMixin, Base):
    __tablename__ = "validation_tracks"
    __table_args__ = (UniqueConstraint("org_id", "track", name="uq_validation_track"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    track: Mapped[str] = mapped_column(String(30))   # operational | financial | interviews
    status: Mapped[str] = mapped_column(String(30), default="not_started")
    note: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PlanningCandidate(TenantMixin, Base):
    """A location the user is investigating as a possible new charging site (Map → Plan a new site).

    Kept separate from public listings (PublicStation), simulated chargers and saved proposed
    sites (InvestmentSite). `inputs` holds only values the user entered; `context` holds evidence
    retrieved on explicit request (e.g. an OpenStreetMap amenity lookup) with its source and time."""

    __tablename__ = "planning_candidates"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    lat: Mapped[float] = mapped_column(Float)
    lng: Mapped[float] = mapped_column(Float)
    area: Mapped[str] = mapped_column(String(40), default="")
    inputs: Mapped[dict] = mapped_column(JSON, default=dict)
    context: Mapped[dict] = mapped_column(JSON, default=dict)
    notes: Mapped[str] = mapped_column(Text, default="")
    saved_site_id: Mapped[int | None] = mapped_column(Integer)      # InvestmentSite created from it, after confirmation
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class HistoricalChargingSession(TenantMixin, Base):
    """Historical charging session records (e.g. IEEE ChargingRecords.csv).

    Research dataset only — never live operational telemetry. Preserves the original
    duration and demand values without unverified unit assumptions.
    """

    __tablename__ = "historical_charging_sessions"
    __table_args__ = (
        Index("ix_hist_session_org_hash", "org_id", "record_hash", unique=True),
        Index("ix_hist_session_charger", "org_id", "charger_id"),
        Index("ix_hist_session_start", "org_id", "start_datetime"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[str | None] = mapped_column(String(64), index=True)
    charger_id: Mapped[str] = mapped_column(String(64), index=True)
    charger_company: Mapped[str | None] = mapped_column(String(120))
    location: Mapped[str | None] = mapped_column(String(200))
    charger_type: Mapped[str | None] = mapped_column(String(60))
    start_day: Mapped[str | None] = mapped_column(String(30))
    start_time: Mapped[str | None] = mapped_column(String(30))
    end_day: Mapped[str | None] = mapped_column(String(30))
    end_time: Mapped[str | None] = mapped_column(String(30))
    start_datetime: Mapped[datetime | None] = mapped_column(DateTime)
    end_datetime: Mapped[datetime | None] = mapped_column(DateTime)
    duration: Mapped[float | None] = mapped_column(Float)
    duration_raw: Mapped[str | None] = mapped_column(String(60))
    demand: Mapped[float | None] = mapped_column(Float)
    demand_raw: Mapped[str | None] = mapped_column(String(60))
    demand_unit: Mapped[str] = mapped_column(String(20), default="unspecified")
    dataset_label: Mapped[str] = mapped_column(String(200), default="IEEE Charging Records")
    record_hash: Mapped[str] = mapped_column(String(64), index=True)
    raw_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ImportedStation(TenantMixin, Base):
    """Station inventory records (e.g. state or national EV charger inventories).

    Inventory facts only — never operational status. Retains operator, ownership,
    coordinates, charger/connector ratings and total connector counts.
    """

    __tablename__ = "imported_stations"
    __table_args__ = (
        Index("ix_imp_station_org_hash", "org_id", "record_hash", unique=True),
        Index("ix_imp_station_loc", "org_id", "state", "district"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    station_id: Mapped[str] = mapped_column(String(80), index=True)
    operator: Mapped[str | None] = mapped_column(String(160), index=True)
    ownership: Mapped[str | None] = mapped_column(String(60))
    state: Mapped[str | None] = mapped_column(String(80))
    district: Mapped[str | None] = mapped_column(String(100))
    city_or_village: Mapped[str | None] = mapped_column(String(120))
    station_name: Mapped[str] = mapped_column(String(200))
    lat: Mapped[float] = mapped_column(Float)
    lng: Mapped[float] = mapped_column(Float)
    charger_type: Mapped[str | None] = mapped_column(String(80))
    charger_rating: Mapped[str | None] = mapped_column(String(80))
    connector_rating: Mapped[str | None] = mapped_column(String(80))
    total_connectors: Mapped[int] = mapped_column(Integer, default=1)
    is_karnataka: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_bengaluru: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    dataset_label: Mapped[str] = mapped_column(String(200), default="Station Inventory")
    record_hash: Mapped[str] = mapped_column(String(64), index=True)
    raw_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


from .models_saas import (  # noqa: E402,F401  (register the v9 tables)
    AuditEvent, AuthSession, DemoCheckout, DemoReceipt, EnterpriseEnquiry, IntegrationConfig, Membership, OneTimeToken,
    Organisation, Subscription, SubscriptionEvent, SubscriptionPlan, User,
)
