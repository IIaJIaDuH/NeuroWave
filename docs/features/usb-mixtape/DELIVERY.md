# USB Mixtape — реализация по PR, зависимости, чек-листы

**Статус:** USB-00 — спецификация; USB-01…USB-05 (планировщик, источник, устройства, writer, транскод/M3U8/Car) реализованы · 2026-11-17.  
**Ветка спецификации:** feature/usb-mixtape-spec  
**База:** main по состоянию на SHA 1baf6de3b8d30f453c1a4feb2af30a9b2e6c13aa.  
**Приоритет относительно NeuroWave 0.2:** дополнительная фича. Не блокирует ADR-0005, Sound Lab и Qt Desktop.

## 0. Общий принцип реализации

**Не начинать с автоскачивания.** Сначала готовое, безопасное локальное копирование из пользовательской музыкальной библиотеки. Затем персональная подборка. Затем UI. Только после этого можно исследовать официально разрешённые сетевые загрузки.

Слияние фичи в приложение производится через общие контракты, без прямых изменений mpv, AudioBackend и приватных API Yandex/Spotify. Каждый PR должен содержать проверяемую законченную вертикаль, а не только заглушки.

## 1. Feature-ветка и рекомендуемые PR

| Этап | PR и область | Результат | Гейт |
|---|---|---|---|
| **USB-00** | Specification / ADR / acceptance | ТЗ, структура, источники, safety, QA, DoD | Documentation review; без runtime-кода |
| **USB-01** | models + planner + fake sources | Pure offline deterministic MixtapePlan и preview JSON | ≥20 unit на score, dedup, conflicts, shortage, seed |
| **USB-02** | LocalExportSource + rights policy | Только eligible локальные файлы превращаются в ExportAsset | stream-only/Yandex/Spotify не проходят; path escape rejected |
| **USB-03** | UDisks2 + fake USB device API | Достоверный список removable writable volumes, select/revalidate, read-only error | T-001…T-005; никаких raw /dev writes |
| **USB-04** | safe writer + manifest + resume | Copy/verify/partial/cancel/resume на fake USB | T-006…T-015 и T-020…T-026 |
| **USB-05** | codec / playlists / Car profile | Original, MP3 conversion (optional), FLAC guard, m3u8 | synthetic audio, ENOSPC, tags, FAT32/exFAT |
| **USB-06** | CLI headless integration | devices/plan/export/status/cancel/verify/eject + JSON | subprocess integration, без GUI, user consent |
| **USB-07** | Qt Widgets / bridge | USB Mixtape экран с preview, progress, cancellation | UI не блокируется; playback не прерывается |
| **USB-08** | Hardware beta | 2+ физические USB, автомобильная магнитола при наличии, soak and regression | Signed-off manual report; no-data-loss |
| **USB-09** | Optional official download plugin | Один доказанно разрешённый онлайн-источник | legal/API review + auth/SSRF/rate limit tests |
| **USB-10** | Optional AI playlist suggestions | LLM предлагает кандидатов, но не доступ к I/O | baseline без AI не меняется, zero unwanted picks |

**Не считать USB-09 и USB-10 обязательными для USB MVP.** Планировать зависимости функционально, не по «красивым» срокам.

**Сделано в USB-01:** `src/neurowave/mixtape` — неизменяемые модели,
matcher, протокол `ExportSource` и `MixtapePlanner`; `tests/test_mixtape_planner.py`
(47 тестов). Файловых, сетевых и USB-операций в модуле нет.

**Сделано в USB-02:** `sources/local.py` (`LocalExportSource`) и `rights.py`
(`LocalExportRights`, `ExportRight`); `tests/test_mixtape_local_source.py`
(32 теста). Экспорт возможен только из approved root и только после явного
подтверждения прав; абсолютные пути, `..`, NUL, скрытые сегменты, симлинки
наружу и stream-only провайдеры отклоняются.

**Сделано в USB-03:** `devices/base.py` (протокол, `DeviceIdentity`,
`UsbDeviceError`), `devices/fake.py` и `devices/udisks2.py` (чистый маппер +
ленивый D-Bus, без `/dev`); `tests/test_mixtape_devices.py` (35 тестов,
T-001…T-005). Eject и CLI — план.

**Сделано в USB-04:** `export/` — `SafeWriter`, `manifest.py`, `filenames.py`,
ошибки; `tests/test_mixtape_writer.py` (26 тестов, T-006…T-015 и
T-020…T-026): streaming copy с sha256, no-clobber commit, manifest и resume,
cancel, ENOSPC/EROFS/EIO, выдёргивание fake-устройства, rename race и отказ
чужому/битому manifest. Реальная запись — USB-08.

**Сделано в USB-05:** `export/transcoder.py` (ffmpeg/ffprobe через argv без
shell, таймаут, внедряемый runner), `export/playlist.py` (M3U8 только по
verified-файлам), `car.py` (нумерация, портативные имена, транслитерация);
writer умеет TRANSCODE с проверкой формата/длительности. `tests/test_mixtape_transcode.py`
(17 тестов на синтетическом WAV) плюс EIO/rename-race в writer-тестах.

## 2. Отдельные acceptance gates

### G1. Планировщик (после USB-01)

- [x] Выдаёт одинаковый результат при одинаковых seed, библиотеке и весах.
- [x] Показывает целевую и фактическую длительность; никого не «докачивает» без прав.
- [x] Умеет формировать подборку 1–12 часов в пределах доступного каталога и размера.
- [x] Дубликаты с разных провайдеров не дублируются как один Track.id; настоящий другой ремастер не отбрасывается без оснований.
- [x] Рекомендательный профиль не зависит от обязательного API/LLM.

Все пять пунктов закрыты офлайн-тестами USB-01; preview — `MixtapePlan.to_dict()`
(JSON без абсолютных путей и прав).

### G2. Резолвинг файлов (после USB-02)

- [x] На каждый файл в плане есть ExportAsset с разрешённым provenance и фактическим локальным доступом.
- [x] Любой stream_url, Spotify Connect и MediaCache — не основание на экспорт.
- [x] При неполной библиотеке система честно показывает shortage.
- [x] Источник может ошибаться/пропасть без потери корректной части плана.

Все четыре пункта закрыты офлайн-тестами USB-02: approved root, политика
`ExportRight`, path containment и интеграция с планировщиком.

### G3. USB Device (после USB-03)

- [x] Не предлагает системные разделы как записываемые USB.
- [x] Выбранный drive перепроверяется непосредственно перед записью.
- [x] Две флешки или смена mountpoint требуют нового выбора/согласия.
- [x] Все UDisks2 операции необязательны для обычного плеера.

Пункты закрыты офлайн-тестами USB-03 на fake-сервисе и managed objects;
живой UDisks2 проверяется на целевой машине (USB-08).

### G4. Writer (после USB-04)

- [x] Тесты на symlink swap, traversal, ENOSPC, EIO, EROFS, unplug, rename races.
- [x] Нет записи вне одобренного target root; нет silent overwrite.
- [x] Finished означает проверенную контрольную сумму; Partial означает неполный список.
- [x] Repeat job не создаёт дубли, не переписывает чужой каталог.
- [ ] Pause/Cancel/Resume не забирает управление у аудиодвижка.

USB-04 закрывает symlink/traversal, ENOSPC, EROFS, unplug, cancel/resume и
проверку sha256; EIO, rename races и интеграция с playback — USB-05/08.

### G5. Transcode (после USB-05)

- [x] FFmpeg доступен только при установленной разрешённой сборке; иначе disable transcode.
- [x] Никакого заведомого lossy→FLAC под видом Hi-Fi, MP3 320 не обещает качество источника.
- [x] M3U8 работает на выбранном профиле, пути относительные и проверенные.
- [x] Все конвертации идут ограниченными ресурсами, без shell=True.

FFmpeg-путь проверен через внедряемый runner и fake-транскодер; реальный
ffmpeg/ffprobe и FAT32/exFAT проверяются на аппаратном этапе USB-08.

### G6. UI и аппаратная проверка (после USB-07/08)

- [ ] UI остаётся отзывчивым и не блокирует mpv при копировании.
- [ ] Device, folder, разрешённые треки, estimated bytes, skip reasons видны до записи.
- [ ] Полноценный «отключить USB» вызывается только после завершения worker.
- [ ] На реальном USB корректны физическая запись, чтение другим устройством, безопасное извлечение.
- [ ] Учет реального CPU/PSS и underrun на той же системе, что Sprint 0, с оговоркой об условиях.

## 3. Межкомпонентные зависимости

~~~text
                     [Track + history + likes]
                               |
                           [TasteReader]
                               |
                         [OfflinePlanner]
                               |
                       [LocalExportSource] -------> [rights policy]
                               |                          |
                               +------- [MixtapePlan] ----+
                                          |
                              [User confirm: target/bytes]
                                          |
                             [UDisks2 Device revalidate]
                                          |
                                [Safe writer + verify]
                                          |
                          [Manifest + M3U8 + safe eject]

Qt GUI and CLI both call the same MixtapeService.
AI recommendations NEVER call Safe writer or OS APIs directly.
~~~

## 4. Функциональные границы без редизайна ядра

### Existing code to REUSE (read-only)

- **src/neurowave/domain.py**: Track.key, title, artist, duration, availability; не расширять Track экспортными правами без отдельного решения.
- **src/neurowave/local/adapter.py**: root и перечисление аудиофайлов; для реального export нужен **отдельный безопасный resolver**, не копирование через stream_url.
- **src/neurowave/storage/db.py**: история/лайки, lock; получать snapshot, не пробивать SQLite мимо Database.
- **src/neurowave/providers/base.py**: Capability для музыки; не переиспользовать DIRECT_STREAM как EXPORT_DOWNLOAD.

### New code to ADD IN STAGES

- src/neurowave/mixtape/models.py, planner.py, taste.py, match.py.
- src/neurowave/mixtape/sources/{base,local}.py.
- src/neurowave/mixtape/devices/{base,udisks2}.py.
- src/neurowave/mixtape/export/{filenames,writer,manifest,verify,transcoder}.py.
- src/neurowave/mixtape/service.py и errors.py.
- Позже CLI subcommand и integrations/mixtape_bridge.py.

**Versioning:** добавление сервиса/CLI не меняет контракт mpv; расширение БД делать отдельной миграцией только при доказанной необходимости.

## 5. Технический результат USB-00

Документация в feature/usb-mixtape-spec содержит:

- [README.md](README.md) — scope, ограничения, навигация.
- [SPEC.md](SPEC.md) — требования FR/NFR и сценарии.
- [ARCHITECTURE.md](ARCHITECTURE.md) — модель данных, state machine, file tree, контракты.
- [SOURCES-AND-RIGHTS.md](SOURCES-AND-RIGHTS.md) — права источников, форматы, DRM/caching rules.
- [SAFETY-AND-QA.md](SAFETY-AND-QA.md) — safety threats, writer, тестовая матрица.
- [ADR-0007](../../decisions/0007-usb-mixtape.md) — выбор и аргументация.

На этом этапе в src/ нет реализации и нет новых Runtime dependencies. Это **правильное** состояние: пока не проверили устройство/политику и UX, не рискуем реальными носителями.

## 6. Что поручить первому кодирующему агенту

**Задача USB-01:** «Реализовать deterministic MixtapePlanner на основе утверждённых contracts и только локальных метаданных. Выделить неизменяемые модели и план, не делая никаких файловых, сетевых и USB-операций. Написать unit-тесты на выбор по времени/размеру, dedup между провайдерами, seed, недостаток разрешённых источников, исключения треков и исполнителей, объяснимость рекомендаций. CLI только preview при необходимости; никаких dummy скачиваний».

**Проверка PR:** агент обязан дать список изменённых файлов, команды тестирования, фактический результат тестов, отмеченные ограничения, licence impact и rollback. Запрещено объявлять готовым модуль без права/безопасности copy.

## 7. Что поручить USB writer агенту

**Задача USB-03/04:** «Сначала сделать fake device + evil filesystem tests. Только после прохождения T-001…T-026 добавлять UDisks2 и физическую запись. Никакого raw /dev, форматирования, shutil.rmtree на целевом томе или os.replace поверх чужого файла. Утвердить no-clobber cross-filesystem, правильную реакцию на mounted-path reuse и реальные возможности FAT32/exFAT».

**Проверка PR:** демонстрация fail-closed на подставленном mountpoint после disconnect, явная доказанная запись только на target, сохранность sentinel-файлов.

## 8. Риски и выходы

| Риск | План |
|---|---|
| Нет UDisks2 или Polkit недоступен | Только preview, понятная диагностика; не sudo fallback |
| На локальном диске мало файлов | Полезный частичный сборник; не обходить потоковый сервис |
| Не хватает USB места | Отложить часть по score, user-confirm replanning; не delete чужих файлов |
| Автомагнитола не читает M3U8 | Car profile folder numbering и опциональный plain M3U по тестам |
| FAT32 не поддерживает safe rename variant | Утвердить no-clobber fallback; если не получается, отказ от режима записи |
| Неясные права сетевого источника | Не реализовать downloader, не блокировать local MVP |
| Слишком большой PSS/нагрузка во время конвертации | Bounded workers, throttling, measurements, opt-out |
| Изменения main в Sound Lab/GUI | Слабосвязанный пакет, CLI first, rebase перед merge |

## 9. Definition of Done USB MVP

- Подборка из разрешённых файлов → explicit plan → approved USB target → verified output → safe eject.
- CI unit и sandbox integration зелёные; аппаратный smoke на Linux Mint задокументирован.
- Ни один системный/чужой файл не затронут в тестовых негативных сценариях.
- Нет обходов DRM, hidden download или автоматического экспорта стриминговых кешей.
- Playback не деградирует, оценка PSS/CPU задокументирована.
- UI/CLI корректно показывает partial и ошибки.
- Готовый ZIP/USB содержит **только музыку и минимум метаданных**, никаких ключей и приватных профилей.

После MVP можно планировать smart update, playlist cover, device profiles, optional official download plugin и optional AI. Но каждое отдельно.
