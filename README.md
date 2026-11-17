# NeuroWave 🎧

[![tests](https://github.com/IIaJIaDuH/NeuroWave/actions/workflows/tests.yml/badge.svg)](https://github.com/IIaJIaDuH/NeuroWave/actions/workflows/tests.yml)

**NeuroWave** — проект лёгкого нативного музыкального плеера для Linux с поддержкой собственного аккаунта Яндекс Музыки, качественным аудиотрактом и опциональным ИИ-диджеем.

> **Статус:** Sprint 1 — работает headless-ядро (авторизация, «Моя волна», поиск, очередь, лайки, плейлисты, кеш, вывод через mpv), локальные файлы, SoundCloud и Spotify-пульт как дополнительные провайдеры, а также MPRIS и системный трей. GUI, DSP и ИИ ещё не реализованы. Это независимый проект сообщества, **неофициальный клиент**, не связанный с Яндексом и не одобренный им.

## Идея

Не держать тяжёлый браузер ради музыки. Приоритеты: стабильное воспроизведение, экономный расход памяти, Bluetooth/HDMI, продуманный интерфейс и управляемое качество звука.

**Уже работает (headless, без GUI):**
- вход через OAuth Device Flow и хранение токена в keyring;
- поиск, понравившиеся треки, свои плейлисты, лайки;
- «Моя волна» с обратной связью сервису и предзагрузкой следующего трека;
- очередь с repeat/shuffle, валидируемое состояние плеера, обработка сбоев;
- дисковый кеш 2 ГБ с LRU/TTL, локальная история в SQLite;
- воспроизведение через `mpv` и CLI-управление, диагностика без токенов;
- локальные файлы из своей папки (`--provider local`) без аккаунта, сети и кеша;
- SoundCloud (`--provider soundcloud`): поиск и поток доступных треков в mpv,
  атрибуция автора и отключённый кеш; ключ — `login --token`;
- Spotify как «пульт» (`--provider spotify`): каталог и Spotify Connect —
  `devices`, `connect`, `remote play|pause|next|previous`; поток остаётся в
  приложении Spotify, кеша нет; токен обновляется по refresh token;
- квалифицированные ID `provider:track_id` в очереди, истории и кеше;
- MPRIS и системный трей: пауза, next/prev и качество источника из панели,
  `playerctl` и медиа-клавиш, без окна.

**Планируется (не реализовано):**
- режимы звука **Pure Hi-Fi** (без DSP) и **Enhanced** (EQ, усиление с защитой от клиппинга, лимитер);
- выбор Bluetooth/HDMI из приложения, глобальные горячие клавиши;
- VK, Deezer, TIDAL и другие сервисы — только при официальной потоковой
  поддержке; у VK публичного музыкального API нет (детали — ADR-0004);
- футуристичный тёмный интерфейс и настоящий спектроанализатор;
- ИИ-диджей: предложения треков по настроению, анализ предпочтений, плейлисты, голосовые команды;
- экспериментальная генерация **собственной** музыки отдельным необязательным модулем.

Доступность Lossless, синхронизации между устройствами, текстов песен и некоторых других функций будет проверяться на прототипе. Неофициальные API могут меняться.

## Документация

- [Концепция и ТЗ](docs/SPEC.md)
- [Что уже реализовано](docs/IMPLEMENTATION.md)
- [Архитектура](docs/ARCHITECTURE.md)
- [Дорожная карта](docs/ROADMAP.md)
- [Отчёт о состоянии на 16.11.2026](docs/REPORT-2026-11-16.md) — что сделано и что осталось
- [QA-прогон на живом окружении](docs/QA-RUN-2026-11-16.md) — реальный mpv, звук, аккаунт: прогоны, дефекты, тесты
- [Замеры Sprint 0](docs/SPRINT0-MEASUREMENTS.md) — память, CPU, старт, Bluetooth/HDMI, PCM/DSP, лицензии
- [Риски и ограничения](docs/RISKS.md)
- [Решения по архитектуре (ADR)](docs/decisions/0001-research-first.md)
- [ADR-0002: Python-ядро как первый срез](docs/decisions/0002-python-core-prototype.md)
- [ADR-0003: Основа интерфейса](docs/decisions/0003-ui-foundation.md)
- [ADR-0004: Провайдеры музыки](docs/decisions/0004-multi-provider.md)
- [ADR-0005: Аудиодвижок](docs/decisions/0005-audio-engine.md) — mpv + IPC + lavfi, альтернативы и условия пересмотра
- [Сравнение с CloudMus и YaMusic](docs/COMPARISON-CLOUDMUS-YAMUSIC.md) — старт, память, CPU, локальное воспроизведение
- [ADR-0006: Лицензия](docs/decisions/0006-license.md) — принято: MIT, замена `mutagen` на `tinytag`
- [Бета-чеклист](docs/BETA-CHECKLIST.md) — что проверено, что открыто, замеры soak и памяти
- [Сторонние лицензии и условия поставки](docs/THIRD-PARTY-NOTICES.md) — инвентаризация зависимостей и гейты релиза
- [Smoke-тест Sprint 1 на Mint](docs/SMOKE-SPRINT1.md)

## Текущее состояние

**Sprint 1: Core MVP (headless).** Реализованы адаптер Яндекс Музыки, «Моя волна», поиск, библиотека, лайки, плейлисты, очередь, машина состояний, кеш, SQLite и вывод через mpv. Подробности и границы — в [docs/IMPLEMENTATION.md](docs/IMPLEMENTATION.md).

Сравнение с CloudMus/YaMusic выполнено ([замеры](docs/COMPARISON-CLOUDMUS-YAMUSIC.md)), замеры Sprint 0 — в [SPRINT0-MEASUREMENTS.md](docs/SPRINT0-MEASUREMENTS.md). Открыты: 6-часовой soak (идёт), физический HDMI (порт не подключён) и стриминг конкурентов под живым аккаунтом.

Платформа первой версии: **Linux Mint / Linux x86_64**. Приоритетные аудиоустройства: **Bluetooth и HDMI**.

## Быстрый старт

```bash
sudo apt install mpv
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
neurowave doctor        # проверка окружения (без токенов)
neurowave login         # вход в свой аккаунт через браузер
neurowave wave          # «Моя волна» в терминале
neurowave --provider local wave   # свои файлы из ~/Music (или local_music_dir)
neurowave --provider soundcloud login --token <client_id>  # ключ SoundCloud
neurowave --provider soundcloud play "запрос"              # поток в mpv
neurowave --provider spotify devices                     # устройства Spotify Connect
neurowave --provider spotify remote play "запрос"         # играть на устройстве Spotify
neurowave --provider spotify login --token <access> \
  --refresh-token <refresh> --client-id <id>            # чтобы пульт не отваливался
neurowave output        # выходы: Bluetooth/HDMI, реальный кодек, текущий маршрут
neurowave output --set pulse/bluez_output.…   # выбрать выход (систему не меняем)
neurowave sound         # профиль звука: режим, полосы, лимитер, lavfi-граф
neurowave sound --profile bluetooth --graph   # выбрать профиль и увидеть цепь
neurowave sound --profiles                     # встроенные и свои профили
neurowave app           # окно: транспорт, перемотка, громкость, звук, выход, Sound Lab
neurowave tray          # волна + значок в трее + MPRIS: pip install -e '.[desktop]'
./packaging/install-desktop.sh   # ярлык в меню и на рабочем столе
```

Управление в сессии: `q` — выход, `n` — следующий, `p` — пауза, `+`/`-` — громкость, `e` — Pure/Enhanced, `s` — состояние. Полный список команд: `neurowave --help`, тесты: `python -m pytest`.

## Безопасность и приватность

- Никогда не публикуйте токены Яндекс Музыки, данные аккаунтов, cookie, записи голоса, персональные профили и API-ключи.
- Настройки и секреты должны храниться **локально**, токены — в системном keyring.
- ИИ-подсистема отключаемая и не обязана работать для воспроизведения.
- Для будущих PR запрещены коммиты с личными данными и скомпилированными кешами.

## Лицензирование

**MIT** — см. [LICENSE](LICENSE). Решение и обоснование: [ADR-0006](docs/decisions/0006-license.md).

Сторонние компоненты сохраняют свои лицензии: `mpv` запускается отдельным процессом (GPL-2.0+), `PySide6` — LGPL-3.0 (динамическая линковка), остальные зависимости MIT/Apache-2.0. Теги локальных файлов читает `tinytag` (MIT); GPL-`mutagen` из зависимостей и из кода удалён.

Перед заимствованием компонентов или публикацией бинарников проверяем лицензии исходных проектов, Qt, декодеров и сторонних библиотек. Не импортируем чужой код до юридического и технического решения.

## Дисклеймер

Проект не предназначен для обхода подписки, DRM или ограничений доступа. Используйте только собственную учётную запись в рамках применимых правил сервиса.

---

Обсуждения, идеи и задачи — в [Issues](https://github.com/IIaJIaDuH/NeuroWave/issues).
