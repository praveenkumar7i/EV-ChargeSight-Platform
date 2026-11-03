"""Optional OCPP 1.6J central system (real WebSocket protocol).

Run in a separate terminal:   python -m app.integrations.ocpp_central
Then point an OCPP 1.6J charge point (or a simulator such as
solidstudiosh/ocpp-virtual-charge-point) at
  ws://<host>:<port>/<organisation endpoint key>/<charge_point_id>   (an organisation's OCPP connection), or
  ws://<host>:<port>/<charge_point_id>                               (the system demo workspace, as before v9)
with subprotocol "ocpp1.6". Unknown or disabled endpoint keys are refused; when the connection lists
allowed charge point identities, other identities are refused (the local test simulator is always allowed
and is always labelled SIMULATED OCPP). Data is written only into that organisation.

Messages are written into the same database with source = "ocpp". A charger
counts as connected only after its BootNotification has been received and
accepted over a real WebSocket. This module is separate from the internal
simulator, which never uses this endpoint.

Supported charge-point -> central messages: BootNotification, Heartbeat,
StatusNotification, MeterValues, StartTransaction, StopTransaction, Authorize.

Test traffic: a charge point whose BootNotification vendor is
"ChargeSight-Sim" (see scripts/ocpp_simulator.py), or whose identity
starts with "SIMOCPP", is flagged test_traffic=True and shown as
SIMULATED OCPP, never as real-world telemetry. Any other charge point that
completes a BootNotification is shown as OCPP-connected.

Deduplication: every stored event is keyed by the charge point id, the event
type and the OCPP message's unique id. OCPP 1.6J requires a charge point to
re-send an unanswered CALL with the same unique id, so a retransmitted message
is stored once while distinct messages are never merged.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
from datetime import datetime, timezone

from ocpp.routing import on
from ocpp.v16 import ChargePoint as OcppChargePoint
from ocpp.v16 import call_result
from ocpp.v16.datatypes import IdTagInfo
from ocpp.v16.enums import Action, AuthorizationStatus, RegistrationStatus

from ..config import get_settings
from ..db import begin_immediate, create_schema, session_scope
from ..services.seed import seed_reference_data
from .base import set_health
from .ingest import ingest
from .schema import ExternalEvent

log = logging.getLogger("chargesight.ocpp")
_seq = itertools.count(1)
_tx = itertools.count(1)
HEARTBEAT_INTERVAL = 300
SIM_VENDOR = "ChargeSight-Sim"
CONNECTED: dict[tuple[int, str], str] = {}   # (organisation, charge point id) -> connected-at ISO time (this process)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _ts(value: str | None) -> datetime:
    if value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _bind(db, org_id: int):
    from ..tenancy import set_org

    return set_org(db, org_id)


def _touch_config(db, org_id: int, *, real_boot: bool = False) -> None:
    """Record data arrival on the organisation's OCPP connection; a real (non-test) BootNotification
    verifies it. Never marks a connection verified because of the local test simulator."""
    from sqlalchemy import select

    from ..clock import wall_now
    from ..models_saas import IntegrationConfig

    cfg = db.scalar(select(IntegrationConfig).where(IntegrationConfig.org_id == org_id, IntegrationConfig.kind == "ocpp",
                                                    IntegrationConfig.state != "disabled").limit(1))
    if cfg is None:
        return
    now = wall_now()
    cfg.last_data_at = now
    if real_boot and cfg.operator_authorised:
        cfg.state = "connected"
        cfg.last_success_at = now
        cfg.last_test_ok = True
        cfg.last_test_message = "A real charge point completed a BootNotification on this endpoint."


def _emit(cp_id: str, event_type: str, ts: datetime, dedupe_key: str | None = None, *, org_id: int = 1, **fields) -> None:
    ev = ExternalEvent(
        external_event_id=(f"{cp_id}:{event_type}:{dedupe_key}" if dedupe_key
                           else f"{cp_id}:{event_type}:{ts.isoformat()}:{next(_seq)}"),
        external_charger_id=cp_id,
        timestamp=ts,
        event_type=event_type,
        site_code="OCPP",
        site_name="OCPP-connected chargers",
        **fields,
    )
    with session_scope() as db:
        _bind(db, org_id)
        begin_immediate(db)
        run = ingest(db, [ev], source="ocpp", connector="ocpp")
        if run.accepted:
            _touch_config(db, org_id)
        if run.rejected:
            log.warning("OCPP event rejected: %s", run.errors)
        elif run.duplicates:
            log.info("OCPP duplicate ignored: %s", ev.external_event_id)


def _update_charger(cp_id: str, *, org_id: int = 1, **attrs) -> None:
    from ..models import Charger
    from ..tenancy import nid

    with session_scope() as db:
        _bind(db, org_id)
        begin_immediate(db)
        ch = db.get(Charger, nid(db, f"OCPP-{cp_id}")[:64])
        if ch is not None:
            for k, v in attrs.items():
                setattr(ch, k, v)


def _connected_list(org_id: int) -> list[dict]:
    return [{"id": cp, "since": v} for (o, cp), v in sorted(CONNECTED.items()) if o == org_id]


def _publish_connections(org_id: int, last: str | None = None) -> None:
    with session_scope() as db:
        _bind(db, org_id)
        begin_immediate(db)
        from ..models import ConnectorHealth

        from .base import get_health
        h = get_health(db, "ocpp")
        detail = dict(h.detail or {}) if h else {}
        detail["connected_charge_points"] = _connected_list(org_id)
        if last:
            detail["last_charge_point"] = last
        if h is not None:
            h.detail = detail


def _meter_summary(meter_value: list) -> tuple[float | None, float | None, str | None]:
    """Return (energy kWh, power kW, first timestamp) from OCPP 1.6 MeterValues."""
    energy = power = None
    first_ts = None
    for mv in meter_value or []:
        first_ts = first_ts or mv.get("timestamp")
        for sv in mv.get("sampled_value") or mv.get("sampledValue") or []:
            meas = sv.get("measurand") or "Energy.Active.Import.Register"
            unit = (sv.get("unit") or ("Wh" if meas.startswith("Energy") else "W"))
            try:
                v = float(sv.get("value"))
            except (TypeError, ValueError):
                continue
            if meas == "Energy.Active.Import.Register":
                energy = v / 1000.0 if unit == "Wh" else v
            elif meas == "Power.Active.Import":
                power = v / 1000.0 if unit == "W" else v
    return energy, power, first_ts


class CentralChargePoint(OcppChargePoint):
    _uid: str | None = None

    def __init__(self, cp_id: str, connection, org_id: int = 1):
        super().__init__(cp_id, connection)
        self.org_id = org_id

    async def route_message(self, raw_msg):
        """Remember the incoming CALL's unique id so handlers can use it as a dedupe key."""
        try:
            import json

            msg = json.loads(raw_msg)
            self._uid = str(msg[1]) if isinstance(msg, list) and len(msg) > 2 and msg[0] == 2 else None
        except (ValueError, TypeError):
            self._uid = None
        return await super().route_message(raw_msg)

    @on(Action.boot_notification)
    def on_boot(self, charge_point_vendor: str, charge_point_model: str, **kwargs):
        test = charge_point_vendor == SIM_VENDOR or self.id.upper().startswith("SIMOCPP")
        _emit(self.id, "boot", datetime.now(timezone.utc), org_id=self.org_id, dedupe_key=self._uid,
              raw={"vendor": charge_point_vendor, "model": charge_point_model, "test_traffic": test})
        _update_charger(self.id, org_id=self.org_id, test_traffic=test, ocpp_vendor=charge_point_vendor[:80], ocpp_model=charge_point_model[:80])
        with session_scope() as db:
            _bind(db, self.org_id)
            begin_immediate(db)
            set_health(db, "ocpp", "connected", success=True,
                       detail={"last_charge_point": self.id, "verified_by": "BootNotification accepted",
                               "last_charge_point_is_simulator": test,
                               "connected_charge_points": _connected_list(self.org_id)})
            _touch_config(db, self.org_id, real_boot=not test)
        return call_result.BootNotification(current_time=_now_iso(), interval=HEARTBEAT_INTERVAL,
                                            status=RegistrationStatus.accepted)

    @on(Action.heartbeat)
    def on_heartbeat(self, **kwargs):
        _emit(self.id, "heartbeat", datetime.now(timezone.utc), org_id=self.org_id, dedupe_key=self._uid)
        return call_result.Heartbeat(current_time=_now_iso())

    @on(Action.status_notification)
    def on_status(self, connector_id: int, error_code: str, status: str, timestamp: str | None = None, **kw):
        ts = _ts(timestamp)
        if status == "Faulted" or error_code not in ("NoError", None):
            from ..core.catalog import OCPP_ERROR_TO_CATEGORY

            cat = OCPP_ERROR_TO_CATEGORY.get(error_code, "comm_loss" if error_code == "OtherError" else None)
            _emit(self.id, "fault", ts, org_id=self.org_id, dedupe_key=self._uid, fault_category=cat or "overcurrent",
                  raw={"connectorId": connector_id, "errorCode": error_code, "status": status})
            _update_charger(self.id, org_id=self.org_id, power_now_kw=0.0)
        else:
            etype = "status_available" if status in ("Available", "Preparing", "Finishing") else "status_change"
            _emit(self.id, etype, ts, org_id=self.org_id, dedupe_key=self._uid, status=status, raw={"connectorId": connector_id, "status": status})
        return call_result.StatusNotification()

    @on(Action.meter_values)
    def on_meter(self, connector_id: int, meter_value: list, **kwargs):
        energy, power, first_ts = _meter_summary(meter_value)
        _emit(self.id, "meter_values", _ts(first_ts),
              org_id=self.org_id, dedupe_key=self._uid,
              meter_kwh=energy, raw={"meterValue": meter_value, "power_kW": power, "energy_kWh": energy})
        if power is not None:
            _update_charger(self.id, org_id=self.org_id, power_now_kw=round(power, 2))
        return call_result.MeterValues()

    @on(Action.authorize)
    def on_authorize(self, id_tag: str, **kwargs):
        return call_result.Authorize(id_tag_info=IdTagInfo(status=AuthorizationStatus.accepted))

    @on(Action.start_transaction)
    def on_start(self, connector_id: int, id_tag: str, meter_start: int, timestamp: str, **kwargs):
        tx = next(_tx)
        _emit(self.id, "session_start", _ts(timestamp), org_id=self.org_id, dedupe_key=self._uid,
              status="Charging", meter_kwh=meter_start / 1000.0,
              raw={"connectorId": connector_id, "idTag": id_tag, "transactionId": tx})
        _update_charger(self.id, org_id=self.org_id, session_started_at=_ts(timestamp).replace(tzinfo=None), session_kwh=0.0)
        return call_result.StartTransaction(transaction_id=tx,
                                            id_tag_info=IdTagInfo(status=AuthorizationStatus.accepted))

    @on(Action.stop_transaction)
    def on_stop(self, meter_stop: int, timestamp: str, transaction_id: int, **kwargs):
        _emit(self.id, "session_stop", _ts(timestamp), org_id=self.org_id, dedupe_key=self._uid,
              status="Available", meter_kwh=meter_stop / 1000.0,
              raw={"transactionId": transaction_id, "reason": kwargs.get("reason")})
        _update_charger(self.id, org_id=self.org_id, power_now_kw=0.0, session_started_at=None)
        return call_result.StopTransaction()


async def _handler(websocket):
    try:
        requested = websocket.request.headers.get("Sec-WebSocket-Protocol", "")
    except AttributeError:  # pragma: no cover - older websockets
        requested = websocket.request_headers.get("Sec-WebSocket-Protocol", "")
    if websocket.subprotocol != "ocpp1.6":
        log.warning("Rejecting connection: subprotocol %r (requested %r)", websocket.subprotocol, requested)
        await websocket.close()
        return
    path = getattr(getattr(websocket, "request", None), "path", None) or getattr(websocket, "path", "/")
    parts = [p for p in path.split("?")[0].strip("/").split("/") if p]
    cp_id = (parts[-1] if parts else "") or "unknown"
    org_id = resolve_endpoint(parts[-2] if len(parts) >= 2 else None, cp_id)
    if org_id is None:
        log.warning("Rejecting charge point %s: unknown, disabled or unauthorised endpoint", cp_id[:60])
        await websocket.close()
        return
    log.info("Charge point connected: %s (organisation %s)", cp_id, org_id)
    CONNECTED[(org_id, cp_id)] = _now_iso()
    cp = CentralChargePoint(cp_id, websocket, org_id)
    try:
        await cp.start()
    except Exception as e:  # connection closed
        log.info("Charge point %s disconnected: %s", cp_id, type(e).__name__)
    finally:
        CONNECTED.pop((org_id, cp_id), None)
        try:
            _publish_connections(org_id, cp_id)
        except Exception:  # pragma: no cover - db unavailable during shutdown
            pass


def resolve_endpoint(endpoint_key: str | None, cp_id: str) -> int | None:
    """Organisation for a connection path. No key = the system demo workspace (pre-v9 behaviour)."""
    from sqlalchemy import select

    from ..models_saas import IntegrationConfig
    from ..tenancy import LEGACY_ORG_ID

    if endpoint_key is None:
        return LEGACY_ORG_ID
    with session_scope() as db:
        cfg = db.scalar(select(IntegrationConfig).where(IntegrationConfig.ocpp_endpoint_key == endpoint_key,
                                                        IntegrationConfig.kind == "ocpp"))
        if cfg is None or cfg.state == "disabled":
            return None
        test = cp_id.upper().startswith("SIMOCPP")
        allowed = cfg.ocpp_allowed_ids or []
        if allowed and cp_id not in allowed and not test:
            return None
        return cfg.org_id


async def serve(host: str | None = None, port: int | None = None, stop: asyncio.Event | None = None):
    import websockets

    s = get_settings()
    async with websockets.serve(_handler, host or s.ocpp_host, port or s.ocpp_port, subprotocols=["ocpp1.6"]):
        log.info("OCPP 1.6J central system listening on ws://%s:%s/[<organisation endpoint key>/]<charge_point_id>",
                 host or s.ocpp_host, port or s.ocpp_port)
        if stop is None:
            await asyncio.Future()
        else:
            await stop.wait()


def main() -> None:  # pragma: no cover - CLI
    logging.basicConfig(level=logging.INFO)
    create_schema()
    with session_scope() as db:
        begin_immediate(db)
        seed_reference_data(db)
        set_health(db, "ocpp", "configured_unverified",
                   detail={"note": "Central system running; no charge point verified yet."})
    asyncio.run(serve())


if __name__ == "__main__":  # pragma: no cover
    main()
