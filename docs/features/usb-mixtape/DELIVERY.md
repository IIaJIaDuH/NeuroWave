# USB Mixtape — реализация по PR, зависимости, чек-листы

**Статус:** план (не реализовано) · 2026-11-16.  
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

## 2. Отдельные acceptance gates

### G1. Планировщик (после USB-01)

- [ ] Выдаёт одинаковый результат при одинаковых seed, библиотеке и весах.
- [ ] Показывает целевую и фактическую длительность; никого не «докачивает» без прав.
- [ ] Умеет формировать подборку 1–12 часов в пределах доступного каталога и размера.
- [ ] Дубликаты с разных провайдеров не дублируются как один Track.id; настоящий другой ремастер не отбрасывается без оснований.
- [ ] Рекомендательный профиль не зависит от обязательного API/LLM.

### G2. Резолвинг файлов (после USB-02)

- [ ] На каждый файл в плане есть ExportAsset с разрешённым provenance и фактическим локальным доступом.
- [ ] Любой stream_url, Spotify Connect и MediaCache — не основание на экспорт.
- [ ] При неполной библиотеке система честно показывает shortage.
- [ ] Источник может ошибаться/пропасть без потери корректной части плана.

### G3. USB Device (после USB-03)

- [ ] Не предлагает системные разделы как записываемые USB.
- [ ] Выбранный drive перепроверяется непосредственно перед записью.
- [ ] Две флешки или смена mountpoint требуют нового выбора/согласия.
- [ ] Все UDisks2 операции необязательны для обычного плеера.

### G4. Writer (после USB-04)

- [ ] Тесты на symlink swap, traversal, ENOSPC, EIO, EROFS, unplug, rename races.
- [ ] Нет записи вне одобренного target root; нет silent overwrite.
- [ ] Finished означает проверенную контрольную сумму; Partial означает неполный список.
- [ ] Repeat job не создаёт дубли, не переписывает чужой каталог.
- [ ] Pause/Cancel/Resume не забирает управление у аудиодвижка.

### G5. Transcode (после USB-05)

- [ ] FFmpeg доступен только при установленной разрешённой сборке; иначе disable transcode.
- [ ] Никакого заведомого lossy→FLAC под видом Hi-Fi, MP3 320 не обещает качество источника.
- [ ] M3U8 работает на выбранном профиле, пути относительные и проверенные.
- [ ] Все конвертации идут ограниченными ресурсами, без shell=True.

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
- [ADR-0006](../../decisions/0006-usb-mixtape.md) — выбор и аргументация.

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
