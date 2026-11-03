"""Subscription & Billing (organisation owners/admins) and the public plan catalogue.

All prices and totals are computed on the server from the central catalogue. The browser sends only a
plan key and a billing period; prices, discounts or states sent by a client are ignored.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..clock import iso
from ..db import SessionLocal, begin_immediate, get_db
from ..models_saas import DemoCheckout, DemoReceipt, EnterpriseEnquiry, Organisation, SubscriptionEvent, User
from ..security.guard import client_ip
from ..services import accounts, audit, billing, demo_payment_methods, entitlements

router = APIRouter(prefix="/api")


def _err(e: billing.BillingError) -> HTTPException:
    body = {"code": e.code, "message": str(e)}
    if getattr(e, "field", None):
        body["fields"] = {e.field: str(e)}
    return HTTPException(e.status, body)


def _ctx(request: Request, db: Session) -> tuple[Organisation, User]:
    return db.get(Organisation, request.state.org_id), db.get(User, request.state.user_id)


# --------------------------------------------------------------------------- public catalogue
def _plain_db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@router.get("/public/plans")
def public_plans(db: Session = Depends(_plain_db)):
    pol = billing.policy(db)
    plans = [billing.plan_dict(p, pol) for p in billing.catalogue(db)]
    return {"plans": plans, "periods": [{"months": m, "label": billing.PERIOD_LABELS[m],
                                         "discount_percent": int(pol["discount_bps"].get(str(m), 0)) / 100} for m in billing.PERIODS],
            "feature_labels": billing.FEATURE_LABELS, "currency": "INR", "tax_note": pol["tax_note"],
            "grace_days": pol["grace_days"],
            "disclaimer": ("Illustrative prices for the ChargeSight PME prototype - assumptions, not validated market prices. "
                           "Checkout is a simulation: no payment is taken.")}


@router.get("/public/quote")
def public_quote(plan: str = Query(max_length=20), months: int = Query(1), db: Session = Depends(_plain_db)):
    try:
        return billing.quote(db, plan, months)
    except billing.BillingError as e:
        raise _err(e)


class EnquiryIn(BaseModel):
    kind: str = Field("enterprise", pattern="^(enterprise|contact)$")
    org_name: str = Field(min_length=2, max_length=160)
    contact_name: str = Field("", max_length=120)
    contact_email: str = Field(max_length=254)
    phone: str = Field("", max_length=40)
    charger_count: int | None = Field(None, ge=0, le=1_000_000)
    site_count: int | None = Field(None, ge=0, le=100_000)
    integration_requirements: str = Field("", max_length=4000)
    message: str = Field("", max_length=4000)
    website: str = Field("", max_length=200)          # honeypot: real users leave it empty


@router.post("/public/enquiries")
def enquiry(body: EnquiryIn, request: Request, db: Session = Depends(_plain_db)):
    from ..config import get_settings

    ok, retry = request.app.state.rate_limiter.hit(f"enquiry:{client_ip(request)}", get_settings().public_form_limit, 3600)
    if not ok:
        raise HTTPException(429, {"code": "rate_limited", "message": "Too many enquiries from this address. Try again later."})
    email = accounts.normalise_email(body.contact_email)
    if not accounts.valid_email(email):
        raise HTTPException(422, {"code": "validation", "message": "Enter a valid e-mail address.",
                                  "fields": {"contact_email": "Enter a valid e-mail address."}})
    if body.website:
        return {"ok": True, "stored": False}            # honeypot hit: do not store, do not reveal
    e = EnterpriseEnquiry(kind=body.kind, org_name=body.org_name.strip(), contact_name=body.contact_name.strip(), contact_email=email,
                          phone=body.phone.strip(), charger_count=body.charger_count, site_count=body.site_count,
                          integration_requirements=body.integration_requirements.strip(), message=body.message.strip(),
                          user_id=getattr(request.state, "user_id", None), org_id=getattr(request.state, "org_id", None))
    db.add(e)
    db.commit()                                         # success is reported only after the enquiry is stored
    return {"ok": True, "stored": True, "reference": f"ENQ-{e.id:05d}",
            "message": ("Thank you - your enquiry is stored as reference "
                        f"ENQ-{e.id:05d}. A platform administrator reviews enquiries in ChargeSight; "
                        "no automatic e-mail is sent from this prototype.")}


# --------------------------------------------------------------------------- organisation billing
@router.get("/billing")
def overview(request: Request, db: Session = Depends(get_db)):
    org, _ = _ctx(request, db)
    sub = billing.current(db, org.id)
    db.commit()
    pol = billing.policy(db)
    open_chk = db.scalar(select(DemoCheckout).where(DemoCheckout.org_id == org.id, DemoCheckout.status == "pending")
                         .order_by(DemoCheckout.created_at.desc()).limit(1))
    checkouts = db.scalars(select(DemoCheckout).where(DemoCheckout.org_id == org.id).order_by(DemoCheckout.created_at.desc()).limit(50))
    receipts = db.scalars(select(DemoReceipt).where(DemoReceipt.org_id == org.id).order_by(DemoReceipt.issued_at.desc()).limit(50))
    events = db.scalars(select(SubscriptionEvent).where(SubscriptionEvent.org_id == org.id).order_by(SubscriptionEvent.id.desc()).limit(100))
    return {"subscription": billing.subscription_dict(db, sub), "entitlements": entitlements.summary(db, org.id),
            "plans": [billing.plan_dict(p, pol) for p in billing.catalogue(db)],
            "open_checkout": billing.checkout_dict(open_chk) if open_chk else None,
            "checkouts": [billing.checkout_dict(c) for c in checkouts],
            "receipts": [billing.receipt_dict(r) for r in receipts],
            "events": [{"at": iso(e.at), "kind": e.kind, "from_state": e.from_state, "to_state": e.to_state, "plan_key": e.plan_key,
                        "actor": e.actor_label, "note": e.note} for e in events],
            "can_manage": request.state.role == "owner", "policy": {"grace_days": pol["grace_days"], "tax_note": pol["tax_note"],
                                                                    "checkout_ttl_min": pol["checkout_ttl_min"]},
            "mode_notice": ("DEMO/TEST billing: no real payment provider is connected and no money moves. The payment step "
                            "accepts test details only; nothing sensitive is stored."),
            "test_payment": demo_payment_methods.catalogue()}


@router.get("/billing/quote")
def org_quote(request: Request, plan: str = Query(max_length=20), months: int = Query(1), db: Session = Depends(get_db)):
    org, _ = _ctx(request, db)
    sub = billing.current(db, org.id)
    try:
        p = billing.get_plan(db, plan)
        kind = billing.classify_change(db, sub, p, months)
        from ..clock import wall_now
        credit = billing.unused_credit(db, sub, wall_now()) if kind == "upgrade" else 0
        q = billing.quote_for(p, months, billing.policy(db), credit_paise=credit)
    except billing.BillingError as e:
        raise _err(e)
    return {**q, "change_kind": kind}


class CheckoutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")         # any price / discount / state a client sends is ignored
    plan_key: str = Field(max_length=20)
    billing_months: int
    billing_name: str = Field("", max_length=120)
    billing_email: str = Field("", max_length=254)
    billing_address: str = Field("", max_length=1000)


@router.post("/billing/checkout")
def start_checkout(body: CheckoutIn, request: Request, db: Session = Depends(get_db)):
    begin_immediate(db)
    org, user = _ctx(request, db)
    if body.billing_email and not accounts.valid_email(accounts.normalise_email(body.billing_email)):
        raise HTTPException(422, {"code": "validation", "message": "Enter a valid billing e-mail address.",
                                  "fields": {"billing_email": "Invalid e-mail."}})
    try:
        chk = billing.start_checkout(db, org, user, billing.CheckoutInput(body.plan_key, body.billing_months, body.billing_name,
                                                                         accounts.normalise_email(body.billing_email), body.billing_address))
    except billing.BillingError as e:
        db.rollback()
        raise _err(e)
    audit.record(db, "billing.checkout_started", org_id=org.id, actor=user, target_type="checkout", target_id=chk.id,
                 data={"plan": chk.plan_key, "months": chk.billing_months, "total_paise": chk.total_paise})
    db.commit()
    return billing.checkout_dict(chk)


@router.get("/billing/checkout/{cid}")
def get_checkout(cid: str, request: Request, db: Session = Depends(get_db)):
    chk = db.get(DemoCheckout, cid)
    if chk is None or chk.org_id != request.state.org_id:
        raise HTTPException(404, {"code": "not_found", "message": "Checkout not found."})
    return billing.checkout_result(db, chk)


class PaymentMethodIn(BaseModel):
    """TEST payment details. Only method + a masked label are stored; no CVV / OTP / PIN field exists."""
    model_config = ConfigDict(extra="ignore")         # e.g. a 'cvv', 'otp', 'pin' or 'amount' sent by a client is dropped
    method: str = Field(max_length=20)
    upi_app: str | None = Field(None, max_length=20)
    upi_id: str | None = Field(None, max_length=60)
    card_name: str | None = Field(None, max_length=80)
    card_number: str | None = Field(None, max_length=30)
    card_expiry: str | None = Field(None, max_length=10)
    bank: str | None = Field(None, max_length=40)


@router.post("/billing/checkout/{cid}/payment-method")
def payment_method(cid: str, body: PaymentMethodIn, request: Request, db: Session = Depends(get_db)):
    begin_immediate(db)
    org, user = _ctx(request, db)
    try:
        chk = billing.attach_payment(db, org, user, cid, body.model_dump())
    except billing.BillingError as e:
        db.rollback()
        raise _err(e)
    audit.record(db, "billing.test_payment_method_selected", org_id=org.id, actor=user, target_type="checkout", target_id=chk.id,
                 data={"method": chk.payment_method})           # the masked label only lives on the checkout
    db.commit()
    return billing.checkout_dict(chk)


class CompleteIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    acknowledge_simulation: bool = False
    idempotency_key: str | None = Field(None, max_length=80)


@router.post("/billing/checkout/{cid}/complete")
def complete_checkout(cid: str, body: CompleteIn, request: Request, db: Session = Depends(get_db),
                      idempotency_key: str | None = Header(None, alias="Idempotency-Key")):
    if not body.acknowledge_simulation:
        raise HTTPException(422, {"code": "acknowledge", "message": "Confirm that you understand this is a simulation and no payment is taken."})
    begin_immediate(db)
    org, user = _ctx(request, db)
    try:
        res = billing.complete_checkout(db, org, user, cid, idempotency_key or body.idempotency_key or "")
    except billing.BillingError as e:
        db.commit()                                   # keep expiry / invalidation records
        raise _err(e)
    if not res["replayed"]:
        audit.record(db, "billing.checkout_completed", org_id=org.id, actor=user, target_type="checkout", target_id=cid,
                     data={"receipt": res["receipt"]["number"] if res["receipt"] else None})
    db.commit()
    return res


@router.post("/billing/checkout/{cid}/cancel")
def cancel_checkout(cid: str, request: Request, db: Session = Depends(get_db)):
    begin_immediate(db)
    org, user = _ctx(request, db)
    try:
        chk = billing.cancel_checkout(db, org, user, cid)
    except billing.BillingError as e:
        db.rollback()
        raise _err(e)
    db.commit()
    return billing.checkout_dict(chk)


class ReasonIn(BaseModel):
    reason: str = Field("", max_length=500)
    confirm: bool = False


@router.post("/billing/cancel-renewal")
def cancel_renewal(body: ReasonIn, request: Request, db: Session = Depends(get_db)):
    if not body.confirm:
        raise HTTPException(422, {"code": "confirm", "message": "Confirm the cancellation."})
    begin_immediate(db)
    org, user = _ctx(request, db)
    try:
        sub = billing.cancel_renewal(db, org, user, body.reason)
    except billing.BillingError as e:
        db.rollback()
        raise _err(e)
    audit.record(db, "billing.renewal_cancelled", org_id=org.id, actor=user, target_type="subscription", target_id=sub.id)
    db.commit()
    return {**billing.subscription_dict(db, sub),
            "message": "Renewal cancelled in ChargeSight. Access continues until the end of the current period. "
                       "No e-mail or external cancellation was sent; this demo has no payment provider."}


@router.post("/billing/resume-renewal")
def resume_renewal(request: Request, db: Session = Depends(get_db)):
    begin_immediate(db)
    org, user = _ctx(request, db)
    try:
        sub = billing.resume_renewal(db, org, user)
    except billing.BillingError as e:
        db.rollback()
        raise _err(e)
    db.commit()
    return billing.subscription_dict(db, sub)


class ScheduleIn(BaseModel):
    plan_key: str = Field(max_length=20)
    billing_months: int
    confirm: bool = False


@router.post("/billing/schedule-change")
def schedule_change(body: ScheduleIn, request: Request, db: Session = Depends(get_db)):
    if not body.confirm:
        raise HTTPException(422, {"code": "confirm", "message": "Confirm the change."})
    begin_immediate(db)
    org, user = _ctx(request, db)
    try:
        sub = billing.schedule_change(db, org, user, body.plan_key, body.billing_months)
    except billing.BillingError as e:
        db.rollback()
        raise _err(e)
    db.commit()
    return billing.subscription_dict(db, sub)


@router.get("/billing/receipts/{number}")
def receipt(number: str, request: Request, db: Session = Depends(get_db)):
    r = db.scalar(select(DemoReceipt).where(DemoReceipt.number == number, DemoReceipt.org_id == request.state.org_id))
    if r is None:
        raise HTTPException(404, {"code": "not_found", "message": "Receipt not found."})
    org = db.get(Organisation, r.org_id)
    return {**billing.receipt_dict(r), "organisation": org.name}
