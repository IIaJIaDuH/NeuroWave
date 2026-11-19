"""Построение дерева аргументов CLI NeuroWave.

Выделено из `neurowave.cli` без изменения поведения: только argparse, без
диспетчеризации, провайдеров, mpv, MPRIS и Qt.
"""

from __future__ import annotations

import argparse

from .. import __version__
from ..audio.dsp_profiles import BUILTIN_PROFILES

#: Сколько результатов просить у провайдера, если `--limit` не указан.
DEFAULT_SEARCH_LIMIT = 20


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
