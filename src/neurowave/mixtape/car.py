"""Car USB: нумерация, портативные имена и опциональная транслитерация.

Профиль — данные, а не отдельный алгоритм записи: planning и writer общие.
Имена строятся детерминированно, чтобы повторный job не создавал дубли.
"""

from __future__ import annotations

import unicodedata

from neurowave.mixtape.export.filenames import portable_component

CAR_FOLDER = "MUSIC/NeuroWave"
CAR_PLAYLIST = "playlist.m3u8"

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def transliterate(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    result = []
    for char in text:
        lower = char.lower()
        if lower in _TRANSLIT:
            replacement = _TRANSLIT[lower]
            result.append(replacement.upper() if char.isupper() and replacement else replacement)
        elif ord(char) < 128:
            result.append(char)
        else:
            plain = unicodedata.normalize("NFKD", char).encode("ascii", "ignore").decode("ascii")
            result.append(plain)
    return "".join(result)


def car_filename(
    index: int,
    artist: str,
    title: str,
    extension: str = "mp3",
    *,
    ascii_names: bool = False,
    max_bytes: int = 240,
) -> str:
    if ascii_names:
        artist = transliterate(artist)
        title = transliterate(title)
    suffix = "".join(char for char in extension.lower() if char.isalnum()) or "mp3"
    name = f"{max(1, int(index)):03d} - {artist or 'Неизвестный исполнитель'} - {title or 'Без названия'}.{suffix}"
    return portable_component(name, max_bytes=max_bytes)


def car_destination(
    index: int,
    artist: str,
    title: str,
    extension: str = "mp3",
    *,
    ascii_names: bool = False,
) -> str:
    return f"{CAR_FOLDER}/{car_filename(index, artist, title, extension, ascii_names=ascii_names)}"
