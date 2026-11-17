# USB Mixtape — архитектура, контракты и структура проекта

**Статус:** USB-01…USB-03 (models/planner, LocalExportSource, устройства) реализованы; writer и UI — план.  
**Совместимость:** существующие Track / LocalMusicAdapter / Database / ProviderRouter без breaking changes; ADR-0004 и ADR-0005.

## 1. Схема компонентов

~~~text
Qt USB Mixtape page       CLI neurowave mixtape
         |                        |
         +------ MixtapeService --+
                         |
                  JobCoordinator
              /          |          \
         Planner      Resolver     USBWriter
        /     \        /    \       /     \
  TasteSignals     ExportSources  USBDeviceService
     from DB        local first      UDisks2
                         |              |
                AssetManifest       OS mountpoint
                         \              /
                           Verify/Report
~~~

**Золотое правило:** PlayerService отвечает только за воспроизведение. MixtapeService читает метаданные и пользовательские предпочтения через read-only интерфейсы и **не** инициирует скачивание через stream_url(), **не** использует MediaCache и **не** вызывает mpv.

## 2. Проектируемая структура файлов (для следующих PR)

~~~text
src/neurowave/mixtape/
  __init__.py
  models.py                # immutable DTO, policies, result/error enums
  service.py               # MixtapeService facade, job orchestration
  planner.py               # deterministic candidate ranking and constraints
  taste.py                 # read-only adapter to likes/history/metadata
  match.py                 # identity match; alternatives for ambiguous cases
  sources/
    __init__.py
    base.py                # ExportSource protocol: never a stream adapter
    local.py               # allowed local asset resolver
    downloads.py           # OPTIONAL future downloads, not MVP
  devices/
    __init__.py
    base.py                # UsbDeviceService protocol
    udisks2.py             # UDisks2 D-Bus; discovery and safe eject
    fake.py                # CI fixture only (or tests/fixtures)
  export/
    __init__.py
    filenames.py           # filename / path / charset validation
    writer.py              # limited-buffer streaming copy and temp commit
    transcoder.py          # optional external ffmpeg, bounded workers
    manifest.py            # schema and JSON persistence
    verify.py              # sha256 of destination and playlist consistency
  errors.py                # stable error codes
src/neurowave/integrations/mixtape_bridge.py  # future Qt-only bridge
tests/mixtape/
  test_planner.py
  test_rights.py
  test_local_source.py
  test_device_selection.py
  test_paths.py
  test_manifest_resume.py
  test_writer_faults.py
  test_transcode.py
  test_cli.py
  test_real_usb_smoke.py   # manual, opt-in; NEVER on CI runner disks
docs/features/usb-mixtape/ ...
~~~

**Не добавлять всё сразу.** Каждая директория и зависимость появляется вместе с работоспособным срезом и тестом. Файлы выше — целевая структура, не фиктивный реализованный функционал.

## 3. Основные модели (контракт, псевдо-Python)

~~~python
@dataclass(frozen=True)
class ExportAsset:
    asset_id: str                    # stable identifier in source
    track_key: str | None           # provider-qualified Track.key, optional
    local_path: Path | None         # only validated local source, MVP
    media_format: str
    duration_ms: int | None
    size_bytes: int
    sha256: str | None              # computed or verified as needed
    rights_basis: str               # e.g. USER_OWNED / EXPLICIT_DOWNLOAD_GRANT
    source_id: str                  # "local" in MVP
    eligible: bool                  # never infer from DIRECT_STREAM

@dataclass(frozen=True)
class DeviceIdentity:
    udisks_object_path: str
    block_device: str               # diagnostic only, never write raw
    filesystem_uuid: str | None
    drive_id: str | None
    mountpoint: Path
    filesystem_type: str
    removable: bool
    writable: bool
    capacity_bytes: int
    free_bytes: int

@dataclass(frozen=True)
class MixtapeRequest:
    profile: str
    target_duration_ms: int
    max_bytes: int | None
    output_format: str              # original | mp3 | flac
    strict_unique: bool
    seed: int
    excluded_track_keys: tuple[str, ...]
    destination_subdir: str

@dataclass(frozen=True)
class PlannedItem:
    source_asset_id: str
    source_id: str
    destination_relative: str
    operation: str                  # copy | transcode | skip
    estimated_bytes: int
    reason: str                     # explanation to user
    source_hash: str | None

@dataclass(frozen=True)
class MixtapePlan:
    plan_id: str
    request: MixtapeRequest
    items: tuple[PlannedItem, ...]
    candidate_count: int
    eligible_count: int
    skipped: tuple[str, ...]
    estimated_bytes: int
    estimated_duration_ms: int
    revision: int
~~~

**Критично:** источник, реальный файл и право на экспорт должны связываться *разными* идентификаторами. Track.key существует для catalog/history; он **не** свидетельствует о праве копирования. Идентификатор накопителя должен быть повторно проверен до каждого существенного этапа writer.

## 4. Интерфейсы сервисов (псевдо-Python, API до согласования)

~~~python
class TasteSignals(Protocol):
    def read_preferences(self) -> TasteSnapshot: ...
    def local_candidates(self) -> Sequence[Track]: ...

class ExportSource(Protocol):
    def list_eligible(self, candidates: Sequence[Track]) -> Sequence[ExportAsset]: ...
    def open_verified_asset(self, asset_id: str) -> BinaryIO: ...
    def rights_status(self, asset_id: str) -> ExportRight: ...

class UsbDeviceService(Protocol):
    def list_removable_writable_filesystems(self) -> Sequence[DeviceIdentity]: ...
    def revalidate(self, expected: DeviceIdentity) -> DeviceIdentity: ...
    def safely_eject(self, expected: DeviceIdentity) -> None: ...

class MixtapeService(Protocol):
    def plan(self, request: MixtapeRequest) -> MixtapePlan: ...
    def start(self, plan_id: str, target: DeviceIdentity, consent: Consent) -> str: ...
    def cancel(self, job_id: str) -> None: ...
    def status(self, job_id: str) -> ExportJobStatus: ...
    def verify(self, job_id: str) -> VerificationReport: ...
~~~

**Право на файл:** ExportRight хранит enum PERMITTED / DENIED / UNKNOWN и provenance. Значение UNKNOWN всегда ведёт к skip, не к optimistic download. Сведения о DRM/privacy не сериализовать в публичные логи. Внутри LocalExportSource разрешение локального файла для пользователя — отдельная конфигурация одобренного root; никакой обход относительных путей и symlink наружу.

## 5. Машина состояний

~~~text
NEW → PLANNING → PLANNED → AWAITING_CONFIRMATION
                              |
                        user consent
                              |
                           PREPARING
                              |
                           WRITING ↔ VERIFYING
                            |            |
                     error / missing     |
                            |            v
                         INTERRUPTED    COMPLETE
                            |
                revalidate device + manifest
                            |
                          RESUMING → WRITING

CANCELLED, FAILED are terminal for that attempt.
PARTIAL is a final report status when some items skipped/failed but job finishes.
~~~

**State transition gate:** выбор нового устройства или изменение плана после подтверждения возвращает к AWAITING_CONFIRMATION. READY/COMPLETE означает: все заявленные готовые файлы реально проверены и final manifest последовательно записан, а не только отправлен в write().

## 6. Pipeline

1. **Collect:** локальная библиотека + read-only история/лайки. Нет новых сетевых запросов ради офлайн-режима.
2. **Recommend:** построение предпочтений; считать метрики только по доступным полям. В Track сейчас нет жанра или mood — это данные для будущего enrichment, нельзя выводить из названия без маркировки.
3. **Resolve:** поиск ExportAsset по policy. Для совпавших каталоговых треков — нормализованное название + исполнитель + близкая длительность + ручное разрешение сомнений. Из несопоставленного кандидата файл **не выдумываем**.
4. **Plan:** бюджет времени и байт, итоговый список eligible и причины отказов. Оценка размера для transcode предварительная, с резервом места.
5. **Confirm:** immutable snapshot; явное согласие с устройством, целевым каталогом и overwrite-mode; краткоживущий consent token для одной операции, не сохраняемый как бесконечное разрешение.
6. **Prepare:** повторная оценка свободного места + 10% reserve (настройка), идентичности и права записи; lock одной задачи на том.
7. **Copy/Encode:** bounded-buffer (например, 1 MiB) из разрешённого файла во временный файл **на том же filesystem**. Никаких секретов в имени/тегах. Concurrency по умолчанию 1 writer и не более 1 transcoder, чтобы не мешать аудио.
8. **Verify:** fsync/close, reread destination, sha256; при конвертации compare decoded duration / format, а не bytes checksum исходника и выхода.
9. **Commit:** атомарно переместить *один* завершённый файл внутри целевой ФС. Запись manifest транзакционная на уровне отдельного файла: tmp → fsync → rename, без обещания всей USB-операции как единой транзакции.
10. **Report/Eject:** checksum ledger, playlist, partial/fail reasons, sync, safe unmount/eject через UDisks2 после user action.

## 7. Хранение и восстановление

- Локальный журнал: XDG_DATA_HOME/neurowave/mixtape/jobs/<uuid>.json (права 0600, atomically replaced).
- На USB: MUSIC/NeuroWave/.neurowave-job-<uuid>/ временные данные; MUSIC/NeuroWave/manifest.json с schema_version и перечнем **файлов, которыми владеет именно эта задача**, не всех данных тома.
- В manifest: job_id, version, consented target fingerprint, source identifiers (без токенов/ссылок), relative paths, status, source/destination hash, bytes, timestamps. Хешировать конфиденциальные абсолютные пути в публичном export-report при необходимости.
- Любые пути, прочитанные из manifest на USB, считаются недоверенными; парсинг с размерным лимитом, schema check и собственной проверкой path containment.
- Resume возможен только при совпадении identity тома, plan revision и валидных уже записанных файлов. Без совпадения → новый план и подтверждение.
- При отмене удалять только собственные временные файлы по подтверждённому пути и валидированному job-id; готовые файлы сохранять, если пользователь не запросил безопасную уборку.

## 8. File layout

**Car USB (по умолчанию):**

~~~text
USB_ROOT/
  MUSIC/
    NeuroWave/
      001 - Artist - Title.mp3
      002 - Artist - Title.mp3
      playlist.m3u8
      manifest.json
~~~

Опция «плоская папка MUSIC» — **после тестов конкретных магнитол**, поскольку некоторым устройствам не нравится вложенность. Для FAT32 отдельно проверить ASCII/transliteration mode, case-insensitive collisions и длину пути.

## 9. Планирование вкуса без облачного ИИ

Базовая формула — *пример для PoC*, веса подбирать по offline-оценке, а не выдавать за готовое «обучение»:

~~~text
score = 3*user_liked + log1p(play_count) + 1*recentness
      + 0.5*same_artist_pref - 2*overplayed_penalty
      - 4*recent_skip_count - diversity_penalty
~~~

История SQLite v2 сейчас хранит время и finished, но **не обязана хранить** skip-count, mood и play_count отдельными столбцами. Использовать доступные сигналы, неизвестные — 0 и объяснение ограничения. Seed обеспечивает повторяемость. Начальный план строится по разрешённым local assets, затем пользователь подтверждает результат. Модель ИИ (при появлении) не может обойти policy фильтр.

## 10. Потоки и события

- Qt-слоты только отправляют команды; события мигают через typed signals/bridge, без прямого вызова блокирующего writer в UI thread.
- ExportService не публикует события в mpv read loop, не использует connection SQLite в обход его lock и не передаёт mutable Track из одного потока без снимка.
- Подписки UI могут получать события с rate-limit (5–10 Hz) для прогресса и работать без постоянного polling.
- API stop/cancel идемпотентен. Падение UI не запускает новую запись; судьба незавершённой задачи после процесса — на диске только manifest для ручного resume.

## 11. Технические альтернативы

- D-Bus UDisks2 как основной API Linux. libudev/pyudev допустим только как вспомогательный сигнал, не как основание для записи.
- FFmpeg внешний подпроцесс с ограничением аргументов, timeout, kill на cancel, **без shell=True**, никаких URL в команды MVP.
- CLI и Qt используют один MixtapeService, никаких двух алгоритмов записи.
- Отдельная очередь задач не нужна MVP; **одна активная запись**, остальные показывать busy.
- Первоначальное хранение собственного журнала в JSON достаточно; SQLite migration для фичи **не нужна** без доказанной необходимости.

## 12. Definition of Done архитектуры

- [ ] Контракты утверждены без изменения AudioBackend.
- [ ] Есть один test seam: fake device/source/writer, который умеет сымитировать unplug, permission denied, ENOSPC, collision, malicious manifest.
- [ ] В функциональных тестах статус, реально записанные файлы и журнал согласованы.
- [ ] Все внешние export-байты прошли через ExportSource/ExportRight.
- [ ] За 60 минут фонового музыкального playback запись файлов не привела к явным underrun/заметной деградации (на физическом Mint-стенде).
