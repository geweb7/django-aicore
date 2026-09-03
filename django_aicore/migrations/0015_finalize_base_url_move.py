# Часть 2/2 переноса endpoint (см. 0014): снять старую колонку с AIModel и закрыть
# Provider.base_url от пустых значений — отдельной транзакцией от 0014, где были
# DML на aicore_aimodel. В одной транзакции Postgres не даёт ALTER TABLE следом за
# UPDATE той же таблицы («pending trigger events»).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('aicore', '0014_move_base_url_to_provider'),
    ]

    operations = [
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
