import logging

from django.core.cache import cache

from .core import post_json

logger = logging.getLogger(__name__)

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
CACHE_KEY = "aicore:openrouter:pricing"
CACHE_TTL = 6 * 3600


def _parse_pricing(raw):
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def fetch_openrouter_pricing():
    """Тянет https://openrouter.ai/api/v1/models и возвращает {model_id: pricing}.

    pricing — {prompt, completion} как float $/токен (строки из API).
    Идёт через общий транспорт post_json — с прокси и ретраем, как любой AI-вызов.
    Ошибки не глотаются молча: вызывающий решает, показывать ли «нет данных».
    """
    data = post_json(
        OPENROUTER_MODELS_URL,
        headers={},
        payload=None,
        timeout=15,
        kind="OpenRouter models",
        where="список моделей",
        method="GET",
    )
    result = {}
    for item in data.get("data") or []:
        mid = item.get("id")
        pricing = item.get("pricing") or {}
        if not mid or not isinstance(pricing, dict):
            continue
        prompt = _parse_pricing(pricing.get("prompt"))
        completion = _parse_pricing(pricing.get("completion"))
        if prompt is None or completion is None:
            continue
        result[mid] = {"prompt": prompt, "completion": completion}
    return result


def get_openrouter_pricing(force_refresh=False):
    """Кэшированный доступ к ценам OpenRouter. TTL 6ч, force_refresh — мимо кэша."""
    if not force_refresh:
        cached = cache.get(CACHE_KEY)
        if cached is not None:
            return cached
    try:
        pricing = fetch_openrouter_pricing()
    except Exception as e:
        logger.warning("aicore: не удалось получить цены OpenRouter: %s", e)
        cached = cache.get(CACHE_KEY)
        if cached is not None:
            return cached
        return {}
    cache.set(CACHE_KEY, pricing, CACHE_TTL)
    return pricing


def pricing_for_model(model_id, pricing_map=None):
    """Цена одной модели из карты или из кэша."""
    if pricing_map is None:
        pricing_map = get_openrouter_pricing()
    return pricing_map.get(model_id)


def format_price(per_token):
    """$/токен → $/M токенов строкой. 0 → 'бесплатно', None → '—'."""
    if per_token is None:
        return "—"
    if per_token == 0:
        return "0"
    per_m = per_token * 1_000_000
    if per_m >= 1:
        return f"${per_m:.2f}"
    if per_m >= 0.01:
        return f"${per_m:.3f}"
    return f"${per_m:.4f}"
