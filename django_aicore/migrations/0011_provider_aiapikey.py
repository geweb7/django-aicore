# Часть 1/3 выноса ключа: новые модели Provider/AIApiKey + временное поле-мост на
# AIProvider. Сама привязка существующих провайдеров к ключам — в 0012 (данные),
# срез старых полей и переименования — в 0013 (схема). Три шага, а не один: RunPython
# в 0012 должен видеть на входе уже созданные таблицы, а на выходе — ещё старый
# AIProvider.api_key (строку), чтобы было что переносить.

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('aicore', '0010_aicalllog_app'),
    ]

    operations = [
        migrations.CreateModel(
            name='Provider',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100, verbose_name='Название')),
                ('dialect', models.CharField(choices=[('openai', 'OpenAI-совместимый (POST на указанный endpoint)'), ('gemini', 'Gemini (путь достраивается)'), ('openrouter', 'OpenRouter (адрес фиксирован в коде)')], max_length=20, verbose_name='Диалект API')),
            ],
            options={
                'verbose_name': 'Провайдер',
                'verbose_name_plural': 'Провайдеры',
                'ordering': ['name'],
            },
        ),
        migrations.CreateModel(
            name='AIApiKey',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('key', models.CharField(max_length=500, verbose_name='Ключ')),
                ('name', models.CharField(blank=True, max_length=100, verbose_name='Название')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Добавлен')),
                ('provider', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='api_keys', to='aicore.provider', verbose_name='Провайдер')),
            ],
            options={
                'verbose_name': 'AI ключ',
                'verbose_name_plural': 'AI ключи',
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddField(
            model_name='aiprovider',
            name='api_key_new',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT, related_name='ai_models', to='aicore.aiapikey', verbose_name='API ключ'),
        ),
    ]
