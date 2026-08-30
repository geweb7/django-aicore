"""Тесты слоя. Прогон: `manage.py test aicore --settings=<проект>.settings_test`.

Проверяется то, что `check` не ловит по определению: исполняемое поведение — атрибуция
вызова приложению, отказ вместо тихой подмены провайдера и отрисовка журнала со всеми
фильтрами. Правка, проверенная одним `check`, — непроверенная правка.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .core import AIUnavailableError, resolve_app, resolve_task
from .models import AICallLog, AIProvider, AITask


def make_provider(**kwargs):
    defaults = dict(api_key="k", model="m", base_url="https://example.com/v1/chat/completions",
                    dialect=AIProvider.DIALECT_OPENAI, role=AIProvider.ROLE_SMART, is_active=True,
                    priority=AIProvider.next_free_priority())
    defaults.update(kwargs)
    return AIProvider.objects.create(**defaults)


def run_as_app(app_name, func, *args, **kwargs):
    """Зовёт func из кадра, чей корневой пакет — app_name.

    Иначе проверить обход стека нельзя: сам файл тестов лежит в django_aicore, а этот
    пакет `calling_app` пропускает — и приложением объявился бы `unittest`.
    """
    g = {"__name__": app_name, "func": func, "args": args, "kwargs": kwargs}
    exec(compile("result = func(*args, **kwargs)", f"<{app_name}>", "exec"), g)
    return g["result"]


class ResolveAppTests(TestCase):
    def test_app_taken_from_call_stack(self):
        self.assertEqual(run_as_app("myapp", resolve_app, None), "myapp")

    def test_stack_beats_task_key(self):
        # Ключ задачи — только страховка: вызвать чужую задачу может любое приложение,
        # и платит за вызов то, которое его сделало.
        task = AITask.objects.create(key="otherapp.step", name="Шаг")
        self.assertEqual(run_as_app("myapp", resolve_app, task), "myapp")

    def test_log_row_gets_app_without_being_told(self):
        from .core import _log_call

        provider = make_provider()
        row = run_as_app("myapp", _log_call, "chat", provider, "кто-то", {}, {})
        self.assertEqual(row.app, "myapp")


class ResolveTaskTests(TestCase):
    def test_tag_wins_over_role(self):
        make_provider(role=AIProvider.ROLE_SMART, model="smart")
        tagged = make_provider(role=AIProvider.ROLE_CHEAP, model="free-one")
        tagged.tags.create(name="free")
        AITask.objects.create(key="myapp.free", tag="free")

        _, provider, _ = resolve_task("myapp.free")
        self.assertEqual(provider.pk, tagged.pk)

    def test_unresolvable_tag_fails_instead_of_silent_paid_provider(self):
        paid = make_provider(role=AIProvider.ROLE_SMART, model="paid")
        tagged = make_provider(role=AIProvider.ROLE_CHEAP, model="free-one", is_active=False)
        tagged.tags.create(name="free")
        AITask.objects.create(key="myapp.free", tag="free")

        with self.assertRaises(AIUnavailableError) as ctx:
            resolve_task("myapp.free")
        # Glass box: чинить нужно по первому сообщению — что за задача, какой тег искали.
        self.assertIn("free", str(ctx.exception))
        self.assertIn("myapp.free", str(ctx.exception))
        self.assertNotIn(paid.model, str(ctx.exception).split("Теги активных")[0])

    def test_no_tag_falls_back_to_role(self):
        smart = make_provider(role=AIProvider.ROLE_SMART)
        AITask.objects.create(key="myapp.plain")
        _, provider, _ = resolve_task("myapp.plain", role="cheap")
        self.assertEqual(provider.pk, smart.pk)


class CallsViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("admin", password="x")
        self.client.force_login(self.user)
        self.provider = make_provider()
        self.task = AITask.objects.create(key="promo.agent", name="Агент")
        today = timezone.now()
        self.row = AICallLog.objects.create(
            kind=AICallLog.KIND_CHAT, task=self.task, app="promo", provider=self.provider,
            ok=True, prompt_tokens=100, completion_tokens=50, cost=Decimal("0.5"))
        AICallLog.objects.create(
            kind=AICallLog.KIND_CHAT, app="plants", provider=self.provider, ok=False,
            http_status=429, error_kind="http", error="rate limit", prompt_tokens=10)
        AICallLog.objects.create(
            kind=AICallLog.KIND_EMBED, app="", provider=self.provider, ok=True,
            prompt_tokens=7, cost=Decimal("0"))
        self.today = today.date().isoformat()

    def url(self, **params):
        base = reverse("aicore:calls")
        if not params:
            return base
        return base + "?" + "&".join(f"{k}={v}" for k, v in params.items())

    def test_summary_covers_every_app_and_sums_to_total(self):
        r = self.client.get(self.url())
        self.assertEqual(r.status_code, 200)
        by_app = {row["app"]: row for row in r.context["by_app"]}
        self.assertEqual(set(by_app), {"promo", "plants", ""})
        self.assertEqual(sum(row["calls"] for row in by_app.values()),
                         r.context["totals"]["calls"])
        self.assertEqual(sum(row["cost_total"] for row in by_app.values()),
                         r.context["totals"]["cost_total"])
        self.assertEqual(by_app["promo"]["tokens"], 150)

    def test_app_filter(self):
        r = self.client.get(self.url(app="promo"))
        self.assertEqual(r.context["totals"]["calls"], 1)
        self.assertEqual([c.pk for c in r.context["page"]], [self.row.pk])
        # Сводка считается до фильтра по приложению — иначе сравнивать не с чем.
        self.assertEqual(len(r.context["by_app"]), 3)

    def test_app_filter_none_bucket(self):
        r = self.client.get(self.url(app="-"))
        self.assertEqual(r.context["totals"]["calls"], 1)
        self.assertEqual(r.context["page"][0].app, "")

    def test_zero_cost_is_not_missing_cost(self):
        r = self.client.get(self.url(app="-"))
        # Бесплатная строка входит в число «с ценой»: $0 и «цену не отдали» — разное.
        self.assertEqual(r.context["totals"]["priced"], 1)
        self.assertEqual(r.context["totals"]["cost_total"], Decimal("0"))

    def test_http_filter(self):
        r = self.client.get(self.url(http="429"))
        self.assertEqual(r.context["totals"]["calls"], 1)
        self.assertIn(429, r.context["http_statuses"])

    def test_period_filter_today_and_empty_past(self):
        r = self.client.get(self.url(**{"from": self.today}))
        self.assertEqual(r.context["totals"]["calls"], 3)
        r = self.client.get(self.url(**{"to": "2000-01-01"}))
        self.assertEqual(r.context["totals"]["calls"], 0)

    def test_broken_date_is_reported_not_swallowed(self):
        r = self.client.get(self.url(**{"from": "позавчера"}))
        self.assertEqual(r.context["totals"]["calls"], 3)
        self.assertTrue([m for m in r.context["messages"] if "не дата" in str(m)])
