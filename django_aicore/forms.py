from django import forms
from django.forms import inlineformset_factory

from .models import AIApiKey, AIModel, AIModelTag, Provider, api_url_is_root


class ProviderForm(forms.ModelForm):
    class Meta:
        model = Provider
        fields = ["name", "dialect"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control form-control-sm", "placeholder": "OpenRouter"}),
            "dialect": forms.Select(attrs={"class": "form-select form-select-sm"}),
        }


class AIApiKeyForm(forms.ModelForm):
    class Meta:
        model = AIApiKey
        fields = ["provider", "key", "name"]
        widgets = {
            "provider": forms.Select(attrs={"class": "form-select form-select-sm"}),
            "key": forms.PasswordInput(render_value=True, attrs={"class": "form-control form-control-sm"}),
            "name": forms.TextInput(attrs={"class": "form-control form-control-sm",
                                           "placeholder": "пусто — назовём по маске ключа"}),
        }


class AIModelForm(forms.ModelForm):
    class Meta:
        model = AIModel
        fields = ["description", "api_key", "model", "base_url", "role", "priority",
                  "timeout", "temperature", "is_active"]
        widgets = {
            "api_key": forms.Select(attrs={"class": "form-select"}),
            "priority": forms.NumberInput(attrs={"class": "form-control", "min": "0"}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 2}),
            "model": forms.TextInput(attrs={"class": "form-control", "placeholder": "claude-sonnet-4-6"}),
            "base_url": forms.URLInput(attrs={"class": "form-control", "placeholder": "https://api.anthropic.com/v1/messages"}),
            "role": forms.Select(attrs={"class": "form-select"}),
            "timeout": forms.NumberInput(attrs={"class": "form-control"}),
            "temperature": forms.NumberInput(attrs={"class": "form-control", "step": "0.05", "min": "0", "max": "2"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def clean(self):
        """Проверяет endpoint под тот диалект, которым с ним будут говорить.

        Диалект больше не поле этой формы — он приходит от выбранного ключа
        (api_key.provider.dialect): раньше неработающий base_url принимался молча, а
        расплата приходила через сутки первым же вызовом — HTTP 404 из рантайма. Правило
        простое: если сохранённое значение заведомо не может отработать, форма обязана
        сказать это здесь.
        """
        cleaned = super().clean()
        base_url = cleaned.get("base_url")
        api_key = cleaned.get("api_key")
        if not base_url or not api_key:
            return cleaned

        dialect = api_key.provider.dialect
        is_root, path = api_url_is_root(base_url)

        if dialect == Provider.DIALECT_OPENAI and is_root and cleaned.get("role") != AIModel.ROLE_EMBED:
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
        if dialect == Provider.DIALECT_OPENROUTER and cleaned.get("role") != AIModel.ROLE_EMBED:
            expected = "https://openrouter.ai/api/v1/chat/completions"
            if base_url.rstrip("/") != expected:
                self.add_error("base_url", (
                    f"Для OpenRouter чат-запрос уходит по фиксированному адресу «{expected}» — "
                    f"значение этого поля в запрос не идёт вообще. Укажите тот же адрес, "
                    f"иначе поле будет показывать одно, а вызываться будет другое."
                ))

        return cleaned

    def clean_priority(self):
        # Штатное «AI модель с таким Приоритет уже существует» не говорит, КТО занял
        # значение и какое свободно — без этого правку приходится искать перебором.
        priority = self.cleaned_data["priority"]
        taken = AIModel.objects.filter(priority=priority)
        if self.instance.pk:
            taken = taken.exclude(pk=self.instance.pk)
        occupant = taken.first()
        if occupant:
            free = AIModel.next_free_priority()
            raise forms.ValidationError(
                f"Приоритет {priority} уже занят: «{occupant}» "
                f"(роль «{occupant.get_role_display()}», {'активен' if occupant.is_active else 'выключен'}). "
                f"Приоритеты уникальны — иначе победитель при выборе по роли не виден глазами. "
                f"Свободное значение: {free}."
            )
        return priority


AIModelTagFormSet = inlineformset_factory(
    AIModel, AIModelTag,
    fields=["name"],
    widgets={"name": forms.TextInput(attrs={"class": "form-control form-control-sm", "placeholder": "тег"})},
    extra=3,
    can_delete=True,
)
