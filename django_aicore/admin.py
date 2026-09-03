from django.contrib import admin

from .models import AIApiKey, AIModel, AIModelTag, AITask, Provider, ProxySettings

# Журнал вызовов в админке не регистрируется намеренно: он живёт своим экраном
# Страница журнала (путь зависит от монтирования) — рядом с моделями, задачами и прокси, которыми по нему и правят.


class AIModelTagInline(admin.TabularInline):
    model = AIModelTag
    extra = 1


@admin.register(Provider)
class ProviderAdmin(admin.ModelAdmin):
    list_display = ["name", "dialect", "base_url"]
    list_filter = ["dialect"]


@admin.register(AIApiKey)
class AIApiKeyAdmin(admin.ModelAdmin):
    list_display = ["name", "provider", "created_at"]
    list_filter = ["provider"]
    search_fields = ["name"]


@admin.register(AIModel)
class AIModelAdmin(admin.ModelAdmin):
    list_display = ["model", "role", "priority", "is_active", "description"]
    list_filter = ["is_active", "role"]
    inlines = [AIModelTagInline]


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
