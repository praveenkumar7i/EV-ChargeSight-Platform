"""Payment-provider abstraction.

Only `DemoPaymentProvider` exists. It simulates a confirmation for the PME prototype from the TEST payment
method attached to the checkout (see `demo_payment_methods`): test values that approve are confirmed, test values
that decline are declined. It does not contact any payment network, never sees real card or bank data and never
moves money.

Adding a real gateway later (e.g. Razorpay or Stripe) means implementing `PaymentProvider` so that
`confirm()` returns verified=True ONLY after the server has verified the provider's signed webhook or
server-to-server payment status for this checkout. A browser redirect or a client-side callback must
never be treated as proof of payment.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class PaymentResult:
    verified: bool
    provider: str
    reference: str | None
    simulated: bool
    message: str = ""


class PaymentProvider(Protocol):
    name: str

    def confirm(self, checkout) -> PaymentResult: ...


class DemoPaymentProvider:
    """Simulated confirmation. The result is always flagged simulated=True and labelled DEMO everywhere."""

    name = "demo"

    def confirm(self, checkout) -> PaymentResult:
        if checkout.total_paise < 0:
            return PaymentResult(False, self.name, None, True, "Negative totals are invalid.")
        if not getattr(checkout, "payment_method", None):
            return PaymentResult(False, self.name, None, True, "No test payment method was provided.")
        if getattr(checkout, "payment_test_outcome", None) == "decline":
            return PaymentResult(False, self.name, None, True, "Test payment declined (simulated).")
        return PaymentResult(True, self.name, f"DEMO-{secrets.token_hex(6).upper()}", True,
                             "Simulated confirmation. No payment was taken.")
