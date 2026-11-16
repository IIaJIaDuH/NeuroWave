# Smoke-тест Sprint 1 на Linux Mint

Ручной чек-лист для проверки headless-ядра на целевой машине: реальный аккаунт,
реальное воспроизведение, замеры PSS/CPU, Bluetooth и HDMI.
GUI нет — всё проверяется из терминала. Скрипт замеров:
[`packaging/smoke-measure.sh`](../packaging/smoke-measure.sh).

Ориентировочное время: 1 ч 40 мин (из них 60 мин — пассивный прогон «Моей
волны», всё остальное можно делать параллельно).

---

## Что понадобится

- Linux Mint 21/22 (Cinnamon), `mpv`, `python3-venv`, рабочий PipeWire/PulseAudio;
- свой аккаунт Яндекс Музыки (для lossless нужен Плюс, но lossless не требуется
  для прохождения — важно, что показано **фактическое** качество потока);
- Bluetooth-наушники/колонка и HDMI-выход (монитор, ТВ или ресивер);
- два терминала и место под отчёт: `mkdir -p ~/nw-smoke`.

Перед началом: закройте другие плееры и браузер с музыкой, чтобы замеры CPU и
маршрут звука были чистыми.

## Критерии прохождения

Smoke считается пройденным, если выполнены **все** пункты:

| # | Критерий | Где смотреть |
|---|---|---|
| 1 | `doctor` без `✗` | шаг 1 |
| 2 | `login` + `me` показывают аккаунт; токен не в файле с чужими правами | шаг 2 |
| 3 | `wave` играет 60 минут без остановки и без трейсбеков | шаг 4 |
| 4 | PSS python + mpv ≤ **200 МБ**, рост за час < 15 % | шаг 4, итог скрипта |
| 5 | CPU суммарно: среднее и пик зафиксированы (ориентир ≤ 5 % среднего) | шаг 4 |
| 6 | `underrun=0` во всех замерах, прироста ERR в `pw-top` нет, на слух без щелчков | шаги 4–5 |
| 7 | Bluetooth: A2DP играет стабильно; поведение при смене профиля/обрыве записано | шаг 6 |
| 8 | HDMI: звук идёт, частота совпадает с устройством, xrun нет | шаг 7 |
| 9 | Кеш растёт, не превышает 2048 МБ, `evict`/`clear` работают | шаг 8 |
| 10 | В логах нет токена и OAuth-кода | шаг 10 |

Пункты «поведение записано» — это наблюдения, а не обещания кода: выбор
устройства из приложения, `DEVICE_CHANGED` и MPRIS в Sprint 1 не реализованы,
поэтому смена маршрута — системная (PipeWire), и её результат нужно описать
фактически.

---

## Шаг 0. Окружение (2 мин)

```bash
mkdir -p ~/nw-smoke
{ date -Is; uname -a; lsb_release -ds; mpv --version | head -1; python3 -V;
  pactl info | grep -E 'Server Name|Server Version|Default Sink';
  wpctl status | sed -n '1,12p'; } > ~/nw-smoke/env.txt
cat ~/nw-smoke/env.txt
```

Запишите модель BT-устройства и имя HDMI-sink — они нужны в отчёте.

## Шаг 1. Диагностика (2 мин)

```bash
cd ~/NeuroWave && . .venv/bin/activate
neurowave doctor; echo "exit=$?"
neurowave --json doctor > ~/nw-smoke/doctor.json; echo "json_exit=$?"
```

Ожидаемо: `Python ≥ 3.10`, `yandex-music`, путь к `mpv`, `keyring`, каталоги,
`Токен: нет — нужен neurowave login`, `Сеть до API … доступен`.
`doctor` возвращает `1`, пока нет токена и/или нет `mpv` — это нормально до шага 2.
Флаг `--json` ставится **до** подкоманды.

## Шаг 2. Вход (3 мин)

```bash
neurowave login
# открыть URL, ввести код, подтвердить в браузере
neurowave me
neurowave --json status > ~/nw-smoke/status-login.json
```

Проверка хранения токена:

```bash
ls -l ~/.local/share/neurowave/
```

Ожидаемо: файла `token` нет (keyring) либо `-rw-------` (fallback). Права вида
`rw-r--r--` — дефект, зафиксировать.
Вывод `neurowave login` в файл не сохраняйте: он содержит код подтверждения.

## Шаг 3. Функциональность без звука (10 мин)

```bash
neurowave search "морс" --limit 5
neurowave library --limit 5
neurowave playlists
neurowave like <id из search>
neurowave unlike <id>
neurowave cache info
```

Проверить: реальные ID и названия треков (никаких выдуманных), лайк/анлайк
подтверждены сервисом, `cache info` отвечает.

Воспроизведение по плейлисту (5 минут, звук уже нужен):

```bash
neurowave playlists                  # взять kind
neurowave playlist <kind> --seconds 300 --no-interactive
```

Ожидаемо: строки `▶ Артист — Название — 3:45 [aac 320]`, переходы между
треками без пауз-ошибок.

## Шаг 4. Час «Моей волны» + замеры (60 мин, пассивно)

Терминал A — сессия с логом:

```bash
cd ~/NeuroWave && . .venv/bin/activate
neurowave wave --seconds 3600 --no-interactive 2>&1 | tee ~/nw-smoke/wave-3600.log
echo "wave_exit=$?"
```

Терминал B — замеры (запустить через 1–2 минуты после старта A):

```bash
cd ~/NeuroWave
./packaging/smoke-measure.sh --seconds 3500 --interval 10 --csv ~/nw-smoke/pss.csv \
  | tee ~/nw-smoke/measure.log
```

Скрипт сам находит процесс `neurowave`; если не нашёл — передайте `--pid`.

Во время часа ничего делать не нужно. Записывайте в отчёт всё необычное:
остановки, скачки громкости, щелчки, «залипшие» треки, сообщения `!`.

### 4b. Клавиши управления (3 мин, после часа или до него)

```bash
neurowave wave --seconds 180
```

Нажать по очереди: `n` (следующий), `p` (пауза/продолжить), `+`, `-`
(громкость), `s` (состояние и качество источника), затем `q`.
Ожидаемо: трек меняется, пауза слышна, громкость меняется шагами по 5 %,
`s` печатает `Состояние: … | Трек: … | Источник: aac 320`.

## Шаг 5. Underruns и качество маршрута

Пока играет музыка:

```bash
pw-top -b -n 5
```

Смотреть колонку `ERR` у строки `mpv` и у драйвера: значения должны быть `0`
и не расти между снимками. `B/Q` выше ~0.7 — мало запаса.

```bash
pactl list short sinks
pactl get-default-sink
journalctl --user -b --no-pager | grep -Ei 'xrun|underrun|overrun' | tail -20
```

Строки `UNDERFLOW … underrun:N` в журнале `pipewire-pulse` — это дефект,
зафиксировать частоту и время.

Отдельно — что реально отдаёт поток (правило «lossless только по факту»):

```bash
./packaging/smoke-measure.sh --pid "$(pgrep -n -f neurowave)" --seconds 20 --interval 5
```

Строка `audio: codec=… bitrate=… rate=… ch=… dev=… underrun=0 buf=…`:
кодек и частота должны совпадать с тем, что выводит `pw-top` в колонке FORMAT,
а `underrun` — остаться `0`.

## Шаг 6. Bluetooth (15 мин)

```bash
bluetoothctl
  power on
  scan on
  pair <MAC>
  connect <MAC>
  trust <MAC>
  quit
```

```bash
pactl list short sinks
pactl get-default-sink
pactl list cards | grep -E 'Name:|Active Profile:|codec'
pw-dump | grep -iE 'api.bluez5.codec|api.bluez5.profile'
```

Ожидаемо: sink `bluez_output.<MAC>.1`, профиль `a2dp-sink*`, кодек SBC/AAC/
LDAC/aptX — записать какой. HFP/headset-профиль (`headset-head-unit`) для
музыки не годится, но его переключение полезно проверить:

```bash
neurowave wave --seconds 600 --no-interactive &
./packaging/smoke-measure.sh --seconds 550 --interval 10
# во время игры:
pactl set-card-profile bluez_card.<MAC> headset-head-unit   # и обратно a2dp-sink
```

Записать: переехал ли звук на новый профиль сам, была ли пауза, продолжилась
ли сессия. Затем — обрыв:

```bash
bluetoothctl disconnect <MAC>
```

Ожидаемо по коду: mpv завершает файл с ошибкой, плеер пропускает трек, а после
двух подряд ошибок останавливается с понятным сообщением. Записать фактическое
поведение и текст сообщения; переподключение BT во время сессии не обязано
восстанавливать звук.

## Шаг 7. HDMI (15 мин)

```bash
pactl list short sinks
pactl set-default-sink <hdmi_sink>
pactl get-default-sink
```

```bash
neurowave wave --seconds 600 --no-interactive &
./packaging/smoke-measure.sh --seconds 550 --interval 10
```

Проверить: звук идёт в HDMI-устройство, `rate` в строке `audio:` совпадает с
`pw-top` FORMAT (обычно 48000), `ERR` не растёт. Затем отключить/включить
HDMI-кабель во время игры и записать поведение. В конце вернуть системный
маршрут:

```bash
pactl set-default-sink <обычный_sink>
```

## Шаг 8. Кеш (3 мин)

После часа прогона:

```bash
neurowave cache info
du -sh ~/.cache/neurowave/media
neurowave cache evict
neurowave cache clear
```

Ожидаемо: файлы появились, объём ≤ 2048 МБ, `evict` и `clear` работают.
Повторное прослушивание того же трека в волне помечается `(кеш)` в строке
`▶`. Кеш не является офлайн-библиотекой: при выключенной сети музыка не
заиграет — это ограничение, а не дефект.

## Шаг 9. Сеть и деградации (10 мин)

```bash
nmcli networking off      # на 2–3 минуты во время игры
nmcli networking on
```

Записать: появилось ли сообщение об ошибке, повторились ли запросы с паузой
(backoff), восстановилось ли воспроизведение само. Бесконечных мгновенных
повторов быть не должно.

## Шаг 10. Приватность логов (2 мин)

```bash
grep -il 'token' ~/nw-smoke/*.log
grep -cE '[A-Za-z0-9_-]{40,}' ~/nw-smoke/*.log
```

Ожидаемо: `0` совпадений (длинные строки в логе треков — это названия, проверьте
глазами). Токен и код подтверждения в логах появляться не должны.

---

## Отчёт

Заполните и приложите к задаче:

```
Машина:            <lsb_release -ds>, <uname -r>, <CPU/RAM>
mpv / PipeWire:    <версии>
Аккаунт:           Плюс да/нет, регион
Трек-лист часа:    <кол-во треков из wave-3600.log: grep -c '^▶'>
PSS:               среднее / максимум / первый vs последний замер
CPU:               среднее / максимум
Underrun demuxer:  <N из M замеров>
pw-top ERR:        <0 / прирост>
BT:               <кодек, профиль, поведение при смене профиля и обрыве>
HDMI:             <sink, rate, поведение при hot-plug>
Кеш:              <МБ, файлов после часа>
Дефекты:          <список с шагами воспроизведения>
```

Сводка замеров уже печатается в конце `smoke-measure.sh`; CSV (`pss.csv`) можно
открыть в LibreOffice Calc и посмотреть тренд PSS по времени.

## Если что-то провалилось

- **PSS > 200 МБ или растёт** — снять `pss.csv`, проверить, растёт ли кеш
  файлов (`du -sh ~/.cache/neurowave/media`), и приложить к задаче: это вход в
  ADR-0002 «ядро в C++».
- **`underrun=1` / ERR растёт** — сначала исключить сеть: `ping -c 20 api.music.yandex.net`,
  затем повторить на локальном файле через `mpv <файл>`; если и на файле
  xrun-ы — виноват маршрут/буфер PipeWire, а не ядро.
- **Нет звука, но `▶` печатается** — `pactl list sink-inputs` во время игры:
  поток есть? `pw-top` видит `mpv`? Проверить `mpv --no-video <url>` вручную с
  тем же URL.
- **`401` / «Требуется вход»** — `neurowave login` заново; поведение кода
  (сразу требовать вход, без повторов) считать ожидаемым.
