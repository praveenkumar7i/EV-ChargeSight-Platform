"""v9 SaaS schema: organisations, users, sessions, subscriptions, demo checkout, integrations, audit.

All timestamps are naive UTC. Secrets are never stored in plain text:
* passwords: Argon2id hashes (argon2-cffi);
* session, verification, reset and invitation tokens: SHA-256 of a random 256-bit token;
* operator API credentials: Fernet ciphertext; the key is held outside the database.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base

ROLES = ("owner", "admin", "ops_manager", "technician", "viewer")
ORG_TYPES = ("cpo", "fleet_operator", "service_provider", "site_owner", "other")
SUB_STATES = ("demo", "trial", "pending_checkout", "active", "past_due", "expired", "cancelled", "suspended")
INTEGRATION_STATES = ("not_configured", "draft", "awaiting_credentials", "configured", "testing", "connected",
                      "auth_failed", "connection_failed", "permission_insufficient", "stale", "disabled")


def _now() -> datetime:
    from .clock import wall_now

    return wall_now()


class Organisation(Base):
    __tablename__ = "organisations"
    __table_args__ = (CheckConstraint("org_type IN ('cpo','fleet_operator','service_provider','site_owner','other')",
                                      name="ck_org_type"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    code: Mapped[str] = mapped_column(String(12), unique=True, index=True)     # id prefix, e.g. GRID7
    org_type: Mapped[str] = mapped_column(String(20), default="other")
    country: Mapped[str] = mapped_column(String(60), default="India")
    contact_phone: Mapped[str] = mapped_column(String(40), default="")
    website: Mapped[str] = mapped_column(String(200), default="")
    billing_address: Mapped[str] = mapped_column(Text, default="")
    tax_id: Mapped[str] = mapped_column(String(40), default="")                # optional GSTIN; not validated
    primary_contact_name: Mapped[str] = mapped_column(String(120), default="")
    primary_contact_email: Mapped[str] = mapped_column(String(254), default="")
    sites_estimate: Mapped[int | None] = mapped_column(Integer)
    chargers_estimate: Mapped[int | None] = mapped_column(Integer)
    is_system_demo: Mapped[bool] = mapped_column(Boolean, default=False)       # the legacy prototype workspace
    onboarding: Mapped[dict] = mapped_column(JSON, default=dict)               # server-side wizard progress
    preferences: Mapped[dict] = mapped_column(JSON, default=dict)              # alerts, retention
    demo_workspace_ready: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    created_by_user_id: Mapped[int | None] = mapped_column(Integer)
    # v9.2: "customer" (the operator's own workspace, My Fleet) or "presentation" (a separate, per-user
    # Presentation Demo workspace that holds only SIMULATED synthetic data)
    workspace_kind: Mapped[str] = mapped_column(String(20), default="customer")
    presentation_owner_user_id: Mapped[int | None] = mapped_column(Integer, index=True)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)   # normalised (lower case)
    full_name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(255))
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime)
    is_platform_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime)
    terms_accepted_at: Mapped[datetime | None] = mapped_column(DateTime)
    terms_version: Mapped[str] = mapped_column(String(20), default="")


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("org_id", "user_id", name="uq_membership"),
        CheckConstraint("role IN ('owner','admin','ops_manager','technician','viewer')", name="ck_membership_role"),
        CheckConstraint("status IN ('active','removed')", name="ck_membership_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organisations.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="active")
    technician_id: Mapped[str | None] = mapped_column(String(32))   # technician role: the linked Technician record
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    invited_by_user_id: Mapped[int | None] = mapped_column(Integer)


class AuthSession(Base):
    """Server-side session. The browser holds only a random token in an HttpOnly cookie."""

    __tablename__ = "auth_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    org_id: Mapped[int | None] = mapped_column(ForeignKey("organisations.id"))    # active organisation
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    remember: Mapped[bool] = mapped_column(Boolean, default=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)
    user_agent: Mapped[str] = mapped_column(String(200), default="")


class OneTimeToken(Base):
    """Email verification, password reset and invitation tokens (stored as SHA-256 only)."""

    __tablename__ = "one_time_tokens"
    __table_args__ = (CheckConstraint("purpose IN ('verify_email','reset_password','invite')", name="ck_ott_purpose"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    purpose: Mapped[str] = mapped_column(String(20))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    org_id: Mapped[int | None] = mapped_column(ForeignKey("organisations.id"), index=True)
    email: Mapped[str] = mapped_column(String(254), default="")
    role: Mapped[str | None] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_by_user_id: Mapped[int | None] = mapped_column(Integer)


class SubscriptionPlan(Base):
    """Central plan catalogue. Prices are illustrative PME-prototype assumptions (INR, paise)."""

    __tablename__ = "subscription_plans"

    key: Mapped[str] = mapped_column(String(20), primary_key=True)        # demo | starter | growth | enterprise
    name: Mapped[str] = mapped_column(String(60))
    tagline: Mapped[str] = mapped_column(String(200), default="")
    target: Mapped[str] = mapped_column(String(200), default="")
    monthly_price_paise: Mapped[int | None] = mapped_column(Integer)       # None = custom quotation
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    limits: Mapped[dict] = mapped_column(JSON, default=dict)               # chargers, users, integrations (None = negotiated)
    features: Mapped[list] = mapped_column(JSON, default=list)             # entitlement keys
    highlights: Mapped[list] = mapped_column(JSON, default=list)           # bullet text for the pricing page
    support: Mapped[str] = mapped_column(String(200), default="")
    self_serve: Mapped[bool] = mapped_column(Boolean, default=True)        # can be bought through checkout
    recommended: Mapped[bool] = mapped_column(Boolean, default=False)      # design suggestion only
    sort: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (
        CheckConstraint("state IN ('demo','trial','pending_checkout','active','past_due','expired','cancelled','suspended')",
                        name="ck_subscription_state"),
        CheckConstraint("billing_months IN (1, 6, 12)", name="ck_subscription_months"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organisations.id"), unique=True, index=True)
    plan_key: Mapped[str] = mapped_column(ForeignKey("subscription_plans.key"))
    billing_months: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String(20), default="demo")
    prev_state: Mapped[str | None] = mapped_column(String(20))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    current_period_start: Mapped[datetime | None] = mapped_column(DateTime)
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime)
    grace_until: Mapped[datetime | None] = mapped_column(DateTime)
    auto_renew: Mapped[bool] = mapped_column(Boolean, default=True)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)
    suspended_reason: Mapped[str] = mapped_column(Text, default="")
    scheduled_plan_key: Mapped[str | None] = mapped_column(String(20))      # downgrade at renewal
    scheduled_billing_months: Mapped[int | None] = mapped_column(Integer)
    custom_limits: Mapped[dict] = mapped_column(JSON, default=dict)          # Enterprise: negotiated limits
    custom_features: Mapped[list] = mapped_column(JSON, default=list)        # contract-specific additions
    custom_monthly_price_paise: Mapped[int | None] = mapped_column(Integer)
    mode: Mapped[str] = mapped_column(String(10), default="DEMO")            # DEMO/TEST: no real payment provider
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class SubscriptionEvent(Base):
    __tablename__ = "subscription_events"
    __table_args__ = (Index("ix_subevent_org_at", "org_id", "at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organisations.id"))
    at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    kind: Mapped[str] = mapped_column(String(40))
    from_state: Mapped[str | None] = mapped_column(String(20))
    to_state: Mapped[str | None] = mapped_column(String(20))
    plan_key: Mapped[str | None] = mapped_column(String(20))
    actor_user_id: Mapped[int | None] = mapped_column(Integer)
    actor_label: Mapped[str] = mapped_column(String(80), default="system")
    note: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class DemoCheckout(Base):
    """A simulated checkout. No card or bank data is ever collected or stored."""

    __tablename__ = "demo_checkouts"
    __table_args__ = (
        CheckConstraint("status IN ('pending','completed','expired','cancelled')", name="ck_checkout_status"),
        Index("ix_checkout_org", "org_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)            # chk_<random>
    org_id: Mapped[int] = mapped_column(ForeignKey("organisations.id"))
    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(String(20), default="new")             # new | change | renewal | reactivation
    plan_key: Mapped[str] = mapped_column(ForeignKey("subscription_plans.key"))
    billing_months: Mapped[int] = mapped_column(Integer)
    monthly_price_paise: Mapped[int] = mapped_column(Integer)
    subtotal_paise: Mapped[int] = mapped_column(Integer)
    discount_bps: Mapped[int] = mapped_column(Integer, default=0)
    discount_paise: Mapped[int] = mapped_column(Integer, default=0)
    credit_paise: Mapped[int] = mapped_column(Integer, default=0)
    total_paise: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    status: Mapped[str] = mapped_column(String(20), default="pending")
    billing_name: Mapped[str] = mapped_column(String(120), default="")
    billing_email: Mapped[str] = mapped_column(String(254), default="")
    billing_address: Mapped[str] = mapped_column(Text, default="")
    provider: Mapped[str] = mapped_column(String(20), default="demo")
    provider_reference: Mapped[str | None] = mapped_column(String(80))
    completion_key: Mapped[str | None] = mapped_column(String(80), unique=True)   # idempotency key of the completing request
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    receipt_number: Mapped[str | None] = mapped_column(String(30))
    # v9.2 TEST payment step. Only the method and a masked, non-sensitive label are stored (e.g. "Test card ·
    # Visa ending 1111"). Card numbers, expiry dates, UPI IDs beyond the test handle, CVVs, OTPs and PINs are
    # never stored; a CVV, OTP or PIN is never even requested.
    payment_method: Mapped[str | None] = mapped_column(String(20))
    payment_display: Mapped[str] = mapped_column(String(120), default="")
    payment_attached_at: Mapped[datetime | None] = mapped_column(DateTime)
    payment_test_outcome: Mapped[str | None] = mapped_column(String(10))        # approve | decline (test values only)


class DemoReceipt(Base):
    __tablename__ = "demo_receipts"

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(30), unique=True)              # DEMO-RCPT-000001
    org_id: Mapped[int] = mapped_column(ForeignKey("organisations.id"), index=True)
    checkout_id: Mapped[str] = mapped_column(ForeignKey("demo_checkouts.id"), unique=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    lines: Mapped[list] = mapped_column(JSON, default=list)
    total_paise: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    billed_to: Mapped[dict] = mapped_column(JSON, default=dict)
    label: Mapped[str] = mapped_column(String(200),
                                       default="DEMO RECEIPT - simulation only. No payment was taken. Not a tax invoice.")


class IntegrationConfig(Base):
    __tablename__ = "integration_configs"
    __table_args__ = (
        CheckConstraint("kind IN ('operator_api','ocpp')", name="ck_integration_kind"),
        CheckConstraint("state IN ('not_configured','draft','awaiting_credentials','configured','testing','connected',"
                        "'auth_failed','connection_failed','permission_insufficient','stale','disabled')",
                        name="ck_integration_state"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organisations.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(80))
    state: Mapped[str] = mapped_column(String(30), default="not_configured")
    adapter: Mapped[str] = mapped_column(String(40), default="chargesight_rest_v1")
    base_url: Mapped[str] = mapped_column(String(300), default="")
    auth_method: Mapped[str] = mapped_column(String(20), default="bearer")
    secret_ciphertext: Mapped[str | None] = mapped_column(Text)
    secret_set_at: Mapped[datetime | None] = mapped_column(DateTime)
    timeout_s: Mapped[float] = mapped_column(default=10.0)
    poll_seconds: Mapped[int] = mapped_column(Integer, default=0)
    mapping: Mapped[dict] = mapped_column(JSON, default=dict)
    operator_authorised: Mapped[bool] = mapped_column(Boolean, default=False)   # admin confirms written operator authorisation
    authorisation_note: Mapped[str] = mapped_column(Text, default="")
    ocpp_endpoint_key: Mapped[str | None] = mapped_column(String(40), unique=True)
    ocpp_allowed_ids: Mapped[list] = mapped_column(JSON, default=list)
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_test_message: Mapped[str] = mapped_column(Text, default="")
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_data_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    created_by_user_id: Mapped[int | None] = mapped_column(Integer)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_org_at", "org_id", "at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    org_id: Mapped[int | None] = mapped_column(ForeignKey("organisations.id"))
    actor_user_id: Mapped[int | None] = mapped_column(Integer)
    actor_label: Mapped[str] = mapped_column(String(120), default="system")
    action: Mapped[str] = mapped_column(String(60), index=True)
    target_type: Mapped[str] = mapped_column(String(40), default="")
    target_id: Mapped[str] = mapped_column(String(80), default="")
    ip: Mapped[str] = mapped_column(String(64), default="")
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class EnterpriseEnquiry(Base):
    __tablename__ = "enquiries"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    kind: Mapped[str] = mapped_column(String(20), default="enterprise")    # enterprise | contact
    org_name: Mapped[str] = mapped_column(String(160))
    contact_name: Mapped[str] = mapped_column(String(120), default="")
    contact_email: Mapped[str] = mapped_column(String(254))
    phone: Mapped[str] = mapped_column(String(40), default="")
    charger_count: Mapped[int | None] = mapped_column(Integer)
    site_count: Mapped[int | None] = mapped_column(Integer)
    integration_requirements: Mapped[str] = mapped_column(Text, default="")
    message: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="new")
    user_id: Mapped[int | None] = mapped_column(Integer)
    org_id: Mapped[int | None] = mapped_column(Integer)
