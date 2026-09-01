import logging

from django.core.cache import cache

from .core import post_json

logger = logging.getLogger(__name__)

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
CACHE_KEY = "aicore:openrouter:pricing"
CATALOG_KEY = "aicore:openrouter:catalog"
CACHE_TTL = 6 * 3600


def _parse_pricing(raw):
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _parse_intelligence(item):
    try:
        return float(item["benchmarks"]["artificial_analysis"]["intelligence_index"])
    except Exception:
        return None


def fetch_openrouter_catalog():
    """Тянет https://openrouter.ai/api/v1/models и возвращает {model_id: entry}.

    entry — {"prompt": float, "completion": float, "intelligence": float|None, "name": str}
    Intelligence — Artificial Analysis index из benchmarks.artificial_analysis
    (160 из 420 моделей имеют балл, остальные — None, это не мусор а «нет оценки»).
    Идёт через общий транспорт post_json — с прокси и ретраем.
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
        result[mid] = {
            "prompt": prompt,
            "completion": completion,
            "intelligence": _parse_intelligence(item),
            "name": item.get("name", ""),
        }
    return result


def fetch_openrouter_pricing():
    """Совместимость: только цены из каталога."""
    catalog = fetch_openrouter_catalog()
    return {k: {"prompt": v["prompt"], "completion": v["completion"]} for k, v in catalog.items()}


def get_openrouter_catalog(force_refresh=False):
    if not force_refresh:
        cached = cache.get(CATALOG_KEY)
        if cached is not None:
            return cached
        # fallback со старого ключа
        old = cache.get(CACHE_KEY)
        if isinstance(old, dict) and old and isinstance(next(iter(old.values())), dict) and "intelligence" in next(iter(old.values())):
            return old
    try:
        catalog = fetch_openrouter_catalog()
    except Exception as e:
        logger.warning("aicore: не удалось получить каталог OpenRouter: %s", e)
        cached = cache.get(CATALOG_KEY)
        if cached is not None:
            return cached
        old = cache.get(CACHE_KEY)
        if old is not None:
            return old
        return {}
    cache.set(CATALOG_KEY, catalog, CACHE_TTL)
    cache.set(CACHE_KEY, catalog, CACHE_TTL)
    return catalog


def get_openrouter_pricing(force_refresh=False):
    """Кэшированный доступ к ценам. TTL 6ч."""
    catalog = get_openrouter_catalog(force_refresh=force_refresh)
    return {k: {"prompt": v["prompt"], "completion": v["completion"]} for k, v in catalog.items()}


def pricing_for_model(model_id, pricing_map=None):
    if pricing_map is None:
        pricing_map = get_openrouter_pricing()
    return pricing_map.get(model_id)


def _resolve_entry(model_id, catalog):
    """Точный id или суффикс после '/' — glass box для bare имён вроде 'claude-fable-5'."""
    if model_id in catalog:
        return catalog[model_id]
    # bare name без префикса: ищем id, оканчивающийся на '/' + model_id
    if "/" not in model_id:
        for mid, entry in catalog.items():
            if mid.endswith("/" + model_id):
                return entry
    # префикс '~' у автораутинга OpenRouter: '~anthropic/...' — пробуем без тильды
    if model_id.startswith("~"):
        cand = model_id[1:]
        if cand in catalog:
            return catalog[cand]
    return None


def catalog_for_model(model_id, catalog=None):
    if catalog is None:
        catalog = get_openrouter_catalog()
    return _resolve_entry(model_id, catalog)


def format_price(per_token):
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


def tier_of(intelligence):
    """Класс по интеллекту: бакет 10 пунктов. 160 моделей с баллом 5.5–63.1."""
    if intelligence is None:
        return None
    return int(intelligence // 10) * 10


def tier_label(tier):
    if tier is None:
        return "без оценки"
    return f"{tier}–{tier+10}"


def _avg_price(entry):
    return (entry["prompt"] + entry["completion"]) / 2


def cheaper_in_tier(model_id, catalog=None, limit=5):
    """Модели дешевле текущей в том же тире, отсорт. по интеллекту (лучшие сначала)."""
    if catalog is None:
        catalog = get_openrouter_catalog()
    cur = _resolve_entry(model_id, catalog)
    if not cur or cur.get("intelligence") is None:
        return []
    tier = tier_of(cur["intelligence"])
    if tier is None:
        return []
    cur_avg = _avg_price(cur)
    candidates = []
    for mid, entry in catalog.items():
        if entry.get("intelligence") is None:
            continue
        if tier_of(entry["intelligence"]) != tier:
            continue
        # :free — лимит 20 req/min, :batch — батч-API с другой латентностью, 0/0 — не для продакшена
        if mid.endswith(":free") or mid.endswith(":batch") or (entry["prompt"] == 0 and entry["completion"] == 0):
            continue
        if mid == model_id or mid.endswith("/" + model_id):
            continue
        avg = _avg_price(entry)
        if avg >= cur_avg:
            continue
        saving = (1 - avg / cur_avg) * 100 if cur_avg else 0
        candidates.append({
            "id": mid,
            "name": entry.get("name", mid),
            "prompt": entry["prompt"],
            "completion": entry["completion"],
            "prompt_str": format_price(entry["prompt"]),
            "completion_str": format_price(entry["completion"]),
            "intelligence": entry["intelligence"],
            "tier": tier,
            "avg": avg,
            "saving": saving,
        })
    candidates.sort(key=lambda x: (-x["intelligence"], x["avg"]))
    return candidates[:limit]
