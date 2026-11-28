import re
import uuid
from decimal import Decimal
from io import BytesIO

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.db import models
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver
from django.urls import reverse
from PIL import Image, ImageOps

MAX_UPLOAD_MB = 10
MAX_SIDE_PX = 1200
WEBP_QUALITY = 82


def product_image_storage():
    """Фото товарів лежать у static/images/ і віддаються за адресою /static/images/..."""
    return FileSystemStorage(
        location=settings.PRODUCT_IMAGES_DIR,
        base_url=settings.STATIC_URL + "images/",
    )


def validate_image_size(f):
    if f.size > MAX_UPLOAD_MB * 1024 * 1024:
        raise ValidationError(f"Файл завеликий (максимум {MAX_UPLOAD_MB} МБ).")


def to_webp(uploaded, base_name):
    """Конвертує завантажене зображення у WebP, зменшуючи до MAX_SIDE_PX."""
    uploaded.seek(0)
    img = ImageOps.exif_transpose(Image.open(uploaded))
    img.thumbnail((MAX_SIDE_PX, MAX_SIDE_PX))
    has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
    img = img.convert("RGBA" if has_alpha else "RGB")
    buf = BytesIO()
    img.save(buf, format="WEBP", quality=WEBP_QUALITY, method=6)
    return ContentFile(buf.getvalue(), name=f"{base_name}-{uuid.uuid4().hex[:8]}.webp")


class Category(models.Model):
    name = models.CharField("Назва", max_length=120)
    slug = models.SlugField("Slug", unique=True)
    emoji = models.CharField("Емодзі", max_length=8, blank=True)
    parent = models.ForeignKey(
        "self", verbose_name="Батьківська категорія", null=True, blank=True,
        on_delete=models.CASCADE, related_name="children",
    )
    order = models.PositiveSmallIntegerField("Порядок", default=0)
    in_menu = models.BooleanField("Показувати в меню", default=True)

    class Meta:
        ordering = ["order", "name"]
        verbose_name = "Категорія"
        verbose_name_plural = "Категорії"

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("catalog_category", args=[self.slug])

    def with_children_ids(self):
        return [self.pk, *self.children.values_list("pk", flat=True)]


class Product(models.Model):
    category = models.ForeignKey(
        Category, verbose_name="Категорія", on_delete=models.PROTECT, related_name="products"
    )
    name = models.CharField("Назва", max_length=200)
    slug = models.SlugField("Slug", unique=True)
    emoji = models.CharField("Емодзі", max_length=8, blank=True, default="🛍️")
    color = models.CharField("Колір фону картки", max_length=7, default="#e4e6ee")
    image = models.ImageField(
        "Фото", storage=product_image_storage, blank=True, validators=[validate_image_size],
        help_text="JPG, PNG, GIF або WebP до 10 МБ. Автоматично зберігається як WebP у static/images/.",
    )
    description = models.TextField("Опис", blank=True)
    price = models.DecimalField("Ціна", max_digits=10, decimal_places=2)
    old_price = models.DecimalField("Стара ціна", max_digits=10, decimal_places=2, null=True, blank=True)
    reviews_count = models.PositiveIntegerField("Кількість відгуків", default=0)
    is_active = models.BooleanField("Активний", default=True)
    is_hit = models.BooleanField("Хіт продажу", default=False)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created", "name"]
        verbose_name = "Товар"
        verbose_name_plural = "Товари"

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("product_detail", args=[self.slug])

    @property
    def active_variants(self):
        """Активні варіанти (розмір/колір). Використовує prefetch_related("variants"), якщо він є."""
        return [v for v in self.variants.all() if v.is_active]

    @property
    def has_variants(self):
        return bool(self.active_variants)

    @property
    def in_stock(self):
        """Товар без варіантів вважається завжди доступним; з варіантами — якщо хоч один є в наявності."""
        variants = self.active_variants
        return not variants or any(v.stock > 0 for v in variants)

    def save(self, *args, **kwargs):
        old_name = None
        if self.pk:
            old_name = Product.objects.filter(pk=self.pk).values_list("image", flat=True).first()
        if self.image and not self.image._committed:  # завантажено новий файл
            webp = to_webp(self.image.file, self.slug)
            self.image.save(webp.name, webp, save=False)
        super().save(*args, **kwargs)
        if old_name and old_name != (self.image.name or ""):
            self.image.storage.delete(old_name)  # прибираємо старе фото

    def delete(self, *args, **kwargs):
        photos = list(self.photos.all())
        image = self.image
        super().delete(*args, **kwargs)
        for photo in photos:
            if photo.image:
                photo.image.storage.delete(photo.image.name)
        if image:
            image.storage.delete(image.name)

    @property
    def discount_percent(self):
        if self.old_price and self.old_price > self.price:
            return round((1 - self.price / self.old_price) * 100)
        return 0


class ProductImage(models.Model):
    product = models.ForeignKey(
        Product, verbose_name="Товар", on_delete=models.CASCADE, related_name="photos"
    )
    image = models.ImageField(
        "Фото", storage=product_image_storage, validators=[validate_image_size],
        help_text="JPG, PNG, GIF або WebP до 10 МБ. Автоматично зберігається як WebP у static/images/.",
    )
    order = models.PositiveSmallIntegerField("Порядок", default=0)
    color = models.CharField(
        "Колір", max_length=60, blank=True,
        help_text="Порожньо — фото для всіх (показується одразу). Якщо вибрати колір, фото з'явиться лише коли покупець обере цей колір.",
    )

    class Meta:
        ordering = ["order", "pk"]
        verbose_name = "Фото товару"
        verbose_name_plural = "Фото товару"

    def __str__(self):
        return f"{self.product.name} — фото {self.order + 1}"

    def save(self, *args, **kwargs):
        old_name = None
        if self.pk:
            old_name = ProductImage.objects.filter(pk=self.pk).values_list("image", flat=True).first()
        if self.image and not self.image._committed:
            webp = to_webp(self.image.file, self.product.slug)
            self.image.save(webp.name, webp, save=False)
        super().save(*args, **kwargs)
        if old_name and old_name != (self.image.name or ""):
            self.image.storage.delete(old_name)

    def delete(self, *args, **kwargs):
        image = self.image
        super().delete(*args, **kwargs)
        if image:
            image.storage.delete(image.name)


class Review(models.Model):
    RATING_CHOICES = [(value, str(value)) for value in range(1, 6)]

    product = models.ForeignKey(
        Product, verbose_name="Товар", on_delete=models.CASCADE, related_name="reviews"
    )
    name = models.CharField("Ім'я", max_length=120)
    rating = models.PositiveSmallIntegerField("Оцінка", choices=RATING_CHOICES)
    text = models.TextField("Відгук")
    is_published = models.BooleanField("Опублікований", default=True)
    created = models.DateTimeField("Створено", auto_now_add=True)

    class Meta:
        ordering = ["-created", "pk"]
        verbose_name = "Відгук"
        verbose_name_plural = "Відгуки"

    def __str__(self):
        return f"{self.product.name} — {self.name} ({self.rating}/5)"

    @property
    def stars(self):
        return "★" * self.rating + "☆" * (5 - self.rating)

    @property
    def approved_photo_list(self):
        return getattr(self, "approved_photos", None) or []

    @staticmethod
    def _sync_product_count(product_id):
        count = Review.objects.filter(product_id=product_id, is_published=True).count()
        Product.objects.filter(pk=product_id).update(reviews_count=count)

    def save(self, *args, **kwargs):
        old_product_id = None
        if self.pk:
            old_product_id = Review.objects.filter(pk=self.pk).values_list("product_id", flat=True).first()
        super().save(*args, **kwargs)
        if old_product_id and old_product_id != self.product_id:
            self._sync_product_count(old_product_id)
        self._sync_product_count(self.product_id)

    def delete(self, *args, **kwargs):
        product_id = self.product_id
        super().delete(*args, **kwargs)
        self._sync_product_count(product_id)

MAX_REVIEW_PHOTOS = 4


class ReviewPhoto(models.Model):
    review = models.ForeignKey(Review, verbose_name="Відгук", on_delete=models.CASCADE, related_name="photos")
    image = models.ImageField(
        "Фото", storage=product_image_storage, validators=[validate_image_size],
        help_text="Автоматично зберігається як WebP.",
    )
    is_approved = models.BooleanField(
        "Показувати", default=False,
        help_text="Фото з'являється на сайті лише після того, як ви поставите цю позначку.",
    )
    order = models.PositiveSmallIntegerField("Порядок", default=0)
    created = models.DateTimeField("Додано", auto_now_add=True)

    class Meta:
        ordering = ["order", "pk"]
        verbose_name = "Фото до відгуку"
        verbose_name_plural = "Фото до відгуків"

    def __str__(self):
        return f"Фото до відгуку #{self.review_id}"

    def save(self, *args, **kwargs):
        if self.image and not self.image._committed:
            webp = to_webp(self.image.file, f"review-{self.review.product.slug}")
            self.image.save(webp.name, webp, save=False)
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        image = self.image
        super().delete(*args, **kwargs)
        if image:
            image.storage.delete(image.name)


@receiver(post_delete, sender=ReviewPhoto)
def _drop_review_photo_file(sender, instance, **kwargs):
    # файл прибирається і тоді, коли фото видаляється каскадом (разом із відгуком чи товаром)
    if instance.image:
        instance.image.storage.delete(instance.image.name)



class Order(models.Model):
    class Status(models.TextChoices):
        NEW = "new", "Нове"
        CONFIRMED = "confirmed", "Підтверджене"
        SHIPPED = "shipped", "Відправлене"
        DONE = "done", "Виконане"
        CANCELED = "canceled", "Скасоване"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="Покупець", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="orders",
    )
    name = models.CharField("Ім'я", max_length=120)
    phone = models.CharField("Телефон", max_length=32)
    email = models.EmailField("Email", blank=True)
    city = models.CharField("Місто", max_length=120)
    address = models.CharField("Адреса / відділення", max_length=255)
    comment = models.TextField("Коментар", blank=True)
    status = models.CharField("Статус", max_length=12, choices=Status.choices, default=Status.NEW)
    total = models.DecimalField("Сума", max_digits=12, decimal_places=2, default=Decimal("0"))
    created = models.DateTimeField("Створено", auto_now_add=True)

    class Meta:
        ordering = ["-created"]
        verbose_name = "Замовлення"
        verbose_name_plural = "Замовлення"

    def __str__(self):
        return f"Замовлення №{self.pk} — {self.name}"

    def save(self, *args, **kwargs):
        prev = Order.objects.filter(pk=self.pk).values_list("status", flat=True).first() if self.pk else None
        super().save(*args, **kwargs)
        if prev and prev != self.Status.CANCELED and self.status == self.Status.CANCELED:
            # скасування замовлення повертає товар на склад
            for item in self.items.exclude(variant__isnull=True):
                ProductVariant.objects.filter(pk=item.variant_id).update(stock=models.F("stock") + item.quantity)


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.SET_NULL, null=True)
    variant = models.ForeignKey("ProductVariant", on_delete=models.SET_NULL, null=True, blank=True)
    variant_label = models.CharField(max_length=110, blank=True)
    product_name = models.CharField(max_length=200)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    quantity = models.PositiveIntegerField(default=1)

    class Meta:
        verbose_name = "Позиція замовлення"
        verbose_name_plural = "Позиції замовлення"

    @property
    def subtotal(self):
        return self.price * self.quantity

    @property
    def full_name(self):
        return f"{self.product_name} ({self.variant_label})" if self.variant_label else self.product_name


class Profile(models.Model):
    """Додаткові дані покупця: телефон і адреса доставки за замовчуванням."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, verbose_name="Користувач", on_delete=models.CASCADE, related_name="profile"
    )
    phone = models.CharField("Телефон", max_length=32, blank=True)
    city = models.CharField("Місто", max_length=120, blank=True)
    address = models.CharField("Адреса / відділення", max_length=255, blank=True)

    class Meta:
        verbose_name = "Профіль покупця"
        verbose_name_plural = "Профілі покупців"

    def __str__(self):
        return f"Профіль: {self.user}"


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_profile(sender, instance, created, **kwargs):
    if created:
        Profile.objects.get_or_create(user=instance)


def get_profile(user):
    """Профіль користувача; для акаунтів, створених до появи профілів, створюється на льоту."""
    profile, _ = Profile.objects.get_or_create(user=user)
    return profile


class ProductVariant(models.Model):
    """Варіант товару: розмір і/або колір з власним залишком (і, за потреби, власною ціною)."""

    product = models.ForeignKey(Product, verbose_name="Товар", on_delete=models.CASCADE, related_name="variants")
    size = models.CharField("Розмір", max_length=40, blank=True, help_text="Напр. 42, M, 160×200. Можна залишити порожнім.")
    color_name = models.CharField("Колір", max_length=60, blank=True, help_text="Напр. Чорний. Можна залишити порожнім.")
    color_hex = models.CharField(
        "Колір кружечка", max_length=7, blank=True,
        help_text="Можна не заповнювати: для звичайних кольорів (чорний, білий, синій…) він підставиться сам за назвою. "
                  "Для рідкісного кольору виберіть його мишкою у віконці поруч.",
    )
    stock = models.PositiveIntegerField("Залишок, шт.", default=0)
    price = models.DecimalField(
        "Ціна варіанту", max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="Залиште порожнім, щоб діяла ціна товару.",
    )
    is_active = models.BooleanField("Активний", default=True)
    order = models.PositiveIntegerField("Порядок", default=0)

    class Meta:
        ordering = ["order", "id"]
        verbose_name = "Варіант товару"
        verbose_name_plural = "Варіанти товару (розмір / колір)"
        constraints = [
            models.UniqueConstraint(fields=["product", "size", "color_name"], name="uniq_variant_per_product"),
        ]

    def __str__(self):
        return self.label or "Без назви"

    @property
    def label(self):
        return " / ".join(x for x in (self.size, self.color_name) if x)

    @property
    def effective_price(self):
        return self.price if self.price is not None else self.product.price

    def save(self, *args, **kwargs):
        if not self.color_hex and self.color_name:
            from .colors import hex_for_name
            self.color_hex = hex_for_name(self.color_name)
        super().save(*args, **kwargs)

    def clean(self):
        if not self.size.strip() and not self.color_name.strip():
            raise ValidationError("Вкажіть розмір або колір.")
        if self.color_hex and not re.fullmatch(r"#[0-9a-fA-F]{6}", self.color_hex):
            raise ValidationError({"color_hex": "Код кольору у форматі #RRGGBB."})


class ChatThread(models.Model):
    """Розмова покупця з продавцем: одна на акаунт або на гостьову сесію."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="Покупець", null=True, blank=True,
        on_delete=models.CASCADE, related_name="chat_threads",
    )
    created = models.DateTimeField("Створено", auto_now_add=True)
    updated = models.DateTimeField("Остання активність", auto_now=True)

    class Meta:
        ordering = ["-updated"]
        verbose_name = "Чат"
        verbose_name_plural = "Чати"

    def __str__(self):
        return f"Чат #{self.pk}: {self.customer_name}"

    @property
    def customer_name(self):
        if self.user_id:
            return self.user.first_name or self.user.email or self.user.username
        return f"Гість #{self.pk}"


class ChatMessage(models.Model):
    thread = models.ForeignKey(ChatThread, on_delete=models.CASCADE, related_name="messages")
    seller = models.BooleanField("Від продавця", default=False)
    text = models.TextField("Текст", max_length=1000)
    product = models.ForeignKey(
        Product, verbose_name="Товар", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    read = models.BooleanField("Прочитано", default=False)  # прочитано протилежною стороною
    created = models.DateTimeField("Надіслано", auto_now_add=True)

    class Meta:
        ordering = ["id"]
        verbose_name = "Повідомлення чату"
        verbose_name_plural = "Повідомлення чату"
        indexes = [models.Index(fields=["thread", "seller", "read"])]

    def __str__(self):
        return f"{'Продавець' if self.seller else 'Покупець'}: {self.text[:40]}"


class Favorite(models.Model):
    """Товар в обраному покупця (для гостей обране тримається в сесії)."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="favorites")
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="favorited_by")
    created = models.DateTimeField("Додано", auto_now_add=True)

    class Meta:
        ordering = ["-created", "-id"]
        verbose_name = "Обраний товар"
        verbose_name_plural = "Обране"
        constraints = [models.UniqueConstraint(fields=["user", "product"], name="uniq_favorite_per_user")]

    def __str__(self):
        return f"{self.user} ♥ {self.product}"

class Order(models.Model):
    STATUS = [("new", "Нове"), ("paid", "Оплачено"), ("failed", "Помилка")]
    total = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=10, choices=STATUS, default="new")
    created = models.DateTimeField(auto_now_add=True)