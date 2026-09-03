# Часть 2/3: сама привязка. Существующие AIProvider группируются по (dialect, api_key) —
# один Provider на диалект, один AIApiKey на уникальное значение ключа внутри диалекта,
# иначе одинаковый ключ, использованный в нескольких провайдерах, задвоился бы по числу
# строк вместо переиспользования (в чём и был смысл выноса).
#
# Имя провайдера при миграции — подпись диалекта из старого AIProvider.DIALECT_CHOICES:
# человеческого названия («мой ключ OpenRouter») никогда не было, оно появится только у
# ключей, заведённых руками после этого хода. Имя ключа — маска mask_key_name (первые 12
# + «…» + последние 3 символа), тем же расчётом, что и для новых ключей в форме (SSOT).

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

    providers_by_dialect = {}
    keys_by_dialect_and_value = {}

    for row in AIProvider.objects.all():
        provider = providers_by_dialect.get(row.dialect)
        if provider is None:
            provider = Provider.objects.create(
                name=dialect_labels.get(row.dialect, row.dialect or "?"), dialect=row.dialect)
            providers_by_dialect[row.dialect] = provider

        cache_key = (row.dialect, row.api_key)
        api_key = keys_by_dialect_and_value.get(cache_key)
        if api_key is None:
            api_key = AIApiKey.objects.create(
                key=row.api_key, name=mask_key_name(row.api_key), provider=provider)
            keys_by_dialect_and_value[cache_key] = api_key

        row.api_key_new_id = api_key.pk
        row.save(update_fields=["api_key_new"])


class Migration(migrations.Migration):

    dependencies = [
        ('aicore', '0011_provider_aiapikey'),
    ]

    operations = [
        migrations.RunPython(link_keys_to_providers, migrations.RunPython.noop),
    ]
