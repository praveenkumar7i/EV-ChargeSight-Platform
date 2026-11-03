"""Per-organisation operator integrations (operator REST API and OCPP 1.6J).

States (IntegrationConfig.state):
  not_configured -> draft / awaiting_credentials -> configured -> testing -> connected
                                                               \\-> auth_failed | connection_failed | permission_insufficient
  connected -> stale (no successful sync / message within the stale window)      any -> disabled -> (enable) -> derived
"Connected / verified" is set ONLY by a successful connection test (operator API) or an accepted
BootNotification from a real, non-test charge point over the organisation's own OCPP endpoint.
A paid plan never enables an integration by itself; the operator's authorisation must be confirmed and
the connection must pass verification. Credentials are encrypted at rest and never returned.
"""
from __future__ import annotations

import secrets
import socket
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..clock import iso, wall_now
from ..config import get_settings
from ..models_saas import IntegrationConfig
from ..security import crypto
from ..security.ssrf import UnsafeUrl, check_url

TRANSPORT = None                 # tests inject an httpx.MockTransport here
RESOLVER = socket.getaddrinfo    # tests inject a fake DNS resolver here
STALE_AFTER = timedelta(minutes=30)
OCPP_STALE_AFTER = timedelta(minutes=12)
AUTH_METHODS = ("bearer",)       # the existing adapter sends the credential as an HTTP Bearer token
ADAPTERS = {"chargesight_rest_v1": "ChargeSight REST contract v1 (GET /health, GET /events?since=) - see docs/INTEGRATION.md"}
STATE_LABELS = {"not_configured": "Not configured", "draft": "Draft", "awaiting_credentials": "Awaiting operator credentials",
                "configured": "Configured (not yet verified)", "testing": "Testing", "connected": "Connected / verified",
                "auth_failed": "Authentication failed", "connection_failed": "Connection failed",
                "permission_insufficient": "Permission insufficient", "stale": "Stale", "disabled": "Disabled"}


class IntegrationError(Exception):
    def __init__(self, message: str, status: int = 422, code: str = "integration"):
        super().__init__(message)
        self.status = status
        self.code = code


def _redact(text: str, secret: str | None) -> str:
    from ..integrations.base import redact

    out = redact(str(text))
    if secret and len(secret) >= 4:
        out = out.replace(secret, "***REDACTED***")
    return out[:500]


def _secret(cfg: IntegrationConfig) -> str | None:
    return crypto.decrypt(cfg.secret_ciphertext) if cfg.secret_ciphertext else None


def derive_state(cfg: IntegrationConfig) -> str:
    """Configuration-only state (before any test)."""
    if cfg.state == "disabled":
        return "disabled"
    if cfg.kind == "ocpp":
        return "configured" if cfg.operator_authorised else "draft"
    has_url, has_secret = bool(cfg.base_url), bool(cfg.secret_ciphertext)
    if not has_url and not has_secret:
        return "not_configured"
    if has_url and not has_secret:
        return "awaiting_credentials"
    if has_secret and not has_url:
        return "draft"
    return "configured" if cfg.operator_authorised else "draft"


def effective_state(cfg: IntegrationConfig, now=None) -> str:
    now = now or wall_now()
    if cfg.state == "connected":
        last = cfg.last_data_at or cfg.last_success_at
        window = OCPP_STALE_AFTER if cfg.kind == "ocpp" else STALE_AFTER
        if last is not None and now - last > window:
            return "stale"
    return cfg.state


def create(db: Session, org_id: int, user, kind: str, name: str) -> IntegrationConfig:
    from .entitlements import check_limit

    if kind not in ("operator_api", "ocpp"):
        raise IntegrationError("Choose an operator REST API or an OCPP connection.")
    name = (name or "").strip()
    if not (2 <= len(name) <= 80):
        raise IntegrationError("Give the connection a name (2-80 characters).")
    check_limit(db, org_id, "integrations")
    cfg = IntegrationConfig(org_id=org_id, kind=kind, name=name, state="not_configured", created_by_user_id=getattr(user, "id", None),
                            created_at=wall_now(), updated_at=wall_now())
    if kind == "ocpp":
        cfg.ocpp_endpoint_key = secrets.token_urlsafe(12).replace("-", "x").replace("_", "y")
        cfg.state = "draft"
    db.add(cfg)
    db.flush()
    return cfg


def update(db: Session, cfg: IntegrationConfig, data: dict) -> IntegrationConfig:
    if cfg.state == "disabled":
        raise IntegrationError("Enable the connection before editing it.", 409, "disabled")
    s = get_settings()
    changed = False
    if "name" in data and data["name"] is not None:
        n = data["name"].strip()
        if not (2 <= len(n) <= 80):
            raise IntegrationError("Give the connection a name (2-80 characters).")
        cfg.name = n
    if cfg.kind == "operator_api":
        if "base_url" in data and data["base_url"] is not None:
            url = data["base_url"].strip()
            if url:
                try:   # syntax and scheme now; DNS / address checks happen at every connection test
                    url = check_url(url, allow_private=True) if s.allow_private_integration_hosts else _syntax_only(url)
                except UnsafeUrl as e:
                    raise IntegrationError(str(e))
            changed |= url != cfg.base_url
            cfg.base_url = url
        if data.get("auth_method") is not None:
            if data["auth_method"] not in AUTH_METHODS:
                raise IntegrationError("Unsupported authentication method for the existing adapter (use Bearer token).")
            cfg.auth_method = data["auth_method"]
        if data.get("adapter") is not None:
            if data["adapter"] not in ADAPTERS:
                raise IntegrationError("Unknown adapter.")
            cfg.adapter = data["adapter"]
        if data.get("credential"):
            cred = str(data["credential"]).strip()
            if not (8 <= len(cred) <= 500) or any(ch in cred for ch in "\r\n"):
                raise IntegrationError("The credential must be 8-500 characters on one line.")
            cfg.secret_ciphertext = crypto.encrypt(cred)
            cfg.secret_set_at = wall_now()
            changed = True
        if data.get("clear_credential"):
            cfg.secret_ciphertext = None
            cfg.secret_set_at = None
            changed = True
        if data.get("timeout_s") is not None:
            t = float(data["timeout_s"])
            if not (1 <= t <= 30):
                raise IntegrationError("Timeout must be between 1 and 30 seconds.")
            cfg.timeout_s = t
        if data.get("poll_seconds") is not None:
            p = int(data["poll_seconds"])
            if p != 0 and not (300 <= p <= 86400):
                raise IntegrationError("Polling interval must be 0 (manual) or between 300 and 86400 seconds.")
            cfg.poll_seconds = p
        if data.get("mapping") is not None:
            if not isinstance(data["mapping"], dict) or len(str(data["mapping"])) > 4000:
                raise IntegrationError("Mapping must be a JSON object (max 4000 characters).")
            cfg.mapping = data["mapping"]
            changed = True
    else:
        if data.get("ocpp_allowed_ids") is not None:
            ids = [str(i).strip()[:60] for i in data["ocpp_allowed_ids"] if str(i).strip()]
            if len(ids) > 500:
                raise IntegrationError("At most 500 charge point identities.")
            cfg.ocpp_allowed_ids = ids
            changed = True
    if data.get("operator_authorised") is not None:
        changed |= bool(data["operator_authorised"]) != cfg.operator_authorised
        cfg.operator_authorised = bool(data["operator_authorised"])
    if data.get("authorisation_note") is not None:
        cfg.authorisation_note = str(data["authorisation_note"])[:1000]
    if changed or cfg.state not in ("connected", "stale"):
        cfg.state = derive_state(cfg)            # any change needs a new verification
        if changed:
            cfg.last_test_ok = None
    cfg.updated_at = wall_now()
    return cfg


def _syntax_only(url: str) -> str:
    """Scheme / host / literal-IP checks without a DNS lookup (names are resolved and checked at test time)."""
    import ipaddress

    def resolver(host, port, **_):
        try:
            ipaddress.ip_address(host.strip("[]"))
            return [(None, None, None, None, (host.strip("[]"), port))]
        except ValueError:
            return [(None, None, None, None, ("93.184.215.14", port))]      # placeholder public address for a name
    return check_url(url, allow_private=False, resolver=resolver)


def disable(cfg: IntegrationConfig) -> None:
    cfg.state = "disabled"
    cfg.updated_at = wall_now()


def enable(db: Session, cfg: IntegrationConfig) -> None:
    from .entitlements import check_limit

    if cfg.state != "disabled":
        raise IntegrationError("The connection is not disabled.", 409, "not_disabled")
    check_limit(db, cfg.org_id, "integrations")
    cfg.state = "configured"
    cfg.state = derive_state(cfg)
    cfg.updated_at = wall_now()


def _connector(cfg: IntegrationConfig, secret: str):
    from ..integrations.operator_api import OperatorApiConnector

    settings = SimpleNamespace(operator_api_base_url=cfg.base_url, operator_api_key=secret,
                               operator_api_timeout_s=float(cfg.timeout_s or 10), operator_api_max_retries=2)
    return OperatorApiConnector(settings=settings, transport=TRANSPORT, sleep=lambda s: None)


def test_connection(db: Session, cfg: IntegrationConfig) -> dict:
    """Explicit connection test. Never fabricates success; every outcome is recorded on the config."""
    from ..integrations.base import ConnectorError

    now = wall_now()
    if cfg.state == "disabled":
        raise IntegrationError("The connection is disabled.", 409, "disabled")
    if cfg.kind == "ocpp":
        return _ocpp_check(db, cfg)
    pre = derive_state(cfg)
    if pre in ("not_configured", "awaiting_credentials") or not cfg.base_url or not cfg.secret_ciphertext:
        cfg.state = pre
        return _result(cfg, False, "The connection needs the operator's base URL and API credential before it can be tested.")
    if not cfg.operator_authorised:
        cfg.state = "draft"
        return _result(cfg, False, "Confirm that the operator has authorised ChargeSight to access this API before testing.")
    cfg.state = "testing"
    cfg.last_test_at = now
    try:
        secret = _secret(cfg)
    except crypto.SecretUnavailable as e:
        return _finish(cfg, "connection_failed", False, str(e))
    try:
        check_url(cfg.base_url, allow_private=get_settings().allow_private_integration_hosts, resolver=RESOLVER)
    except UnsafeUrl as e:
        return _finish(cfg, "connection_failed", False, f"Blocked: {e}")
    conn = _connector(cfg, secret)
    try:
        conn.health_check()
    except ConnectorError as e:
        return _finish(cfg, _classify(str(e)), False, _redact(e, secret))
    try:
        events, errors = conn.fetch_events()
    except ConnectorError as e:
        return _finish(cfg, _classify(str(e)), False, "Health check passed, but reading events failed: " + _redact(e, secret))
    msg = (f"Connected and verified: GET /health and GET /events succeeded ({len(events)} valid events available"
           + (f", {len(errors)} did not match the schema" if errors else "") + "). Nothing was imported; use Sync to import.")
    cfg.last_success_at = now
    return _finish(cfg, "connected", True, msg, events_available=len(events), invalid_events=len(errors))


def _classify(msg: str) -> str:
    if "HTTP 401" in msg:
        return "auth_failed"
    if "HTTP 403" in msg:
        return "permission_insufficient"
    return "connection_failed"


def _finish(cfg: IntegrationConfig, state: str, ok: bool, message: str, **extra) -> dict:
    cfg.state = state
    cfg.last_test_ok = ok
    cfg.last_test_message = message[:1000]
    cfg.updated_at = wall_now()
    return _result(cfg, ok, message, **extra)


def _result(cfg: IntegrationConfig, ok: bool, message: str, **extra) -> dict:
    return {"ok": ok, "state": cfg.state, "state_label": STATE_LABELS[cfg.state], "message": message, **extra}


def sync(db: Session, cfg: IntegrationConfig) -> dict:
    """Import events from a verified operator API connection into this organisation only."""
    from ..db import begin_immediate
    from ..integrations.base import ConnectorError
    from ..integrations.ingest import ingest
    from ..models import IngestionRun

    if cfg.kind != "operator_api":
        raise IntegrationError("Only operator API connections are synchronised; OCPP charge points push data.", 409)
    if effective_state(cfg) not in ("connected", "stale"):
        raise IntegrationError("Run a successful connection test before syncing.", 409, "not_verified")
    secret = _secret(cfg)
    since = cfg.last_data_at
    db.commit()                                   # no transaction is held open across the network call
    try:
        events, errors = _connector(cfg, secret).fetch_events(since=since)
    except ConnectorError as e:
        begin_immediate(db)
        cfg.state = _classify(str(e))
        cfg.last_test_ok = False
        cfg.last_test_message = "Sync failed: " + _redact(e, secret)
        db.commit()
        raise IntegrationError(cfg.last_test_message, 502, "sync_failed")
    begin_immediate(db)
    run: IngestionRun = ingest(db, events, source="operator_api", connector="operator_api",
                               dataset_label=f"{cfg.name} (operator API sync)", pre_errors=errors)
    now = wall_now()
    cfg.last_success_at = now
    if run.accepted:
        cfg.last_data_at = now
    cfg.state = "connected"
    cfg.updated_at = now
    db.commit()
    return {"run_id": run.id, "received": run.received, "accepted": run.accepted, "duplicates": run.duplicates,
            "rejected": run.rejected, "errors": run.errors[:20]}


def _ocpp_check(db: Session, cfg: IntegrationConfig) -> dict:
    st = ocpp_status(db, cfg)
    if not cfg.operator_authorised:
        cfg.state = "draft"
        return _result(cfg, False, "Confirm that the charge points are authorised to connect to this organisation's endpoint.", **st)
    if st["real_charge_points"] and st["last_boot_at"]:
        cfg.state = "connected" if not st["stale"] else "stale"
        cfg.last_test_ok = True
        cfg.last_test_message = "A real charge point completed a BootNotification on this endpoint."
        return _result(cfg, True, cfg.last_test_message, **st)
    cfg.state = "configured"
    cfg.last_test_ok = False
    cfg.last_test_message = ("No real charge point has completed a BootNotification on this endpoint yet."
                             + (" Only the local OCPP test simulator has connected (not counted as a real charger)."
                                if st["test_charge_points"] else ""))
    return _result(cfg, False, cfg.last_test_message, **st)


def ocpp_status(db: Session, cfg: IntegrationConfig) -> dict:
    from ..models import Charger, Event

    s = get_settings()
    org = cfg.org_id
    now = wall_now()
    q = select(Charger).where(Charger.org_id == org, Charger.source == "ocpp")
    chargers = list(db.scalars(q))
    real = [c for c in chargers if not c.test_traffic]
    test = [c for c in chargers if c.test_traffic]

    def last(event_type):
        return db.scalar(select(func.max(Event.ts)).where(Event.org_id == org, Event.source == "ocpp", Event.type == event_type))

    last_boot, last_hb = last("boot"), last("heartbeat")
    last_status = db.scalar(select(func.max(Event.ts)).where(Event.org_id == org, Event.source == "ocpp",
                                                             Event.type.in_(("status_change", "status_available", "fault"))))
    last_any = db.scalar(select(func.max(Event.ts)).where(Event.org_id == org, Event.source == "ocpp"))
    return {"endpoint": f"ws://{s.ocpp_host}:{s.ocpp_port}/{cfg.ocpp_endpoint_key}/<charge_point_id>",
            "ocpp_version": "OCPP 1.6J (subprotocol ocpp1.6)",
            "real_charge_points": len(real), "test_charge_points": len(test),
            "last_boot_at": iso(last_boot), "last_heartbeat_at": iso(last_hb), "last_status_at": iso(last_status),
            "last_message_at": iso(last_any), "stale": bool(last_any and now - last_any > OCPP_STALE_AFTER),
            "connected_now": _connected_now(db, org)}


def _connected_now(db: Session, org_id: int) -> list:
    from ..integrations.base import get_health
    from ..tenancy import as_org

    with as_org(db, org_id):
        h = get_health(db, "ocpp")
    return (h.detail or {}).get("connected_charge_points", []) if h else []


def config_dict(db: Session, cfg: IntegrationConfig) -> dict:
    st = effective_state(cfg)
    d = {"id": cfg.id, "kind": cfg.kind, "name": cfg.name, "state": st, "state_label": STATE_LABELS[st],
         "adapter": cfg.adapter, "adapter_label": ADAPTERS.get(cfg.adapter, cfg.adapter), "base_url": cfg.base_url,
         "auth_method": cfg.auth_method, "credential": {"set": bool(cfg.secret_ciphertext), "set_at": iso(cfg.secret_set_at),
                                                        "masked": "••••••••" if cfg.secret_ciphertext else ""},
         "timeout_s": cfg.timeout_s, "poll_seconds": cfg.poll_seconds, "mapping": cfg.mapping or {},
         "operator_authorised": cfg.operator_authorised, "authorisation_note": cfg.authorisation_note,
         "last_test_at": iso(cfg.last_test_at), "last_test_ok": cfg.last_test_ok, "last_test_message": cfg.last_test_message,
         "last_success_at": iso(cfg.last_success_at), "last_data_at": iso(cfg.last_data_at),
         "created_at": iso(cfg.created_at), "updated_at": iso(cfg.updated_at), "next_step": next_step(cfg, st)}
    if cfg.kind == "ocpp":
        d["ocpp_allowed_ids"] = cfg.ocpp_allowed_ids or []
        d["ocpp"] = ocpp_status(db, cfg)
    return d


def next_step(cfg: IntegrationConfig, st: str) -> str:
    if cfg.kind == "ocpp":
        return {"draft": "Confirm the charge points are authorised, then configure them with the endpoint URL below.",
                "configured": "Point an authorised OCPP 1.6J charge point at the endpoint; it shows Connected after its BootNotification.",
                "connected": "", "stale": "No OCPP message for over 12 minutes: check the charge points and the central system process.",
                "disabled": "Enable the connection to accept charge points again."}.get(st, "")
    return {"not_configured": "Ask the operator for the API base URL and a read-only API credential.",
            "awaiting_credentials": "Enter the API credential issued by the operator.",
            "draft": "Enter the base URL and credential, and confirm the operator's written authorisation.",
            "configured": "Press Test connection.", "testing": "Test in progress.",
            "connected": "Press Sync to import events.", "auth_failed": "The operator rejected the credential. Check it with the operator.",
            "connection_failed": "Check the base URL and that the operator API is reachable, then test again.",
            "permission_insufficient": "The credential is valid but lacks permission to read the required data. Ask the operator.",
            "stale": "No successful sync recently. Press Sync.", "disabled": "Enable the connection to use it."}.get(st, "")


def org_operator_config(db: Session, org_id: int) -> IntegrationConfig | None:
    return db.scalar(select(IntegrationConfig).where(IntegrationConfig.org_id == org_id, IntegrationConfig.kind == "operator_api",
                                                     IntegrationConfig.state != "disabled").order_by(IntegrationConfig.id))
