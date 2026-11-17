# NeuroWave Music Core — что реализовано

Документ описывает **фактическое** состояние кода, а не план. Всё, что здесь
перечислено, покрыто unit-тестами и запускается на Linux без браузера.
Всё, чего здесь нет, не реализовано.

## Что это

Headless-ядро плеера: авторизация, каталог, «Моя волна», очередь, состояние
воспроизведения, кеш, локальная история и вывод звука через `mpv`.
Это вертикальный срез Sprint 1 из [ТЗ](SPEC.md); Qt-интерфейс, DSP, ИИ и студия
**не входят** в текущий срез. MPRIS и трей реализованы отдельно в
`src/neurowave/integrations` и ядро о них не знает.

```
CLI (сейчас) / Qt UI (позже)
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
| `neurowave me` | аккаунт и подписка; без сети — последнее известное состояние |
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
| `neurowave remote play\|pause\|next\|previous` | управление чужим плеером (`--uri` для play, `--device` переносит звук) |
| `neurowave --json status` | конфигурация, провайдер, кеш, недавняя история |
| `neurowave --provider local play "запрос"` | воспроизведение файлов из локальной папки |
| `neurowave --provider local wave` | случайная выборка из локальной библиотеки |

`--json` — общий флаг и ставится до подкоманды (`neurowave --json status`, не `neurowave status --json`).
Команды `devices`/`connect`/`remote` требуют возможности
`DELEGATED_PLAYBACK`: у Яндекса и локальных файлов они отвечают «не
поддерживает», а не молчат.

В интерактивном режиме: `q` — выход, `n` — следующий, `p` — пауза,
`+`/`-` — громкость, `s` — состояние и качество источника.
`--backend null` отключает звук и полезен для проверки логики.

## Провайдеры музыки

Провайдер — это не «сервис вообще», а набор явных возможностей
(`src/neurowave/providers`). Подключены четыре:

| Провайдер | Возможности | Вход | Кеш |
|---|---|---|---|
| `yandex` | `CATALOG`, `LIBRARY`, `PLAYLISTS`, `PERSONAL_RADIO`, `DIRECT_STREAM` | OAuth Device Flow | да |
| `local` | `CATALOG`, `DIRECT_STREAM`, `SHUFFLE_LIBRARY` | не требуется | нет |
| `soundcloud` | `CATALOG`, `DIRECT_STREAM` | API-ключ (`login --token`) | нет |
| `spotify` | `CATALOG`, `DELEGATED_PLAYBACK` | OAuth PKCE в браузере (запасной путь — `login --token`) | нет |

```bash
neurowave providers                    # что подключено и что умеет
neurowave --json providers             # то же машиночитаемо
neurowave --provider yandex wave       # провайдер на сессию
neurowave --provider local wave        # случайная выборка, не персональный поток
neurowave --provider soundcloud login --token <client_id>   # ключ приложения
neurowave --provider soundcloud play "запрос"               # поток playable-треков в mpv
neurowave --provider spotify login --client-id <id>        # вход по PKCE
neurowave --provider spotify devices                       # устройства Spotify Connect
neurowave --provider spotify connect "Kitchen"             # перенести воспроизведение
neurowave --provider spotify remote play "запрос"          # играть на устройстве Spotify
```

Выбор провайдера хранится в `config.json` (`provider`, по умолчанию `yandex`).
Токен у каждого провайдера свой: запись keyring `<provider>-oauth` или файл
`token.<provider>` с правами 0600 (для Яндекса — исторические имена);
провайдерам с `AuthKind.API_TOKEN` ключ передаётся через `login --token`
(в вывод не попадает); у `AuthKind.OAUTH_BROWSER` (Spotify) — вход по PKCE;
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
neurowave --provider spotify login --client-id <client_id>
neurowave --provider spotify devices
neurowave --provider spotify connect "Living Room"
neurowave --provider spotify remote play "запрос"     # или --uri spotify:track:…
neurowave --provider spotify remote pause|next|previous
```

### Вход Spotify по PKCE

Основной вход — OAuth 2.0 Authorization Code + PKCE: client secret не нужен и
не хранится, браузер возвращает код на loopback, токены не печатаются.

```bash
neurowave --provider spotify login --client-id <client_id>
neurowave --provider spotify login --client-id <id> --port 8899  # если 8888 занят
```

Что нужно зарегистрировать в Spotify:

1. Открыть [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard)
   и создать приложение (Web API).
2. В Settings → Redirect URIs добавить ровно
   `http://127.0.0.1:8888/callback` (при другом `--port` — соответствующий
   адрес). Spotify допускает `http` только для loopback `127.0.0.1`;
   остальные адреса должны быть `https`.
3. Скопировать **Client ID**. Client secret для PKCE не нужен и не должен
   попадать в репозиторий или настройки.
4. Войти и подтвердить доступ: access и refresh token лягут в `TokenStore`, а
   `client_id` — в `config.json`, чтобы следующий вход не требовал флага.

Запрашиваемые scopes: `user-read-playback-state`, `user-modify-playback-state`,
`user-read-currently-playing`, `user-read-private`. Параметр `state`
проверяется, callback слушает только `127.0.0.1`, код и `code_verifier`
никуда не выводятся. Ручной вход
`login --token <access> --refresh-token <refresh> --client-id <id>` остаётся
запасным путём для headless-окружений.

Токен проверяется запросом `/me` при входе (и пробным обновлением refresh
token, если он есть); `403` переводится в понятное требование Premium или
недостающих scopes, `404` — в «нет активного устройства». `me` показывает
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

## USB Mixtape: планировщик подборки (USB-01)

Реализован только офлайновый планировщик из [плана USB Mixtape](features/usb-mixtape/DELIVERY.md):
пакет `src/neurowave/mixtape` (`models`, `match`, `taste`, `sources/base`,
`planner`) строит неизменяемый `MixtapePlan` из метаданных каталога и
разрешённых файлов источников. Файловых, сетевых, SQLite- и USB-операций в
модуле нет: `LocalExportSource`, UDisks2, writer и CLI — следующие этапы
(USB-02…USB-06).

* **Детерминизм.** Одинаковые кандидаты, seed и веса профиля дают тот же
  `plan_id`; seed влияет только на tie-break равных score.
* **Бюджет.** Целевая длительность 1–12 часов и, опционально, `max_bytes`;
  лишнее попадает в `skipped` с причиной, а не выкидывается молча.
* **Дедупликация.** Нормализованные исполнитель/название и допуск 3 с;
  ремастер с другой длительностью остаётся отдельным треком. `Track.key` сам
  по себе правом на файл не считается.
* **Права.** Экспортируются только `ExportAsset` с `rights=PERMITTED` и
  `eligible=True`; `UNKNOWN`/`DENIED`, отсутствие файла и stream-only
  кандидаты дают `rights_unknown`/`rights_denied`/`no_asset`.
* **Профили.** `car`, `road`, `party`, `chill`, `favorites` — данные с весами
  и лимитом треков на исполнителя. Причина (`reason`) объясняет выбор:
  «лайк», «часто слушаешь», «недавно слушал», «из локальной библиотеки».
* **Preview.** `MixtapePlan.to_dict()` — JSON без абсолютных путей, токенов
  и прав; `shortage_ms` и `skipped` показывают реальную нехватку.

**USB-02 — локальный источник.** `LocalExportSource` принимает только явно
одобренный root и по локальным трекам создаёт `ExportAsset` с фактическим
размером, форматом и lossless-флагом. Абсолютные пути, `..`, NUL, скрытые
сегменты, не-аудио файлы и симлинки за пределы root отклоняются; симлинк
внутри root разрешён, пустой файл — нет. `LocalExportRights(confirmed=False)`
по умолчанию даёт `UNKNOWN`: без подтверждения пользователя планировщик не
берёт файл. `rights_status` отвечает только за реально выданные этим
источником asset id, а не за произвольную строку `local-...`.

**USB-03 — устройства.** `DeviceIdentity` фиксирует UDisks2 object path,
block device (только диагностика), mountpoint, UUID, drive id, removable,
writable, объёмы и системный флаг. `UDisks2DeviceService` строит список через
`ObjectManager.GetManagedObjects`: принимаются removable- или USB-тома,
отбрасываются swap/шифрованные и немонтированные, системные mountpoint и
`HintSystem` помечаются и не предлагаются. Штатные точки монтирования Mint
(`/media/...`) и Fedora (`/run/media/...`) разрешены; системными считаются
только `/run`, `/run/lock` и `/run/user`. `revalidate()` заново читает шину
и отклоняет исчезнувший, изменённый, read-only или системный том.
`FakeUsbDeviceService` воспроизводит те же правила без D-Bus; `dbus-next`
импортируется лениво, запись в `/dev` отсутствует.

**USB-04 — writer.** `SafeWriter` копирует потоково с ограниченным буфером,
считает sha256, делает fsync и проверяет временный файл до commit. Все
операции идут относительно открытого каталога с `O_NOFOLLOW`, поэтому
симлинк-подмена и `..` не выводят запись за target root. Существующий файл не
перезаписывается: commit через no-clobber link с fallback для FAT, иначе
честный `exists`. `manifest.json` пишется атомарно после каждого файла и
позволяет resume: verified-файлы перепроверяются по sha256, пропавшие —
копируются заново, несовпавшие не затираются. Cancel, ENOSPC, EROFS и
пропажа устройства дают `cancelled`/`partial`/`failed` без ложного complete;
чужой или битый manifest приводит к отказу, а не к оптимистичному resume.

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
  прослушивания; данные аккаунта не отправляются наружу. `me` пишет только
  последнее известное состояние аккаунта в файл `0600` без токенов и
  использует его, когда сети нет.
- **`doctor` не врёт о зависимостях.** `mpv` требуется только у провайдеров
  с `DIRECT_STREAM`; у Spotify-пульта он отмечен как необязательный.
- **Кеш ограничен.** Квота по умолчанию 2 ГБ, LRU-вытеснение, TTL 7 дней,
  ручная очистка; кеш не превращается в офлайн-библиотеку. `cache info`
  предупреждает, если свободного места на диске меньше квоты.
- **Корректное состояние.** Переходы плеера валидируются
  (`idle → ready → buffering → playing ↔ paused → stopped`), ошибки живут в
  отдельных overlay-состояниях и не подменяют состояние воспроизведения.

## Границы текущего среза

Не реализовано и не заявлено работающим:

- полноценный Qt-интерфейс, темы, мини-плеер, визуализация;
- адаптеры VK/Deezer/TIDAL/YouTube Music и др. — только реестр и контракт
  возможностей (Яндекс, локальные файлы, SoundCloud и Spotify-пульт уже
  реализованы);
- Qt-интерфейс как основной экран, глобальные горячие клавиши, автозапуск;
- Pure/Enhanced DSP, эквалайзер, лимитер, пресеты устройств, FFT;
- тексты песен, подкасты, Ynison;
- ИИ-диджей, профиль вкуса, голос, TTS, студия генерации;
- AppImage и упаковка;
- USB Mixtape: есть планировщик, `LocalExportSource`, UDisks2-детект и writer;
  нет eject, транскодинга, M3U8, CLI и Qt-экрана;
- выбор аудиоустройства Bluetooth/HDMI из приложения (используется системный
  маршрут PipeWire/PulseAudio).

Проверено в этом окружении: 558 unit-тестов ядра, интеграций, провайдеров,
планировщика, источника, устройств и writer,
запуск CLI
(`doctor`, `status`, `cache`, `tray --help`, обработка отсутствия токена),
импорт и сборка пакета, работа MPRIS на отдельном сессионном шине
(`busctl`: свойства, `PlayPause`, `Next`, `Seek`, `SetPosition`,
`PropertiesChanged`). **Не проверено:** реальные запросы с аккаунтом,
реальное воспроизведение, Bluetooth/HDMI, а также трей на живом рабочем
столе и MPRIS в связке с системной панелью — для этого нужны собственный
аккаунт, звуковое устройство и рабочий стол на целевой машине.

## Тесты

```bash
python -m pytest        # 558 тестов: очередь, состояния, плеер, кеш, SQLite и миграция, адаптеры, маршрутизация, CLI, MPRIS, провайдеры, PKCE, USB-планировщик, LocalExportSource и устройства
```

Тесты не ходят в сеть: адаптер проверяется на фейковом клиенте, аудиовыход —
на `NullBackend`, кеш — на временных каталогах и подменяемых часах.
Миграция SQLite проверяется на настоящей базе схемы v1, локальный провайдер —
на временной папке с файлами (без `tinytag` работает резервный путь метаданных),
SoundCloud — на фейковом транспорте: поиск, отбрасывание превью, отказы
`401/403/429/5xx`, адрес CDN и отключённый кеш. Spotify — так же: URL, Bearer,
тела `PUT`/`POST`, `204` без тела, устройства, каталог и отказы без потока,
обновление по `expires_at` и `401`, ротация refresh token и запись credentials.
USB Mixtape проверяется 140 офлайн-тестами: планировщик (USB-01) — seed и
порядок кандидатов, бюджет времени и байт, dedup между провайдерами,
shortage, права, профили и preview JSON; локальный источник (USB-02) —
approved root, path containment, симлинки и политика `ExportRight`;
устройства (USB-03) — T-001…T-005 на fake-сервисе и managed objects;
writer (USB-04) — T-006…T-015 и T-020…T-026 на временном fake-USB — без
сети и реальных носителей.
Интеграции проверяются без D-Bus-демона и без дисплея: MPRIS-логика
тестируется на заглушке, а `tests/test_integrations_optional.py` отдельными
процессами проверяет, что импорт не тянет `dbus-next`/`PySide6` и что
отсутствие шины или Qt приводит к понятному сообщению, а не к падению.

## Следующие шаги

1. Smoke-тест на Linux Mint: `doctor` → `login` → `wave` 60 минут, замеры
   PSS/CPU и поведение Bluetooth/HDMI. Чек-лист и команды замеров:
   [SMOKE-SPRINT1.md](SMOKE-SPRINT1.md).
2. ADR о выборе UI-основы: принят
   [ADR-0003](decisions/0003-ui-foundation.md) — PySide6/Qt Widgets, ядро в том
   же процессе, мост `PlayerBridge`; критерий пересмотра — замер PSS на Mint.
3. MPRIS + трей: реализованы (`neurowave wave --mpris`, `neurowave tray`),
   ядро не менялось; осталось проверить на живом рабочем столе Mint.
4. Провайдеры: гейт из ADR-0004 закрыт, подключены локальные файлы, SoundCloud
   и Spotify-пульт; следующее — smoke на целевой машине и решение по VK/Deezer/
   TIDAL (только при официальной потоковой поддержке).
5. Sound Lab: Pure/Enhanced поверх `mpv`-фильтров, отдельные пресеты для
   Bluetooth и HDMI.
6. Только после этого — ИИ-диджей как отдельный процесс с валидируемыми
   инструментами и проверкой каждого ID.
