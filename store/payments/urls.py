from django.urls import path

from . import views

app_name = "payments"

urlpatterns = [
    path("pay/<uuid:payment_id>/", views.pay, name="pay"),
    path("result/<uuid:payment_id>/", views.result, name="result"),
    path("callback/", views.callback, name="callback"),
    path("test/", views.create_test, name="test"),
]
