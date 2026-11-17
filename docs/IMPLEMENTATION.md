# NeuroWave Music Core — что реализовано

**Актуализировано 17.11.2026.** Документ разделяет реализованные модули и аппаратные/полевые проверки. Наличие теста или кода **не означает**, что все сценарии GUI и аудиоустройства проверены на реальном железе. Подробный статус — [бета-чеклист](BETA-CHECKLIST.md).

## Что это

Работает headless-ядро плеера: авторизация, каталог, «Моя волна», очередь, состояние, кеш, история и отдельный `mpv` через JSON IPC. **Поверх него уже реализованы** базовый Qt Widgets UI (`ui/app.py`, `ui/viewmodel.py`, `ui/soundlab.py`), Pure/Enhanced DSP, пресеты и выбор выхода. GUI использует `CommandDispatcher`, а не блокирует поток окна синхронными IPC-запросами. **ИИ-диджей, FFT-рендерер и USB Mixtape пока не реализованы.** MPRIS/трей остаются опциональными интеграциями. См. [архитектуру](ARCHITECTURE.md).

```
CLI / Qt Widgets UI (базовая реализация)
        │
PlayerService ── Queue ── PlayerStateMachine
        │            │
        │            └── SQLite (история, лайки, снимок очереди)
        ├── ProviderRouter ── выбор адаптера по track.provider (`provider:track_id`)
        ├── YandexMusicAdapter ── OAuth Device Flow, поиск, волна, ссылки на поток
        ├── LocalMusicAdapter ── файлы из local_music_dir, без сети и кеша
        ├── SoundCloudAdapter ── поиск и поток playable-треков, без кеша
        ├── MediaCache ── лимит 2 ГБ, LRU, TTL 7 дней, предзагрузка
        └── AudioBackend ── mpv (JSON IPC) | null (тесты и диагностика)
```

## Установка

Нужны Python ≥ 3.10 и `mpv` для звука.

```bash
sudo apt install mpv
python3 -m venv .venv
. .venv/bin/activate
pip install -e .            # при желании: pip install -e '.[keyring]'
neurowave doctor            # проверка окружения, без токенов
neurowave login             # OAuth Device Flow в браузере
neurowave wave              # «Моя волна», управление из терминала
```

Токен хранится в системном keyring, если он доступен; иначе — в файле с
правами `0600` в `~/.local/share/neurowave/token`. Токен не выводится в
логи, диагностику и отчёты.

## Ярлык на рабочем столе

После установки окружения:

```bash
./packaging/install-desktop.sh          # меню приложений + рабочий стол
./packaging/install-desktop.sh --menu   # только меню
```

Скрипт создаёт `~/.local/share/applications/neurowave.desktop` и ярлык на
рабочем столе с абсолютным путём к `.venv/bin/neurowave`, запускает
`neurowave wave` в терминале, ставит иконку из `packaging/neurowave.svg` и
отмечает файл доверенным через `gio` (требование Cinnamon в Linux Mint).
Если `mpv` не найден, скрипт предупреждает об этом, но ярлык всё равно создаёт.

Удалить ярлык: `rm ~/.local/share/applications/neurowave.desktop ~/Desktop/neurowave.desktop`.

## Команды

| Команда | Назначение |
|---|---|
| `neurowave --json doctor` | окружение: Python, зависимости выбранного провайдера, mpv, keyring, каталоги, токен, сеть |
| `neurowave login` / `logout` | вход по Device Flow и удаление токена |
| `neurowave me` | аккаунт и признак подписки Плюс |
| `neurowave search "запрос"` | треки, исполнители, альбомы, плейлисты |
| `neurowave wave [--seconds N]` | непрерывная «Моя волна» с feedback |
| `neurowave wave --mpris` | то же + публикация в MPRIS (панель, `playerctl`, медиа-клавиши) |
| `neurowave play "запрос"` | поиск → очередь → воспроизведение |
| `neurowave play --id 123456` | воспроизведение по ID (сырой ID выбранного провайдера) |
| `neurowave playlist 3` | воспроизведение своего плейлиста по `kind` |
| `neurowave tray` | волна + значок в системном трее + MPRIS (нужен extras `ui`) |
| `neurowave library [--limit N]` | понравившиеся треки |
| `neurowave playlists` | список своих плейлистов |
| `neurowave like ID` / `unlike ID` | лайк и снятие лайка |
| `neurowave cache info\|clear\|evict` | состояние и очистка кеша |
| `neurowave providers` | провайдеры: что подключено, что умеет, что не подключено и почему |
| `neurowave devices` | устройства Spotify Connect (`*` — активное) |
| `neurowave connect "имя"` | перенести воспроизведение на устройство Spotify |
| `neurowave remote play\|pause\|next\|previous` | управление чужим плеером (`--uri` для play) |
| `neurowave --json status` | конфигурация, провайдер, кеш, недавняя история |
| `neurowave --provider local play "запрос"` | воспроизведение файлов из локальной папки |
| `neurowave --provider local wave` | случайная выборка из локальной библиотеки |
| `neurowave app` | базовое окно Qt: transport, seek, громкость, выходы, Sound Lab (extras `[ui]`) |
| `neurowave sound --profiles` | встроенные/свои DSP-профили; Pure/Enhanced, PEQ, limiter |
| `neurowave output` | доступные аудиовыходы, тип/кодек, установка выхода |

`--json` — общий флаг и ставится до подкоманды (`neurowave --json status`, не `neurowave status --json`).
Команды `devices`/`connect`/`remote` требуют возможности
`DELEGATED_PLAYBACK`: у Яндекса и локальных файлов они отвечают «не
поддерживает», а не молчат.

В интерактивном режиме: `q` — выход, `n` — следующий, `p` — пауза,
`+`/`-` — громкость, `s` — состояние и качество источника.
`--backend null` отключает звук и полезен для проверки логики.

## Провайдеры музыки

Провайдер — это не «сервис вообще», а набор явных возможностей
(`src/neurowave/providers`). Реализованы четыре:

| Провайдер | Возможности | Вход | Кеш |
|---|---|---|---|
| `yandex` | `CATALOG`, `LIBRARY`, `PLAYLISTS`, `PERSONAL_RADIO`, `DIRECT_STREAM` | OAuth Device Flow | да |
| `local` | `CATALOG`, `DIRECT_STREAM`, `SHUFFLE_LIBRARY` | не требуется | нет |
| `soundcloud` | `CATALOG`, `DIRECT_STREAM` | API-ключ (`login --token`) | нет |
| `spotify` | `CATALOG`, `DELEGATED_PLAYBACK` | OAuth access token (`login --token`) | нет |

```bash
neurowave providers                    # что подключено и что умеет
neurowave --json providers             # то же машиночитаемо
neurowave --provider yandex wave       # провайдер на сессию
neurowave --provider local wave        # случайная выборка, не персональный поток
neurowave --provider soundcloud login --token <client_id>   # ключ приложения
neurowave --provider soundcloud play "запрос"               # поток playable-треков в mpv
neurowave --provider spotify devices                       # устройства Spotify Connect
neurowave --provider spotify connect "Kitchen"             # перенести воспроизведение
neurowave --provider spotify remote play "запрос"          # играть на устройстве Spotify
```

Выбор провайдера хранится в `config.json` (`provider`, по умолчанию `yandex`).
Токен у каждого провайдера свой: запись keyring `<provider>-oauth` или файл
`token.<provider>` с правами 0600 (для Яндекса — исторические имена);
провайдерам с `AuthKind.API_TOKEN` ключ передаётся через `login --token`
(в вывод не попадает);
провайдерам с `AuthKind.NONE` токен не нужен, и `login` так и говорит.

### Квалифицированные ID

`Track.key` = `provider:track_id`; `Track.id` остаётся сырым ID для адаптера.
По `key` работают очередь, история, лайки, снимок очереди и ключи дискового
кеша, поэтому одинаковые ID у разных провайдеров не смешиваются.
`ProviderRouter` отдаёт `stream_url`, `wave_tracks` и `wave_feedback` тому
адаптеру, которому принадлежит трек, а `cache_allowed` решает, можно ли класть
поток в кеш. SQLite переведён на схему v2: при первом открытии старой БД
история и лайки получают провайдера `yandex` (других провайдеров тогда не было).

### Локальные файлы

`LocalMusicAdapter` сканирует `local_music_dir` (по умолчанию `~/Music`),
читает теги через `tinytag` (MIT), если он установлен (`pip install -e '.[local]'`),
иначе берёт «Исполнитель - Название» из имени файла и папку как альбом.
Скрытые файлы и не-аудио пропускаются; пути проверяются на выход за пределы
папки; `lossless` выставляется только по расширению. `PERSONAL_RADIO` у него
нет намеренно: «волна» — случайная выборка, CLI предупреждает об этом.
`library`, `like` и `playlists` возвращают понятный отказ «не поддерживает»,
а не пустой список. Кеш и предзагрузка отключены: файл уже на диске.

### SoundCloud

`SoundCloudAdapter` подключён ровно в границах API: поиск и поток для
`playable`-треков. Ключ приложения или пользовательский access token
проверяется дешёвым запросом при входе и сохраняется тем же `TokenStore`
(`neurowave login --token <ключ>`): неверный ключ отклоняется сразу и не
записывается, а при недоступной сети ключ сохраняется с предупреждением.
Access token отправляется заголовком `Authorization: OAuth`, client_id —
параметром `client_id`. Сеть инкапсулирована в `soundcloud/client.py` на стандартной
библиотеке; транспорт, сон и повторы инъектируются, поэтому тесты не ходят в
интернет.

Что явно не делается: превью и `blocked` треки не попадают в очередь (30 секунд
— не трек), `stream_url` перепроверяет доступность перед выдачей ссылки,
`library`/`playlists`/`wave` отвечают «не поддерживает», а кеш и предзагрузка
отключены (`cache_allowed=False`) — постоянное хранение запрещено условиями.
Атрибуция обязательна: исполнитель берётся из `user.username`, страница трека
лежит в `Track.url` и печатается в `search` и при воспроизведении.

### Spotify — только «пульт»

`SpotifyAdapter` умеет ровно две вещи: каталог (`CATALOG`) и управление чужим
устройством (`DELEGATED_PLAYBACK`). `stream_url` в адаптере нет намеренно —
Web API не выдаёт ссылку на поток, поэтому `mpv` не участвует, а `neurowave
play` для Spotify отвечает «не поддерживает: поток в mpv» ещё до проверки
входа.

```bash
neurowave --provider spotify login --token <access token> \
  --refresh-token <refresh token> --client-id <client_id>
neurowave --provider spotify devices
neurowave --provider spotify connect "Living Room"
neurowave --provider spotify remote play "запрос"     # или --uri spotify:track:…
neurowave --provider spotify remote pause|next|previous
```

Токен проверяется запросом `/me` при входе; `403` переводится в понятное
требование Premium, `404` — в «нет активного устройства». `me` показывает
подписку Premium, если сервис вернул `product=premium`. Кеша нет
(`cache_allowed=False`): хранить нечего, звук остаётся в Spotify. Сеть
изолирована в `spotify/client.py`, транспорт подменяется в тестах.

**Жизнь токена.** С `--refresh-token` и `--client-id` credentials лежат в
одном JSON-слоте `TokenStore` (`spotify/auth.py`, плоский токен тоже читается —
старые входы не ломаются). Клиент обновляет access token заранее по
`expires_at` и один раз по `401`, затем повторяет исходный запрос; новый
`refresh_token` и срок сохраняются, а CLI после команды записывает обновлённые
credentials обратно. Если обновление не настроено или refresh отклонён, ошибка
называет причину и точную команду входа: «Токен Spotify истёк или отозван,
обновление не настроено. Выполните вход заново: neurowave login --token …
--refresh-token … --client-id …».

Плоский access token без refresh остаётся рабочим, но живёт час — при
истечении `play`/`devices` попросят войти заново, а не покажут «неизвестную
ошибку».

Сторонние провайдеры подключаются без правки ядра — entry points группы
`neurowave.providers`, возвращающие `ProviderSpec`:

```toml
[project.entry-points."neurowave.providers"]
soundcloud = "my_provider:spec"
```

**Честные ограничения** (детали и ссылки — [ADR-0004](decisions/0004-multi-provider.md)):

* поток в `mpv` официально отдают Яндекс Музыка, SoundCloud (для `playable`
  треков) и локальные файлы;
* Spotify, Deezer, TIDAL, Apple Music отдают только каталог/библиотеку, а
  воспроизведение — в своём плеере или через SDK; Spotify возможен только как
  «пульт» через Spotify Connect;
* у VK Музыки публичного музыкального API нет (аудио-методы закрыты с 2016),
  YouTube Music отдельного API не имеет, а извлечение аудио запрещено политикой.

## MPRIS и системный трей

Ядро о D-Bus и Qt не знает: интеграции живут в `src/neurowave/integrations`
и вызывают только публичные методы `PlayerService`. В `PlayerService` ничего
не менялось.

* `neurowave wave --mpris` — сессия в терминале плюс сервис
  `org.mpris.MediaPlayer2.neurowave` на пути `/org/mpris/MediaPlayer2`.
* `neurowave tray` — то же плюс значок в системном трее: пауза, следующий,
  предыдущий, текущее качество источника, выход.
* Поддерживаются `Play`, `Pause`, `PlayPause`, `Stop`, `Next`, `Previous`,
  `Seek`, `SetPosition` (игнорируется для чужого `trackid`), свойства
  `PlaybackStatus`, `Metadata`, `Position`, `Volume` (0.0 … 1.3) и сигнал
  `PropertiesChanged` раз в 0.5 с при изменениях.
* `CanQuit` включается только когда сессии передан колбэк завершения; из CLI
  это `false`, чтобы MPRIS-клиент не убил воспроизведение.
* Нет `dbus-next`, нет сессионной шины или нет `PySide6` — сессия продолжает
  играть, а причина печатается в stderr. Зависимости: extras `mpris`, `ui`
  или `desktop`.

```bash
pip install -e '.[desktop]'      # MPRIS + трей + keyring
playerctl -p neurowave status    # если установлен playerctl
busctl --user introspect org.mpris.MediaPlayer2.neurowave /org/mpris/MediaPlayer2
```

## Гарантии, заложенные в код

- **Честное качество.** Показывается кодек и битрейт фактически выданного
  потока; `lossless` выставляется только по кодеку (`flac`, `alac` и т. п.),
  а не по наличию подписки.
- **Никаких выдуманных треков.** В очередь попадают только объекты,
  полученные от сервиса; поиск и очередь работают с реальными ID.
- **Устойчивость к сбоям.** Сетевые ошибки повторяются с экспоненциальным
  backoff; `401` сразу превращается в требование повторного входа; недоступный
  трек пропускается, но после двух подряд ошибок сессия останавливается с
  понятным overlay-состоянием вместо бесконечных запросов.
- **Ошибка feedback не ломает музыку.** Отправка `trackStarted`/`skip`/
  `finished` в «Мою волну» проглатывается при сбое.
- **Треки не смешиваются.** Любая запись — это `provider:track_id`; очередь,
  история, лайки и кеш различают одинаковые ID разных провайдеров.
- **Аудио не ждёт UI и сеть.** Аудиовыход — отдельный процесс `mpv`; ядро
  только отдаёт ссылку и команды.
- **Приватность.** В логах и `doctor` нет токена, заголовков и истории
  прослушивания; данные аккаунта не отправляются наружу.
- **Кеш ограничен.** Квота по умолчанию 2 ГБ, LRU-вытеснение, TTL 7 дней,
  ручная очистка; кеш не превращается в офлайн-библиотеку.
- **Корректное состояние.** Переходы плеера валидируются
  (`idle → ready → buffering → playing ↔ paused → stopped`), ошибки живут в
  отдельных overlay-состояниях и не подменяют состояние воспроизведения.

## Границы текущего среза (17.11.2026)

**Реализовано в коде, но не объявляется полностью готовым пользовательским продуктом:**

- Qt Widgets-окно: транспорт, seek, громкость, запуск волны, Sound Lab, выбор выхода; нет полноценной библиотеки/каталога внутри GUI, оформленной тёмной темы и мини-плеера.
- Pure/Enhanced, PEQ, preamp, limiter, пользовательские пресеты и rollback при отказе mpv; **цифровой boost выше 100 % намеренно выключен**. Pure означает обход *наших* DSP-фильтров, не bit-perfect.
- Менеджер устройств Bluetooth/HDMI через mpv/PipeWire; **физический звук HDMI на слух на целевом стенде не подтверждён**, поддержка AAC/LDAC зависит от реального железа.
- PCM-tap: PoC изоляции только NeuroWave прошёл; **FFT/визуальный спектроанализатор не реализован**.
- Headless-плеер: 6-часовой `--ao=null` soak на 2690 переходов показал стабильные дескрипторы/потоки; память во время этого прогона уходила в swap, что ограничивает вывод об утечках.

**Не реализовано / не проверено до beta:**

- ИИ-диджей, генерация музыки, студия, USB Mixtape (спецификация уже в `main`), глобальные горячие клавиши, полный UI библиотеки/поиска/плейлистов.
- Поддержка новых стриминговых провайдеров за пределами Yandex, Local, SoundCloud, Spotify remote; реальное воспроизведение Spotify в mpv не поддерживается.
- Полевые тесты доступности GUI, переключения сети, долгой работы **с настоящим аудиовыходом и окном**, безопасность бинарной упаковки, полноценный аудит сторонних notices.
- Неофициальный Yandex API: адаптер изолирован, но внешний сервис может поменяться — будущий контрактный тест и деградация в local-only режиме нужны до beta.

Исторический отчёт о Sprint 1 от 16.11.2026 с числами тестов старого среза — [REPORT-2026-11-16.md](REPORT-2026-11-16.md). Текущий статус в [BETA-CHECKLIST.md](BETA-CHECKLIST.md), [ROADMAP](ROADMAP.md).

## Тесты

```bash
pip install -e '.[dev,local,mpris]'
python -m pytest -q -m "not integration" --ignore=tests/test_ui_qt.py

# Если есть Qt:
pip install -e '.[ui]'
QT_QPA_PLATFORM=offscreen python -m pytest -q tests/test_ui_qt.py

# Если установлен настоящий mpv (тесты используют --ao=null):
python -m pytest -q -m integration
```

Тесты на фейковых адаптерах не заменяют аккаунт, физический Bluetooth/HDMI и ручной UX-сценарий. Пакет требует выбранных extras, система — соответствующих библиотек. Точные результаты и ограничения фиксируются отдельно от успешного exit code.

## Следующие шаги

1. Стабилизировать то, что уже написано: ежедневный GUI-smoke на Linux Mint и отсутствие деградации воспроизведения во время действий пользователя.
2. Снизить риск неофициального API Яндекс Музыки: контрактные фикстуры из обезличенных метаданных, типизированные ошибки и проверка local-only после отказов.
3. Разрезать `cli.py` (~1336 строк на 17.11.2026) на независимые обработчики **с сохранением команд, JSON-формата, сообщений ошибок и кодов выхода**.
4. Довести release-gates (права третьих сторон, локальный аудиовыход, измерения памяти без свопинга); только потом расширять USB/AI/FFT.
5. Следовать [плану стабилизации](STABILIZATION-PLAN-2026-11-17.md), [ТЗ агентам](AGENT-WORK-ORDERS-2026-11-17.md) и [политике ревью](REVIEW-GATES.md).
