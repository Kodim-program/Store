import base64
import json
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse

from . import services
from .models import Payment
from .signals import payment_paid

KEYS = dict(
    LIQPAY_PUBLIC_KEY="pub_test",
    LIQPAY_PRIVATE_KEY="priv_test",
    LIQPAY_SANDBOX=True,
    SITE_URL="http://localhost:8000",
)


def signed(payload, private="priv_test"):
    data = base64.b64encode(json.dumps(payload).encode()).decode()
    return {"data": data, "signature": services.get_liqpay().str_to_sign(private + data + private)}


@override_settings(**KEYS)
class PaymentFlowTests(TestCase):
    def setUp(self):
        self.p = Payment.objects.create(amount=Decimal("100.00"), description="Test", order_ref="1")
        self.paid_signals = []
        payment_paid.connect(self._on_paid, weak=False)

    def tearDown(self):
        payment_paid.disconnect(self._on_paid)

    def _on_paid(self, sender, payment, **kw):
        self.paid_signals.append(payment.pk)

    def payload(self, **over):
        d = {"order_id": str(self.p.id), "amount": 100, "currency": "UAH", "status": "sandbox"}
        d.update(over)
        return d

    def test_pay_page_renders(self):
        r = self.client.get(reverse("payments:pay", args=[self.p.id]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "LiqPayCheckout")

    def test_callback_marks_paid_once(self):
        url = reverse("payments:callback")
        self.assertEqual(self.client.post(url, signed(self.payload())).status_code, 200)
        self.assertEqual(self.client.post(url, signed(self.payload())).status_code, 200)  # повтор
        self.p.refresh_from_db()
        self.assertEqual(self.p.status, Payment.Status.PAID)
        self.assertEqual(len(self.paid_signals), 1)  # сигнал лише раз

    def test_bad_signature_rejected(self):
        body = signed(self.payload(), private="WRONG")
        self.assertEqual(self.client.post(reverse("payments:callback"), body).status_code, 400)
        self.p.refresh_from_db()
        self.assertEqual(self.p.status, Payment.Status.NEW)

    def test_amount_mismatch_rejected(self):
        r = self.client.post(reverse("payments:callback"), signed(self.payload(amount=1)))
        self.assertEqual(r.status_code, 400)
        self.p.refresh_from_db()
        self.assertEqual(self.p.status, Payment.Status.NEW)

    def test_failure_status(self):
        self.client.post(reverse("payments:callback"), signed(self.payload(status="failure")))
        self.p.refresh_from_db()
        self.assertEqual(self.p.status, Payment.Status.FAILED)

    def test_result_page_polls_liqpay_status(self):
        fake = {"result": "ok", **self.payload()}
        with mock.patch.object(services.LiqPay, "api", return_value=fake):
            r = self.client.post(reverse("payments:result", args=[self.p.id]))
        self.assertEqual(r.status_code, 200)
        self.p.refresh_from_db()
        self.assertEqual(self.p.status, Payment.Status.PAID)

    def test_result_page_survives_liqpay_error(self):
        with mock.patch.object(services.LiqPay, "api", side_effect=RuntimeError("net")):
            r = self.client.get(reverse("payments:result", args=[self.p.id]))
        self.assertEqual(r.status_code, 200)

    @override_settings(DEBUG=True)
    def test_test_page_creates_payment(self):
        r = self.client.post(reverse("payments:test"), {"amount": "10", "description": "x"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(Payment.objects.filter(order_ref="test", amount=Decimal("10.00")).exists())

    @override_settings(DEBUG=False)
    def test_test_page_hidden_in_production(self):
        self.assertEqual(self.client.get(reverse("payments:test")).status_code, 404)
