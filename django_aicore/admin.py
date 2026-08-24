from django.contrib import admin

from .models import AIProvider, AIProviderTag, AITask, ProxySettings

# Журнал вызовов в админке не регистрируется намеренно: он живёт своим экраном
# Страница журнала (путь зависит от монтирования) — рядом с провайдерами, задачами и прокси, которыми по нему и правят.


class AIProviderTagInline(admin.TabularInline):
    model = AIProviderTag
    extra = 1


@admin.register(AIProvider)
class AIProviderAdmin(admin.ModelAdmin):
    list_display = ["model", "role", "priority", "is_active", "description"]
    list_filter = ["is_active", "role"]
    inlines = [AIProviderTagInline]


@admin.register(ProxySettings)
class ProxySettingsAdmin(admin.ModelAdmin):
    list_display = ["host", "port", "username", "is_active", "oks", "fails", "fail_rate",
                    "created_at", "description"]
    list_filter = ["is_active"]
    search_fields = ["host", "description"]


@admin.register(AITask)
class AITaskAdmin(admin.ModelAdmin):
    list_display = ["key", "name", "tag", "role", "temperature", "default_role", "default_temperature"]
    list_filter = ["tag", "role", "default_role"]
    search_fields = ["key", "name"]
    readonly_fields = ["key", "default_role", "default_temperature", "created_at"]
