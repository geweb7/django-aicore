# Часть 3/3: срез старых полей и переименования. Отдельным ходом от 0012 — здесь уже
# нет данных, которые можно потерять, поэтому схемные операции идут без RunPython.
#
# AIProvider → AIModel, AIProviderTag → AIModelTag: старое имя обозначало провайдера API
# (openai/openrouter/gemini), а на деле это конкретная сконфигурированная модель —
# endpoint, роль, приоритет. Тем именем теперь называется новая сущность Provider.

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('aicore', '0012_link_keys_to_providers'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='aiprovider',
            name='api_key',
        ),
        migrations.RenameField(
            model_name='aiprovider',
            old_name='api_key_new',
            new_name='api_key',
        ),
        migrations.AlterField(
            model_name='aiprovider',
            name='api_key',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='ai_models', to='aicore.aiapikey', verbose_name='API ключ'),
        ),
        migrations.RemoveField(
            model_name='aiprovider',
            name='dialect',
        ),
        migrations.RenameModel(
            old_name='AIProvider',
            new_name='AIModel',
        ),
        migrations.AlterModelOptions(
            name='aimodel',
            options={'ordering': ['priority', 'id'], 'verbose_name': 'AI модель', 'verbose_name_plural': 'AI модели'},
        ),
        migrations.RenameModel(
            old_name='AIProviderTag',
            new_name='AIModelTag',
        ),
        migrations.RenameField(
            model_name='aimodeltag',
            old_name='provider',
            new_name='ai_model',
        ),
        migrations.AlterUniqueTogether(
            name='aimodeltag',
            unique_together={('ai_model', 'name')},
        ),
        migrations.AlterModelOptions(
            name='aimodeltag',
            options={'verbose_name': 'Тег модели', 'verbose_name_plural': 'Теги модели'},
        ),
        migrations.RenameField(
            model_name='aicalllog',
            old_name='provider',
            new_name='ai_model',
        ),
        migrations.AlterField(
            model_name='aicalllog',
            name='ai_model',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='aicore.aimodel', verbose_name='Модель'),
        ),
        migrations.AlterField(
            model_name='aitask',
            name='tag',
            field=models.CharField(blank=True, max_length=100, verbose_name='Тег модели'),
        ),
    ]
