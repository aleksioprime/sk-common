"""Настройки сервиса, которыми пользуется пакет.

Сервис создаёт свои настройки (наследник sk_common.config.ServiceSettings) и регистрирует их один раз,
до первого запроса: sk_common.configure(settings). Модули пакета читают настройки только во время вызова,
не при импорте, поэтому порядок импорта не важен.
"""

from typing import Any

_settings: Any = None


def configure(settings: Any) -> None:
    global _settings
    _settings = settings


def get_settings() -> Any:
    if _settings is None:
        raise RuntimeError('sk_common не настроен: вызовите sk_common.configure(settings) при старте сервиса')
    return _settings


class _SettingsProxy:
    """settings.auth.access_cookie и т. п. — обращение к зарегистрированным настройкам сервиса."""

    def __getattr__(self, name: str) -> Any:
        return getattr(get_settings(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(get_settings(), name, value)


settings: Any = _SettingsProxy()
