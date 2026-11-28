from django.dispatch import Signal

# Відправляється один раз, коли платіж став "Оплачено".
# Підключіть свій обробник, щоб позначити замовлення оплаченим (див. README).
# Аргументи: sender=Payment, payment=<Payment>
payment_paid = Signal()
