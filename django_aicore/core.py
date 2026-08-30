import json
import logging
import re
import socket
import sys
import time
from decimal import Decimal
from urllib.parse import quote, urlparse

import requests

from .models import AIProvider, AIProviderTag, ProxySettings, api_url_is_root

logger = logging.getLogger(__name__)


class AIUnavailableError(Exception):
    def __init__(self, message, usage=None):
        super().__init__(message)
        self.usage = usage or {}


# Фазы отказа, в которых виноват тракт до провайдера, а не сам провайдер: запрос либо не
# дошёл, либо оборвался по дороге. Только они двигают счётчик сбоев прокси и, значит,
# ротацию. Ответ с HTTP-ошибкой, кривым JSON или отказом модели (http / bad_json /
# empty_body / upstream_error) означает, что прокси как раз отработал — доставил запрос
# и принёс ответ; наказывать его за то, что ответ не понравился, нельзя, иначе ротация
# начнёт гонять по кругу исправные прокси. read_timeout — это молчание модели уже после
# установленного соединения, тоже не вина прокси.
PROXY_FAIL_KINDS = {"proxy", "connect_timeout", "connection"}

# Сколько ДОПОЛНИТЕЛЬНЫХ попыток делать после первой, когда запрос не дошёл по вине тракта
# (фазы из PROXY_FAIL_KINDS). Каждая попытка — обязательно через другой прокси; кончились
# прокси — кончились попытки. 0 = как было, ни одного повтора.
#
# Это не отмена fail fast: повторяется только НЕДОСТАВЛЕННЫЙ запрос. Ответ, который дошёл
# (HTTP-ошибка, кривой JSON, отказ хостера) и молчание модели после установленного
# соединения (read_timeout) не повторяются никогда — там повтор лечил бы не тракт, а
# оплачивал бы те же токены второй раз.
PROXY_RETRIES = 1


# Корневые пакеты, которые не могут быть приложением-инициатором AI-вызова: сам слой,
# проект и рантайм. Кадры из них при поиске приложения проходятся насквозь.
NON_APP_ROOTS = {"django_aicore", "django", "threading", "concurrent", "socketserver", "__main__"}


def calling_app():
    """Приложение, из которого пришёл вызов: корневой пакет первого кадра вне слоя
    («atlas», «promo», «plants»). Пусто — если вызов пришёл не из приложения.

    Спрашиваем стек, а не вызывающего. Аргумент пришлось бы передавать в каждой точке
    вызова во всех приложениях, и атрибуция снова держалась бы на памяти разработчика —
    ровно так легаси-`call()` и уходил в OpenRouter под «aicore»: он про приложение просто
    не знает.
    """
    frame = sys._getframe(1)
    while frame is not None:
        root = (frame.f_globals.get("__name__") or "").split(".", 1)[0]
        if root and root not in NON_APP_ROOTS:
            return root
        frame = frame.f_back
    return ""


def resolve_app(task=None):
    """Приложение-инициатор вызова — одно значение и для X-Title, и для журнала.

    Раздваивать нельзя: OpenRouter в Activity разбивал бы траты по одному признаку, а
    наш собственный экран — по другому, и сойтись они не обязаны.

    Слаг задачи — страховка на случай вызова не из приложения (management-команда самого
    слоя, shell); последний фолбэк называет слой честным именем, а не оставляет пусто:
    пустое поле в журнале означает «вызов записан до появления поля», и путать эти два
    случая нельзя.
    """
    return calling_app() or (task.key.split(".", 1)[0] if task else "") or "django_aicore"


def get_provider(role="smart"):
    # Активных с одной ролью может быть несколько; кто победит при выборе по роли,
    # решает AIProvider.Meta.ordering = [priority, id], а не порядок строк в таблице.
    active = AIProvider.objects.filter(is_active=True)
    provider = active.filter(tags__name=role).first()
    if provider:
        return provider
    provider = active.filter(role=role).first()
    if not provider:
        if role == "vision":
            # Без фолбэка: vision-вызов на текстовой модели молча не сработает.
            raise AIUnavailableError(
                "Нет активного AI провайдера с тегом или ролью «vision» — "
                "настройте провайдера с тегом vision в разделе Настройки → AI."
            )
        if role in ("cheap", "smart-alt", "genius"):
            provider = active.filter(tags__name="smart").first() or active.filter(role="smart").first()
        if not provider:
            raise AIUnavailableError("Нет активного AI провайдера. Настройте в разделе Настройки → AI.")
    return provider


def provider_type(provider):
    return provider.dialect


def http_error_parts(response):
    """Из HTTP-ответа с ошибкой достаёт (status, detail, body_text[:1000]) для прозрачных сообщений."""
    if response is None:
        return "?", "", ""
    body = (response.text or "")[:1000]
    detail = ""
    try:
        err = response.json()
        if isinstance(err.get("error"), dict):
            detail = err["error"].get("message", "")
        else:
            detail = str(err.get("error", err.get("detail", "")))
    except Exception:
        detail = ""
    return response.status_code, detail, body


def diagnose_network(proxies, target_url):
    """После сетевой ошибки прощупывает тракт и говорит, где именно обрыв:
    TCP до прокси → HTTPS через прокси до хоста цели. Это диагностика для
    сообщения об ошибке, не ретрай — исходный AI-запрос не повторяется."""
    try:
        lines = []
        target_host = urlparse(target_url).hostname or target_url
        proxy_url = (proxies or {}).get("https") or (proxies or {}).get("http")

        if not proxy_url:
            try:
                with socket.create_connection((target_host, 443), timeout=5):
                    lines.append(f"TCP {target_host}:443 напрямую — доступен; обрыв был выше уровнем (TLS/HTTP).")
            except OSError as e:
                lines.append(f"TCP {target_host}:443 напрямую — недоступен: {e}")
            return "Диагностика:\n" + "\n".join("— " + l for l in lines)

        p = urlparse(proxy_url)
        proxy_host = p.hostname
        proxy_port = p.port or 80
        try:
            with socket.create_connection((proxy_host, proxy_port), timeout=5):
                lines.append(f"TCP {proxy_host}:{proxy_port} — порт прокси открыт.")
        except socket.timeout:
            lines.append(
                f"TCP {proxy_host}:{proxy_port} — таймаут за 5 сек: прокси недоступен "
                "(выключен, сменился адрес/порт или трафик к нему блокируется)."
            )
            return "Диагностика:\n" + "\n".join("— " + l for l in lines)
        except OSError as e:
            lines.append(f"TCP {proxy_host}:{proxy_port} — соединение не устанавливается: {e}")
            return "Диагностика:\n" + "\n".join("— " + l for l in lines)

        try:
            r = requests.get(f"https://{target_host}/", proxies=proxies, timeout=(5, 10))
            lines.append(
                f"HTTPS через прокси к {target_host} — прошёл (HTTP {r.status_code}): "
                "тракт жив, ошибка была разовой или в самом AI-запросе."
            )
        except requests.exceptions.ProxyError as e:
            lines.append(f"HTTPS через прокси к {target_host} — прокси отверг CONNECT (логин/пароль? протокол?): {e}")
        except requests.exceptions.Timeout:
            lines.append(f"HTTPS через прокси к {target_host} — таймаут: прокси принимает TCP, но трафик через него не проходит.")
        except requests.exceptions.ConnectionError as e:
            lines.append(f"HTTPS через прокси к {target_host} — обрыв: {e}")
        return "Диагностика:\n" + "\n".join("— " + l for l in lines)
    except Exception as e:
        return f"Диагностика не удалась: {e}"


def _post_once(url, headers, payload, timeout, connect_timeout, proxies, *, kind, where, meta,
               method="POST"):
    """Один запрос через заданный прокси: либо ответ, либо AIUnavailableError с проставленной
    в meta фазой отказа (error_kind). Ретраев нет — вызывается ровно один раз.

    Вынесено из post_json, чтобы у всех транспортных отказов была одна общая точка выхода:
    там post_json и решает, засчитывать ли сбой прокси. Иначе учёт пришлось бы дублировать
    в каждой except-ветке.
    """
    # max_retries=0 явно: ретраев нет. Формулировка «Max retries exceeded» в
    # тексте сетевых ошибок — стандартный текст urllib3 даже при нуле попыток.
    http = requests.Session()
    adapter = requests.adapters.HTTPAdapter(max_retries=0)
    http.mount("https://", adapter)
    http.mount("http://", adapter)
    t_http = time.monotonic()
    try:
        try:
            resp = http.request(method, url, headers=headers,
                                json=payload if payload is not None else None,
                                timeout=(connect_timeout, timeout), proxies=proxies)
            meta["http_status"] = resp.status_code
            resp.raise_for_status()
            return resp
        finally:
            # Длительность фиксируем в момент сбоя, а не в общем finally: except-ветки
            # ниже зовут diagnose_network() с TCP/HTTPS-пробами (до ~15 сек), и это время
            # не должно попасть ни в сообщение, ни в журнал вызовов.
            meta["duration_s"] = round(time.monotonic() - t_http, 2)
    except requests.exceptions.ProxyError as e:
        meta["error_kind"] = "proxy"
        raise AIUnavailableError(
            f"Не удалось подключиться к прокси ({kind} {where}). "
            f"Исходная ошибка: {e}\n\n{diagnose_network(proxies, url)}"
        )
    except requests.exceptions.ConnectTimeout as e:
        meta["error_kind"] = "connect_timeout"
        proxy_hint = ""
        if proxies:
            proxy_url = proxies.get("https") or proxies.get("http") or ""
            if proxy_url:
                p = urlparse(proxy_url)
                proxy_hint = f" через прокси {p.hostname}:{p.port}" if p.port else f" через прокси {p.hostname}"
        raise AIUnavailableError(
            f"Не удалось установить соединение с {kind}{proxy_hint} за {connect_timeout} сек "
            f"(запрос до модели не дошёл — подозреваемый прокси/сеть, не модель) {where}: {e}"
            f"\n\n{diagnose_network(proxies, url)}"
        )
    except requests.exceptions.Timeout as e:
        # При HTTPS через прокси urllib3 бросает ReadTimeout (не ConnectTimeout) на CONNECT-фазе,
        # подставляя connect_timeout в значение. Отличаем фазы по фактическому значению в сообщении.
        m = re.search(r"read timeout=(\d+(?:\.\d+)?)", str(e))
        fired = float(m.group(1)) if m else None
        meta["error_kind"] = "read_timeout"
        if fired is not None and abs(fired - connect_timeout) < 0.5 and connect_timeout < timeout:
            meta["error_kind"] = "connect_timeout"
            proxy_hint = ""
            if proxies:
                proxy_url = proxies.get("https") or proxies.get("http") or ""
                if proxy_url:
                    p = urlparse(proxy_url)
                    proxy_hint = f" через прокси {p.hostname}:{p.port}" if p.port else f" через прокси {p.hostname}"
            raise AIUnavailableError(
                f"Не удалось установить соединение с {kind}{proxy_hint} за {connect_timeout} сек "
                f"(запрос до модели не дошёл — подозреваемый прокси/сеть, не модель) {where}: {e}"
                f"\n\n{diagnose_network(proxies, url)}"
            )
        raise AIUnavailableError(
            f"{kind} не ответил за {timeout} сек (ждали ответ модели) {where}: {e}"
            f"\n\n{diagnose_network(proxies, url)}"
        )
    except (requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError) as e:
        meta["error_kind"] = "connection"
        raise AIUnavailableError(
            f"Обрыв соединения с {kind} через {meta['duration_s']} сек после отправки запроса "
            f"(таймаут был {timeout} сек) {where}: {e}\n\n{diagnose_network(proxies, url)}"
        )
    except requests.exceptions.HTTPError as e:
        meta["error_kind"] = "http"
        status, detail, body = http_error_parts(e.response)
        msg = f"Ошибка {kind} (HTTP {status}) {where}"
        if detail:
            msg += f": {detail}"
        if body:
            msg += f"\n\nПолный ответ сервера:\n{body}"
        resp_headers = dict(e.response.headers) if e.response is not None else {}
        waf = resp_headers.get("Server") or resp_headers.get("server") or ""
        if waf:
            msg += f"\n(Server: {waf})"
        if status == 404:
            is_root, path = api_url_is_root(url)
            if is_root:
                msg += (
                    f" — endpoint неполный: POST ушёл на «{path or '/'}» — это корень API, а не метод, "
                    f"и сервер не может знать, что от него хотят. В base_url провайдера нужен полный "
                    f"путь метода: «{path}/chat/completions» (Настройки → AI). "
                    f"Имя модели тут ни при чём — до неё запрос не дошёл."
                )
            else:
                msg += " — вероятно неверное имя модели или base_url провайдера (Настройки → AI)."
        raise AIUnavailableError(msg)


def _retry_error(failures):
    """Итоговая ошибка после всех попыток: [(прокси, фаза, ошибка)] → AIUnavailableError.

    Одна попытка — исходная ошибка без изменений, пересказывать нечего. Было несколько —
    в сообщение идут ВСЕ, с прокси и фазой каждой: «упал один прокси» и «упали три подряд»
    — это разные диагнозы (дохлый прокси против лежащей сети или провайдера), а по одной
    последней ошибке их не различить.
    """
    if len(failures) == 1:
        return failures[0][2]
    lines = [f"Попытка {i} через {proxy or 'без прокси'} — фаза «{phase}»:\n{err}"
             for i, (proxy, phase, err) in enumerate(failures, 1)]
    return AIUnavailableError(
        f"Все попытки ({len(failures)}) провалились, каждая через свой прокси.\n\n"
        + "\n\n".join(lines),
        usage=failures[-1][2].usage,
    )


def post_json(url, headers, payload, timeout, *, kind, where, meta=None, method="POST"):
    """Единый HTTP-тракт для ВСЕХ запросов к AI-провайдерам (chat, embeddings, справки).

    Всегда через прокси. Ретрай — только когда запрос не дошёл по вине тракта, до
    PROXY_RETRIES дополнительных попыток и каждая через ДРУГОЙ прокси (упавший исключается
    из выбора). Всё остальное — fail fast: ошибка сразу бросает AIUnavailableError с полным
    контекстом. Ротация по счётчикам остаётся выбором прокси для СЛЕДУЮЩЕГО запроса; здесь
    же — вторая попытка для этого.
    `kind` — тип запроса для сообщений («AI API» / «embedding API»),
    `where` — описание источника (модель, роль, url).
    `meta` — необязательный dict, куда складываются факты транспорта для журнала
    вызовов: какой прокси использован (после ретрая — тот, что сработал), HTTP-статус,
    длительность, число потраченных ретраев и на какой фазе оборвалось (error_kind).
    Только этот слой знает про прокси и фазы.
    `method` — GET нужен справочным запросам к провайдеру (цена генерации у OpenRouter);
    свой транспорт для них не заводится, иначе прокси, ретрай и разбор ошибок разойдутся.
    """
    meta = meta if meta is not None else {}
    connect_timeout = min(30, timeout)
    tried = []
    failures = []
    while True:
        active_proxy = ProxySettings.pick(exclude=tried)
        proxies = active_proxy.requests_proxies() if active_proxy else None
        meta["proxy"] = active_proxy
        # Фаза прошлой попытки не должна пережить удачную: иначе успешный после ретрая
        # вызов лёг бы в журнал с чужим error_kind.
        meta.pop("error_kind", None)
        meta["retries"] = len(failures)
        try:
            resp = _post_once(url, headers, payload, timeout, connect_timeout, proxies,
                              kind=kind, where=where, meta=meta, method=method)
            break
        except AIUnavailableError as e:
            # Единственная точка учёта сбоев: фаза отказа уже проставлена в meta, а прокси
            # известен только здесь. Ротация — следствие этого счётчика, отдельного решения
            # «переключиться» нет: следующий запрос сам возьмёт того, у кого сбоев меньше.
            transport_fault = active_proxy is not None and meta.get("error_kind") in PROXY_FAIL_KINDS
            if transport_fault:
                active_proxy.register_fail()
            failures.append((active_proxy, meta.get("error_kind") or "?", e))

            # Без прокси ретрай некуда направить (маршрут тот же), а доставленный ответ и
            # молчание модели не повторяются по определению — оба случая падают сразу.
            if not transport_fault or len(failures) > PROXY_RETRIES:
                raise _retry_error(failures) from None
            if ProxySettings.pick(exclude=tried + [active_proxy.pk]) is None:
                raise AIUnavailableError(
                    f"{e}\n\nРетрай не сделан: другого активного прокси в пуле нет "
                    f"(опробован {active_proxy}, всего активных настроено "
                    f"{ProxySettings.objects.filter(is_active=True).exclude(host='').count()}). "
                    f"Добавьте второй прокси в Настройки → AI → Прокси, иначе ретраить нечем.",
                    usage=e.usage,
                ) from None
            tried.append(active_proxy.pk)
            logger.warning("aicore: %s через %s — %s, ретрай через другой прокси",
                           kind, active_proxy, meta.get("error_kind"))

    if failures:
        logger.warning("aicore: %s прошёл через %s (ретраев: %d)", kind, active_proxy, len(failures))
    # Успех транспорта — тот же критерий, что и сбой, только с другим знаком: запрос дошёл
    # и ответ вернулся. Что будет дальше с телом ответа (не-JSON, отказ модели), прокси уже
    # не касается, поэтому успех засчитывается здесь, а не в конце функции.
    if active_proxy:
        active_proxy.register_ok()

    try:
        return resp.json()
    except Exception:
        text = resp.text or ""
        if text.strip():
            meta["error_kind"] = "bad_json"
            raise AIUnavailableError(
                f"{kind} вернул не-JSON ответ (HTTP {resp.status_code}). Начало ответа:\n{text[:500]}"
            )
        meta["error_kind"] = "empty_body"
        raise AIUnavailableError(f"{kind} вернул пустой ответ (HTTP {resp.status_code}).")


def _gemini_parts(content):
    """OpenAI-content (строка или список частей text/image_url) → parts для Gemini."""
    if isinstance(content, str):
        return [{"text": content}]
    parts = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append({"text": item.get("text", "")})
        elif isinstance(item, dict) and item.get("type") == "image_url":
            url = (item.get("image_url") or {}).get("url", "")
            if not url.startswith("data:"):
                raise AIUnavailableError(
                    f"Gemini принимает изображения только как data URI (base64), получено: «{url[:100]}»."
                )
            header, _, b64 = url.partition(",")
            mime = header[len("data:"):].split(";", 1)[0] or "image/jpeg"
            parts.append({"inline_data": {"mime_type": mime, "data": b64}})
        else:
            raise AIUnavailableError(f"Неподдерживаемая часть content для Gemini: «{str(item)[:200]}».")
    return parts


def render_messages(messages):
    """messages → текст для показа пользователю: что ушло в модель.

    Единственный способ показать отправленное. Строится из тех же messages, что
    уходят в сеть, — не реконструкция по памяти вызывающего. Картинки не
    разворачиваются: base64 фотографии (plants) — это мегабайты, читать их некому.
    """
    blocks = []
    for m in messages or []:
        content = m.get("content")
        if isinstance(content, list):
            parts = []
            for item in content:
                if not isinstance(item, dict):
                    parts.append(str(item))
                elif item.get("type") == "text":
                    parts.append(item.get("text", ""))
                elif item.get("type") == "image_url":
                    url = (item.get("image_url") or {}).get("url", "")
                    head = url[:30]
                    parts.append(f"[изображение: {head}… всего {len(url)} симв.]")
                else:
                    parts.append(f"[часть {item.get('type', '?')}]")
            content = "\n".join(parts)
        blocks.append(f"[{m.get('role', '?')}]\n{content}")
    return "\n\n".join(blocks)


def _log_call(kind, provider, caller, meta, usage, error=None, task=None):
    """Строка в журнал вызовов. Пишется и на успехе, и на каждой ошибке.

    Не глотает своих ошибок: если запись не прошла, вызов падает. Молча не
    записавшийся журнал — это журнал, которого нет, а пользователь об этом никогда
    не узнает и будет принимать решения по дырявой статистике.

    Возвращает созданную строку: её id уезжает в usage, иначе вызывающий не может
    связать свой ход с журналом — по caller туда склеиваются все ходы всех сценариев.

    Приложение спрашивается здесь, а не принимается аргументом: сюда сходятся ВСЕ пути
    записи (чат через реестр, легаси-call, эмбеддинги), и новый путь получает атрибуцию
    сам. Аргумент пришлось бы не забыть в каждой новой точке — а забывается такое сразу.
    """
    from .models import AICallLog

    usage = usage or {}
    proxy = meta.get("proxy")
    cost = usage.get("cost")
    return AICallLog.objects.create(
        kind=kind,
        task=task,
        app=resolve_app(task)[:50],
        caller=(caller or "")[:100],
        provider=provider,
        role=provider.role if provider else "",
        model=provider.model if provider else "",
        upstream=(meta.get("upstream") or "")[:100],
        proxy=proxy,
        proxy_label=str(proxy) if proxy else "",
        ok=error is None,
        http_status=meta.get("http_status"),
        finish_reason=(usage.get("finish_reason") or "")[:50],
        error_kind=(meta.get("error_kind") or "")[:30],
        error=str(error) if error else "",
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
        reasoning_tokens=usage.get("reasoning_tokens"),
        duration_s=meta.get("duration_s"),
        # Через str, а не float: DecimalField от двоичной дроби получил бы хвост вроде
        # 0.00012299999999999999 и записал бы в поле не то число, которое назвал провайдер.
        cost=Decimal(str(cost)) if cost is not None else None,
        gen_id=(meta.get("gen_id") or "")[:100],
    )


def fetch_generation_cost(provider, gen_id, timeout=30):
    """Цена одной генерации у OpenRouter по её id. Возвращает float.

    Единственный способ узнать цену задним числом: в теле ответа она приходит только тем
    вызовам, которые уже ушли с usage.include. Своей строки в журнал не пишет — это
    справка о вызове, а не вызов; токены за неё не платятся.
    """
    headers = {"Authorization": f"Bearer {provider.api_key}"}
    data = post_json(
        f"https://openrouter.ai/api/v1/generation?id={quote(gen_id)}", headers, None, timeout,
        kind="OpenRouter /generation", where=f"по id генерации «{gen_id}»", method="GET",
    )
    body = data.get("data") if isinstance(data, dict) else None
    if not isinstance(body, dict):
        raise AIUnavailableError(
            f"OpenRouter /generation ответил без блока data (id «{gen_id}»). "
            f"Ответ: {str(data)[:1000]}"
        )
    # Имя поля проверяем явно, а не берём первое похожее: пустая цена и отсутствующее поле
    # — разные вещи, и молча превращать второе в null значит спрятать смену формата.
    for key in ("total_cost", "cost"):
        if body.get(key) is not None:
            try:
                return float(body[key])
            except (TypeError, ValueError):
                raise AIUnavailableError(
                    f"OpenRouter /generation вернул нечисловую цену {key}={body[key]!r} "
                    f"(id «{gen_id}»). Блок data: {str(body)[:1000]}"
                )
    raise AIUnavailableError(
        f"В ответе OpenRouter /generation нет ни total_cost, ни cost (id «{gen_id}») — "
        f"формат ответа изменился. Блок data целиком: {str(body)[:1000]}"
    )


def refreshable_costs():
    """Строки журнала, которым цену ещё можно добрать: цены нет, id генерации есть,
    провайдер жив и openrouter-овский.

    Один и тот же набор для счётчика на странице и для самой кнопки — иначе цифра
    обещала бы одно, а кнопка делала другое.
    """
    from .models import AICallLog

    return (AICallLog.objects.filter(cost__isnull=True)
            .exclude(gen_id="")
            .filter(provider__dialect=AIProvider.DIALECT_OPENROUTER)
            .select_related("provider"))


def refresh_costs(limit=200):
    """Добирает цену у OpenRouter по строкам журнала без цены. Один HTTP-запрос на строку.

    Возвращает {updated, left, errors}: `left` — сколько осталось необновлённых после
    прогона. Потолок нужен потому, что запросов ровно столько же, сколько строк, а у
    gunicorn есть таймаут; необработанный остаток называется вслух, а не отбрасывается.

    Ошибка одной строки не отменяет остальные, но и не теряется: все до единой уходят
    в errors и показываются пользователем.
    """
    updated = 0
    errors = []
    for row in refreshable_costs().order_by("-created_at")[:limit]:
        try:
            cost = fetch_generation_cost(row.provider, row.gen_id)
        except AIUnavailableError as e:
            errors.append(f"строка #{row.pk} ({row.created_at:%d.%m %H:%M}, id «{row.gen_id}»): {e}")
            continue
        row.cost = Decimal(str(cost))
        row.save(update_fields=["cost"])
        updated += 1
    return {"updated": updated, "left": refreshable_costs().count(), "errors": errors}


def resolve_task(key, name="", role="smart", temperature=None):
    """Задача → (task, provider, temperature). Регистрирует задачу при первом вызове.

    role/temperature из кода — это дефолты регистрации, «что попросил код». Они
    обновляются в реестре при каждом вызове (чтобы точка отсчёта не устаревала), но
    НИКОГДА не затирают переопределения пользователя.

    Выбор провайдера: тег задачи среди активных (по priority) → иначе роль с обычной
    цепочкой фолбэков. Температура: из реестра → из кода → из провайдера.
    """
    from .models import AITask

    task, created = AITask.objects.get_or_create(
        key=key,
        defaults={"name": name, "default_role": role, "default_temperature": temperature},
    )
    if not created:
        stale = {}
        if name and task.name != name:
            stale["name"] = name
        if task.default_role != role:
            stale["default_role"] = role
        if task.default_temperature != temperature:
            stale["default_temperature"] = temperature
        if stale:
            for field, value in stale.items():
                setattr(task, field, value)
            task.save(update_fields=list(stale))

    provider = None
    if task.tag:
        provider = AIProvider.objects.filter(is_active=True, tags__name=task.tag).first()
        if provider is None:
            # Тег — прицел в конкретный провайдер, а не пожелание. Уйти отсюда на роль
            # значило бы молча заменить выбранную модель другой и выставить за неё счёт:
            # в журнале это выглядит обычной успешной строкой, и заметить подмену нечем.
            # Провайдер мог быть выключен или переименован уже после настройки задачи, а
            # выпадашка тегов в UI собирается только по активным — строка в задаче при
            # этом остаётся и продолжает промахиваться.
            known = sorted(AIProviderTag.objects.filter(provider__is_active=True)
                           .values_list("name", flat=True).distinct())
            raise AIUnavailableError(
                f"Задача «{task}» (ключ {task.key}) настроена на провайдера с тегом "
                f"«{task.tag}», но активного провайдера с таким тегом нет. "
                f"Теги активных провайдеров: {', '.join(known) if known else '(ни одного)'}. "
                f"Включите нужный провайдер или смените тег задачи в разделе Настройки → "
                f"AI → Задачи. На роль «{task.effective_role}» вызов не уведён намеренно: "
                f"это была бы другая модель за другие деньги без единого следа."
            )
    if provider is None:
        provider = get_provider(task.effective_role)

    if task.temperature is not None:
        temp = task.temperature
    elif temperature is not None:
        temp = temperature
    else:
        temp = provider.temperature
    return task, provider, temp


def call_task(key, messages, *, name="", role="smart", temperature=None, max_tokens=16000,
              timeout=None, extra_payload=None):
    """Вызов через реестр задач: слой сам решает, каким провайдером и с какой температурой.

    Returns (content_str, usage_dict). `key` — стабильный слаг («atlas.tags.cleanup»),
    `name` — человеческая подпись для UI, `role`/`temperature` — дефолты регистрации.
    Всё остальное настраивается пользователем в UI задач (путь зависит от монтирования), без правки кода.
    """
    task, provider, temp = resolve_task(key, name=name, role=role, temperature=temperature)
    return _run_chat(provider, messages, max_tokens=max_tokens, timeout=timeout, temperature=temp,
                     extra_payload=extra_payload, caller=task.name or task.key, task=task)


def call(provider, messages, max_tokens=16000, timeout=None, temperature=None, extra_payload=None,
         caller=""):
    """DEPRECATED — переезжайте на call_task(). Пока работает без изменений.

    Здесь провайдера и температуру выбирает вызывающий, поэтому настроить задачу из UI
    нельзя: она не проходит через реестр. Останется до переезда всех потребителей.
    """
    return _run_chat(provider, messages, max_tokens=max_tokens, timeout=timeout,
                     temperature=temperature, extra_payload=extra_payload, caller=caller, task=None)


def _run_chat(provider, messages, max_tokens, timeout, temperature, extra_payload, caller, task):
    """Общий ствол обеих точек входа: вызов, журнал на успехе и на ошибке, факты в usage.

    В usage кладутся и то, что ушло (sent), и то, чем это обслужили (model/role/tag/
    temperature/upstream). После переезда на реестр провайдера и температуру выбирает
    слой — значит только слой и может сказать, что фактически сработало. И на успехе,
    и в usage упавшего исключения: при ошибке эти факты нужнее всего.
    """
    meta = {}
    sent = render_messages(messages)
    facts = {
        "sent": sent,
        "model": provider.model,
        "role": provider.role,
        # Чем сделан выбор: тег задачи или роль. Догадка кода тут не годится — реестр
        # меняется в рантайме.
        "tag": task.tag if task else "",
        "temperature": temperature if temperature is not None else provider.temperature,
    }
    # Приложение-инициатор уходит в X-Title, чтобы OpenRouter в Activity разбивал траты по
    # приложениям на общем ключе. Тем же значением пишется поле app в журнале (resolve_app).
    app = resolve_app(task)
    try:
        content, usage = _call_provider(provider, messages, max_tokens=max_tokens, timeout=timeout,
                                        temperature=temperature, extra_payload=extra_payload,
                                        meta=meta, app=app)
    except AIUnavailableError as e:
        e.usage.update(facts, upstream=meta.get("upstream", ""), retries=meta.get("retries", 0))
        # Сбой журнала виден пользователю, но не стирает ошибку AI: наружу уходят обе.
        try:
            row = _log_call("chat", provider, caller, meta, e.usage, error=e, task=task)
            e.usage["call_log_id"] = row.pk
        except Exception as log_err:
            raise AIUnavailableError(
                f"{e}\n\nКроме того, не удалось записать журнал вызовов (AICallLog): {log_err}"
            ) from e
        raise
    # retries — сколько прокси пришлось перебрать, чтобы запрос дошёл. В usage, а не только
    # в лог: удавшийся со второго прокси вызов иначе неотличим от прошедшего с первого, и
    # ретрай тихо съедал бы симптом вместо того, чтобы его показать.
    usage.update(facts, upstream=meta.get("upstream", ""), retries=meta.get("retries", 0))
    # call_log_id — ключ к строке журнала этого круга. Без него приложение не может собрать
    # свой ход (несколько вызовов подряд) из журнала: по caller туда попадают все ходы всех
    # сценариев сразу, а разделить их постфактум нечем.
    usage["call_log_id"] = _log_call("chat", provider, caller, meta, usage, task=task).pk
    return content, usage


def _gemini_tool_calls(parts):
    """functionCall из ответа Gemini → тот же формат, что у openai-совместимых.

    Приложение обязано разбирать ОДИН формат вызовов, а не по одному на провайдера:
    иначе нативные инструменты придётся писать заново под каждого, а смена провайдера
    в настройках молча ломала бы работающую роль.
    """
    calls = []
    for i, part in enumerate(parts):
        fc = part.get("functionCall") if isinstance(part, dict) else None
        if not fc:
            continue
        name = fc.get("name") or "?"
        try:
            arguments = json.dumps(fc.get("args") or {}, ensure_ascii=False)
        except (TypeError, ValueError):
            arguments = str(fc.get("args"))
        calls.append({
            "id": f"{name}:{i}",
            "type": "function",
            "function": {"name": name, "arguments": arguments},
        })
    return calls


def _tool_calls_not_requested(provider, tool_calls, data, usage):
    """Модель ответила вызовами инструментов, которых у неё не просили.

    Отдать «» такому вызывающему нельзя: он ждёт текст или JSON, получит пустоту и
    упадёт слоем ниже на «невалидный JSON: «»», где причина уже не видна. Поэтому
    ошибка — но называющая произошедшее и не теряющая вызовы: они лежат в usage,
    а usage едет вместе с исключением.
    """
    try:
        raw = json.dumps(data, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        raw = str(data)
    names = ", ".join(
        (c.get("function") or {}).get("name") or c.get("id") or "?" for c in tool_calls
    )
    return AIUnavailableError(
        f"Модель ответила нативными вызовами инструментов ({len(tool_calls)}: {names}) "
        f"вместо текста, хотя tools в запросе не передавались "
        f"(finish_reason={usage.get('finish_reason') or '—'}). "
        f"Модель: {provider.model} (роль «{provider.role}»). "
        f"Вызовы не потеряны — они в usage['tool_calls']. "
        f"Нужны вызовы — передайте tools в extra_payload, тогда слой вернёт их штатно, "
        f"без исключения. Не нужны — принудите модель к тексту или JSON (response_format) "
        f"либо смените модель.\n\n"
        f"Сырой ответ API:\n{raw[:3000]}",
        usage=usage,
    )


def _tokens_line(usage, max_tokens):
    """Картина упёршегося лимита: сколько ушло в промпт, сколько нагенерено (и сколько
    из этого съели рассуждения), где стоял потолок. Без reasoning неразличимы «модель
    написала простыню» и «модель ушла в рассуждение», без потолка — упёрлись мы в свой
    лимит или в модельный. Обе ветки finish_reason=length печатают её одинаково.
    """
    parts = []
    if "prompt_tokens" in usage:
        parts.append(f"промпт: {usage['prompt_tokens']} tok")
    if "completion_tokens" in usage:
        parts.append(f"completion: {usage['completion_tokens']} tok")
    if "reasoning_tokens" in usage:
        parts.append(f"из них reasoning: {usage['reasoning_tokens']} tok")
    parts.append(
        f"наш потолок: max_tokens={max_tokens}" if max_tokens
        else "свой max_tokens не слали — потолок модельный"
    )
    return f" Токены: {', '.join(parts)}."


def _call_provider(provider, messages, max_tokens=16000, timeout=None, temperature=None,
                   extra_payload=None, meta=None, app=""):
    """messages — канонический OpenAI-формат для всех провайдеров; content может
    быть строкой или списком частей ({"type": "text"} / {"type": "image_url",
    "image_url": {"url": "data:image/...;base64,..."}}). Для gemini content
    конвертируется, для openai/openrouter проходит насквозь.
    """
    meta = meta if meta is not None else {}
    ptype = provider_type(provider)
    effective_timeout = timeout if timeout is not None else provider.timeout
    temperature = temperature if temperature is not None else provider.temperature

    if ptype == "gemini":
        headers = {"Content-Type": "application/json", "x-goog-api-key": provider.api_key}
        url = provider.base_url.rstrip("/")
        if ":generateContent" not in url:
            if "v1beta" not in url and "v1" not in url:
                url = f"{url}/v1beta"
            if url.endswith("/models"):
                url = url[:-len("/models")]
            url = f"{url}/models/{provider.model}:generateContent"
        system_instruction = None
        contents = []
        for m in messages:
            if m["role"] == "system":
                system_instruction = {"parts": _gemini_parts(m["content"])}
            elif m["role"] == "user":
                contents.append({"role": "user", "parts": _gemini_parts(m["content"])})
            elif m["role"] == "assistant":
                contents.append({"role": "model", "parts": _gemini_parts(m["content"])})
        gen_config = {"temperature": temperature}
        if max_tokens:
            gen_config["maxOutputTokens"] = max_tokens
        payload = {"contents": contents, "generationConfig": gen_config}
        if system_instruction:
            payload["systemInstruction"] = system_instruction
    elif ptype == "openrouter":
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {provider.api_key}",
            "HTTP-Referer": "https://aicore.local",
            "X-Title": app,
        }
        url = "https://openrouter.ai/api/v1/chat/completions"
        payload = {"model": provider.model, "messages": messages, "temperature": temperature}
        if max_tokens:
            payload["max_tokens"] = max_tokens
        # Без этого OpenRouter цену в ответе не присылает вовсе. Считать её самим (прайс
        # модели × токены) нельзя: кэш промпта, BYOK и разные хостеры дают другое число.
        payload["usage"] = {"include": True}
    else:
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {provider.api_key}"}
        url = provider.base_url
        payload = {"model": provider.model, "messages": messages, "temperature": temperature}
        if max_tokens:
            payload["max_tokens"] = max_tokens

    if extra_payload:
        payload.update(extra_payload)

    t0 = time.monotonic()
    data = post_json(
        url, headers, payload, effective_timeout,
        kind="AI API",
        where=f"от модели «{provider.model}» (роль «{provider.role}», {url})",
        meta=meta,
    )
    logger.debug("AI raw response: %s", data)

    # Ответ пришёл с HTTP 200, но отказать мог сам хостер, которому OpenRouter отдал
    # запрос: тогда причина лежит в choices[0].error, а имя хостера — в data["provider"].
    if isinstance(data, dict):
        meta["upstream"] = data.get("provider") or ""
        try:
            body_error = data["choices"][0].get("error") or {}
        except (KeyError, IndexError, AttributeError, TypeError):
            body_error = {}
        if body_error:
            meta["error_kind"] = (body_error.get("metadata") or {}).get("error_type") or "upstream_error"

    usage = {"elapsed_s": round(time.monotonic() - t0, 1)}
    if ptype == "gemini":
        u = data.get("usageMetadata", {})
        if u.get("promptTokenCount") is not None:
            usage["prompt_tokens"] = u["promptTokenCount"]
        if u.get("candidatesTokenCount") is not None:
            usage["completion_tokens"] = u["candidatesTokenCount"]
    else:
        u = data.get("usage", {})
        if u.get("prompt_tokens") is not None:
            usage["prompt_tokens"] = u["prompt_tokens"]
        if u.get("completion_tokens") is not None:
            usage["completion_tokens"] = u["completion_tokens"]
        details = u.get("completion_tokens_details") or {}
        if details.get("reasoning_tokens"):
            usage["reasoning_tokens"] = details["reasoning_tokens"]
        if ptype == "openrouter":
            # Цена и id генерации — OpenRouter-специфичные поля: у openai-совместимых их
            # нет, и подставлять туда нечего. В usage кладём float, а не Decimal: usage
            # уезжает в фоновую задачу через json.dump, а Decimal не сериализуется — тред
            # умер бы молча, оставив задачу вечно «running».
            meta["gen_id"] = data.get("id") or ""
            if u.get("cost") is not None:
                try:
                    usage["cost"] = float(u["cost"])
                except (TypeError, ValueError):
                    raise AIUnavailableError(
                        f"OpenRouter вернул нечисловую стоимость: usage.cost={u['cost']!r}. "
                        f"Блок usage целиком: {u}. Модель: {provider.model}.",
                        usage=usage,
                    )

    if ptype == "gemini":
        try:
            fr = data["candidates"][0].get("finishReason")
            if fr:
                usage["finish_reason"] = fr
        except (KeyError, IndexError):
            pass
    else:
        try:
            fr = data["choices"][0].get("finish_reason")
            if fr:
                usage["finish_reason"] = fr
        except (KeyError, IndexError):
            pass

    # Инструменты просил сам вызывающий — по этому и различаем, кому пустой ответ с
    # вызовами является ответом, а кому поломкой. Ключ «tools» одинаков у обоих
    # диалектов, так что признак один на все ветки.
    tools_requested = bool((extra_payload or {}).get("tools"))

    if ptype == "gemini":
        try:
            parts = data["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError, TypeError):
            raise AIUnavailableError(f"Неожиданный ответ Gemini: {data}", usage=usage)
        if usage.get("finish_reason") == "MAX_TOKENS":
            raise AIUnavailableError(
                f"Модель обрубила ответ по лимиту токенов (finishReason=MAX_TOKENS): "
                f"сгенерировано {usage.get('completion_tokens', '?')} токенов, ответ неполный. "
                f"Модель: {provider.model} (роль «{provider.role}»).",
                usage=usage,
            )

        tool_calls = _gemini_tool_calls(parts)
        if tool_calls:
            usage["tool_calls"] = tool_calls
        text_parts = [p["text"] for p in parts if isinstance(p, dict) and "text" in p]
        text = "".join(text_parts) if text_parts else None

        if tool_calls and tools_requested:
            return text or "", usage
        if text is None:
            if tool_calls:
                raise _tool_calls_not_requested(provider, tool_calls, data, usage)
            raise AIUnavailableError(f"Неожиданный ответ Gemini: {data}", usage=usage)
        return text, usage

    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError):
        raise AIUnavailableError(f"Неожиданный ответ API: {data}", usage=usage)
    # Через get, а не по ключу: отвечая вызовами инструментов, одни провайдеры шлют
    # content: null, другие не шлют ключ вовсе. Оба варианта штатные и разбираются ниже.
    content = message.get("content") if isinstance(message, dict) else None

    if isinstance(content, list):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )

    # Вызовы инструментов — второй штатный вид ответа, а не поломка. В usage кладём их
    # ВСЕГДА, в том числе когда следом падаем: содержательную часть ответа слой терять
    # не имеет права, а usage доезжает до вызывающего и внутри исключения.
    tool_calls = (message.get("tool_calls") or []) if isinstance(message, dict) else []
    if tool_calls:
        usage["tool_calls"] = tool_calls
        if tools_requested:
            return content or "", usage
        if content is None:
            raise _tool_calls_not_requested(provider, tool_calls, data, usage)

    if content is None:
        choice = data["choices"][0]
        msg = choice.get("message", {})
        refusal = msg.get("refusal") or ""
        reasoning = msg.get("reasoning_content") or ""
        finish = choice.get("finish_reason", "")
        if finish == "length":
            hint = ""
            if reasoning:
                hint = f" Модель потратила {len(reasoning)} симв. на внутренние рассуждения и не успела выдать ответ."
            usage_hint = _tokens_line(usage, max_tokens)
            raise AIUnavailableError(
                f"Модель исчерпала лимит токенов (finish_reason=length) и не вернула ответ."
                f"{hint}{usage_hint} Модель: {provider.model}.",
                usage=usage,
            )
        detail_parts = []
        if refusal:
            detail_parts.append(f"refusal: {refusal}")
        if finish:
            detail_parts.append(f"finish_reason={finish}")
        if reasoning:
            detail_parts.append(f"reasoning_content (первые 300 симв.): {reasoning[:300]}")
        try:
            raw = json.dumps(data, ensure_ascii=False, indent=2)
        except Exception:
            raw = str(data)
        raise AIUnavailableError(
            f"AI вернул content=null. {' | '.join(detail_parts) or 'без деталей'}. "
            f"Модель: {provider.model} (роль «{provider.role}»).\n\n"
            f"Сырой ответ API:\n{raw[:3000]}",
            usage=usage,
        )

    if usage.get("finish_reason") == "length":
        raise AIUnavailableError(
            f"Модель обрубила ответ по лимиту токенов (finish_reason=length), ответ неполный."
            f"{_tokens_line(usage, max_tokens)} "
            f"Модель: {provider.model} (роль «{provider.role}»).",
            usage=usage,
        )

    return content, usage


def escape_control_chars_in_strings(text):
    """Escape literal control characters inside JSON string values (common LLM output error)."""
    result = []
    in_str = False
    esc = False
    for ch in text:
        if esc:
            esc = False
            result.append(ch)
        elif ch == '\\' and in_str:
            esc = True
            result.append(ch)
        elif ch == '"':
            in_str = not in_str
            result.append(ch)
        elif in_str and ch == '\n':
            result.append('\\n')
        elif in_str and ch == '\r':
            result.append('\\r')
        elif in_str and ch == '\t':
            result.append('\\t')
        else:
            result.append(ch)
    return ''.join(result)


def _parse_json_diagnostics(text, usage):
    """Диагностика к неудачному разбору: чем обслужен вызов и оборван ли ответ.

    Обрубленный ответ по тексту ошибки неотличим от битого JSON, и по такому
    сообщению чинят не то — правят промпт про кавычки, когда упёрлись в лимит
    вывода. Вывод об обрыве делается по самому тексту, а не по finish_reason:
    поля может не быть, а может прийти «stop» при обрезанном по дороге теле.
    """
    usage = usage or {}
    parts = [f"finish_reason={usage.get('finish_reason') or 'не сообщён'}"]
    if "completion_tokens" in usage:
        parts.append(f"токенов в ответе {usage['completion_tokens']}")
    if usage.get("model"):
        parts.append(f"модель {usage['model']}")
    parts.append(f"длина ответа {len(text or '')} знаков")
    out = "\n\nДиагностика вызова: " + ", ".join(parts) + "."
    if text and not text.rstrip().endswith(("}", "]")):
        out += (
            "\nОтвет не закрыт скобкой — он оборван на полуслове, а не сломан по синтаксису. "
            "Причину искать в лимите вывода или в транспорте, а не в разметке промпта."
        )
    return out


def parse_json(text, usage=None):
    if not text:
        raise AIUnavailableError(
            "AI вернул пустой ответ." + _parse_json_diagnostics(text, usage),
            usage=usage,
        )
    text = text.strip()
    if "```" in text:
        m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
        if m:
            text = m.group(1).strip()
    first_bracket = text.find("[")
    first_brace = text.find("{")
    if first_bracket != -1 and (first_brace == -1 or first_bracket < first_brace):
        order = [("[", "]"), ("{", "}")]
    else:
        order = [("{", "}"), ("[", "]")]
    for start_ch, end_ch in order:
        start = text.find(start_ch)
        if start == -1:
            continue
        depth = 0
        in_str = False
        esc = False
        for i, ch in enumerate(text[start:], start):
            if esc:
                esc = False
                continue
            if ch == "\\" and in_str:
                esc = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == start_ch:
                depth += 1
            elif ch == end_ch:
                depth -= 1
                if depth == 0:
                    candidate = text[start:i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        try:
                            return json.loads(escape_control_chars_in_strings(candidate))
                        except json.JSONDecodeError:
                            break
    err = AIUnavailableError(
        f"AI вернул невалидный JSON: «{text[:300]}»" + _parse_json_diagnostics(text, usage),
        usage=usage,
    )
    err.raw = text
    raise err


def get_embeddings_batch(texts, batch_size=100, caller=""):
    """
    texts: list of str
    Возвращает (list of (vector | None), error_str | None).
    Бьёт на батчи по batch_size если нужно.
    Каждый батч — отдельная строка в журнале вызовов (AICallLog).
    """
    provider = AIProvider.objects.filter(is_active=True, role="embed").first()
    if not provider:
        return [None] * len(texts), "Нет активного провайдера с ролью «эмбеддинг»"

    base = provider.base_url.rstrip("/")
    url = base if base.endswith("/embeddings") else base + "/embeddings"
    headers = {"Authorization": f"Bearer {provider.api_key}", "Content-Type": "application/json"}
    where = f"от модели «{provider.model}» ({url})"

    result = [None] * len(texts)
    for start in range(0, len(texts), batch_size):
        chunk = texts[start:start + batch_size]
        meta = {}
        try:
            body = post_json(
                url, headers, {"model": provider.model, "input": chunk}, 60,
                kind="embedding API", where=where, meta=meta,
            )
        except AIUnavailableError as e:
            try:
                _log_call("embedding", provider, caller, meta, {}, error=e)
            except Exception as log_err:
                return result, f"{e}\n\nКроме того, не удалось записать журнал вызовов (AICallLog): {log_err}"
            return result, str(e)
        usage = body.get("usage") or {}
        _log_call("embedding", provider, caller, meta,
                  {"prompt_tokens": usage.get("prompt_tokens")})
        items = body.get("data", [])
        if not items:
            return result, f"Пустой ответ от embedding API: {body}"
        for i, item in enumerate(items):
            idx = item.get("index", i)
            result[start + idx] = item.get("embedding")

    return result, None


def cosine_similarity(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)
