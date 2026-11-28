from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.http import Http404, HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt

from . import services
from .models import Payment


def pay(request, payment_id):
    """Сторінка оплати з віджетом LiqPay."""
    payment = get_object_or_404(Payment, pk=payment_id)
    if payment.status == Payment.Status.PAID:
        return render(request, "payments/result.html", {"payment": payment})
    data, signature = services.build_checkout(payment)
    return render(request, "payments/pay.html", {
        "payment": payment, "data": data, "signature": signature,
    })


@csrf_exempt  # LiqPay може повернути клієнта на result_url методом POST
def result(request, payment_id):
    """Куди повертається клієнт після оплати."""
    payment = get_object_or_404(Payment, pk=payment_id)
    if payment.status == Payment.Status.NEW:
        # Якщо callback ще не дійшов (або його не може бути на localhost),
        # самі уточнюємо статус у LiqPay.
        payment = services.refresh_from_liqpay(payment)
    return render(request, "payments/result.html", {"payment": payment})


@csrf_exempt
def callback(request):
    """LiqPay викликає цей URL сам. Основне підтвердження оплати."""
    if request.method != "POST":
        return HttpResponseBadRequest()

    data = request.POST.get("data", "")
    signature = request.POST.get("signature", "")
    if not services.verify_signature(data, signature):
        return HttpResponseBadRequest("bad signature")

    resp = services.get_liqpay().decode_data_from_str(data)
    if services.apply_liqpay_result(resp) is None:
        return HttpResponseBadRequest("order mismatch")
    return HttpResponse("ok")


def create_test(request):
    """Тестова сторінка для створення платежу без кошика. Тільки при DEBUG=True."""
    if not settings.DEBUG:
        raise Http404()
    if request.method == "POST":
        try:
            amount = Decimal(request.POST.get("amount", "0")).quantize(Decimal("0.01"))
        except InvalidOperation:
            amount = Decimal("0")
        if amount > 0:
            payment = Payment.objects.create(
                amount=amount,
                description=request.POST.get("description") or "Тестовий платіж",
                order_ref="test",
            )
            return redirect("payments:pay", payment_id=payment.id)
    return render(request, "payments/test_create.html")
