import json
import logging
import os
import tempfile
import threading
import traceback
import uuid
from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db.models import Count, DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date

from .core import AIUnavailableError, get_provider, refresh_costs, refreshable_costs
from .forms import AIProviderForm, AIProviderTagFormSet
from .models import AICallLog, AIProvider, AIProviderTag, AITask, ProxySettings

logger = logging.getLogger(__name__)

# Единственный слой фоновых AI-задач для всех приложений проекта: любой фоновый
# flow идёт через run_async + task_poll, своих тредов и поллингов в приложениях нет.
# Формат задачи, контракт с фронтом и политика ошибок — aicore/README.md.

_TASK_DIR = os.path.join(tempfile.gettempdir(), "aicore_session_tasks")

# Дедуп: атомарный «занято/свободно» на ключ задачи. cache.add ставит значение только
# если ключа не было — второй одновременный запуск с тем же ключом проиграет гонку и
# заберёт task_id победителя. TTL — страховка на случай, если процесс умрёт, не сняв
# ключ в finally: чуть больше типичной массовой задачи, иначе ключ завис бы навсегда.
_DEDUP_TTL = 900


def _dedup_cache_key(dedup_key):
    return f"aicore_dedup:{dedup_key}"


def _task_path(task_id):
    os.makedirs(_TASK_DIR, exist_ok=True)
    return os.path.join(_TASK_DIR, f"{task_id}.json")


def _write_task(task_id, data):
    path = _task_path(task_id)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def read_task(task_id):
    path = _task_path(task_id)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def run_async(task_func, poll_url_name="aicore:task_poll", dedup_key=None):
    """Запускает task_func в фоновом треде, возвращает (task_id, poll_url).

    dedup_key задан → защита от повторного запуска той же задачи (двойной клик, два
    параллельных POST): если задача с этим ключом уже идёт, второй запуск НЕ создаётся —
    возвращается (task_id, poll_url) уже идущей задачи, и второй клиент поллит её же.
    Ключ снимается по завершении задачи (в finally), так что следующий честный запуск
    после её конца снова стартует новую.
    """
    task_id = str(uuid.uuid4())

    ck = _dedup_cache_key(dedup_key) if dedup_key else None
    if ck is not None and not cache.add(ck, task_id, timeout=_DEDUP_TTL):
        # Гонку за ключ проиграли — задача уже идёт. Отдаём её id, а не заводим вторую.
        existing_id = cache.get(ck)
        if existing_id:
            return existing_id, reverse(poll_url_name, args=[existing_id])
        # Ключ истёк между add и get (задача только что закончилась) — забираем его себе.
        cache.set(ck, task_id, timeout=_DEDUP_TTL)

    _write_task(task_id, {"status": "running"})

    def run():
        from django.db import close_old_connections
        close_old_connections()
        try:
            _write_task(task_id, {"status": "done", "result": task_func()})
        except AIUnavailableError as e:
            # Своя ошибка слоя: сообщение уже написано для человека, тип к нему не клеим.
            # usage несёт исключение — синхронный вызывающий его видит, и фоновый должен
            # тоже: при падении «что ушло и чем обслужено» нужнее всего, а иначе оно
            # умирает вместе с тредом.
            _write_task(task_id, {"status": "error", "error": str(e),
                                  "raw_response": getattr(e, "raw", ""),
                                  "usage": getattr(e, "usage", {}),
                                  "traceback": traceback.format_exc()})
        except Exception as e:
            # Неожиданное исключение: без типа и traceback по тексту вроде
            # «Field 'id' expected a number» не понять ни где упало, ни над чем.
            logger.exception("async task %s failed", task_id)
            _write_task(task_id, {"status": "error", "error": f"{type(e).__name__}: {e}",
                                  "raw_response": getattr(e, "raw", ""),
                                  "usage": getattr(e, "usage", {}),
                                  "traceback": traceback.format_exc()})
        finally:
            close_old_connections()
            if ck is not None:
                cache.delete(ck)

    threading.Thread(target=run, daemon=True).start()
    return task_id, reverse(poll_url_name, args=[task_id])


@login_required
def task_poll(request, task_id):
    """Generic poll для любого фонового run_async задания."""
    task = read_task(task_id)
    if task is None:
        return JsonResponse({"status": "unknown"}, status=404)
    return JsonResponse(task)


@login_required
def providers(request):
    # По умолчанию группируем по роли: роль — то, чем провайдер отличается от соседа
    # по назначению, а внутри роли порядок остаётся (priority, id) — тот самый, которым
    # выбирает get_provider. Сортируем в Python: ключ роли — позиция в ROLE_CHOICES,
    # а не алфавит («Гений» не должен оказываться между «Дешёвой» и «Умной»).
    role_order = {value: i for i, (value, _) in enumerate(AIProvider.ROLE_CHOICES)}
    sort_keys = {
        "role": lambda p: (role_order.get(p.role, 99), p.priority, p.pk),
        "priority": lambda p: (p.priority, p.pk),
        "status": lambda p: (not p.is_active, p.priority, p.pk),
        "dialect": lambda p: (p.dialect, p.priority, p.pk),
        "model": lambda p: (p.model.lower(), p.priority, p.pk),
        "tags": lambda p: (sorted(t.name.lower() for t in p.tags.all()) or [""], p.priority, p.pk),
    }
    sort = request.GET.get("sort") or "role"
    if sort not in sort_keys:
        sort = "role"
    desc = request.GET.get("dir") == "desc"

    items = sorted(AIProvider.objects.prefetch_related("tags"), key=sort_keys[sort], reverse=desc)

    # Кого реально вернёт выбор по роли — спрашиваем сам get_provider, а не пересказываем
    # его логику в шаблоне. Так в списке видно и тай-брейк по приоритету, и перехват
    # тегом с именем роли, и фолбэк ролей на smart — ровно то, что произойдёт в рантайме.
    default_for = {}
    for value, label in AIProvider.ROLE_CHOICES:
        try:
            winner = get_provider(value)
        except AIUnavailableError:
            continue
        default_for.setdefault(winner.pk, []).append(label)
    for p in items:
        p.default_roles = default_for.get(p.pk, [])

    return render(request, "aicore/providers.html", {
        "providers": items,
        "sort": sort,
        "dir": "desc" if desc else "asc",
        "next_dir": "asc" if desc else "desc",
    })


@login_required
def tasks(request):
    known_tags = sorted(AIProviderTag.objects.filter(provider__is_active=True)
                        .values_list("name", flat=True).distinct())
    return render(request, "aicore/tasks.html", {
        "tasks": AITask.objects.all(),
        "known_tags": known_tags,
        "roles": AIProvider.ROLE_CHOICES,
    })


@login_required
def task_save(request, pk):
    task = get_object_or_404(AITask, pk=pk)
    if request.method != "POST":
        return redirect("aicore:tasks")

    task.tag = (request.POST.get("tag") or "").strip()
    task.role = (request.POST.get("role") or "").strip()
    temp = (request.POST.get("temperature") or "").strip()
    if temp == "":
        task.temperature = None
    else:
        try:
            task.temperature = float(temp.replace(",", "."))
        except ValueError:
            messages.error(request, f"«{temp}» — не число. Temperature не изменена.")
            return redirect("aicore:tasks")
    task.save(update_fields=["tag", "role", "temperature"])
    messages.success(request, f"Задача «{task}» сохранена.")
    return redirect("aicore:tasks")


@login_required
def task_delete(request, pk):
    task = get_object_or_404(AITask, pk=pk)
    if request.method == "POST":
        # Задача зарегистрируется заново при следующем вызове — с дефолтами из кода.
        task.delete()
        messages.success(request, "Задача удалена, настройки сброшены к значениям из кода.")
    return redirect("aicore:tasks")


@login_required
def provider_add(request):
    form = AIProviderForm(request.POST or None,
                          initial={"priority": AIProvider.next_free_priority()})
    tag_formset = AIProviderTagFormSet(request.POST or None, instance=AIProvider())
    if form.is_valid() and tag_formset.is_valid():
        provider = form.save()
        tag_formset.instance = provider
        tag_formset.save()
        messages.success(request, "Провайдер добавлен.")
        return redirect("aicore:providers")
    return render(request, "aicore/provider_form.html", {
        "form": form, "tag_formset": tag_formset, "title": "Новый провайдер",
    })


@login_required
def provider_edit(request, pk):
    provider = get_object_or_404(AIProvider, pk=pk)
    form = AIProviderForm(request.POST or None, instance=provider)
    tag_formset = AIProviderTagFormSet(request.POST or None, instance=provider)
    if form.is_valid() and tag_formset.is_valid():
        form.save()
        tag_formset.save()
        messages.success(request, "Провайдер сохранён.")
        return redirect("aicore:providers")
    return render(request, "aicore/provider_form.html", {
        "form": form, "tag_formset": tag_formset, "title": "Редактировать провайдер", "provider": provider,
    })


@login_required
def provider_copy(request, pk):
    src = get_object_or_404(AIProvider, pk=pk)
    tags = list(src.tags.values_list("name", flat=True))
    src.pk = None
    src.is_active = False
    # Приоритет уникален — копия не может унаследовать чужой; уводим в конец очереди.
    src.priority = AIProvider.next_free_priority()
    src.save()
    for name in tags:
        src.tags.create(name=name)
    messages.success(request, f"Провайдер скопирован (приоритет {src.priority}) — отредактируйте копию.")
    return redirect("aicore:provider_edit", pk=src.pk)


@login_required
def provider_delete(request, pk):
    provider = get_object_or_404(AIProvider, pk=pk)
    if request.method == "POST":
        provider.delete()
        messages.success(request, "Провайдер удалён.")
    return redirect("aicore:providers")


# Значение фильтра «приложение», означающее пустое поле app. Пустая строка занята
# смыслом «все», поэтому нужен отдельный символ: иначе «строки без приложения» с экрана
# не выбрать, а именно на них сходится проверка «сумма по приложениям = общей сумме».
APP_NONE = "-"


def _spend(qs, *group_by):
    """Расход по срезу: вызовов, токенов, $ — одной формулой на все сводки.

    Разрезов два (приложения и задачи), а считаются они одинаково; разведи их по двум
    местам — и однажды в одной колонке окажется цена без reasoning-токенов, а в
    соседней с ними.
    """
    return list(
        qs.values(*group_by)
          .annotate(calls=Count("id"),
                    priced=Count("cost"),
                    # Строки без цены названы отдельным числом, а не выведены вычитанием
                    # в шаблоне: сумма по срезу с ними не сходится, и молчать об этом
                    # нельзя — «$0.4» при десяти неоценённых строках вводит в заблуждение.
                    unpriced=Count("id") - Count("cost"),
                    cost_total=Coalesce(Sum("cost"), Value(Decimal("0"),
                                        output_field=DecimalField(max_digits=12, decimal_places=8))),
                    tokens=Coalesce(Sum("prompt_tokens"), Value(0))
                           + Coalesce(Sum("completion_tokens"), Value(0)))
          .order_by("-cost_total", "-calls")
    )


def _calls_period(f):
    """Границы периода из GET → (qs-фильтр, сообщения об ошибках разбора).

    Дата, а не «сегодня/7/30» отдельным параметром: период в ссылке должен читаться
    глазами и не меняться назавтра. Быстрые кнопки в шапке — те же ссылки с проставленными
    датами, отдельного механизма под них нет.
    """
    lookups, errors = {}, []
    for key, lookup in (("from", "created_at__date__gte"), ("to", "created_at__date__lte")):
        if not f[key]:
            continue
        parsed = parse_date(f[key])
        if parsed is None:
            errors.append(f"«{f[key]}» — не дата (ждём ГГГГ-ММ-ДД). Период по «{key}» не применён.")
            continue
        lookups[lookup] = parsed
    return lookups, errors


@login_required
def calls(request):
    """Журнал вызовов AI. Фильтры — GET-параметрами, чтобы отфильтрованный экран можно
    было дать ссылкой, а не пересказом «выбери там-то то-то»."""
    qs = AICallLog.objects.select_related("task", "provider")

    f = {k: (request.GET.get(k) or "").strip()
         for k in ("q", "ok", "kind", "error_kind", "model", "upstream", "task", "app",
                   "http", "from", "to")}
    if f["q"]:
        qs = qs.filter(Q(caller__icontains=f["q"]) | Q(model__icontains=f["q"])
                       | Q(upstream__icontains=f["q"]) | Q(error__icontains=f["q"]))
    if f["ok"] in ("1", "0"):
        qs = qs.filter(ok=(f["ok"] == "1"))
    for field in ("kind", "error_kind", "model", "upstream"):
        if f[field]:
            qs = qs.filter(**{field: f[field]})
    if f["task"]:
        qs = qs.filter(task_id=f["task"])
    if f["http"]:
        if f["http"].isdigit():
            qs = qs.filter(http_status=int(f["http"]))
        else:
            # Молча показать всё — значит соврать: экран выглядел бы отфильтрованным.
            messages.error(request, f"«{f['http']}» — не HTTP-код. Фильтр по коду не применён.")

    period, period_errors = _calls_period(f)
    for err in period_errors:
        messages.error(request, err)
    qs = qs.filter(**period)

    # Сводка по приложениям считается ДО фильтра по приложению — иначе таблица «чьи это
    # вызовы» показывала бы одну строку, ту самую, которую и так выбрали. Все остальные
    # фильтры (период, исход, модель) в неё входят: сравнивать надо в одних условиях.
    by_app = _spend(qs, "app")

    if f["app"]:
        qs = qs.filter(app="" if f["app"] == APP_NONE else f["app"])

    # По задачам — уже внутри выбранного приложения: это разбор той суммы, которую видно
    # выше, а не второй независимый счёт.
    by_task = _spend(qs, "app", "task_id", "task__key", "task__name")

    # Сумма — по отфильтрованному, а не по странице: цена вопроса «сколько стоил этот
    # сценарий» и есть смысл фильтра. priced отдельно, потому что строки без цены в сумму
    # не входят и молча занижали бы её.
    # Алиас не «cost»: имя агрегата перекрывает одноимённое поле, и следующий за ним
    # Count("cost") считал бы уже сумму, а не строки с ценой (FieldError).
    totals = qs.aggregate(cost_total=Sum("cost"), priced=Count("cost"), calls=Count("id"))

    page = Paginator(qs, 100).get_page(request.GET.get("page"))

    params = request.GET.copy()
    params.pop("page", None)

    # Ссылки строк сводок: те же условия, но со своим разрезом — его из ссылки убираем.
    app_params = params.copy()
    app_params.pop("app", None)
    task_params = params.copy()
    task_params.pop("task", None)

    # Быстрые периоды — обычные ссылки с датами, сохраняющие прочие фильтры.
    # timezone.now().date(), а не localdate(): пакет обязан работать и при USE_TZ=False,
    # где localtime() на naive datetime падает всегда.
    today = timezone.now().date()
    period_params = request.GET.copy()
    for key in ("from", "to", "page"):
        period_params.pop(key, None)
    quick = []
    for label, start in (("сегодня", today), ("7 дней", today - timedelta(days=6)),
                         ("30 дней", today - timedelta(days=29))):
        p = period_params.copy()
        p["from"] = start.isoformat()
        quick.append({"label": label, "query": p.urlencode(),
                      "active": f["from"] == start.isoformat() and not f["to"]})

    no_cost = AICallLog.objects.filter(kind=AICallLog.KIND_CHAT, cost__isnull=True).count()
    refreshable = refreshable_costs().count()

    return render(request, "aicore/calls.html", {
        "page": page,
        "f": f,
        "totals": totals,
        "by_app": by_app,
        "by_task": by_task,
        "app_none": APP_NONE,
        "quick": quick,
        "period_reset": period_params.urlencode(),
        "query": params.urlencode(),
        "query_no_app": app_params.urlencode(),
        "query_no_task": task_params.urlencode(),
        "refreshable": refreshable,
        # Цену этих строк не добрать ничем: id генерации у них нет (записаны до того, как
        # слой начал его сохранять) либо провайдер уже не openrouter-овский.
        "stuck": no_cost - refreshable,
        "tasks": AITask.objects.all(),
        "apps": sorted(set(AICallLog.objects.exclude(app="")
                           .values_list("app", flat=True).distinct())),
        "models": sorted(set(AICallLog.objects.exclude(model="")
                             .values_list("model", flat=True).distinct())),
        "upstreams": sorted(set(AICallLog.objects.exclude(upstream="")
                                .values_list("upstream", flat=True).distinct())),
        "error_kinds": sorted(set(AICallLog.objects.exclude(error_kind="")
                                  .values_list("error_kind", flat=True).distinct())),
        "http_statuses": sorted(AICallLog.objects.filter(http_status__isnull=False)
                                .values_list("http_status", flat=True).distinct()),
        "kinds": AICallLog.KIND_CHOICES,
    })


@login_required
def calls_refresh_costs(request):
    """Добрать цену у OpenRouter по строкам без неё. Один HTTP-запрос на строку, поэтому
    за раз обрабатывается не больше потолка в refresh_costs — остаток называется в
    сообщении, а не отбрасывается молча."""
    if request.method != "POST":
        return redirect("aicore:calls")

    result = refresh_costs()
    if result["updated"]:
        messages.success(request, f"Цена добрана по строкам: {result['updated']}.")
    if result["left"]:
        messages.info(request, f"Осталось строк без цены, которые ещё можно добрать: "
                               f"{result['left']} — нажмите кнопку ещё раз.")
    if not result["updated"] and not result["left"] and not result["errors"]:
        messages.info(request, "Добирать нечего: у всех строк с id генерации цена уже есть.")
    for err in result["errors"]:
        messages.error(request, err)
    return redirect("aicore:calls")


@login_required
def proxies(request):
    # Сначала все активные (пул ротации), внутри группы — по тому же ключу, которым
    # транспорт выбирает прокси под запрос; неактивные тем же ключом ниже.
    ordered = sorted(ProxySettings.objects.all(), key=lambda p: (not p.is_active, p.reliability_key))
    return render(request, "aicore/proxies.html", {
        "proxies": ordered,
        # Кого возьмёт следующий запрос — спрашиваем сам транспорт, а не пересказываем
        # его логику в шаблоне. Это первый активный из списка выше.
        "next_proxy": ProxySettings.pick(),
    })


def _proxy_fill_from_post(proxy, post):
    proxy.description = post.get("description", "").strip()
    proxy.host = post.get("host", "").strip()
    proxy.port = int(post.get("port"))
    proxy.username = post.get("username", "").strip()
    proxy.password = post.get("password", "").strip()
    proxy.expires_at = parse_date(post.get("expires_at", ""))
    # Активных может быть сколько угодно: активные и есть пул ротации.
    proxy.is_active = bool(post.get("is_active"))
    proxy.save()


@login_required
def proxy_add(request):
    if request.method != "POST":
        return redirect("aicore:proxies")
    if not request.POST.get("host", "").strip():
        messages.error(request, "Хост прокси не может быть пустым.")
        return redirect("aicore:proxies")
    if not request.POST.get("port", "").strip().isdigit():
        messages.error(request, "Порт прокси не может быть пустым и должен быть числом.")
        return redirect("aicore:proxies")
    _proxy_fill_from_post(ProxySettings(), request.POST)
    messages.success(request, "Прокси добавлен.")
    return redirect("aicore:proxies")


@login_required
def proxy_save(request, pk):
    if request.method != "POST":
        return redirect("aicore:proxies")
    proxy = get_object_or_404(ProxySettings, pk=pk)
    if not request.POST.get("port", "").strip().isdigit():
        messages.error(request, "Порт прокси не может быть пустым и должен быть числом.")
        return redirect("aicore:proxies")
    _proxy_fill_from_post(proxy, request.POST)
    messages.success(request, "Настройки прокси сохранены.")
    return redirect("aicore:proxies")


@login_required
def proxy_delete(request, pk):
    proxy = get_object_or_404(ProxySettings, pk=pk)
    if request.method == "POST":
        proxy.delete()
        messages.success(request, "Прокси удалён.")
    return redirect("aicore:proxies")
