# DJ Franky — анимация головы в такт музыке: техническая спецификация

**Статус:** будущая необязательная фича, код не написан.  
**Приоритет:** после daily-driver beta и UI стабилизации.  
**Основное правило:** музыка и ядро работают без персонажа и без анализатора; сбой анимации не может остановить воспроизведение.

## 1. Желаемый эффект

В режиме Now Playing DJ Franky слегка качает головой в ритме музыки: мягкий поворот/кивок на основной бит, более сильное движение на басовых атаках, краткая улыбка/мигание и деликатная пульсация бликов очков. Он **не** должен хаотично трястись на каждый пик или дёргаться под речь.

Состояния:
- **Idle:** статичная поза, возможна редкая ненавязчивая мимика, без «выдуманного» бита.
- **Playing + valid analysis:** реакция на настоящий сигнал, небольшой диапазон движения.
- **Paused/stopped/muted / signal unavailable:** затухание к нейтральной позе за ~250–500 мс.
- **Seeking/track change:** сброс сглаживания, очистка старой сетки событий/beat phase.
- **Reduced motion / user disabled / battery-saving:** статичная голова, никаких скрытых таймеров анализа.
- **Window hidden/minimized:** остановить отрисовку и ненужные вычисления (не остановить mpv).
- **Hardware/visualizer error:** тихо отключить motion, оставить нормальное проигрывание.

## 2. Допустимый источник сигнала

**Использовать только изолированный PCM tap для потока NeuroWave**, проверенный в отдельном PoC. Не использовать global monitor/default sink, который захватывает звук других приложений. Перед включением production-анализатора заново подтвердить существование подходящего живого per-app PCM source и правомерность его захвата.

**Важно:** наличие PCM-tap PoC **не означает**, что реализованы FFT, beat tracker, BPM estimator или одобренный producer API.

Предлагаемый data flow:

```text
[mpv / per-app PipeWire stream]
             |
     [isolated PCM tap]
             |
 [bounded ring buffer, e.g. 0.3–1 s]
             |
 [audio-analysis worker]
  | RMS / low-band energy
  | onset peak + adaptive threshold
  | optional tempo confidence
             |
  [bounded timestamped MotionSignal]
             |
 [UI timer, 30–60 FPS max]
             |
 [Franky head/visor transforms]
```

Транспортировать в UI **только** энергию, onset/confidence и timestamps. Не записывать PCM на диск, в лог, на сервер, в облачный AI; не передавать сторонним виджетам. При отсутствии валидации — использовать ручное demo-анимирование **только** в превью дизайна, не маркировать как «синхронно с реальной музыкой».

## 3. Предлагаемая модель сигналов

```python
@dataclass(frozen=True)
class MotionSignal:
    monotonic_time_ns: int
    track_generation: int
    low_band_energy: float        # normalized [0, 1]
    broadband_energy: float       # normalized [0, 1]
    onset_strength: float         # normalized [0, 1]
    confidence: float             # beat confidence [0, 1]
    beat_event: bool              # validated transient, may be False
```

Это **контракт для обсуждения**, не существующий тип данных. Использовать monotonically increasing timestamps и `track_generation`, чтобы поздний callback старой песни не дёргал Franky на новом треке.

## 4. Пример алгоритма (PoC, не гарантирует точный BPM)

1. Аудио разбивается на неперекрывающиеся или слегка перекрывающиеся окна ~10–25 мс. Вычислять energy/RMS, low-band component (например 40–180 Hz при адекватной частоте дискретизации), выравнивать относительно скользящего median/noise floor.
2. Сравнивать прирост low-band энергии с адаптивным порогом и refractory interval ~150–250 мс. Не классифицировать любой громкий пик как удар бочки; хранить confidence.
3. Из onset events оценивать повторяемость интервалов и темп **только при достаточном подтверждении**. Речь, эмбиент и нерегулярная музыка могут не давать устойчивого темпа — в таком случае делать мягкую energy-based motion без claims «качает на каждый бит».
4. В UI огибающая атаки быстро поднимается, спад затухает 120–300 мс. Применять ограничения скоростей/ускорения и небольшой hysteresis, чтобы не было резких поворотов.
5. Сила движения — регулируемая пользователем; без жесткой привязки к абсолютной громкости, иначе тихий мастер не оживёт, а пережатый мастер будет постоянно биться об ограничение.
6. **Никакого управления музыкой из motion worker.** Он только читает аудио и публикует маленький immutable snapshot.

## 5. Параметры движения (предложения для тестов)

| Параметр | Кандидат по умолчанию | Лимит/комментарий |
|---|---|---|
| Head tilt | ±3–6° | физически читаемый мягкий кивок, clamp до ±10° |
| Vertical bounce | 1–4 px при 256px | относительная величина от canvas, без jump |
| Visor glow | +5–15% intensity | не яркие мигания |
| Shoulder secondary | ≤50% амплитуды головы | необязательно |
| Blink | редкое мягкое событие | не чаще ~1 раза в несколько секунд |
| Transition to idle | 250–500 мс | no abrupt reset |
| Animation rate | 30 FPS default | 60 FPS только при доказанной необходимости |
| UI update queue | latest-only | bounded, drop stale frames |

Числа **предварительные**: калибровка по устройству, тестам анимации и субъективной читаемости. Не использовать быстрое стробирование/повторяющиеся flashes; учитывать доступность.

## 6. Изоляция и производительность

- Headless, CLI, MPRIS и mpv не импортируют анимационные/Qt-библиотеки.
- Worker получает **копию** PCM, не держит блокирующих lock аудиопути; буфер ограничен, при переполнении drop старых data, не buffer all audio.
- Отрисовка только на UI thread; любые вычисления/FFT в worker, без Python loop на каждый sample в основном интерфейсе.
- Ориентир на первый прототип: **добавочная CPU <2% одного ядра и PSS <20 MiB** на целевой машине, при 30 FPS; это **проектные цели**, не текущие измерения.
- No leaks: 30–60 min playing/paused/minimized/unplug с замерами workers, fd, PSS, VmSwap и CPU.
- **Latency target:** видимое движение согласуется с слышимым событием в пределах ±100 мс **на протестированном выходе**, учитывая задержку Bluetooth; проверять объективно или на высокочастотной видеосъёмке с тестовым кликом. Без калибровки DSP/PipeWire/BT такой точности **не обещать**.
- При CPU pressure, underrun, отключении tap или скрытом окне — автоматически упрощать или выключать motion; пользователь настраивает persistent off.
- Не захватывать и не сохранять чужой звук; осторожно с системными разрешениями и privacy.

## 7. Тесты

- Synthetic signal (собственноручный): click track c 60/90/120/150 BPM, bass-heavy drums, compression/noise, silence, speech-like, ambient. Проверять устойчивость onset/false positives; исходный тестовый звук **создавать в тесте**, не сохранять чужие защищённые файлы.
- Beat switch 90→150 BPM + track change: отсутствие stale generation и runaway ring buffer.
- No PCM, paused, disconnected, minimized, high CPU load: музыка продолжает играть, UI реагирует, worker завершается.
- Accessibility: reduced motion, запрет photosensitive flash, color-safe, статичный mascot всегда доступен.
- Performance: PSS/CPU vs clean GUI + mpv baseline, Bluetooth latency отдельно от HDMI/DP.
- Soak: static/no-motion не запускает analyzer; optional animation закрывается без orphan worker.

## 8. Порядок внедрения

**FRANKY-01:** утвердить статичные артефакты и motion rig; **FRANKY-02:** безопасная изолированная UI-анимация по заранее созданной demo-envelope без реального PCM; **FRANKY-03:** уточнить проверенный PCM tap producer; **FRANKY-04:** лёгкий beat/onset analyzer и вход сигналов в UI; **FRANKY-05:** измерения, reduced motion, refactor и full integration.

Никаких релизных обещаний до gate. Существующий `PCM tap` и будущий `FFT` не обязаны иметь общую тяжёлую реализацию: можно обойтись RMS + onset.
