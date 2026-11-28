import uuid

from django.db import models


class Payment(models.Model):
    class Status(models.TextChoices):
        NEW = "new", "Очікує оплати"
        PAID = "paid", "Оплачено"
        FAILED = "failed", "Помилка"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    amount = models.DecimalField("Сума, грн", max_digits=10, decimal_places=2)
    description = models.CharField("Призначення", max_length=255)
    # Номер/ID вашого замовлення, щоб знайти його після оплати
    order_ref = models.CharField("Замовлення", max_length=64, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.NEW)
    liqpay_status = models.CharField(max_length=32, blank=True)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created"]
        verbose_name = "Платіж"
        verbose_name_plural = "Платежі"

    def __str__(self):
        return f"{self.id} | {self.amount} UAH | {self.get_status_display()}"
