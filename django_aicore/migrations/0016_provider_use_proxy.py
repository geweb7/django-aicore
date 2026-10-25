# Флаг на Provider: default True — текущее поведение всех уже заведённых провайдеров.
# Значение дальше только из поля (форма), без правок по имени, диалекту или URL.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("aicore", "0015_finalize_base_url_move"),
    ]

    operations = [
        migrations.AddField(
            model_name="provider",
            name="use_proxy",
            field=models.BooleanField(default=True, verbose_name="Использовать прокси"),
        ),
    ]
