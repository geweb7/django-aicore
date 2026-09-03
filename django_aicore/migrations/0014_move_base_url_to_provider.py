# Перенос API endpoint с AIModel на Provider — новой миграцией, а не правкой 0011/0013.
#
# Правка уже применённых миграций 0011 (add-в-CreateModel) и 0013 (RemoveField) — та самая
# ошибка: Django помнит применённые миграции по имени, а не по содержимому, и на базах,
# где 0011/0013 уже прошли (psi), поздние правки внутри тех же файлов НИКОГДА не
# выполняются — `migrate` их просто пропускает, база расходится с текущим кодом
# (`column aicore_provider.base_url does not exist`). Поэтому 0011-0013 — как были
# опубликованы, а весь перенос endpoint — здесь, одной и той же логикой для всех: и для
# проекта, где 0001-0013 уже накатаны, и для чистой установки, где всё применится подряд.
#
# base_url собирается по большинству среди AIModel одного Provider: несколько моделей
# честно ссылались на разные endpoint (например, отдельный адрес под /embeddings) — такое
# значение не отбрасывается, а уезжает на новый Provider с тем же диалектом и копией
# ключа (тот же ключ, другой endpoint), и видно потом на странице «Провайдеры».

from collections import Counter
from urllib.parse import urlparse

from django.db import migrations, models


def move_base_url_to_provider(apps, schema_editor):
    Provider = apps.get_model("aicore", "Provider")
    AIApiKey = apps.get_model("aicore", "AIApiKey")
    AIModel = apps.get_model("aicore", "AIModel")

    def suffix_for(url):
        path = urlparse(url).path.strip("/")
        return path.rsplit("/", 1)[-1] if path else (urlparse(url).hostname or url)

    for provider in Provider.objects.all():
        models_here = list(
            AIModel.objects.filter(api_key__provider=provider).exclude(base_url="").order_by("id"))
        if not models_here:
            # Ни одной модели с заполненным endpoint под этим провайдером — оставляем
            # base_url пустым, а не гадаем: дозаполнит владелец на странице «Провайдеры».
            continue

        primary_url = Counter(m.base_url for m in models_here).most_common(1)[0][0]
        provider.base_url = primary_url
        provider.save(update_fields=["base_url"])

        split_by_url = {}
        for m in models_here:
            if m.base_url == primary_url:
                continue
            split_provider = split_by_url.get(m.base_url)
            if split_provider is None:
                split_provider = Provider.objects.create(
                    name=f"{provider.name} ({suffix_for(m.base_url)})",
                    dialect=provider.dialect,
                    base_url=m.base_url,
                )
                split_by_url[m.base_url] = split_provider
            old_key = m.api_key
            new_key = AIApiKey.objects.create(key=old_key.key, name=old_key.name, provider=split_provider)
            m.api_key = new_key
            m.save(update_fields=["api_key"])


class Migration(migrations.Migration):

    dependencies = [
        ('aicore', '0013_finalize_model_rename'),
    ]

    operations = [
        migrations.AddField(
            model_name='provider',
            name='base_url',
            field=models.URLField(blank=True, default='', verbose_name='API endpoint'),
        ),
        migrations.RunPython(move_base_url_to_provider, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name='aimodel',
            name='base_url',
        ),
        migrations.AlterField(
            model_name='provider',
            name='base_url',
            field=models.URLField(verbose_name='API endpoint'),
        ),
    ]
