"""Провайдер Spotify: каталог и пульт Spotify Connect (без потока)."""

from __future__ import annotations

from typing import Callable

from ..errors import AuthError
from ..spotify import SpotifyAdapter
from ..spotify.oauth import DEFAULT_REDIRECT_PORT, login_with_pkce
from .base import AuthKind, Capability, ProviderContext, ProviderInfo, ProviderSpec

SPOTIFY_INFO = ProviderInfo(
    id="spotify",
    title="Spotify",
    capabilities=frozenset({Capability.CATALOG, Capability.DELEGATED_PLAYBACK}),
    #: Основной вход — Authorization Code + PKCE в браузере; ручной токен
    #: остаётся запасным путём для headless-сценариев и старых входов.
    auth=AuthKind.OAUTH_BROWSER,
    implemented=True,
    cache_allowed=False,
    subscription="Premium",
    summary="Каталог и пульт Spotify Connect: звук остаётся в приложении Spotify",
    notes=(
        "Web API не отдаёт ссылку на поток, поэтому mpv не участвует и кеша нет. "
        "Управление воспроизведением требует Premium и активного устройства; "
        "Linux-SDK Spotify нет, поэтому включён только режим «пульт»."
    ),
)


def build(context: ProviderContext) -> SpotifyAdapter:
    return SpotifyAdapter(token=context.token)


def spotify_login(
    context: ProviderContext, on_code: Callable[[str, str], None] | None = None
) -> str:
    """Вход по PKCE: client_id приложения + браузер + loopback-callback."""
    options = context.options or {}
    client_id = str(
        options.get("client_id") or context.settings.spotify_client_id or ""
    ).strip()
    if not client_id:
        raise AuthError(
            "Spotify: для входа по PKCE нужен client_id приложения. "
            "Зарегистрируйте приложение на https://developer.spotify.com/dashboard, "
            f"добавьте Redirect URI http://127.0.0.1:{DEFAULT_REDIRECT_PORT}/callback "
            "и выполните: neurowave --provider spotify login --client-id <id>"
        )
    port = int(options.get("port") or DEFAULT_REDIRECT_PORT)
    credentials = login_with_pkce(
        client_id,
        port=port,
        on_url=(lambda url: on_code(url, "")) if on_code else None,
    )
    return credentials.to_storage()


SPOTIFY_SPEC = ProviderSpec(
    info=SPOTIFY_INFO, factory=build, login=spotify_login
)
