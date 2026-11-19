"""CLI-интерфейс headless-ядра NeuroWave.

Это не заменяет будущий Qt-интерфейс: CLI работает поверх того же сервиса и
позволяет проверить авторизацию, каталог, очередь и аудиовыход без браузера.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import sys
import time
from dataclasses import asdict, dataclass
from typing import Any, Sequence

from . import __version__
from .audio import MpvBackend, NullBackend
from .audio.devices import (
    build_outputs,
    describe_kind,
    probe_default_sink,
    probe_mpv_devices,
    probe_sinks,
)
from .audio.dsp_graph import build_filter_graph, describe_profile
from .audio.dsp_profiles import BUILTIN_PROFILES, DspError, get_profile, resolve_profile
from .audio.profile_store import ProfileRegistry
from .config import ConfigStore, Paths, Settings
from .domain import AccountInfo, Track
from .errors import AuthError, ConfigError, NetworkError, NeuroWaveError, PlayerError
from .player import PlayerService
from .providers import (
    AuthKind,
    Capability,
    ProviderContext,
    ProviderNotImplemented,
    all_specs,
    create,
    discover,
    get_spec,
    planned,
    plugin_errors,
)
from .providers.router import ProviderRouter
from .queue import Queue
from .state import PlayerStatus
from .storage import Database, MediaCache, TokenStore, keyring_disabled

DEFAULT_SEARCH_LIMIT = 20
API_HOSTS = {
    "yandex": "api.music.yandex.net",
    "soundcloud": "api.soundcloud.com",
    "spotify": "api.spotify.com",
}


@dataclass
class Context:
    paths: Paths
    settings: Settings
    db: Database
    cache: MediaCache
    tokens: TokenStore
    adapter: Any
    provider: str = "yandex"
    router: Any = None
    @property
    def provider_info(self):
        return get_spec(self.provider).info


# --- разбор аргументов -------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="neurowave",
        description="NeuroWave — музыкальное ядро для Linux (Яндекс Музыка).",
    )
    parser.add_argument("--version", action="version", version=f"NeuroWave {__version__}")
    parser.add_argument(
        "--home",
        metavar="DIR",
        help="изолированный каталог состояния (по умолчанию XDG-пути)",
    )
    parser.add_argument(
        "--backend",
        choices=["mpv", "null"],
        help="аудиовыход; null — без звука (тесты/диагностика)",
    )
    parser.add_argument(
        "--provider",
        metavar="ID",
        help="музыкальный провайдер (по умолчанию из настроек: yandex)",
    )
    parser.add_argument("--json", action="store_true", help="машиночитаемый вывод")
    sub = parser.add_subparsers(dest="command", required=True)

    login = sub.add_parser("login", help="вход: OAuth Device Flow или API-ключ провайдера")
    login.add_argument(
        "--token",
        metavar="KEY",
        help="API-ключ провайдера (SoundCloud: client_id/access token; Spotify: access token)",
    )
    login.add_argument(
        "--refresh-token",
        dest="refresh_token",
        metavar="TOKEN",
        help="Spotify: refresh token, чтобы пульт переживал истечение access token",
    )
    login.add_argument(
        "--client-id",
        dest="client_id",
        metavar="ID",
        help="Spotify: client_id приложения, нужен вместе с refresh token",
    )
    sub.add_parser("logout", help="удалить сохранённый токен")
    sub.add_parser("me", help="аккаунт и статус подписки")

    search = sub.add_parser("search", help="поиск треков, артистов, альбомов")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=DEFAULT_SEARCH_LIMIT)

    wave = sub.add_parser("wave", help="включить «Мою волну»")
    _add_playback_options(wave)

    play = sub.add_parser("play", help="воспроизвести найденные треки")
    play.add_argument("query", nargs="?", help="поисковый запрос")
    play.add_argument("--id", dest="track_id", help="ID трека")
    play.add_argument("--limit", type=int, default=DEFAULT_SEARCH_LIMIT)
    _add_playback_options(play)

    sub.add_parser("devices", help="устройства Spotify Connect (режим «пульт»)")
    connect = sub.add_parser("connect", help="перенести воспроизведение на устройство")
    connect.add_argument("device", help="id или имя устройства")
    remote = sub.add_parser("remote", help="управление чужим плеером (Spotify Connect)")
    remote.add_argument("action", choices=["play", "pause", "next", "previous"])
    remote.add_argument("query", nargs="?", help="запрос для play")
    remote.add_argument("--uri", help="spotify:track:… вместо поиска")
    remote.add_argument("--limit", type=int, default=5)

    library = sub.add_parser("library", help="понравившиеся треки")
    library.add_argument("--limit", type=int, default=50)

    sub.add_parser("playlists", help="список плейлистов")

    playlist = sub.add_parser("playlist", help="воспроизвести плейлист")
    playlist.add_argument("identifier", help="kind плейлиста")
    _add_playback_options(playlist)

    tray = sub.add_parser("tray", help="трей и MPRIS поверх «Моей волны»")
    tray.add_argument(
        "--no-mpris", dest="mpris", action="store_false", help="не публиковать MPRIS"
    )
    tray.set_defaults(mpris=True, seconds=None)

    like = sub.add_parser("like", help="поставить лайк треку")
    like.add_argument("track_id")
    unlike = sub.add_parser("unlike", help="убрать лайк")
    unlike.add_argument("track_id")

    cache = sub.add_parser("cache", help="управление дисковым кешем")
    cache.add_argument("action", choices=["info", "clear", "evict"])

    sound = sub.add_parser("sound", help="профиль звука: Pure/Enhanced, EQ, лимитер")
    sound.add_argument("--profile", help=f"id профиля ({', '.join(sorted(BUILTIN_PROFILES))})")
    sound.add_argument("--pure", action="store_true", help="снять наши фильтры (Pure)")
    sound.add_argument("--enhanced", action="store_true", help="вернуть обработку (Enhanced)")
    sound.add_argument("--graph", action="store_true", help="показать lavfi-граф профиля")
    sound.add_argument("--profiles", action="store_true", help="список профилей, включая свои")

    output = sub.add_parser("output", help="аудиовыходы: Bluetooth/HDMI, кодек, выбор")
    output.add_argument("--set", dest="set_id", help="id выхода из списка (auto — системный выбор)")

    sub.add_parser("status", help="состояние конфигурации и локальных данных")
    sub.add_parser("doctor", help="диагностика окружения (без токенов)")
    sub.add_parser("providers", help="провайдеры музыки: что подключено и что умеет")
    sub.add_parser("app", help="графическое окно (Qt): транспорт, громкость, звук, выход")
    return parser


def _add_playback_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--seconds",
        type=float,
        default=None,
        help="остановиться через N секунд (для smoke-проверки)",
    )
    parser.add_argument(
        "--no-interactive",
        action="store_true",
        help="не читать команды из stdin",
    )
    parser.add_argument(
        "--mpris",
        action="store_true",
        help="опубликовать сессию в MPRIS (playerctl, медиа-клавиши, панель)",
    )


# --- точка входа -------------------------------------------------------
def _raise_interrupt(signum, frame):
    """Завершение по SIGTERM/SIGHUP не должно оставлять mpv-сироту.

    Сигнал превращается в KeyboardInterrupt, чтобы отработали finally
    в сессии и backend.shutdown() закрыл mpv.
    """
    raise KeyboardInterrupt


def _install_signal_handlers() -> None:
    for name in ("SIGTERM", "SIGHUP"):
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            signal.signal(number, _raise_interrupt)
        except (OSError, ValueError, RuntimeError):
            pass


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.home:
        os.environ["NEUROWAVE_HOME"] = args.home
    _install_signal_handlers()
    try:
        return _dispatch(args, parser)
    except KeyboardInterrupt:
        print("\nОстановлено.", file=sys.stderr)
        return 130
    except NeuroWaveError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2


def _dispatch(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.command == "doctor":
        return _cmd_doctor(args)
    if args.command == "cache":
        return _cmd_cache(args)
    if args.command == "providers":
        return _cmd_providers(args)
    ctx = _context(args)
    try:
        handlers = {
            "login": _cmd_login,
            "logout": _cmd_logout,
            "me": _cmd_me,
            "search": _cmd_search,
            "library": _cmd_library,
            "playlists": _cmd_playlists,
            "like": _cmd_like,
            "unlike": _cmd_unlike,
            "status": _cmd_status,
            "sound": _cmd_sound,
            "output": _cmd_output,
            "wave": _cmd_wave,
            "play": _cmd_play,
            "playlist": _cmd_playlist,
            "tray": _cmd_tray,
            "app": _cmd_app,
            "devices": _cmd_devices,
            "connect": _cmd_connect,
            "remote": _cmd_remote,
        }
        return handlers[args.command](args, ctx)
    finally:
        if args.command not in {"login", "logout"}:
            _persist_refreshed_token(ctx)
        ctx.db.close()


def _persist_refreshed_token(ctx: Context) -> None:
    """Сохранить ротированные credentials, если провайдер их обновляет."""
    storage_token = getattr(ctx.adapter, "storage_token", None)
    if not callable(storage_token):
        return
    try:
        current = storage_token()
    except NeuroWaveError:
        return
    if not current:
        return
    if ctx.tokens.load() != current:
        ctx.tokens.save(current)


# --- контекст ----------------------------------------------------------
def _resolve_provider_id(args: argparse.Namespace, settings: Settings) -> str:
    return getattr(args, "provider", None) or settings.provider or "yandex"


def _context(args: argparse.Namespace) -> Context:
    paths = Paths.resolve().ensure()
    settings = ConfigStore(paths).load()
    db = Database(paths.database_file)
    cache = MediaCache(
        paths.cache_dir / "media",
        quota_bytes=settings.cache_quota_bytes,
        ttl_seconds=settings.cache_ttl_seconds,
    )
    cache.evict_expired()
    provider_id = _resolve_provider_id(args, settings)
    discover()
    get_spec(provider_id)  # валидация выбора: UnknownProvider/ProviderNotImplemented
    specs = {spec.info.id: spec for spec in all_specs()}

    def builder_for(info_id: str):
        def build():
            token = TokenStore(paths, provider=info_id).load()
            return create(
                specs[info_id],
                ProviderContext(token=token, paths=paths, settings=settings),
            )

        return build

    router = ProviderRouter(
        {info_id: builder_for(info_id) for info_id in specs}, default=provider_id
    )
    tokens = TokenStore(paths, provider=provider_id)
    return Context(
        paths=paths,
        settings=settings,
        db=db,
        cache=cache,
        tokens=tokens,
        adapter=router.adapter(provider_id),
        provider=provider_id,
        router=router,
    )


def _build_backend(args: argparse.Namespace, settings: Settings):
    choice = getattr(args, "backend", None) or settings.audio_backend
    if choice == "null":
        return NullBackend()
    return MpvBackend()


def _require_token(ctx: Context) -> None:
    info = ctx.provider_info
    if info.auth is AuthKind.NONE:
        return
    if not getattr(ctx.adapter, "token", None):
        hint = (
            "neurowave login --token <ключ>"
            if info.auth is AuthKind.API_TOKEN
            else "neurowave login"
        )
        raise AuthError(f"Требуется вход в {info.title}. Выполните: {hint}")


def _require_capability(ctx: Context, capability: Capability) -> None:
    """Не показывать действие, которого у провайдера нет."""
    info = ctx.provider_info
    if info.supports(capability):
        return
    raise ProviderNotImplemented(
        f"«{info.title}» не поддерживает: {capability.title}."
        + (f" {info.summary}" if info.summary else "")
    )


def _console_listener(event: str, payload: dict[str, Any]) -> None:
    if event == "track":
        track = payload.get("track")
        quality = payload.get("quality")
        if track is not None:
            suffix = f" [{quality.label()}]" if quality else ""
            origin = " (кеш)" if payload.get("from_cache") else ""
            print(f"▶ {track.label} — {track.duration_text}{suffix}{origin}", flush=True)
            if track.url:
                print(f"   Источник: {track.url}", flush=True)
    elif event in {"paused", "resumed", "stopped"}:
        print({"paused": "⏸ пауза", "resumed": "▶ продолжаем", "stopped": "■ остановлено"}[event], flush=True)
    elif event == "volume_limited":
        print(
            f"! Громкость ограничена до {payload.get('applied')} %: усиление выше "
            f"{payload.get('limit')} % включится только после проверки лимитера. "
            f"В настройках сохранено {payload.get('requested')} %.",
            file=sys.stderr,
            flush=True,
        )
    elif event == "error":
        print(f"! {payload.get('error')}", file=sys.stderr, flush=True)
    elif event == "finished":
        print("Очередь завершена.", flush=True)


# --- команды: аккаунт --------------------------------------------------
def _cmd_login(args: argparse.Namespace, ctx: Context) -> int:
    spec = get_spec(ctx.provider)
    if spec.info.auth is AuthKind.NONE:
        print(f"Вход не требуется: «{spec.info.title}» работает с локальными файлами.")
        return 0
    if args.token:
        if spec.info.auth is not AuthKind.API_TOKEN:
            raise AuthError(
                f"«{spec.info.title}» не принимает ключ вручную: выполните "
                "интерактивный вход neurowave login."
            )
        token = args.token.strip()
        if not token:
            raise AuthError("Ключ пустой")
        stored_token = token
        if args.refresh_token or args.client_id:
            if spec.info.id != "spotify":
                raise AuthError(
                    "--refresh-token и --client-id поддержаны только для Spotify"
                )
            from .spotify.auth import SpotifyCredentials

            stored_token = SpotifyCredentials(
                access_token=token,
                refresh_token=(args.refresh_token or "").strip() or None,
                client_id=(args.client_id or "").strip() or None,
            ).to_storage()
        adapter = create(
            spec,
            ProviderContext(token=stored_token, paths=ctx.paths, settings=ctx.settings),
        )
        verify = getattr(adapter, "verify", None)
        if verify is not None:
            print("Проверяю ключ…", flush=True)
            try:
                verify()
            except NetworkError as exc:
                # Ключ не подтверждён, но и не опровергнут: сеть — не повод его терять.
                print(
                    f"Ключ не проверен: {exc}. Сохраняю — проверить можно позже.",
                    file=sys.stderr,
                    flush=True,
                )
        ctx.tokens.save(stored_token)
        ctx.adapter = adapter
        print(f"Ключ сохранён ({spec.info.title}). Хранилище: {ctx.tokens.backend}.")
        if args.refresh_token and args.client_id:
            print(
                "Обновление токена настроено: при истечении access token будет "
                "использован refresh token."
            )
        return 0
    if spec.login is None:
        raise ProviderNotImplemented(
            f"Вход для «{spec.info.title}» не поддержан: провайдеру нужен API-ключ, "
            "а не интерактивный вход. Передайте его: neurowave login --token <ключ>"
        )

    def on_code(url: str, code: str) -> None:
        print("Откройте страницу подтверждения:", url)
        print("Введите код:", code)
        print("Ожидаю подтверждения…", flush=True)

    token = spec.login(
        ProviderContext(token=None, paths=ctx.paths, settings=ctx.settings), on_code
    )
    ctx.tokens.save(token)
    print(f"Вход выполнен ({spec.info.title}). Токен сохранён: {ctx.tokens.backend}.")
    ctx.adapter = create(
        spec, ProviderContext(token=token, paths=ctx.paths, settings=ctx.settings)
    )
    try:
        account = ctx.adapter.account()
    except NeuroWaveError as exc:
        print(f"Аккаунт пока недоступен: {exc}")
        return 0
    _print_account(
        account, args.json, spec.info.subscription, spec.info.unavailable_hint or None
    )
    return 0


def _cmd_logout(args: argparse.Namespace, ctx: Context) -> int:
    ctx.tokens.delete()
    if ctx.provider == "yandex":
        print("Токен удалён. Персональные данные больше не запрашиваются.")
    else:
        print(f"Токен провайдера «{ctx.provider}» удалён.")
    return 0


def _cmd_me(args: argparse.Namespace, ctx: Context) -> int:
    _require_token(ctx)
    info = ctx.provider_info
    _print_account(
        ctx.adapter.account(), args.json, info.subscription, info.unavailable_hint or None
    )
    return 0


def _print_account(
    account: AccountInfo,
    as_json: bool,
    subscription: str | None = None,
    unavailable_hint: str | None = None,
) -> None:
    if as_json:
        print(
            json.dumps(
                {
                    "uid": account.uid,
                    "login": account.login,
                    "display_name": account.display_name,
                    "service_available": account.service_available,
                    "plus": account.plus,
                },
                ensure_ascii=False,
            )
        )
        return
    print(f"Аккаунт: {account.title}")
    if account.login:
        print(f"Логин: {account.login}")
    if subscription:
        # Значение приходит из адаптера как bool|None; любой другой тип — «неизвестно».
        if account.plus is True:
            state = "активна"
        elif account.plus is False:
            state = "не активна"
        else:
            state = "неизвестно"
        print(f"Подписка {subscription}: {state}")
    if not account.service_available:
        print(
            unavailable_hint
            or "Сервис сообщает о недоступности — функции каталога могут не работать."
        )


# --- команды: каталог --------------------------------------------------
def _cmd_search(args: argparse.Namespace, ctx: Context) -> int:
    _require_token(ctx)
    _require_capability(ctx, Capability.CATALOG)
    results = ctx.adapter.search(args.query, limit=args.limit)
    if args.json:
        print(
            json.dumps(
                {
                    "tracks": [track.to_dict() for track in results.tracks],
                    "artists": [ref.__dict__ for ref in results.artists],
                    "albums": [ref.__dict__ for ref in results.albums],
                    "playlists": [ref.__dict__ for ref in results.playlists],
                },
                ensure_ascii=False,
            )
        )
        return 0
    if not results.tracks:
        print("Треки не найдены.")
    for index, track in enumerate(results.tracks, start=1):
        print(f"{index:2d}. {track.label} — {track.duration_text}  id={track.id}")
        if track.url:
            print(f"    Источник: {track.url}")
    if results.artists:
        print("\nИсполнители: " + ", ".join(ref.name for ref in results.artists))
    if results.albums:
        print("Альбомы: " + ", ".join(ref.name for ref in results.albums))
    if results.playlists:
        print("Плейлисты: " + ", ".join(f"{ref.name} ({ref.id})" for ref in results.playlists))
    return 0


def _cmd_library(args: argparse.Namespace, ctx: Context) -> int:
    _require_token(ctx)
    _require_capability(ctx, Capability.LIBRARY)
    tracks = ctx.adapter.liked_tracks(limit=args.limit)
    if args.json:
        print(json.dumps([track.to_dict() for track in tracks], ensure_ascii=False))
        return 0
    if not tracks:
        print("В понравившихся пока нет треков.")
    for index, track in enumerate(tracks, start=1):
        print(f"{index:2d}. {track.label} — {track.duration_text}  id={track.id}")
    return 0


def _cmd_devices(args: argparse.Namespace, ctx: Context) -> int:
    """Список устройств Spotify Connect: без них режим «пульт» бесполезен."""
    _require_capability(ctx, Capability.DELEGATED_PLAYBACK)
    _require_token(ctx)
    devices = ctx.adapter.devices()
    if args.json:
        print(json.dumps([asdict(device) for device in devices], ensure_ascii=False))
        return 0
    if not devices:
        print(
            "Устройства Spotify Connect не найдены: откройте Spotify на телефоне "
            "или компьютере и войдите в тот же аккаунт."
        )
        return 1
    for device in devices:
        mark = "*" if device.is_active else " "
        volume = f" · {device.volume_percent}%" if device.volume_percent is not None else ""
        print(f"{mark} {device.name} — {device.type}{volume}  id={device.id}")
    return 0


def _cmd_connect(args: argparse.Namespace, ctx: Context) -> int:
    _require_capability(ctx, Capability.DELEGATED_PLAYBACK)
    _require_token(ctx)
    target = (args.device or "").strip()
    devices = ctx.adapter.devices()
    match = _match_device(devices, target)
    if match is None:
        names = ", ".join(device.name for device in devices) or "ни одного"
        raise PlayerError(f"Устройство «{target}» не найдено. Доступны: {names}.")
    ctx.adapter.transfer(match.id)
    print(f"Воспроизведение перенесено на «{match.name}».")
    return 0


def _match_device(devices: list, target: str):
    """id точный, затем имя точно, затем имя по подстроке — без догадок."""
    if not target:
        return None
    for device in devices:
        if device.id == target:
            return device
    needle = target.casefold()
    for device in devices:
        if device.name.casefold() == needle:
            return device
    for device in devices:
        if needle in device.name.casefold():
            return device
    return None


def _cmd_remote(args: argparse.Namespace, ctx: Context) -> int:
    """Управление устройством сервиса: звук идёт мимо нашего `mpv`."""
    _require_capability(ctx, Capability.DELEGATED_PLAYBACK)
    _require_token(ctx)
    action = args.action
    if action == "play":
        uri = (args.uri or "").strip()
        if uri:
            ctx.adapter.remote_play(uri=uri)
            print(f"Отправлено в Spotify: {uri}")
            return 0
        if not args.query:
            raise PlayerError("Для remote play укажите запрос или --uri")
        results = ctx.adapter.search(args.query, limit=args.limit)
        if not results.tracks:
            raise PlayerError(f"По запросу «{args.query}» треки не найдены")
        track = results.tracks[0]
        ctx.adapter.remote_play(track_ids=[track.id])
        print(f"Играет на устройстве Spotify: {track.label}")
        return 0
    if action == "pause":
        ctx.adapter.remote_pause()
        print("Пауза отправлена в Spotify.")
    elif action == "next":
        ctx.adapter.remote_next()
        print("Следующий трек отправлен в Spotify.")
    else:
        ctx.adapter.remote_previous()
        print("Предыдущий трек отправлен в Spotify.")
    return 0


def _cmd_playlists(args: argparse.Namespace, ctx: Context) -> int:
    _require_token(ctx)
    _require_capability(ctx, Capability.PLAYLISTS)
    playlists = ctx.adapter.playlists()
    if args.json:
        print(json.dumps([ref.__dict__ for ref in playlists], ensure_ascii=False))
        return 0
    if not playlists:
        print("Плейлисты не найдены.")
    for ref in playlists:
        suffix = f" — {ref.subtitle}" if ref.subtitle else ""
        print(f"[{ref.id}] {ref.name}{suffix}")
    return 0


def _cmd_like(args: argparse.Namespace, ctx: Context) -> int:
    _require_token(ctx)
    _require_capability(ctx, Capability.LIBRARY)
    ok = ctx.adapter.like(args.track_id)
    print("Лайк поставлен." if ok else "Сервис не подтвердил лайк.")
    return 0 if ok else 1


def _cmd_unlike(args: argparse.Namespace, ctx: Context) -> int:
    _require_token(ctx)
    _require_capability(ctx, Capability.LIBRARY)
    ok = ctx.adapter.unlike(args.track_id)
    print("Лайк убран." if ok else "Сервис не подтвердил удаление лайка.")
    return 0 if ok else 1


def _cmd_output(args: argparse.Namespace, ctx: Context) -> int:
    """Показать выходы и, при желании, выбрать один. Систему не переключаем."""
    devices = probe_mpv_devices(ctx.settings.audio_backend)
    outputs = build_outputs(
        devices, probe_sinks(), ctx.settings.audio_output or None, probe_default_sink()
    )
    if args.set_id:
        known = {output.id for output in outputs}
        if args.set_id not in known:
            print(f"Нет такого аудиовыхода: {args.set_id}", file=sys.stderr)
            print("Доступны: " + ", ".join(sorted(known)), file=sys.stderr)
            return 2
        ctx.settings.audio_output = args.set_id
        ConfigStore(ctx.paths).save(ctx.settings)
        outputs = build_outputs(devices, probe_sinks(), args.set_id, probe_default_sink())
    if args.json:
        data = {
            "selected": ctx.settings.audio_output or "auto",
            "outputs": [output.to_dict() for output in outputs],
        }
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    if not outputs:
        print("mpv не найден или не отдал список устройств. Проверьте: neurowave doctor")
        return 1
    for output in outputs:
        mark = "*" if output.current else " "
        extra = []
        if output.kind != "other":
            extra.append(describe_kind(output.kind))
        if output.codec:
            extra.append(output.codec.upper())
        if output.profile:
            extra.append(output.profile)
        suffix = f"  [{', '.join(extra)}]" if extra else ""
        print(f"{mark} {output.name}{suffix}\n    id={output.id}")
    auto = next((output for output in outputs if output.via_auto), None)
    if args.set_id:
        print(f"Выбрано: {args.set_id} — применится при следующем воспроизведении.")
    elif ctx.settings.audio_output:
        print(f"Сохранённый выбор: {ctx.settings.audio_output}")
    elif auto is not None:
        print(f"Выбор: auto → {auto.name}")
    else:
        print("Выбор: auto (системный маршрут PipeWire)")
    return 0


def _cmd_sound(args: argparse.Namespace, ctx: Context) -> int:
    """Показать или выбрать профиль. Работающий mpv не трогаем из другого процесса."""
    if args.pure and args.enhanced:
        print("Нельзя одновременно --pure и --enhanced.", file=sys.stderr)
        return 2
    chosen = None
    if args.pure:
        chosen = BUILTIN_PROFILES["flat"]
    elif args.enhanced:
        chosen = resolve_profile(ctx.settings.dsp_profile)
        if chosen.mode.value == "pure":
            chosen = BUILTIN_PROFILES["hifi"]
    elif args.profile:
        registry = ProfileRegistry.from_settings(ctx.settings)
        try:
            chosen = registry.get(args.profile) or get_profile(args.profile)
        except DspError as exc:
            print(f"Ошибка: {exc}", file=sys.stderr)
            return 2
    if chosen is not None:
        ctx.settings.dsp_profile = chosen.id
        ConfigStore(ctx.paths).save(ctx.settings)
    registry = ProfileRegistry.from_settings(ctx.settings)
    profile = registry.resolve(ctx.settings.dsp_profile)
    graph = build_filter_graph(profile)
    note = " (сохранено, применится при следующем воспроизведении)" if chosen is not None else ""
    data = {
        "profile": profile.id,
        "name": profile.name,
        "mode": profile.mode.value,
        "description": describe_profile(profile),
        "preamp_db": profile.preamp_db,
        "bands": len(profile.bands),
        "limiter": profile.limiter.to_dict() if profile.limiter.enabled else None,
        "graph": graph,
        "boost_verified": ctx.settings.boost_verified,
        "available": sorted(BUILTIN_PROFILES),
    }
    if args.profiles:
        data["profiles"] = [
            {"id": item.id, "name": item.name, "builtin": registry.is_builtin(item.id)}
            for item in registry.all()
        ]
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    if args.profiles:
        for item in registry.all():
            mark = "*" if item.id == profile.id else " "
            kind = "встроенный" if registry.is_builtin(item.id) else "свой"
            print(f"{mark} {item.name} · {item.id} ({kind})")
        if registry.errors:
            print("Пропущены при чтении: " + "; ".join(registry.errors))
        return 0
    print(f"Профиль: {describe_profile(profile)}{note}")
    print(f"Режим: {profile.mode.value} · id: {profile.id}")
    if args.graph:
        print(f"Граф: {graph or '— (без обработки)'}")
    if profile.limiter.enabled:
        print(
            f"Лимитер: потолок {profile.limiter.ceiling_dbfs:g} dBFS, "
            f"lookahead {profile.limiter.lookahead_ms:g} мс"
        )
    if not ctx.settings.boost_verified:
        print("Усиление выше 100 % выключено: лимитер ещё не подтверждён измерениями")
    return 0


def _cmd_status(args: argparse.Namespace, ctx: Context) -> int:
    stats = ctx.cache.stats()
    recent = ctx.db.recent_tracks(limit=5)
    info = ctx.provider_info
    needs_auth = info.auth is not AuthKind.NONE
    data = {
        "version": __version__,
        "home": str(ctx.paths.config_dir.parent),
        "provider": {"id": info.id, "title": info.title, "auth": info.auth.value},
        "token": {
            "present": bool(getattr(ctx.adapter, "token", None)),
            "required": needs_auth,
            "backend": ctx.tokens.backend,
        },
        "backend": ctx.settings.audio_backend,
        "audio_output": ctx.settings.audio_output or "auto",
        "volume": ctx.settings.volume,
        "volume_effective": ctx.settings.effective_volume(),
        "cache": {
            "files": stats.count,
            "mb": stats.megabytes_used,
            "quota_mb": stats.megabytes_quota,
        },
        "recent": [track.to_dict() for track in recent],
    }
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    print(f"NeuroWave {__version__}")
    print(f"Каталог состояния: {ctx.paths.config_dir.parent}")
    print(f"Провайдер: {info.title} ({info.id})")
    if needs_auth:
        print(
            "Токен: "
            + ("сохранён" if data["token"]["present"] else "нет")
            + f" ({ctx.tokens.backend})"
        )
    else:
        print("Токен: не требуется")
    print(
        f"Аудиовыход: {ctx.settings.audio_backend} "
        f"({ctx.settings.audio_output or 'auto'}), громкость {ctx.settings.effective_volume()}%"
    )
    if ctx.settings.volume_limited():
        print(
            f"Ограничение: в настройках {ctx.settings.volume}%, применяется "
            f"{ctx.settings.effective_volume()}% — лимитер ещё не подтверждён"
        )
    print(f"Кеш: {stats.megabytes_used} МБ из {stats.megabytes_quota} МБ ({stats.count} файлов)")
    if recent:
        print("Недавно слушали:")
        for track in recent:
            print(f"  • {track.label}")
    return 0


# --- команды: воспроизведение -----------------------------------------
def _cmd_wave(args: argparse.Namespace, ctx: Context) -> int:
    info = ctx.provider_info
    if not info.supports(Capability.PERSONAL_RADIO):
        if info.supports(Capability.SHUFFLE_LIBRARY):
            print(
                f"У «{info.title}» нет персонального потока: случайная выборка из библиотеки.",
                file=sys.stderr,
                flush=True,
            )
        else:
            raise ProviderNotImplemented(
                f"«{info.title}» не поддерживает: {Capability.PERSONAL_RADIO.title}."
            )
    _require_token(ctx)
    return _play_session(args, ctx, lambda player: player.start_wave())


def _cmd_play(args: argparse.Namespace, ctx: Context) -> int:
    # Сначала объясняем отсутствие потока: вход тут ничего не изменит.
    _require_capability(ctx, Capability.DIRECT_STREAM)
    _require_token(ctx)
    if not args.query and not args.track_id:
        raise PlayerError("Укажите поисковый запрос или --id")
    if args.track_id:
        # Проверяем доступность по ID через поиск ссылки на поток.
        track = Track(
            id=str(args.track_id), title=f"Трек {args.track_id}", provider=ctx.provider
        )
        return _play_session(args, ctx, lambda player: player.play_track(track))
    results = ctx.adapter.search(args.query, limit=args.limit)
    if not results.tracks:
        raise PlayerError(f"По запросу «{args.query}» треки не найдены")
    tracks = list(results.tracks)
    print(f"Найдено треков: {len(tracks)}. Ставлю в очередь.")
    return _play_session(args, ctx, lambda player: player.play_tracks(tracks))


def _cmd_playlist(args: argparse.Namespace, ctx: Context) -> int:
    _require_token(ctx)
    _require_capability(ctx, Capability.PLAYLISTS)
    tracks = ctx.adapter.playlist_tracks(args.identifier)
    if not tracks:
        raise PlayerError("Плейлист пуст или недоступен")
    print(f"Треков в плейлисте: {len(tracks)}.")
    return _play_session(args, ctx, lambda player: player.play_tracks(tracks))


def _build_player(args: argparse.Namespace, ctx: Context, listener=None) -> PlayerService:
    backend = _build_backend(args, ctx.settings)
    queue = Queue(repeat=ctx.settings.repeat, shuffle=ctx.settings.shuffle)
    return PlayerService(
        adapter=ctx.adapter,
        backend=backend,
        queue=queue,
        settings=ctx.settings,
        store=ctx.db,
        cache=ctx.cache,
        listener=listener or _console_listener,
        router=ctx.router,
    )


def _start_mpris(player: PlayerService, quit_callback=None):
    """Поднять MPRIS; отсутствие шины не мешает музыке."""
    from .integrations.mpris_dbus import start_mpris

    service = start_mpris(player, quit_callback=quit_callback)
    if service is not None and not service.available:
        print(f"MPRIS недоступен: {service.error}", file=sys.stderr)
        return None
    return service


def _stop_quietly(service) -> None:
    if service is None:
        return
    try:
        service.stop()
    except Exception:
        pass


def _cmd_app(args: argparse.Namespace, ctx: Context) -> int:
    from .ui.app import UiUnavailable, is_available, run_window

    available, reason = is_available()
    if not available:
        print(f"Окно недоступно: {reason}", file=sys.stderr)
        return 1

    player = _build_player(args, ctx)
    mpris = _start_mpris(player) if getattr(args, "mpris", False) else None
    try:
        try:
            return run_window(player)
        except UiUnavailable as exc:
            print(f"Окно недоступно: {exc}", file=sys.stderr)
            return 1
    finally:
        _stop_quietly(mpris)
        player.stop()
        player.close()
        ConfigStore(ctx.paths).save(ctx.settings)


def _cmd_tray(args: argparse.Namespace, ctx: Context) -> int:
    _require_token(ctx)
    from .integrations.tray import TrayUnavailable, is_available, run_tray

    available, reason = is_available()
    if not available:
        print(f"Трей недоступен: {reason}", file=sys.stderr)
        return 1

    player = _build_player(args, ctx)
    mpris = _start_mpris(player) if getattr(args, "mpris", False) else None
    try:
        player.start_wave()
        try:
            return run_tray(player)
        except TrayUnavailable as exc:
            print(f"Трей недоступен: {exc}", file=sys.stderr)
            return 1
    finally:
        _stop_quietly(mpris)
        player.stop()
        player.close()
        ConfigStore(ctx.paths).save(ctx.settings)


def _play_session(
    args: argparse.Namespace,
    ctx: Context,
    starter,
) -> int:
    player = _build_player(args, ctx)
    mpris = _start_mpris(player) if getattr(args, "mpris", False) else None
    try:
        starter(player)
        if not args.no_interactive:
            _run_session(player, seconds=args.seconds, interactive=sys.stdin.isatty())
        elif args.seconds:
            _wait_for_seconds(player, args.seconds)
    finally:
        _stop_quietly(mpris)
        player.stop()
        player.close()
        ConfigStore(ctx.paths).save(ctx.settings)
    return 0


def _run_session(player: PlayerService, seconds: float | None, interactive: bool) -> None:
    started = time.monotonic()
    if interactive:
        print(
            "Управление: q — выход, n — следующий, p — пауза, +/- — громкость, s — состояние",
            flush=True,
        )
    while _still_playing(player, started, seconds):
        if not interactive:
            time.sleep(0.2)
            continue
        try:
            import select

            ready, _, _ = select.select([sys.stdin], [], [], 0.2)
        except (OSError, ValueError):
            time.sleep(0.2)
            continue
        if not ready:
            continue
        line = sys.stdin.readline()
        if not line:
            break
        if not _handle_key(player, line.strip().lower()):
            break


def _wait_for_seconds(player: PlayerService, seconds: float) -> None:
    started = time.monotonic()
    while _still_playing(player, started, seconds):
        time.sleep(0.1)


def _still_playing(player: PlayerService, started: float, seconds: float | None) -> bool:
    if seconds is not None and time.monotonic() - started >= seconds:
        return False
    return player.state.status not in (PlayerStatus.STOPPED, PlayerStatus.IDLE)


def _handle_key(player: PlayerService, key: str) -> bool:
    if key in {"q", "quit", "exit"}:
        return False
    if key in {"n", "next"}:
        player.next_track()
    elif key in {"p", "pause", " "}:
        try:
            player.toggle_pause()
        except PlayerError as exc:
            print(exc, file=sys.stderr)
    elif key in {"+", "="}:
        player.set_volume(player.volume + 5)
    elif key == "-":
        player.set_volume(player.volume - 5)
    elif key in {"e", "effects"}:
        mode = "pure" if player.dsp.status.mode.value == "enhanced" else "enhanced"
        status = player.set_dsp_mode(mode)
        print(f"Звук: {status.mode.value} · {status.name}", flush=True)
    elif key in {"s", "status"}:
        track = player.current_track
        quality = player.current_quality
        parts = [f"Состояние: {player.state.describe()}"]
        if track:
            parts.append(f"Трек: {track.label}")
        if quality:
            parts.append(f"Источник: {quality.label()}")
        print(" | ".join(parts), flush=True)
    return True


# --- команды: провайдеры ----------------------------------------------
def _cmd_providers(args: argparse.Namespace) -> int:
    paths = Paths.resolve().ensure()
    settings = ConfigStore(paths).load()
    current = _resolve_provider_id(args, settings)
    discover()

    rows = []
    for spec in all_specs():
        info = spec.info
        rows.append(
            {
                "id": info.id,
                "title": info.title,
                "summary": info.summary,
                "capabilities": [capability.value for capability in info.capabilities],
                "capabilities_text": info.capabilities_text,
                "auth": info.auth.value,
                "plays_in_mpv": info.plays_in_mpv,
                "cache_allowed": info.cache_allowed,
                "notes": info.notes,
                "token_present": TokenStore(paths, provider=info.id).present(),
                "current": info.id == current,
            }
        )
    unavailable = [
        {
            "id": info.id,
            "title": info.title,
            "summary": info.summary,
            "capabilities_text": info.capabilities_text,
            "notes": info.notes,
        }
        for info in planned()
    ]

    if args.json:
        print(
            json.dumps(
                {
                    "current": current,
                    "connected": rows,
                    "not_connected": unavailable,
                    "plugin_errors": plugin_errors(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print("Провайдеры:")
    for row in rows:
        mark = "*" if row["current"] else " "
        if row["auth"] == AuthKind.NONE.value:
            state = "вход не требуется"
        else:
            state = "токен есть" if row["token_present"] else "нужен вход"
        playback = "поток в mpv" if row["plays_in_mpv"] else "без прямого потока"
        print(f"{mark} {row['id']:<10} {row['title']} — {state}, {playback}")
        if row["summary"]:
            print(f"    {row['summary']}")
        print(f"    возможности: {row['capabilities_text']}")
        if row["notes"]:
            print(f"    ограничения: {row['notes']}")

    if unavailable:
        print("\nИзвестны, но не подключены:")
        for row in unavailable:
            print(f"  {row['id']:<14} {row['title']}")
            if row["summary"]:
                print(f"    {row['summary']}")
            if row["notes"]:
                print(f"    ограничения: {row['notes']}")

    errors = plugin_errors()
    if errors:
        print("\nОшибки сторонних провайдеров:")
        for error in errors:
            print(f"  ! {error}")

    print(f"\nТекущий: {current}. Сменить: neurowave --provider ID …")
    return 0


# --- команды: кеш и диагностика ---------------------------------------
def _cmd_cache(args: argparse.Namespace) -> int:
    paths = Paths.resolve().ensure()
    settings = ConfigStore(paths).load()
    cache = MediaCache(
        paths.cache_dir / "media",
        quota_bytes=settings.cache_quota_bytes,
        ttl_seconds=settings.cache_ttl_seconds,
    )
    if args.action == "clear":
        cache.clear()
        print("Кеш очищен.")
    elif args.action == "evict":
        removed = cache.evict_expired()
        cache.set_quota(settings.cache_quota_bytes)
        print(f"Удалено устаревших файлов: {removed}.")
    stats = cache.stats()
    print(
        f"Кеш: {stats.megabytes_used} МБ из {stats.megabytes_quota} МБ, файлов: {stats.count}"
    )
    return 0


def _provider_dependency_checks(provider_id: str) -> list[tuple[str, bool, str]]:
    """Проверить только то, что нужно выбранному провайдеру."""
    if provider_id == "yandex":
        try:
            import yandex_music

            return [
                (
                    "yandex-music",
                    True,
                    getattr(yandex_music, "__version__", "установлена"),
                )
            ]
        except ImportError:
            return [("yandex-music", False, "pip install yandex-music")]
    if provider_id == "local":
        try:
            import tinytag

            return [("tinytag", True, getattr(tinytag, "__version__", "установлен"))]
        except ImportError:
            return [
                (
                    "tinytag",
                    True,
                    "не установлен — теги из имён файлов (extras [local])",
                )
            ]
    if provider_id == "soundcloud":
        return [
            ("soundcloud API", True, "стандартная библиотека, доп. пакеты не нужны")
        ]
    if provider_id == "spotify":
        return [
            (
                "spotify API",
                True,
                "стандартная библиотека; поток отдаёт только приложение Spotify",
            )
        ]
    return []


def _cmd_doctor(args: argparse.Namespace) -> int:
    checks: list[tuple[str, bool, str]] = []

    python_ok = sys.version_info >= (3, 10)
    checks.append(("Python ≥ 3.10", python_ok, sys.version.split()[0]))

    mpv_path = shutil.which("mpv")
    checks.append(("mpv", bool(mpv_path), mpv_path or "sudo apt install mpv"))

    if keyring_disabled():
        checks.append(
            ("keyring", True, "отключён (NEUROWAVE_KEYRING) — токен в файле 0600")
        )
    else:
        try:
            import keyring

            backend = keyring.get_keyring()
            checks.append(("keyring", True, type(backend).__name__))
        except Exception:
            checks.append(
                ("keyring", True, "нет backend — токен будет в файле 0600")
            )

    paths = Paths.resolve()
    try:
        paths.ensure()
        probe = paths.data_dir / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        checks.append(("Каталоги XDG", True, str(paths.config_dir.parent)))
    except OSError as exc:
        checks.append(("Каталоги XDG", False, str(exc)))

    # Повреждённый config.json не должен прекращать диагностику: отмечаем
    # неуспешную проверку и продолжаем с безопасными значениями по умолчанию.
    try:
        settings = ConfigStore(paths).load()
    except ConfigError as exc:
        checks.append(("Конфигурация", False, str(exc)))
        settings = Settings()
    else:
        checks.append(("Конфигурация", True, str(paths.settings_file)))

    provider_id = _resolve_provider_id(args, settings)
    checks.extend(_provider_dependency_checks(provider_id))
    try:
        provider_title = get_spec(provider_id).info.title
        provider_ok, provider_note = True, f"{provider_id} — {provider_title}"
    except NeuroWaveError as exc:
        provider_ok, provider_note = False, str(exc)
    checks.append(("Провайдер", provider_ok, provider_note))

    try:
        needs_auth = get_spec(provider_id).info.auth is not AuthKind.NONE
    except NeuroWaveError:
        needs_auth = True
    token_present = TokenStore(paths, provider=provider_id).present()
    if not needs_auth:
        checks.append(("Токен", True, "не требуется — локальные файлы"))
    else:
        checks.append(
            ("Токен", True, "сохранён" if token_present else "нет — нужен neurowave login")
        )

    if needs_auth:
        host = API_HOSTS.get(provider_id, "api.music.yandex.net")
        network_ok, network_note = _probe_network(host)
        checks.append(("Сеть до API", network_ok, network_note))
    else:
        checks.append(("Сеть", True, "не требуется — файлы на диске"))

    if args.json:
        print(
            json.dumps(
                [
                    {"check": name, "ok": ok, "detail": detail}
                    for name, ok, detail in checks
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if all(ok for _, ok, _ in checks) else 1

    for name, ok, detail in checks:
        print(f"{'✓' if ok else '✗'} {name}: {detail}")
    critical = [name for name, ok, _ in checks if not ok]
    if critical:
        print("\nТребует внимания: " + ", ".join(critical))
        return 1
    print("\nОкружение готово к работе (звук проверяется на реальном устройстве).")
    return 0


def _probe_network(host: str = "api.music.yandex.net", port: int = 443) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=1.5):
            return True, f"{host}:{port} доступен"
    except OSError as exc:
        return False, f"нет соединения: {exc}"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
