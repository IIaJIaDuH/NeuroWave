"""CLI-интерфейс headless-ядра NeuroWave.

Это не заменяет будущий Qt-интерфейс: CLI работает поверх того же сервиса и
позволяет проверить авторизацию, каталог, очередь и аудиовыход без браузера.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sys
import time
from dataclasses import dataclass
from typing import Any, Sequence

from . import __version__
from .audio import MpvBackend, NullBackend
from .config import ConfigStore, Paths, Settings
from .domain import AccountInfo, Track
from .errors import AuthError, NeuroWaveError, PlayerError
from .player import PlayerService
from .queue import Queue
from .state import PlayerStatus
from .storage import Database, MediaCache, TokenStore
from .yandex import YandexMusicAdapter

DEFAULT_SEARCH_LIMIT = 20


@dataclass
class Context:
    paths: Paths
    settings: Settings
    db: Database
    cache: MediaCache
    tokens: TokenStore
    adapter: YandexMusicAdapter


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
    parser.add_argument("--json", action="store_true", help="машиночитаемый вывод")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("login", help="вход через OAuth Device Flow")
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

    sub.add_parser("status", help="состояние конфигурации и локальных данных")
    sub.add_parser("doctor", help="диагностика окружения (без токенов)")
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
def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.home:
        os.environ["NEUROWAVE_HOME"] = args.home
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
            "wave": _cmd_wave,
            "play": _cmd_play,
            "playlist": _cmd_playlist,
            "tray": _cmd_tray,
        }
        return handlers[args.command](args, ctx)
    finally:
        ctx.db.close()


# --- контекст ----------------------------------------------------------
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
    tokens = TokenStore(paths)
    token = tokens.load()
    adapter = YandexMusicAdapter(token=token)
    return Context(
        paths=paths,
        settings=settings,
        db=db,
        cache=cache,
        tokens=tokens,
        adapter=adapter,
    )


def _build_backend(args: argparse.Namespace, settings: Settings):
    choice = getattr(args, "backend", None) or settings.audio_backend
    if choice == "null":
        return NullBackend()
    return MpvBackend()


def _require_token(ctx: Context) -> None:
    if not ctx.adapter.token:
        raise AuthError("Требуется вход. Выполните: neurowave login")


def _console_listener(event: str, payload: dict[str, Any]) -> None:
    if event == "track":
        track = payload.get("track")
        quality = payload.get("quality")
        if track is not None:
            suffix = f" [{quality.label()}]" if quality else ""
            origin = " (кеш)" if payload.get("from_cache") else ""
            print(f"▶ {track.label} — {track.duration_text}{suffix}{origin}", flush=True)
    elif event in {"paused", "resumed", "stopped"}:
        print({"paused": "⏸ пауза", "resumed": "▶ продолжаем", "stopped": "■ остановлено"}[event], flush=True)
    elif event == "error":
        print(f"! {payload.get('error')}", file=sys.stderr, flush=True)
    elif event == "finished":
        print("Очередь завершена.", flush=True)


# --- команды: аккаунт --------------------------------------------------
def _cmd_login(args: argparse.Namespace, ctx: Context) -> int:
    adapter = YandexMusicAdapter()

    def on_code(url: str, code: str) -> None:
        print("Откройте страницу подтверждения:", url)
        print("Введите код:", code)
        print("Ожидаю подтверждения…", flush=True)

    token = adapter.device_login(on_code)
    ctx.tokens.save(token)
    print(f"Вход выполнен. Токен сохранён: {ctx.tokens.backend}.")
    try:
        account = adapter.account()
    except NeuroWaveError as exc:
        print(f"Аккаунт пока недоступен: {exc}")
        return 0
    _print_account(account, args.json)
    return 0


def _cmd_logout(args: argparse.Namespace, ctx: Context) -> int:
    ctx.tokens.delete()
    print("Токен удалён. Персональные данные больше не запрашиваются.")
    return 0


def _cmd_me(args: argparse.Namespace, ctx: Context) -> int:
    _require_token(ctx)
    _print_account(ctx.adapter.account(), args.json)
    return 0


def _print_account(account: AccountInfo, as_json: bool) -> None:
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
    plus = {True: "активна", False: "не активна", None: "неизвестно"}[account.plus]
    print(f"Подписка Плюс: {plus}")
    if not account.service_available:
        print("Сервис сообщает о недоступности — функции каталога могут не работать.")


# --- команды: каталог --------------------------------------------------
def _cmd_search(args: argparse.Namespace, ctx: Context) -> int:
    _require_token(ctx)
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
    if results.artists:
        print("\nИсполнители: " + ", ".join(ref.name for ref in results.artists))
    if results.albums:
        print("Альбомы: " + ", ".join(ref.name for ref in results.albums))
    if results.playlists:
        print("Плейлисты: " + ", ".join(f"{ref.name} ({ref.id})" for ref in results.playlists))
    return 0


def _cmd_library(args: argparse.Namespace, ctx: Context) -> int:
    _require_token(ctx)
    tracks = ctx.adapter.liked_tracks(limit=args.limit)
    if args.json:
        print(json.dumps([track.to_dict() for track in tracks], ensure_ascii=False))
        return 0
    if not tracks:
        print("В понравившихся пока нет треков.")
    for index, track in enumerate(tracks, start=1):
        print(f"{index:2d}. {track.label} — {track.duration_text}  id={track.id}")
    return 0


def _cmd_playlists(args: argparse.Namespace, ctx: Context) -> int:
    _require_token(ctx)
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
    ok = ctx.adapter.like(args.track_id)
    print("Лайк поставлен." if ok else "Сервис не подтвердил лайк.")
    return 0 if ok else 1


def _cmd_unlike(args: argparse.Namespace, ctx: Context) -> int:
    _require_token(ctx)
    ok = ctx.adapter.unlike(args.track_id)
    print("Лайк убран." if ok else "Сервис не подтвердил удаление лайка.")
    return 0 if ok else 1


def _cmd_status(args: argparse.Namespace, ctx: Context) -> int:
    stats = ctx.cache.stats()
    recent = ctx.db.recent_tracks(limit=5)
    data = {
        "version": __version__,
        "home": str(ctx.paths.config_dir.parent),
        "token": {"present": bool(ctx.adapter.token), "backend": ctx.tokens.backend},
        "backend": ctx.settings.audio_backend,
        "volume": ctx.settings.volume,
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
    print(
        "Токен: "
        + ("сохранён" if data["token"]["present"] else "нет")
        + f" ({ctx.tokens.backend})"
    )
    print(f"Аудиовыход: {ctx.settings.audio_backend}, громкость {ctx.settings.volume}%")
    print(f"Кеш: {stats.megabytes_used} МБ из {stats.megabytes_quota} МБ ({stats.count} файлов)")
    if recent:
        print("Недавно слушали:")
        for track in recent:
            print(f"  • {track.label}")
    return 0


# --- команды: воспроизведение -----------------------------------------
def _cmd_wave(args: argparse.Namespace, ctx: Context) -> int:
    _require_token(ctx)
    return _play_session(args, ctx, lambda player: player.start_wave())


def _cmd_play(args: argparse.Namespace, ctx: Context) -> int:
    _require_token(ctx)
    if not args.query and not args.track_id:
        raise PlayerError("Укажите поисковый запрос или --id")
    if args.track_id:
        # Проверяем доступность по ID через поиск ссылки на поток.
        track = Track(id=str(args.track_id), title=f"Трек {args.track_id}")
        return _play_session(args, ctx, lambda player: player.play_track(track))
    results = ctx.adapter.search(args.query, limit=args.limit)
    if not results.tracks:
        raise PlayerError(f"По запросу «{args.query}» треки не найдены")
    tracks = list(results.tracks)
    print(f"Найдено треков: {len(tracks)}. Ставлю в очередь.")
    return _play_session(args, ctx, lambda player: player.play_tracks(tracks))


def _cmd_playlist(args: argparse.Namespace, ctx: Context) -> int:
    _require_token(ctx)
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


def _cmd_doctor(args: argparse.Namespace) -> int:
    checks: list[tuple[str, bool, str]] = []

    python_ok = sys.version_info >= (3, 10)
    checks.append(("Python ≥ 3.10", python_ok, sys.version.split()[0]))

    try:
        import yandex_music

        checks.append(("yandex-music", True, getattr(yandex_music, "__version__", "установлена")))
    except ImportError:
        checks.append(("yandex-music", False, "pip install yandex-music"))

    mpv_path = shutil.which("mpv")
    checks.append(("mpv", bool(mpv_path), mpv_path or "sudo apt install mpv"))

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

    token_present = TokenStore(paths).present()
    checks.append(("Токен", True, "сохранён" if token_present else "нет — нужен neurowave login"))

    network_ok, network_note = _probe_network()
    checks.append(("Сеть до API", network_ok, network_note))

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
