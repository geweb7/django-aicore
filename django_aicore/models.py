import random
import re
from urllib.parse import urlparse

from django.db import models
from django.db.models import F
from django.utils import timezone

# Хвост пути, который означает «корень API», а не метод: пусто, /api, /v1, /v1beta, /v2…
# openai-совместимый вызов уходит на base_url КАК ЕСТЬ (aicore/core.py, ветка else), путь
# метода не дописывается — такой URL гарантированно вернёт 404. Один детектор на два
# места: валидация формы (не дать сохранить) и подсказка в ошибке (объяснить 404).
_API_ROOT_TAIL = re.compile(r"^(v\d+[a-z]*|api)$", re.I)


def api_url_is_root(url):
    """URL указывает на корень API, а не на конкретный метод. Возвращает (bool, path)."""
    path = urlparse(url or "").path.rstrip("/")
    tail = path.rsplit("/", 1)[-1] if path else ""
    return (tail == "" or bool(_API_ROOT_TAIL.match(tail))), path


class AIProvider(models.Model):
    ROLE_GENIUS = "genius"
    ROLE_SMART = "smart"
    ROLE_SMART_ALT = "smart-alt"
    ROLE_CHEAP = "cheap"
    ROLE_EMBED = "embed"
    ROLE_VISION = "vision"
    ROLE_CHOICES = [
        (ROLE_GENIUS, "Гений (особо умная)"),
        (ROLE_SMART, "Умная"),
        (ROLE_SMART_ALT, "Умная (альт.)"),
        (ROLE_CHEAP, "Дешёвая"),
        (ROLE_EMBED, "Эмбеддинг"),
        (ROLE_VISION, "Vision (фото)"),
    ]

    # Диалект — как именно разговаривать с провайдером: формат запроса, заголовки и то,
    # достраивается ли путь. Это выбор администратора, а не догадка по домену: раньше
    # он выводился из base_url, и всё неопознанное молча объявлялось openai-совместимым.
    # Молчаливое предположение живёт до первого вызова, а потом всплывает HTTP-ошибкой
    # в трёх слоях от места, где его сделали.
    DIALECT_OPENAI = "openai"
    DIALECT_GEMINI = "gemini"
    DIALECT_OPENROUTER = "openrouter"
    DIALECT_CHOICES = [
        (DIALECT_OPENAI, "OpenAI-совместимый (POST на указанный endpoint)"),
        (DIALECT_GEMINI, "Gemini (путь достраивается)"),
        (DIALECT_OPENROUTER, "OpenRouter (адрес фиксирован в коде)"),
    ]

    description = models.TextField(blank=True, verbose_name="Описание")
    api_key = models.CharField(max_length=500, verbose_name="API ключ")
    model = models.CharField(max_length=100, verbose_name="Модель")
    base_url = models.URLField(verbose_name="API endpoint")
    dialect = models.CharField(max_length=20, choices=DIALECT_CHOICES, verbose_name="Диалект API")
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default=ROLE_SMART, verbose_name="Роль")
    is_active = models.BooleanField(default=False, verbose_name="Активный")
    # Несколько активных провайдеров с одной ролью — нормальное состояние: задачи выбирают
    # между ними по тегу. Когда выбор идёт по роли, побеждает меньший priority — правило
    # должно быть предсказуемым и видимым, а не «первая строка в таблице».
    # unique: при равных priority победителя решал бы тай-брейк по id — невидимый глазами.
    # Уникальность делает порядок выбора полным и читаемым прямо из списка.
    priority = models.PositiveSmallIntegerField(
        default=100, unique=True, verbose_name="Приоритет (меньше — раньше)")
    timeout = models.PositiveIntegerField(default=90, verbose_name="Таймаут (сек)")
    temperature = models.FloatField(default=0.3, verbose_name="Temperature")

    class Meta:
        ordering = ["priority", "id"]
        verbose_name = "AI провайдер"
        verbose_name_plural = "AI провайдеры"

    @classmethod
    def next_free_priority(cls):
        """Наименьший свободный приоритет ниже всех существующих (SSOT для формы и копии)."""
        last = cls.objects.order_by("-priority").values_list("priority", flat=True).first()
        return 100 if last is None else last + 1

    def __str__(self):
        return f"{self.dialect}: {self.model}"


class AIProviderTag(models.Model):
    provider = models.ForeignKey(AIProvider, on_delete=models.CASCADE, related_name='tags')
    name = models.CharField(max_length=100, verbose_name="Тег")

    class Meta:
        unique_together = [('provider', 'name')]
        verbose_name = "Тег провайдера"
        verbose_name_plural = "Теги провайдера"

    def __str__(self):
        return self.name


class ProxySettings(models.Model):
    description = models.TextField(blank=True, verbose_name="Описание")
    host = models.CharField(max_length=200, blank=True, verbose_name="IP / хост")
    port = models.PositiveIntegerField(default=63094, verbose_name="HTTP порт")
    username = models.CharField(max_length=200, blank=True, verbose_name="Пользователь")
    password = models.CharField(max_length=200, blank=True, verbose_name="Пароль")
    is_active = models.BooleanField(default=False, verbose_name="Использовать для запросов к ИИ")
    expires_at = models.DateField(null=True, blank=True, verbose_name="Дата окончания")
    # Счётчики транспорта: успешно доставленных запросов и сбоев тракта. Одни сбои без
    # успехов — цифра без знаменателя: 5 сбоев на 5 запросах и 5 на 500 означают разное,
    # а по одному счётчику они неотличимы.
    oks = models.PositiveIntegerField(default=0, verbose_name="Успехов")
    fails = models.PositiveIntegerField(default=0, verbose_name="Сбоев")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Создан")

    class Meta:
        verbose_name = "Прокси"
        verbose_name_plural = "Прокси"

    def __str__(self):
        return f"{self.host}:{self.port}" if self.host else f"Прокси #{self.pk}"

    def save(self, *args, **kwargs):
        """Наступившая дата окончания снимает активность и оставляет след в описании —
        проверяется здесь, в единственном месте, куда идёт и сохранение формы, и
        ленивая проверка из `pick()`. Раздвоить эту логику по двум местам значило бы
        чинить один и тот же факт дважды и однажды разойтись.
        """
        if self.is_active and self.expires_at and self.expires_at <= timezone.now().date():
            today = timezone.now().date()
            note = f"[{today:%d.%m.%Y}] отключён автоматически: истёк срок действия (до {self.expires_at:%d.%m.%Y})"
            self.is_active = False
            self.description = f"{self.description}\n{note}" if self.description else note
        super().save(*args, **kwargs)

    @classmethod
    def pick(cls, exclude=()):
        """Прокси для следующего запроса: случайный из активных, с весом по доле успехов.

        Активных может быть несколько — они и есть пул ротации. Отдельного решения
        «переключиться» нет: каждый запрос тянет прокси случайно, но вероятность
        пропорциональна накопленной надёжности, поэтому сбои сами смещают нагрузку.

        Строгий выбор лучшего (min по fail_rate) отдавал бы весь трафик одному прокси,
        а прокси с одним ранним сбоем без успехов застревал бы на 100% сбоев навсегда —
        его не выбрали бы уже никогда. Взвешенный случай распределяет нагрузку по пулу
        и оставляет каждому шанс реабилитироваться.

        Вес — сглаженная доля успехов (oks+1)/(calls+2): необкатанный прокси стартует с
        1/2, а не с 0, поэтому получает трафик; один сбой роняет вес, но не до нуля.

        `exclude` — pk прокси, уже опробованных в рамках ОДНОГО запроса (ретрай транспорта).
        Ретрай через тот же прокси бессмыслен: он повторит тот же обрыв, — поэтому
        исключение делает вызывающий транспорт, а не случай.
        """
        cls._deactivate_expired()
        pool = [p for p in cls.objects.filter(is_active=True).exclude(host="")
                if p.pk not in exclude]
        if not pool:
            return None
        weights = [(p.oks + 1) / (p.calls + 2) for p in pool]
        return random.choices(pool, weights=weights, k=1)[0]

    @classmethod
    def _deactivate_expired(cls):
        """Ловит прокси, чей срок истёк без сохранения формы (форму никто не открывал).
        Отдельного крона у aicore нет — проверка лениво висит на `pick()`, а его
        проходит каждый реальный AI-вызов через прокси, поэтому истёкший прокси выпадает
        из пула практически сразу. Сама проверка — в `save()`, здесь только находим, кого
        сохранить, чтобы она сработала.
        """
        today = timezone.now().date()
        for proxy in cls.objects.filter(is_active=True, expires_at__isnull=False, expires_at__lte=today):
            proxy.save()

    @property
    def reliability_key(self):
        """Ключ надёжности: меньше — надёжнее. Один и тот же и для отбора прокси под
        запрос, и для сортировки списка в UI — иначе страница показывала бы один порядок,
        а транспорт жил по другому.

        Мерило — доля сбоев, а не их число: 5 сбоев на 500 запросах лучше, чем 2 на 3.
        Необкатанный прокси (запросов не было) идёт как 0% — доказанного плохого за ним
        нет, и так он получает шанс себя показать; при равной доле вперёд выходит тот, у
        кого меньше сбоев в абсолюте, затем — кто раньше заведён.
        """
        return (self.fail_rate or 0.0, self.fails, self.pk)

    def register_fail(self):
        """+1 к счётчику сбоев. Инкремент делается в SQL (F), а не чтением-записью в
        питоне: параллельные запросы иначе затирали бы инкременты друг друга."""
        type(self).objects.filter(pk=self.pk).update(fails=F("fails") + 1)
        self.fails += 1

    def register_ok(self):
        """+1 к счётчику успехов, тем же способом и по тому же критерию, что и сбой:
        считается доставка запроса и получение ответа, а не то, понравился ли ответ."""
        type(self).objects.filter(pk=self.pk).update(oks=F("oks") + 1)
        self.oks += 1

    @property
    def calls(self):
        return self.oks + self.fails

    @property
    def fail_rate(self):
        """Доля сбоев в процентах, либо None — если через прокси ещё ничего не гоняли.
        None, а не 0: «ни одного запроса» и «ни одного сбоя на сотне запросов» — разные
        вещи, и показывать их одинаковым нулём нельзя."""
        if not self.calls:
            return None
        return round(100.0 * self.fails / self.calls, 1)

    def proxy_url(self):
        if not self.host:
            return ""
        from urllib.parse import quote
        auth = ""
        if self.username:
            auth = quote(self.username, safe="")
            if self.password:
                auth += ":" + quote(self.password, safe="")
            auth += "@"
        return f"http://{auth}{self.host}:{self.port}"

    def requests_proxies(self):
        """dict прокси для requests, либо None если неактивен/не настроен.
        HTTP-прокси туннелирует и https через CONNECT."""
        if not self.is_active:
            return None
        url = self.proxy_url()
        return {"http": url, "https": url} if url else None


class AITask(models.Model):
    """Задача = точка вызова AI. Что ей отвечает — настраивается в UI, а не в коде.

    Подбор модели под задачу — эксплуатационная настройка (модели меняются, дорожают,
    тупеют), поэтому она живёт в базе. Задача сама регистрируется при первом вызове со
    значениями из кода; пользователь потом переопределяет их, ничего не заводя руками.

    Ключ — стабильный слаг, а НЕ подпись. Подпись меняется свободно; если бы ключом была
    она, переименование шага молча создавало бы новую задачу с дефолтами из кода, и
    настройка пользователя так же молча переставала бы применяться.
    """

    key = models.SlugField(max_length=100, unique=True, verbose_name="Ключ (стабильный)")
    name = models.CharField(max_length=200, blank=True, verbose_name="Название")

    # Что попросил код при регистрации. В UI только для чтения — это точка отсчёта.
    default_role = models.CharField(max_length=20, blank=True, verbose_name="Роль из кода")
    default_temperature = models.FloatField(null=True, blank=True, verbose_name="Temperature из кода")

    # Переопределения пользователя. Пусто — значит не переопределять.
    tag = models.CharField(max_length=100, blank=True, verbose_name="Тег провайдера")
    role = models.CharField(max_length=20, blank=True, choices=AIProvider.ROLE_CHOICES,
                            verbose_name="Роль (замена)")
    temperature = models.FloatField(null=True, blank=True, verbose_name="Temperature (замена)")

    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Зарегистрирована")

    class Meta:
        ordering = ["key"]
        verbose_name = "AI задача"
        verbose_name_plural = "AI задачи"

    def __str__(self):
        return self.name or self.key

    @property
    def effective_role(self):
        return self.role or self.default_role or AIProvider.ROLE_SMART


class AICallLog(models.Model):
    """Журнал вызовов AI: строка на каждый вызов, включая успешные.

    Нужен, чтобы отвечать на вопросы, на которые нельзя ответить по одному сбою:
    падает конкретный хостер или модель вообще, виноват прокси или удалённая
    сторона, зависит ли отказ от размера промпта. Без доли успешных вызовов эти
    цифры бессмысленны, поэтому пишутся и они.
    """

    KIND_CHAT = "chat"
    KIND_EMBED = "embedding"
    KIND_CHOICES = [(KIND_CHAT, "Чат"), (KIND_EMBED, "Эмбеддинг")]

    created_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="Время")
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default=KIND_CHAT, verbose_name="Тип вызова")
    # task — новый путь (реестр), caller — старый (свободная метка). Группировать статистику
    # по task надёжнее: подпись можно переименовать, ключ — нет.
    task = models.ForeignKey("AITask", null=True, blank=True, on_delete=models.SET_NULL,
                             verbose_name="Задача")
    caller = models.CharField(max_length=100, blank=True, db_index=True, verbose_name="Вызывающий")
    # Приложение-инициатор: тот же факт, что уходит в X-Title (см. resolve_app). Пишется
    # полем, а не выводится из префикса task.key: у легаси-call() и у эмбеддингов задачи
    # нет вовсе, а удаление задачи (SET_NULL выше) стёрло бы приложение и у остальных.
    # Пусто бывает только у строк старше самого поля: вызов, приложение которого слой
    # определить не смог, не делается вовсе (resolve_app).
    app = models.CharField(max_length=50, blank=True, db_index=True, verbose_name="Приложение")

    provider = models.ForeignKey(AIProvider, null=True, blank=True, on_delete=models.SET_NULL,
                                 verbose_name="Провайдер")
    role = models.CharField(max_length=20, blank=True, verbose_name="Роль")
    model = models.CharField(max_length=100, blank=True, verbose_name="Модель")
    # Хостер, которому OpenRouter отдал запрос (поле provider в теле ответа).
    upstream = models.CharField(max_length=100, blank=True, db_index=True, verbose_name="Хостер")

    proxy = models.ForeignKey(ProxySettings, null=True, blank=True, on_delete=models.SET_NULL,
                              verbose_name="Прокси")
    proxy_label = models.CharField(max_length=200, blank=True, verbose_name="Прокси (снимок)")

    ok = models.BooleanField(default=False, db_index=True, verbose_name="Успех")
    http_status = models.PositiveSmallIntegerField(null=True, blank=True, verbose_name="HTTP")
    finish_reason = models.CharField(max_length=50, blank=True, verbose_name="finish_reason")
    # На какой фазе оборвалось: proxy / connect_timeout / read_timeout / connection /
    # http / bad_json / empty_body — от транспорта; upstream_error / no_content — от модели.
    error_kind = models.CharField(max_length=30, blank=True, db_index=True, verbose_name="Фаза отказа")
    error = models.TextField(blank=True, verbose_name="Ошибка")

    prompt_tokens = models.PositiveIntegerField(null=True, blank=True, verbose_name="Токены промпта")
    completion_tokens = models.PositiveIntegerField(null=True, blank=True, verbose_name="Токены ответа")
    reasoning_tokens = models.PositiveIntegerField(null=True, blank=True, verbose_name="Токены рассуждений")
    duration_s = models.FloatField(null=True, blank=True, verbose_name="Длительность, с")

    # Цену называет сам OpenRouter, мы её не считаем: прайс модели × токены разошёлся бы
    # с фактом на кэше промпта, BYOK и разных хостерах, а в поле такое число неотличимо
    # от настоящего. null — цены нет (не openrouter либо не пришла), 0 — вызов бесплатный.
    cost = models.DecimalField(max_digits=12, decimal_places=8, null=True, blank=True,
                               verbose_name="Стоимость, $")
    # id генерации у OpenRouter — единственный ключ, по которому цену можно добрать позже
    # (GET /generation?id=…). Строка без него не обновляема ничем и никогда.
    gen_id = models.CharField(max_length=100, blank=True, db_index=True, verbose_name="id генерации")

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Вызов AI"
        verbose_name_plural = "Журнал вызовов AI"

    def __str__(self):
        return f"{self.created_at:%d.%m %H:%M} {self.model} — {'ок' if self.ok else self.error_kind or 'ошибка'}"
