from django.contrib import messages
from django.db import transaction
from django.core.paginator import Paginator
from django.db.models import Avg, Count, F, Prefetch, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from . import novaposhta
from .cart import Cart
from .forms import CheckoutForm, ReviewForm
from . import recent as recent_views
from .search import search_products
from .services import OutOfStock, place_order, remember_delivery
from .models import Category, Order, OrderItem, Product, ProductVariant, ReviewPhoto, get_profile

def index(request):
    hits = Product.objects.filter(is_active=True, is_hit=True).select_related("category").prefetch_related("variants")
    sub_ids = hits.values_list("category_id", flat=True)
    tabs = Category.objects.filter(pk__in=sub_ids)
    tab = request.GET.get("tab")
    if tab:
        hits = hits.filter(category__slug=tab)
    return render(request, "index.html", {
        "hits": hits[:8],
        "tabs": tabs,
        "active_tab": tab,
        "tiles": Category.objects.filter(parent__isnull=True),
        "recent": recent_views.recent_products(request),
    })


def catalog(request, slug=None):
    products = Product.objects.filter(is_active=True).select_related("category").prefetch_related("variants")
    category = None
    title = "Усі товари"
    if slug:
        category = get_object_or_404(Category, slug=slug)
        products = products.filter(category_id__in=category.with_children_ids())
        title = category.name
    q = request.GET.get("q", "").strip()
    if q:
        products = search_products(products, q)
        title = f"Пошук: {q}"
    if request.GET.get("sale"):
        products = products.filter(old_price__gt=F("price"))
        title = "SALE"
    sort = request.GET.get("sort", "")
    products = {
        "price": products.order_by("price"),
        "-price": products.order_by("-price"),
    }.get(sort, products)
    page = Paginator(products, PAGE_SIZE).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    return render(request, "catalog.html", {
        "category": category,
        "title": title,
        "page": page,
        "q": q,
        "sort": sort,
        "querystring": params.urlencode(),
        "subcategories": category.children.all() if category else Category.objects.filter(parent__isnull=True),
    })


def product_detail(request, slug):
    product = get_object_or_404(Product.objects.select_related("category").prefetch_related("variants"), slug=slug, is_active=True)
    related = Product.objects.filter(category=product.category, is_active=True).exclude(pk=product.pk).prefetch_related("variants")[:4]
    recent = recent_views.recent_products(request, exclude=product.pk)
    recent_views.remember(request, product)
    initial = {"name": request.user.first_name} if request.user.is_authenticated else None
    form = ReviewForm(request.POST if request.method == "POST" else None,
                      request.FILES if request.method == "POST" else None, initial=initial)
    just_reviewed = False
    if request.method == "POST" and form.is_valid():
        if form.cleaned_data["website"]:
            return redirect(f"{product.get_absolute_url()}#reviews")
        review = form.save(commit=False)
        review.product = product
        files = form.cleaned_data["photos"]
        created = []
        try:
            with transaction.atomic():
                review.save()
                for n, f in enumerate(files):
                    created.append(ReviewPhoto.objects.create(review=review, image=f, order=n))
        except Exception:  # напр. пошкоджений файл: нічого не лишаємо, ні відгуку, ні файлів
            for ph in created:
                ph.image.storage.delete(ph.image.name)
            form.add_error("photos", "Не вдалося обробити фото. Спробуйте інший файл.")
        else:
            return redirect(f"{product.get_absolute_url()}?reviewed={'photos' if files else '1'}#reviews")

    photos = [p for p in product.photos.all() if p.image]
    # нейтральні фото показуються одразу; фото кольору — лише коли покупець обирає цей колір
    gallery = ([product.image.url] if product.image else []) + [p.image.url for p in photos if not p.color]
    gallery_colors = {}
    for name in dict.fromkeys(v.color_name for v in product.active_variants if v.color_name):
        urls = [p.image.url for p in photos if p.color.lower() == name.lower()]
        if urls:
            gallery_colors[name] = urls
    reviews = product.reviews.filter(is_published=True).prefetch_related(
        Prefetch("photos", queryset=ReviewPhoto.objects.filter(is_approved=True), to_attr="approved_photos"))
    review_stats = reviews.aggregate(total=Count("pk"), average=Avg("rating"))
    variants_data = [{
        "id": v.pk, "size": v.size, "color": v.color_name, "hex": v.color_hex, "stock": v.stock,
        "price": float(v.effective_price),
    } for v in product.active_variants]
    return render(request, "product_detail.html", {
        "product": product,
        "variants_data": variants_data,
        "related": related,
        "gallery": gallery,
        "gallery_colors": gallery_colors,
        "recent": recent,
        "has_photos": bool(gallery or gallery_colors),
        "reviews": reviews,
        "reviews_total": review_stats["total"],
        "rating_avg": review_stats["average"],
        "form": form,
        "just_reviewed": request.GET.get("reviewed") in ("1", "photos"),
        "reviewed_photos": request.GET.get("reviewed") == "photos",
    })


def _back(request, fallback="cart"):
    nxt = request.POST.get("next", "")
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        return redirect(nxt)
    return redirect(fallback)


@require_POST
def recent_clear(request):
    recent_views.clear(request)
    return _back(request, fallback="index")


def _is_ajax(request):
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _drawer_response(request):
    """Вміст висувного кошика + кількість товарів для бейджа в шапці."""
    cart = Cart(request)
    items = list(cart.items())  # спершу items(): він прибирає неактивні товари
    html = render_to_string(
        "partials/_cart_drawer_body.html",
        {"items": items, "total": sum(i["subtotal"] for i in items)},
        request=request,
    )
    return JsonResponse({"count": len(cart), "html": html})


def cart_detail(request):
    items = list(Cart(request).items())
    return render(request, "cart.html", {"items": items, "total": sum(i["subtotal"] for i in items)})


def cart_drawer(request):
    return _drawer_response(request)


def _cart_add_error(request, product, text):
    """Помилка додавання: AJAX отримує 400 (і сторінка сама перейде на звичайну відправку форми)."""
    if _is_ajax(request):
        return JsonResponse({"error": text}, status=400)
    messages.error(request, text)
    return redirect(product.get_absolute_url())


@require_POST
def cart_add(request, pk):
    product = get_object_or_404(Product, pk=pk, is_active=True)
    try:
        qty = max(1, int(request.POST.get("quantity", 1)))
    except ValueError:
        qty = 1
    variant = None
    options = product.active_variants
    if options:  # для товару з варіантами обов'язково обрати розмір/колір
        wanted = request.POST.get("variant", "")
        variant = next((v for v in options if str(v.pk) == wanted), None)
        if variant is None:
            return _cart_add_error(request, product, "Оберіть розмір і колір, перш ніж додати товар до кошика.")
        if variant.stock <= 0:
            return _cart_add_error(request, product, "Цього варіанту немає в наявності.")
    in_cart = Cart(request).add(product, qty, variant)
    if _is_ajax(request):
        return _drawer_response(request)
    name = f"{product.name} ({variant.label})" if variant else product.name
    if variant and in_cart >= variant.stock:
        messages.warning(request, f"«{name}» у кошику: {in_cart} шт. — це весь залишок.")
    else:
        messages.success(request, f"«{name}» додано до кошика")
    return _back(request)


@require_POST
def cart_update(request, key):
    try:
        qty = int(request.POST.get("quantity", 1))
    except ValueError:
        qty = 1
    Cart(request).set(key, qty)
    if _is_ajax(request):
        return _drawer_response(request)
    return redirect("cart")


@require_POST
def cart_remove(request, key):
    Cart(request).remove(key)
    if _is_ajax(request):
        return _drawer_response(request)
    return redirect("cart")


def _checkout_initial(user):
    """Дані покупця для автозаповнення форми оформлення."""
    profile = get_profile(user)
    return {
        "name": user.get_full_name() or user.first_name, "phone": profile.phone, "email": user.email,
        "city": profile.city, "address": profile.address,
    }
    """Запам'ятовує телефон і адресу в профілі, якщо там ці поля ще порожні."""
    profile = get_profile(user)
    changed = []
    for field in ("phone", "city", "address"):
        if not getattr(profile, field) and getattr(order, field):
            setattr(profile, field, getattr(order, field))
            changed.append(field)
    if changed:
        profile.save(update_fields=changed)


def checkout(request):
    cart = Cart(request)
    items = list(cart.items())
    if not items:
        messages.info(request, "Кошик порожній")
        return redirect("catalog")
    initial = _checkout_initial(request.user) if request.user.is_authenticated else None
    form = CheckoutForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            order = place_order(form.save(commit=False), items, request.user)
        except OutOfStock as e:
            messages.error(request, f"{e} Перегляньте кошик.")
            return redirect("cart")
        if request.user.is_authenticated:
            remember_delivery(request.user, order)
        cart.clear()
        request.session["last_order"] = order.pk
        return redirect("order_success", pk=order.pk)


def order_success(request, pk):
    # показуємо лише власне замовлення з поточної сесії
    if request.session.get("last_order") != pk:
        return redirect("index")
    order = get_object_or_404(Order, pk=pk)
    return render(request, "order_success.html", {"order": order})
