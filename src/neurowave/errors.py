"""Ошибки ядра NeuroWave.

Все ожидаемые сбои наследуются от :class:`NeuroWaveError`, чтобы UI и CLI
могли показывать понятное сообщение вместо трассировки. Сообщения никогда
не должны содержать токены или заголовки авторизации.
"""

from __future__ import annotations


class NeuroWaveError(Exception):
    """Базовый класс ожидаемых ошибок приложения."""


class ConfigError(NeuroWaveError):
    """Некорректная или отсутствующая конфигурация."""


class AuthError(NeuroWaveError):
    """Требуется вход или сохранённый токен недействителен."""


class SubscriptionRequired(NeuroWaveError):
    """Функция недоступна без активной подписки сервиса."""


class ApiError(NeuroWaveError):
    """Сервис вернул ошибку, которую нельзя исправить локально."""


class NetworkError(NeuroWaveError):
    """Сеть недоступна или запрос не удался после повторов."""


class TrackUnavailable(NeuroWaveError):
    """Трек отсутствует в каталоге или недоступен аккаунту."""


class PlayerError(NeuroWaveError):
    """Некорректная команда плеера в текущем состоянии."""


class InvalidTransition(PlayerError):
    """Недопустимый переход машины состояний."""


class CacheError(NeuroWaveError):
    """Ошибка дискового кеша."""
