"""v9.2 TEST payment methods for the demo checkout.

This module validates *test* payment details so that the checkout can show a realistic payment step. It never
contacts a payment gateway, bank or UPI network, and it never moves money.

Safety rules
------------
* Only published / clearly fictional test values are accepted:
  - cards: the industry-standard test numbers below (no real card number is accepted);
  - UPI: test handles on the ``@test`` domain (``success@test`` approves, ``failure@test`` declines);
  - net banking: two fictional "ChargeSight Test Bank" entries.
* A CVV, OTP, UPI PIN or bank password is never requested, accepted or stored.
* Nothing sensitive is stored or logged: the checkout keeps only the method and a masked label such as
  "Test card · Visa ending 1111". Error messages never echo what was typed.
* The outcome ("approve" / "decline") is decided by the test value, so the demo can show both results honestly.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

METHODS = {"upi": "UPI", "credit_card": "Credit card", "debit_card": "Debit card", "netbanking": "Net banking"}
UPI_APPS = {"gpay": "Google Pay", "phonepe": "PhonePe", "paytm": "Paytm", "bhim": "BHIM", "other": "Another UPI app"}
# Industry-standard test card numbers (they cannot be charged on any real network).
TEST_CARDS = {
    "4111111111111111": ("Visa", "approve"),
    "5555555555554444": ("Mastercard", "approve"),
    "4000000000000002": ("Visa", "decline"),
}
TEST_BANKS = {"test_bank_approve": ("ChargeSight Test Bank", "approve"),
              "test_bank_decline": ("ChargeSight Test Bank (declines)", "decline")}
UPI_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,39}@test$")
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z .'-]{1,59}$")
EXP_RE = re.compile(r"^(0[1-9]|1[0-2])\s*/\s*(\d{2})$")


class PaymentDetailsError(ValueError):
    def __init__(self, message: str, field: str):
        super().__init__(message)
        self.field = field


@dataclass(frozen=True)
class PaymentChoice:
    method: str
    display: str            # masked, non-sensitive label shown in the UI, receipts and audit data
    outcome: str            # approve | decline


def catalogue() -> dict:
    """What the browser shows. Every value here is a test value; none is a real credential."""
    return {
        "methods": [{"key": k, "label": v} for k, v in METHODS.items()],
        "upi_apps": [{"key": k, "label": v} for k, v in UPI_APPS.items()],
        "test_upi_ids": [{"value": "success@test", "result": "approves"}, {"value": "failure@test", "result": "declines"}],
        "test_cards": [{"number": "4111 1111 1111 1111", "brand": "Visa", "result": "approves"},
                       {"number": "5555 5555 5555 4444", "brand": "Mastercard", "result": "approves"},
                       {"number": "4000 0000 0000 0002", "brand": "Visa", "result": "declines"}],
        "test_banks": [{"key": k, "label": v[0], "result": "approves" if v[1] == "approve" else "declines"}
                       for k, v in TEST_BANKS.items()],
        "notice": ("TEST payment details only. No payment gateway is connected and no money moves. ChargeSight never "
                   "asks for a CVV, OTP, UPI PIN or bank password. Do not enter real card or bank details."),
    }


def _luhn(num: str) -> bool:
    total, alt = 0, False
    for d in reversed(num):
        n = int(d)
        if alt:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
        alt = not alt
    return total % 10 == 0


def validate(data: dict, now: datetime) -> PaymentChoice:
    """Validate test payment details. Raises PaymentDetailsError (field-specific, never echoing input)."""
    method = str(data.get("method") or "")
    if method not in METHODS:
        raise PaymentDetailsError("Choose a payment method.", "method")
    if method == "upi":
        app = str(data.get("upi_app") or "")
        if app not in UPI_APPS:
            raise PaymentDetailsError("Choose a UPI app.", "upi_app")
        vpa = str(data.get("upi_id") or "").strip().lower()
        if not UPI_RE.match(vpa):
            raise PaymentDetailsError("Enter a test UPI ID on the @test handle, for example success@test. "
                                      "Real UPI IDs are not accepted in this demo.", "upi_id")
        outcome = "decline" if vpa.startswith("failure@") else "approve"
        return PaymentChoice(method, f"UPI · {UPI_APPS[app]} · {vpa} (test UPI ID)", outcome)
    if method in ("credit_card", "debit_card"):
        name = str(data.get("card_name") or "").strip()
        if not NAME_RE.match(name):
            raise PaymentDetailsError("Enter the name on the test card (letters only).", "card_name")
        num = re.sub(r"[\s-]", "", str(data.get("card_number") or ""))
        if not num.isdigit() or not 12 <= len(num) <= 19 or not _luhn(num):
            raise PaymentDetailsError("Enter a valid test card number.", "card_number")
        if num not in TEST_CARDS:
            raise PaymentDetailsError("Only the listed test card numbers are accepted. Never enter a real card in this demo.",
                                      "card_number")
        m = EXP_RE.match(str(data.get("card_expiry") or "").strip())
        if not m:
            raise PaymentDetailsError("Enter the expiry as MM/YY.", "card_expiry")
        month, year = int(m.group(1)), 2000 + int(m.group(2))
        if (year, month) < (now.year, now.month):
            raise PaymentDetailsError("This test card's expiry date is in the past.", "card_expiry")
        brand, outcome = TEST_CARDS[num]
        kind = "credit" if method == "credit_card" else "debit"
        return PaymentChoice(method, f"Test {kind} card · {brand} ending {num[-4:]}", outcome)
    bank = str(data.get("bank") or "")
    if bank not in TEST_BANKS:
        raise PaymentDetailsError("Choose a test bank.", "bank")
    label, outcome = TEST_BANKS[bank]
    return PaymentChoice(method, f"Net banking · {label} (test)", outcome)
