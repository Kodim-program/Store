from django.contrib import admin

from .models import Payment


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("id", "order_ref", "amount", "status", "liqpay_status", "created")
    list_filter = ("status",)
    search_fields = ("id", "order_ref")
    readonly_fields = ("id", "created", "updated")
