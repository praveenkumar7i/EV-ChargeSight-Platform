"""Local OCPP 1.6J charge-point simulator for ChargeSight (TEST TRAFFIC ONLY).

Connects to the ChargeSight central system over a real WebSocket with the
"ocpp1.6" subprotocol and plays a scripted sequence:

  BootNotification -> Heartbeat -> StatusNotification(Available)
  -> Authorize -> StartTransaction -> StatusNotification(Charging)
  -> MeterValues x N (energy + power) -> StopTransaction
  -> StatusNotification(Available) -> StatusNotification(Faulted, <error code>)
  [-> optional recovery: StatusNotification(Available)]

It identifies itself with vendor "ChargeSight-Sim", so the central system
stores its traffic with source=ocpp but flags the charger test_traffic=True;
the UI labels it SIMULATED OCPP. It is NOT a real charger.

Uses the open-source `ocpp` (MIT) and `websockets` (BSD-3) packages already
pinned in requirements.txt. No downloads, no executables.

Usage (central system must be running, see docs/INTEGRATION.md):
    python scripts/ocpp_simulator.py
    python scripts/ocpp_simulator.py --id SIMOCPP-002 --fault HighTemperature --recover
    python scripts/ocpp_simulator.py --url ws://127.0.0.1:9000 --meter-count 6 --interval 2
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import random
import sys
from datetime import datetime, timezone

import websockets
from ocpp.v16 import ChargePoint as CP
from ocpp.v16 import call
from ocpp.v16.enums import ChargePointErrorCode, ChargePointStatus, RegistrationStatus

VENDOR = "ChargeSight-Sim"
log = logging.getLogger("ocpp-simulator")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class SimChargePoint(CP):
    async def status(self, status: ChargePointStatus, error: ChargePointErrorCode = ChargePointErrorCode.no_error):
        await self.call(call.StatusNotification(connector_id=1, error_code=error, status=status, timestamp=now()))
        log.info("StatusNotification %s (%s)", status.value, error.value)

    async def run(self, args) -> int:
        boot = await self.call(call.BootNotification(charge_point_vendor=VENDOR, charge_point_model=args.model,
                                                     charge_point_serial_number=args.id, firmware_version="sim-1.0"))
        log.info("BootNotification -> %s (heartbeat interval %ss)", boot.status, boot.interval)
        if boot.status != RegistrationStatus.accepted:
            log.error("Central system did not accept the boot; stopping.")
            return 2
        hb = await self.call(call.Heartbeat())
        log.info("Heartbeat -> central time %s", hb.current_time)
        await self.status(ChargePointStatus.available)
        await asyncio.sleep(args.interval)

        auth = await self.call(call.Authorize(id_tag=args.id_tag))
        log.info("Authorize %s -> %s", args.id_tag, auth.id_tag_info["status"] if isinstance(auth.id_tag_info, dict) else auth.id_tag_info)
        meter_wh = args.meter_start_wh
        start = await self.call(call.StartTransaction(connector_id=1, id_tag=args.id_tag, meter_start=meter_wh, timestamp=now()))
        tx = start.transaction_id
        log.info("StartTransaction -> transaction %s", tx)
        await self.status(ChargePointStatus.charging)

        rng = random.Random(args.seed)
        for i in range(args.meter_count):
            await asyncio.sleep(args.interval)
            power_w = int(args.power_kw * 1000 * (0.85 + 0.15 * rng.random()))
            meter_wh += int(power_w * args.sim_minutes_per_sample / 60)
            await self.call(call.MeterValues(connector_id=1, transaction_id=tx, meter_value=[{
                "timestamp": now(),
                "sampledValue": [
                    {"value": str(meter_wh), "measurand": "Energy.Active.Import.Register", "unit": "Wh", "context": "Sample.Periodic"},
                    {"value": str(power_w), "measurand": "Power.Active.Import", "unit": "W", "context": "Sample.Periodic"},
                ]}]))
            log.info("MeterValues %s/%s: %.1f kWh, %.1f kW", i + 1, args.meter_count, meter_wh / 1000, power_w / 1000)

        await asyncio.sleep(args.interval)
        await self.call(call.StopTransaction(meter_stop=meter_wh, timestamp=now(), transaction_id=tx, reason="EVDisconnected"))
        log.info("StopTransaction %s at %.1f kWh", tx, meter_wh / 1000)
        await self.status(ChargePointStatus.available)

        if args.fault:
            await asyncio.sleep(args.interval)
            await self.status(ChargePointStatus.faulted, ChargePointErrorCode(args.fault))
            if args.recover:
                await asyncio.sleep(args.interval)
                await self.status(ChargePointStatus.available)
        await self.call(call.Heartbeat())
        log.info("Sequence complete.")
        return 0


async def main_async(args) -> int:
    url = f"{args.url.rstrip('/')}/{args.id}"
    log.info("Connecting to %s (subprotocol ocpp1.6) as %s", url, args.id)
    try:
        async with websockets.connect(url, subprotocols=["ocpp1.6"], open_timeout=10) as ws:
            if ws.subprotocol != "ocpp1.6":
                log.error("Server did not agree to subprotocol ocpp1.6 (got %r).", ws.subprotocol)
                return 2
            cp = SimChargePoint(args.id, ws)
            listener = asyncio.create_task(cp.start())
            try:
                code = await cp.run(args)
                if args.stay:
                    log.info("Staying connected; sending a Heartbeat every %ss (Ctrl+C to stop).", args.stay)
                    while True:
                        await asyncio.sleep(args.stay)
                        await cp.call(call.Heartbeat())
                return code
            finally:
                listener.cancel()
    except (OSError, websockets.exceptions.InvalidHandshake) as e:
        log.error("Could not connect to the central system at %s: %s. Is it running?", url, e)
        return 3


def parse(argv=None):
    ap = argparse.ArgumentParser(description="ChargeSight OCPP 1.6J test charge point (SIMULATED OCPP traffic).")
    ap.add_argument("--url", default="ws://127.0.0.1:9000", help="central system base URL (default ws://127.0.0.1:9000)")
    ap.add_argument("--id", default="SIMOCPP-001", help="charge point identity (default SIMOCPP-001)")
    ap.add_argument("--model", default="Virtual DC 60 kW")
    ap.add_argument("--id-tag", default="SIMTAG01")
    ap.add_argument("--power-kw", type=float, default=50.0)
    ap.add_argument("--meter-start-wh", type=int, default=100000)
    ap.add_argument("--meter-count", type=int, default=4)
    ap.add_argument("--sim-minutes-per-sample", type=float, default=5.0, help="energy added per sample, in minutes at that power")
    ap.add_argument("--interval", type=float, default=1.0, help="wall-clock seconds between messages")
    ap.add_argument("--fault", default="GroundFailure",
                    help="OCPP 1.6 error code for the final fault, e.g. GroundFailure, HighTemperature, OverCurrentFailure; '' for none")
    ap.add_argument("--recover", action="store_true", help="send Available after the fault")
    ap.add_argument("--stay", type=float, default=0, help="stay connected and heartbeat every N seconds")
    ap.add_argument("--seed", type=int, default=1)
    return ap.parse_args(argv)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("ocpp").setLevel(logging.WARNING)
    try:
        return asyncio.run(main_async(parse(argv)))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
