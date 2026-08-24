from django import forms
from django.forms import inlineformset_factory

from .models import AIProvider, AIProviderTag, api_url_is_root


class AIProviderForm(forms.ModelForm):
    class Meta:
        model = AIProvider
        fields = ["description", "api_key", "model", "base_url", "dialect", "role", "priority",
                  "timeout", "temperature", "is_active"]
        widgets = {
            "dialect": forms.Select(attrs={"class": "form-select"}),
            "priority": forms.NumberInput(attrs={"class": "form-control", "min": "0"}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 2}),
            "api_key": forms.PasswordInput(render_value=True, attrs={"class": "form-control"}),
            "model": forms.TextInput(attrs={"class": "form-control", "placeholder": "claude-sonnet-4-6"}),
            "base_url": forms.URLInput(attrs={"class": "form-control", "placeholder": "https://api.anthropic.com/v1/messages"}),
            "role": forms.Select(attrs={"class": "form-select"}),
            "timeout": forms.NumberInput(attrs={"class": "form-control"}),
            "temperature": forms.NumberInput(attrs={"class": "form-control", "step": "0.05", "min": "0", "max": "2"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def clean(self):
        """Проверяет endpoint под тот диалект, которым с ним будут говорить.

        Раньше неработающий base_url принимался молча, а расплата приходила через сутки
        первым же вызовом — HTTP 404 из рантайма. Правило простое: если сохранённое
        значение заведомо не может отработать, форма обязана сказать это здесь.
        """
        cleaned = super().clean()
        base_url = cleaned.get("base_url")
        if not base_url:
            return cleaned

        dialect = cleaned.get("dialect")
        is_root, path = api_url_is_root(base_url)

        if dialect == AIProvider.DIALECT_OPENAI and is_root and cleaned.get("role") != AIProvider.ROLE_EMBED:
            self.add_error("base_url", (
                f"Это корень API, а не метод: запрос уйдёт ровно на «{path or '/'}» — путь "
                f"не дописывается, ответ будет HTTP 404. Нужен полный путь, например "
                f"«{base_url.rstrip('/')}/chat/completions». "
                f"Путь достраивают сами только диалект «Gemini» и эмбеддинги "
                f"(роль «Эмбеддинг» — им дописывается /embeddings)."
            ))

        # Для openrouter чат-вызов идёт по адресу из кода, а поле в запрос не попадает.
        # Разрешать здесь произвольный путь — значит держать в базе значение, которое
        # выглядит настройкой, но ни на что не влияет.
        if dialect == AIProvider.DIALECT_OPENROUTER and cleaned.get("role") != AIProvider.ROLE_EMBED:
            expected = "https://openrouter.ai/api/v1/chat/completions"
            if base_url.rstrip("/") != expected:
                self.add_error("base_url", (
                    f"Для OpenRouter чат-запрос уходит по фиксированному адресу «{expected}» — "
                    f"значение этого поля в запрос не идёт вообще. Укажите тот же адрес, "
                    f"иначе поле будет показывать одно, а вызываться будет другое."
                ))

        return cleaned

    def clean_priority(self):
        # Штатное «AI провайдер с таким Приоритет уже существует» не говорит, КТО занял
        # значение и какое свободно — без этого правку приходится искать перебором.
        priority = self.cleaned_data["priority"]
        taken = AIProvider.objects.filter(priority=priority)
        if self.instance.pk:
            taken = taken.exclude(pk=self.instance.pk)
        occupant = taken.first()
        if occupant:
            free = AIProvider.next_free_priority()
            raise forms.ValidationError(
                f"Приоритет {priority} уже занят: «{occupant}» "
                f"(роль «{occupant.get_role_display()}», {'активен' if occupant.is_active else 'выключен'}). "
                f"Приоритеты уникальны — иначе победитель при выборе по роли не виден глазами. "
                f"Свободное значение: {free}."
            )
        return priority


AIProviderTagFormSet = inlineformset_factory(
    AIProvider, AIProviderTag,
    fields=["name"],
    widgets={"name": forms.TextInput(attrs={"class": "form-control form-control-sm", "placeholder": "тег"})},
    extra=3,
    can_delete=True,
)
