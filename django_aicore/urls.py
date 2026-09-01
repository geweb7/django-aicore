from django.urls import path

from . import views

app_name = "aicore"

urlpatterns = [
    path("task-poll/<str:task_id>/", views.task_poll, name="task_poll"),
    path("providers/", views.providers, name="providers"),
    path("providers/refresh-pricing/", views.providers_pricing_refresh, name="providers_refresh_pricing"),
    path("providers/<int:pk>/switch-model/", views.provider_switch_model, name="provider_switch_model"),
    path("providers/add/", views.provider_add, name="provider_add"),
    path("providers/<int:pk>/edit/", views.provider_edit, name="provider_edit"),
    path("providers/<int:pk>/copy/", views.provider_copy, name="provider_copy"),
    path("providers/<int:pk>/delete/", views.provider_delete, name="provider_delete"),
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
