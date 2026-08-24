from django.db import migrations, models


def fill_dialect(apps, schema_editor):
    """Проставляет диалект существующим строкам — ровно то, что до сих пор выводилось
    из base_url на лету. Правило переносится сюда один раз и больше нигде не живёт:
    дальше диалект выбирает администратор явно, а не угадывает код при каждом вызове.
    """
    AIProvider = apps.get_model("aicore", "AIProvider")
    for provider in AIProvider.objects.all():
        url = (provider.base_url or "").lower()
        if "generativelanguage.googleapis.com" in url and "openai" not in url:
            provider.dialect = "gemini"
        elif "openrouter.ai" in url:
            provider.dialect = "openrouter"
        else:
            provider.dialect = "openai"
        provider.save(update_fields=["dialect"])


class Migration(migrations.Migration):

    dependencies = [
        ("aicore", "0006_aiprovider_priority_unique"),
    ]

    operations = [
        migrations.AddField(
            model_name="aiprovider",
            name="dialect",
            field=models.CharField(default="", max_length=20, verbose_name="Диалект API"),
            preserve_default=False,
        ),
        migrations.RunPython(fill_dialect, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="aiprovider",
            name="dialect",
            field=models.CharField(
                choices=[
                    ("openai", "OpenAI-совместимый (POST на указанный endpoint)"),
                    ("gemini", "Gemini (путь достраивается)"),
                    ("openrouter", "OpenRouter (адрес фиксирован в коде)"),
                ],
                max_length=20,
                verbose_name="Диалект API",
            ),
        ),
    ]
