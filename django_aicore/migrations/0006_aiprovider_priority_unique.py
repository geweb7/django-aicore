from django.db import migrations, models


def renumber_duplicates(apps, schema_editor):
    """Разводит совпадающие priority, СОХРАНЯЯ текущий порядок выбора.

    Идём в том же порядке, в котором провайдера выбирает get_provider — (priority, id), —
    и делаем последовательность строго возрастающей: значение поднимается только если
    оно не больше предыдущего. Относительный порядок не меняется ни для одной пары,
    значит ни для одной роли не меняется победитель; трогаются только строки-дубли.
    """
    AIProvider = apps.get_model("django_aicore", "AIProvider")
    last = -1
    for provider in AIProvider.objects.order_by("priority", "id"):
        new = provider.priority if provider.priority > last else last + 1
        if new != provider.priority:
            provider.priority = new
            provider.save(update_fields=["priority"])
        last = new


class Migration(migrations.Migration):

    dependencies = [
        ("django_aicore", "0005_proxysettings_oks_alter_proxysettings_fails"),
    ]

    operations = [
        migrations.RunPython(renumber_duplicates, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="aiprovider",
            name="priority",
            field=models.PositiveSmallIntegerField(
                default=100, unique=True, verbose_name="Приоритет (меньше — раньше)"),
        ),
    ]
