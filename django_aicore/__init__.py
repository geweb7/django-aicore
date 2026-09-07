"""django-aicore — общий AI-слой: модели по ролям, ключи, вызовы, прокси, фон, журнал.

Контракт потребителя — в `django_aicore/README.md` ТОЙ ЖЕ установки
(едет в wheel как данные пакета) и на странице `aicore:help` (`help/`
от точки монтирования, напр. `/ai/help/`):

    from django_aicore import get_contract_text
    print(get_contract_text())   # тот же текст, что на странице help

    # или файлом: importlib.resources.files("django_aicore") / "README.md"

Полный текст контракта — в README, здесь только указатель: дублировать
370 строк в docstring значило бы два источника правды и рассинхрон с кодом.
"""

try:
    from importlib.metadata import version as _dist_version
except ImportError:  # pragma: no cover — Python >= 3.10 всегда имеет importlib.metadata
    _dist_version = None


def _detect_version():
    if _dist_version is None:
        return "0.0.0+unknown"
    try:
        return _dist_version("django-aicore")
    except Exception:
        return "0.0.0+dev"


__version__ = _detect_version()


def get_contract_text():
    """Текст контракта из ТОЙ ЖЕ установки (django_aicore/README.md в wheel).

    Raises LookupError, если файл не найден — значит установка битая
    (wheel собран без данных пакета).
    """
    try:
        from importlib import resources
    except ImportError as e:  # pragma: no cover
        raise LookupError(f"importlib.resources недоступен: {e}")
    try:
        ref = resources.files("django_aicore").joinpath("README.md")
        with ref.open("r", encoding="utf-8") as f:
            return f.read()
    except (FileNotFoundError, IsADirectoryError, OSError) as e:
        raise LookupError(
            "Контракт django-aicore (django_aicore/README.md) не найден в установленной "
            f"версии {__version__} — wheel собран без данных пакета: {e}"
        )
