# Часть 2/3: сама привязка. Существующие AIProvider группируются по (dialect, base_url) —
# один Provider на уникальную пару, один AIApiKey на уникальное значение ключа внутри неё.
# base_url входит в ключ группировки наравне с dialect: два openai-совместимых провайдера
# с РАЗНЫМИ endpoint (например, OpenAI и самохостнутый прокси) — это два разных
# провайдера, и группировать их по одному диалекту значило бы слить два разных сервиса
# в один Provider с одним endpoint, который не подойдёт ни одному из них.
#
# Имя провайдера при миграции — подпись диалекта из старого AIProvider.DIALECT_CHOICES,
# с хостом endpoint в скобках при коллизии (несколько провайдеров одного диалекта):
# человеческого названия («мой ключ OpenRouter») никогда не было, оно появится только у
# ключей и провайдеров, заведённых руками после этого хода. Имя ключа — маска
# mask_key_name (первые 12 + «…» + последние 3 символа), тем же расчётом, что и для
# новых ключей в форме (SSOT).

from urllib.parse import urlparse

from django.db import migrations


def link_keys_to_providers(apps, schema_editor):
    from django_aicore.models import mask_key_name

    AIProvider = apps.get_model("aicore", "AIProvider")
    Provider = apps.get_model("aicore", "Provider")
    AIApiKey = apps.get_model("aicore", "AIApiKey")

    dialect_labels = {
        "openai": "OpenAI-совместимый (POST на указанный endpoint)",
        "gemini": "Gemini (путь достраивается)",
        "openrouter": "OpenRouter (адрес фиксирован в коде)",
    }

    rows = list(AIProvider.objects.all())

    # Коллизия — больше одного различного base_url на один dialect: имени тогда нужен
    # хост endpoint, иначе в списке провайдеров окажутся две одинаковые подписи.
    base_urls_by_dialect = {}
    for row in rows:
        base_urls_by_dialect.setdefault(row.dialect, set()).add(row.base_url)

    providers_by_key = {}
    keys_by_provider_and_value = {}

    for row in rows:
        provider_key = (row.dialect, row.base_url)
        provider = providers_by_key.get(provider_key)
        if provider is None:
            label = dialect_labels.get(row.dialect, row.dialect or "?")
            if len(base_urls_by_dialect.get(row.dialect, ())) > 1:
                host = urlparse(row.base_url or "").hostname or row.base_url or "?"
                label = f"{label} ({host})"
            provider = Provider.objects.create(name=label, dialect=row.dialect, base_url=row.base_url)
            providers_by_key[provider_key] = provider

        cache_key = (provider_key, row.api_key)
        api_key = keys_by_provider_and_value.get(cache_key)
        if api_key is None:
            api_key = AIApiKey.objects.create(
                key=row.api_key, name=mask_key_name(row.api_key), provider=provider)
            keys_by_provider_and_value[cache_key] = api_key

        row.api_key_new_id = api_key.pk
        row.save(update_fields=["api_key_new"])


class Migration(migrations.Migration):

    dependencies = [
        ('aicore', '0011_provider_aiapikey'),
    ]

    operations = [
        migrations.RunPython(link_keys_to_providers, migrations.RunPython.noop),
    ]
