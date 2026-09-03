from django.urls import path

from . import views

app_name = "aicore"

urlpatterns = [
    path("task-poll/<str:task_id>/", views.task_poll, name="task_poll"),
    path("ai-models/", views.ai_models, name="ai_models"),
    path("ai-models/refresh-pricing/", views.ai_models_refresh_pricing, name="ai_models_refresh_pricing"),
    path("ai-models/<int:pk>/switch-model/", views.ai_model_switch_model, name="ai_model_switch_model"),
    path("ai-models/add/", views.ai_model_add, name="ai_model_add"),
    path("ai-models/<int:pk>/edit/", views.ai_model_edit, name="ai_model_edit"),
    path("ai-models/<int:pk>/copy/", views.ai_model_copy, name="ai_model_copy"),
    path("ai-models/<int:pk>/delete/", views.ai_model_delete, name="ai_model_delete"),
    path("providers/", views.providers, name="providers"),
    path("providers/add/", views.provider_add, name="provider_add"),
    path("providers/<int:pk>/save/", views.provider_save, name="provider_save"),
    path("providers/<int:pk>/delete/", views.provider_delete, name="provider_delete"),
    path("api-keys/", views.api_keys, name="api_keys"),
    path("api-keys/add/", views.api_key_add, name="api_key_add"),
    path("api-keys/<int:pk>/save/", views.api_key_save, name="api_key_save"),
    path("api-keys/<int:pk>/delete/", views.api_key_delete, name="api_key_delete"),
    path("tasks/", views.tasks, name="tasks"),
    path("tasks/<int:pk>/save/", views.task_save, name="task_save"),
    path("tasks/<int:pk>/delete/", views.task_delete, name="task_delete"),
    path("calls/", views.calls, name="calls"),
    path("calls/refresh-costs/", views.calls_refresh_costs, name="calls_refresh_costs"),
    path("proxies/", views.proxies, name="proxies"),
    path("proxies/add/", views.proxy_add, name="proxy_add"),
    path("proxies/<int:pk>/save/", views.proxy_save, name="proxy_save"),
    path("proxies/<int:pk>/delete/", views.proxy_delete, name="proxy_delete"),
]
