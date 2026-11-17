# NeuroWave — задания исполнителю (17.11.2026)

**Цель:** стабилизировать уже реализованный плеер. **НЕ добавлять новые фичи.**

**Рабочий репозиторий:** https://github.com/IIaJIaDuH/NeuroWave  
**База:** только актуальный `origin/main` на момент начала конкретной задачи.  
**Приоритет:** WO-01 и WO-03 (P0), затем WO-02 (P1), затем WO-04 (P1).  
**Вид сдачи:** **отдельная ветка и PR на каждую задачу**, без прямого push/force в main, без самостоятельного merge. Следовать [REVIEW-GATES.md](REVIEW-GATES.md).

## ОБЩИЙ ПРОМПТ ИСПОЛНИТЕЛЮ — прочитать до работы

Ты — инженер NeuroWave, задача — **доказанная работоспособность и контроль регрессий**, а не скорость генерации строк. Репозиторий содержит Python headless Music Core, внешний mpv с JSON IPC, ProviderRouter, Qt Widgets UI, Sound Lab и MPRIS. На входе НЕ придумывай отсутствующие модули и НЕ меняй архитектуру без измеренного повода.

Обязательный порядок:

1. `git status --short`, `git fetch origin`, `git switch main`, `git pull --ff-only`; проверить, что дерево чистое и ветка актуальна; не трогать чужие локальные правки.
2. Прочитать `README.md`, `docs/ARCHITECTURE.md`, `docs/decisions/0004-multi-provider.md`, `docs/decisions/0005-audio-engine.md`, `docs/BETA-CHECKLIST.md`, `docs/STABILIZATION-PLAN-2026-11-17.md` и данное ТЗ.
3. Перед кодом написать **3–10 пунктов плана и таблицу current contract → expected contract**. Обозначить существующие тесты, предполагаемый сбой, ограничить scope.
4. Создать отдельную ветку `fix/... ` или `refactor/...`; делать короткие коммиты; по возможности сначала воспроизводящий тест, затем исправление.
5. При ошибке/несовпадении внешнего API не выдумывать ответ/успех. В логах и PR НИКОГДА не помещать token, cookie, callback URL с секретами, приватные метаданные аккаунта.
6. Прогнать все релевантные unit + mpv integration + Qt offscreen (с учётом имеющегося окружения), дать **реальные** числа passed/skipped/failed; отсутствие инструмента считать `not run`, не «passed».
7. Проверить `git diff --check`, `git status`, `git diff --stat`, внимательно прочитать собственный patch, проверить `git ls-files` чтобы `.gitignore` не спрятал тесты; приложить `git diff --name-status`.
8. Открыть PR с описанием: проблема, до/после, изменённые файлы, команды и результаты тестов, безопасность/лицензии, известные ограничения, откат.
9. **Не начинать WO-02/04, пока результаты предыдущего PR не рассмотрены**, если они затрагивают одни и те же модули.

Запрет: масштабный переписывающий рефакторинг, изменение публичных CLI/JSON contracts ради красоты, переход на новый аудиодвижок, дополнительная сеть в CI, изменение секретного хранилища, сторонний пакет без license review, ручные правки чужих локальных файлов.

---

## WO-01 · P0 — отказоустойчивость Яндекс Музыки

### Проблема

`yandex-music` — неофициальная библиотека. Изоляция через `YandexMusicAdapter` есть, но изменения чужого API могут незаметно сломать основного провайдера. Нужна защита на наших границах, **не** попытка переписать или обойти API.

### Реальные точки кода (проверить перед началом)

- `src/neurowave/yandex/adapter.py` — основной адаптер (около 404 строк), `_call`, `_to_track`, `wave_tracks`, `stream_url`, `account`, `search`, `device_login`.
- `src/neurowave/providers/yandex.py`, `src/neurowave/providers/router.py`, `src/neurowave/providers/base.py` — информация и capability boundary.
- `src/neurowave/domain.py`, `src/neurowave/errors.py`, `src/neurowave/player.py` — контракт наружу.
- `tests/test_yandex_adapter.py`, `tests/test_routing.py`, `tests/test_player.py` — действующие тесты, расширять, не дублировать.

### Scope и задачи

1. Нарисовать матрицу **конкретных вызовов** библиотеки (`init/account/search/users_likes_tracks/rotor_station_tracks/tracks_download_info`) → ожидаемые поля → состояние неизвестных/пустых/изменённых ответов. Отдельно проверить изменения api-объектов версии установленной зависимости, не предполагать их стабильность.
2. Ввести **обезличенные JSON/typed fixture snapshots** для успешных и изменённых форм ответа без настоящих токенов, идентификаторов аккаунтов и защищённого контента. Если snapshot не строится безопасно, использовать синтетические объекты/фейковые клиенты.
3. Тестировать `None`, пустой список, отсутствие вложенных полей, неизвестное перечисление, неожиданный тип чисел/длительности/ID; не передавать в UI испорченные модели. Валидировать только необходимые поля, не падать на новых неизвестных полях.
4. Негативные сценарии: 401/expired token → понятный `AuthError`; 403 или признаки ограничения подписки → `SubscriptionRequired`/контролируемый `ApiError` по действующему контракту; 404 → `TrackUnavailable`; 429/timeout/network/5xx → конечный retry/backoff и понятная ошибка без бесконечных повторов. Не навешивать retry на операции записи/лайков, если повтор может иметь побочный эффект.
5. При несовместимом ответе сервиса — `provider_unavailable` или имеющийся доменный error code, не crash главного `PlayerService` и не бесконечная загрузка; локальный провайдер, кеш метаданных, MPRIS и GUI не должны требовать успешного `Client.init()` Яндекса для своего запуска.
6. Добавить понятную диагностику `doctor`/logs по **типу** ошибки и ожидаемому действию, без вывода секретов и сырых server-body; не печатать URL доступа к аудиопотоку.
7. Не заставлять `ProviderRouter` переводить неофициальные vendor classes в ядро. Всё vendor-specific только внутри адаптера.
8. Не обращаться в приватные сетевые методы или сторонние сервисы в unit/CI. **Полевой тест с действующим аккаунтом — только вручную на локальном Mint** и с явным согласованием, токен остаётся в keyring.

### Приёмка (Definition of Done)

- [ ] Контрактные fixtures и негативные тесты: success/empty/schema-change, 401/403/404/429/5xx/timeout, invalid download info, cancelled login.
- [ ] Все ошибочные сценарии завершаются **за конечное время** (не infinite retry, no GUI freeze).
- [ ] Проверен `--provider local` полностью офлайн и при подменённом отказавшем Yandex adapter.
- [ ] Нет новых обязательных зависимостей; публичные `Track`/`StreamInfo` и `provider:track_id` не изменены.
- [ ] Unit+integration CI зелёные, human-readable failure и no secrets.
- [ ] Описано, что **изменение Яндекс API по-прежнему может ломать интеграцию**, но не весь плеер.

**Ветка:** `fix/yandex-contract-resilience`. **Один PR** (или два: тестовые фикстуры и исправления, если diff слишком велик). Никаких фич SoundCloud/Spotify/USB в этом PR.

---

## WO-02 · P1 — разрезать CLI без изменения поведения

### Проблема

`src/neurowave/cli.py` — ~1336 строк. Здесь одновременно argparse parser, Context/bootstrap, provider authentication, command dispatcher, playback loop, doctor, вывод, звук, cache, MPRIS. Реальный риск избыточной связанности и конфликтов новых PR.

### Scope

**Никакого редизайна команд**. Цель — тонкий `cli.py` (точка входа и совместимые импорты), небольшие модули с отдельной ответственностью. Предлагаемая структура не догма; проверить импорты, чтобы не сделать circular dependency:

```text
src/neurowave/
  cli.py                      # main()/build_parser() API compatibility façade
  cli_commands/
    __init__.py
    parser.py                 # argparse и только flags
    context.py                # Context, config/token/bootstrap
    auth.py                   # login/logout/me
    catalog.py                # search/library/playlists/like/unlike/providers
    remote.py                 # devices/connect/remote Spotify
    sound.py                  # sound/output
    playback.py               # play/wave/playlist/tray/session, signals/MPRIS
    diagnostics.py            # status/doctor/cache
```

1. Снять baseline **списка команд, `--help`, global flags, codes и JSON** до правок. Реальные команды существующей версии: `login/logout/me/search/wave/play/devices/connect/remote/library/playlists/playlist/tray/like/unlike/cache/sound/output/status/doctor/providers/app`.
2. Найти все прямые импорты `neurowave.cli._*`, `main`, `build_parser` в тестах и приложении. В первые PR поддерживать legacy фасад/реэкспорт, не ломать monkeypatch targets и `python -m neurowave`.
3. Выносить **один компактный вертикальный срез** за PR: сначала parser/diagnostics, затем auth/catalog и sound, потом playback/MPRIS. Текущий `_dispatch` и `Context` сохраняют форму, пока не доказана необходимость изменения.
4. Сохранить lazy-import Qt и dbus, чтобы `neurowave --help` не импортировал PySide6/mpv и не требовал сети или токена.
5. Не менять тексты пользовательских ошибок/JSON/exit codes без отдельного согласия. Нельзя превратить `--json` из глобального флага в локальный.
6. Все существующие тесты `tests/test_config_cli.py`, `tests/test_runtime_safety.py`, `tests/test_local_provider.py`, `tests/test_spotify.py`, `tests/test_mpris.py`, `tests/test_ui_qt.py` должны пройти.
7. Добавить snapshot/contract unit, который сравнивает дерево argparse и `--help`, а также JSON-структуры `doctor/status/providers` в чистой изолированной среде. Проверить `login --help`, `app --help`, `sound --help`, `output --help`, `wave --help`.
8. Стандартизировать import rules: домен не импортирует CLI, модули команд не импортируют друг друга по кругу, все бэкенды создаются на границе и управляются владельцем жизненного цикла.

### Приёмка

- [ ] Публичные команды/аргументы/`--help`/JSON/exit codes эквивалентны baseline, изменения обоснованы и отмечены.
- [ ] Вход `neurowave`, `python -m neurowave` и direct `from neurowave.cli import main, build_parser` сохраняются.
- [ ] `cli.py` существенно уменьшен (ориентир <350 строк, но не за счёт копирования monolith в `_impl.py`).
- [ ] Нет циклических импортов и роста baseline PSS/времени старта в разы.
- [ ] Все тесты на fake mpv и настоящем mpv успешны, потоки/sockets закрываются.
- [ ] Diff разделён минимум на два читаемых PR, каждый можно откатить отдельно.

**Ветки:** `refactor/cli-commands-01`, `refactor/cli-commands-02`, ... по одному PR. Никаких одновременных изменений API Yandex, если уже идёт WO-01.

---

## WO-03 · P0 — реальное GUI + hardware QA на Linux Mint

**Только агент на твоей машине с доступом к дисплею и аудио**, здесь через GitHub это не проверить.

### Подготовка

1. Использовать **актуальную main** и личный локальный `.venv`, не коммитить `.venv`, `.tools/AppDir`, логи с токенами, персональные каталоги.
2. `pip install -e '.[dev,desktop,local]'`; запустить `neurowave doctor`, `neurowave app` с фактическим доступным mpv (помнить, что на текущем сервере его бинарник может быть внутри `.tools/AppDir`, а не в `PATH`).
3. Записать OS, Python, mpv, Qt, PipeWire, CPU, VRAM/RAM, доступные USB/BT/HDMI устройства и действительно использованный codec (без серийных номеров и приватных идентификаторов).

### Сценарии, ручная проверка — отмечать pass/fail/not-run

- [ ] Старт GUI из установленного `neurowave app` и ярлыка; окно закрывается, mpv-сокет и процесс не остаются сиротами.
- [ ] 30–60 минут реального прослушивания: старт, пауза, продолжение, след/пред, seek в начале/конце, закрытие во время воспроизведения.
- [ ] Запуск «Моей волны», без ошибок/с контролируемой ошибкой при недействительном токене; теги/кодек/битрейт честны.
- [ ] 50 быстрых next/prev с чередованием seek и pause: нет двойных `end-file`, расхождения реальной очереди и UI.
- [ ] Громкость 0, 30, 100 и запрос >100: фактический mpv не усиливает выше 100, указаны понятные причины.
- [ ] Pure↔Enhanced, пользовательский PEQ/limiter, переключение профиля во время воспроизведения: нет сильного клипа и `dsp_confirmed` соответствует действительности.
- [ ] Bluetooth disconnect/reconnect, переключение system output; неизвестное устройство отклонено, не показывать обещанный несуществующий кодек.
- [ ] Физический HDMI/DP — на слух, если реально есть колонки/ресивер и доступен человек; иначе `not run`, не выдавать digital meter за аудиальное подтверждение.
- [ ] Сеть пропала и восстановилась, локальные файлы работают без Яндекса, UI остаётся отзывчивым. Никаких secrets в окне/логах.
- [ ] UI остаётся отзывчивым при долгой операции; PSS/CPU/mpv underruns и fd/thread drift измерены.

### Протокол артефактов

```text
docs/QA-GUI-2026-11-17.md
  Machine and versions (no secrets)
  Scenarios: pass/fail/not-run and repro steps
  Screenshots optional, PRIVATE user content blurred
  Measurement: PSS/RSS/VmSwap/SwapPss/CPU/fd/threads
  Problems and severity (P0/P1/P2)
  Verdict: Daily-driver ready? Yes/No with evidence
```

**Не жульничать:** если проверяли только `mpv --ao=null` или Qt offscreen, физический вывод наушников/HDMI отмечать `not run`. Логи с персональной историей не выкладывать.

---

## WO-04 · P1 — корректный memory benchmark и релизные артефакты

1. `packaging/soak.py`: проверить, почему в отчёте за ~360 мин указано **71 CSV-строка** при `--interval 30` (ожидалось порядка 720). Сопоставить исходные timestamps/частоту, не менять задним числом итог 2690 смен. При необходимости отдельно считать jitter/пропущенные интервалы и warning.
2. Добавить измерение `SwapPss` из `/proc/PID/smaps_rollup` (где доступно), совместно с RSS/VmSwap/PSS; **не путать** PSS+SwapPss и RSS+VmSwap. Поддерживать ситуацию, когда `smaps_rollup` недоступен.
3. Не определять утечку только по `last PSS - first PSS`, если память активно уходит в swap. Отдельные вердикты: `NO OBVIOUS FD/THREAD LEAK`, `MEMORY INCONCLUSIVE DUE TO SWAP`, `POSSIBLE MEMORY GROWTH`. За 6 часов сравнивать после warm-up, смотреть тренд.
4. Прогнать 30–60 мин со swap=0 в headless и одновременно реальное GUI/audio, не собирать 6-часовой тест без согласования. Числа и фактические длительности в CSV/отчёте.
5. Аудировать содержимое `python -m build`, `wheel METADATA`, LICENSE, `docs/THIRD-PARTY-NOTICES.md`, реальные зависимости, перед упаковкой `.deb`/AppImage. Не маркировать их готовыми без сборки.
6. Полевая проверка физического HDMI/DP и AAC/LDAC — только если устройство действительно поддерживает; иначе задокументировать `not available`.
7. Отдельный PR на инструмент + QA-отчёт. Никаких движковых изменений ради красивых графиков.

### Acceptance

- [ ] Memory verdict корректен и не скрывает swap.
- [ ] Дрейф/sampling interval подтверждён логом; превышенные интервалы отмечаются.
- [ ] Неубранные дескрипторы/процессы являются ошибкой, а не нормой.
- [ ] Hardware/material: источники аудио не содержат copyrighted material из чужих сервисов в репозитории.
- [ ] BETA-CHECKLIST ссылается на настоящий отчёт и реальные цифры, а не на предположения.

---

## Шаблон итогового отчёта для каждого PR

```text
PR: # / branch / base SHA
Scope (что изменено / чего НЕ касались):
Before: reproducer / failing test:
After: фактическое поведение:
Tests: команда → passed / failed / skipped / not-run
Physical tests: pass / fail / not-run; used hardware:
CPU/PSS/SwapPss: measured / not measured:
Licenses/dependencies: added / removed / unchanged:
Secrets/PII/media files: excluded:
Risks / rollback:
Open follow-ups:
```

**Stop condition:** не встраивать в main код, который агент не может объяснить по конкретным строкам и подтвердить тестом. Если внешний API или GUI ломается — сообщить честную причину и сделать минимальный PoC, а не выдумывать успех.
