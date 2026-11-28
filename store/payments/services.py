"""Уся робота з LiqPay в одному місці."""
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.urls import reverse
from liqpay import LiqPay

from .models import Payment
from .signals import payment_paid

SUCCESS_STATUSES = ("success", "sandbox")
FAIL_STATUSES = ("failure", "error", "reversed")


def get_liqpay():
    return LiqPay(settings.LIQPAY_PUBLIC_KEY, settings.LIQPAY_PRIVATE_KEY)


def build_checkout(payment):
    """Повертає data і signature для віджета LiqPay."""
    liqpay = get_liqpay()
    params = {
        "version": "3",
        "action": "pay",
        "amount": str(payment.amount),
        "currency": "UAH",
        "description": payment.description,
        "order_id": str(payment.id),
        "language": "uk",
        "sandbox": 1 if settings.LIQPAY_SANDBOX else 0,
        "server_url": settings.SITE_URL + reverse("payments:callback"),
        "result_url": settings.SITE_URL + reverse("payments:result", args=[payment.id]),
    }
    return liqpay.cnb_data(params), liqpay.cnb_signature(params)


def verify_signature(data, signature):
    liqpay = get_liqpay()
    expected = liqpay.str_to_sign(
        settings.LIQPAY_PRIVATE_KEY + data + settings.LIQPAY_PRIVATE_KEY
    )
    return bool(data) and expected == signature


def apply_liqpay_result(resp):
    """
    Застосовує відповідь LiqPay (з callback або з action=status) до платежу.
    Безпечно викликати повторно. Повертає Payment або None, якщо відповідь не підійшла.
    """
    try:
        paid_amount = Decimal(str(resp.get("amount")))
    except InvalidOperation:
        return None

    just_paid = False
    with transaction.atomic():
        try:
            payment = Payment.objects.select_for_update().get(pk=resp.get("order_id"))
        except (Payment.DoesNotExist, ValueError, TypeError):
            return None

        if paid_amount != payment.amount or resp.get("currency") != "UAH":
            return None

        status = resp.get("status", "")
        payment.liqpay_status = status
        if payment.status != Payment.Status.PAID:
            if status in SUCCESS_STATUSES:
                payment.status = Payment.Status.PAID
                just_paid = True
            elif status in FAIL_STATUSES:
                payment.status = Payment.Status.FAILED
        payment.save()

    if just_paid:
        payment_paid.send(sender=Payment, payment=payment)
    return payment


def refresh_from_liqpay(payment):
    """Запитує статус у LiqPay. Потрібно, коли callback недосяжний (localhost)."""
    try:
        resp = get_liqpay().api("request", {
            "action": "status",
            "version": "3",
            "order_id": str(payment.id),
        })
    except Exception:
        return payment
    if not isinstance(resp, dict) or resp.get("result") == "error":
        return payment
    return apply_liqpay_result(resp) or payment
