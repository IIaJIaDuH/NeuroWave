#!/usr/bin/env bash
# Замеры для smoke-теста NeuroWave (Sprint 1).
#
# Считает PSS и CPU всего дерева процессов запущенной сессии
# (`neurowave wave` + дочерний `mpv`) и раз в интервал опрашивает mpv по
# JSON IPC: кодек, битрейт, частоту дискретизации, устройство вывода и
# признак underrun демуксера (нехватка сети/кеша).
#
# Пример:
#   ./packaging/smoke-measure.sh --seconds 3600 --interval 10 --csv ~/nw-pss.csv
#
# Остановить раньше: Ctrl+C — итог печатается и в этом случае.
set -uo pipefail

SECONDS_TOTAL=${SECONDS_TOTAL:-600}
INTERVAL=5
CSV=""
ROOT_PID=""
AUDIO=1

usage() {
  cat <<'EOF'
Использование: smoke-measure.sh [параметры]

  --seconds N    сколько секунд вести замеры (по умолчанию 600, 0 — без предела)
  --interval S   период опроса в секундах (по умолчанию 5)
  --pid PID      PID процесса `neurowave` (по умолчанию находится сам)
  --csv FILE     писать машинночитаемые строки в FILE
  --no-audio     не опрашивать mpv по IPC
  -h, --help     эта справка
EOF
}

while (($#)); do
  case "$1" in
    --seconds) SECONDS_TOTAL=$2; shift 2 ;;
    --interval) INTERVAL=$2; shift 2 ;;
    --pid) ROOT_PID=$2; shift 2 ;;
    --csv) CSV=$2; shift 2 ;;
    --no-audio) AUDIO=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Неизвестный параметр: $1" >&2; usage >&2; exit 2 ;;
  esac
done

command -v awk >/dev/null || { echo "Нужен awk" >&2; exit 2; }
HZ=$(getconf CLK_TCK 2>/dev/null || echo 100)

find_root() {
  local want=${1:-}
  if [[ -n $want ]]; then echo "$want"; return 0; fi
  # Ищем именно python-процесс neurowave, чтобы не зацепить сам скрипт.
  pgrep -n -f '[.]venv/bin/neurowave|python[0-9.]* -m neurowave' 2>/dev/null \
    || pgrep -n -f 'neurowave wave' 2>/dev/null
}

ROOT=$(find_root "$ROOT_PID")
if [[ -z ${ROOT:-} || ! -d /proc/$ROOT ]]; then
  echo "Не найден процесс neurowave. Запустите сессию и укажите PID: --pid N" >&2
  exit 1
fi

# --- сбор дерева процессов ---------------------------------------------
collect_tree() {
  local queue=("$ROOT") pid child
  while ((${#queue[@]})); do
    pid=${queue[0]}
    queue=("${queue[@]:1}")
    [[ -d /proc/$pid ]] || continue
    printf '%s\n' "$pid"
    while read -r child; do
      [[ -n $child ]] && queue+=("$child")
    done < <(pgrep -P "$pid" 2>/dev/null)
  done
}

comm_of() { tr -d '\n' <"/proc/$1/comm" 2>/dev/null || echo "?"; }

pss_kb() {
  if [[ -r /proc/$1/smaps_rollup ]]; then
    awk '/^Pss:/{s+=$2} END{printf "%d", s+0}' "/proc/$1/smaps_rollup" 2>/dev/null
  elif [[ -r /proc/$1/smaps ]]; then
    awk '/^Pss:/{s+=$2} END{printf "%d", s+0}' "/proc/$1/smaps" 2>/dev/null
  else
    awk '/^VmRSS:/{printf "%d", $2+0}' "/proc/$1/status" 2>/dev/null
  fi
}

cpu_ticks() {
  awk '{printf "%d", $14+$15}' "/proc/$1/stat" 2>/dev/null
}

# --- опрос mpv ----------------------------------------------------------
probe_audio() {
  local sock
  sock=$(ls -t /tmp/neurowave-mpv-*.sock 2>/dev/null | head -1)
  if [[ -z ${sock:-} ]]; then
    echo "audio: mpv IPC не найден (сессия не играет?)"
    return 0
  fi
  python3 - "$sock" <<'PY' 2>/dev/null || return 1
import json, socket, sys

sock = sys.argv[1]
props = [
    "audio-codec-name",
    "audio-bitrate",
    "audio-params",
    "audio-device",
    "demuxer-cache-state",
]
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(2.0)
try:
    s.connect(sock)
except OSError:
    sys.exit(1)
f = s.makefile("rwb", buffering=0)
values = {}
try:
    for index, prop in enumerate(props, start=1):
        f.write((json.dumps({"command": ["get_property", prop], "request_id": index}) + "\n").encode())
        for _ in range(50):
            line = f.readline()
            if not line:
                raise SystemExit(1)
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("request_id") == index:
                values[prop] = message.get("data")
                break
except (OSError, SystemExit):
    pass
finally:
    try:
        s.close()
    except OSError:
        pass

params = values.get("audio-params") or {}
cache = values.get("demuxer-cache-state") or {}
fmt = params.get("format")
if isinstance(fmt, dict):
    fmt = fmt.get("format") or fmt.get("name") or "spdif"
underrun = 1 if cache.get("underrun") else 0
print(
    "audio: codec={codec} bitrate={bitrate} rate={rate} fmt={fmt} ch={ch} dev={dev} underrun={ur} buf={buf}".format(
        codec=values.get("audio-codec-name") or "-",
        bitrate=values.get("audio-bitrate") or "-",
        rate=params.get("samplerate") or "-",
        fmt=fmt or "-",
        ch=params.get("channel-count") or "-",
        dev=values.get("audio-device") or "-",
        ur=underrun,
        buf=cache.get("cache-buffering-state") if cache.get("cache-buffering-state") is not None else "-",
    )
)
PY
}

# --- основной цикл ------------------------------------------------------
declare -A PREV_TICKS=()
declare -A PEAK_PSS=()
declare -A PEAK_CPU=()
declare -A COMM=()

samples=0
pss_sum=0
pss_peak=0
cpu_sum=0
cpu_peak=0
underrun_samples=0
audio_samples=0
audio_fail=0
started=$(date +%s)

if [[ -n $CSV ]]; then
  printf 'epoch,elapsed_s,pids,total_pss_mb,py_pss_mb,mpv_pss_mb,cpu_pct\n' >"$CSV"
fi

echo "NeuroWave smoke-замеры: root pid=$ROOT ($(comm_of "$ROOT")), интервал ${INTERVAL}s, лимит ${SECONDS_TOTAL}s"
echo "Время | PSS всего | CPU всего | процессы"

while :; do
  now=$(date +%s)
  elapsed=$((now - started))
  mapfile -t pids < <(collect_tree)
  if ((${#pids[@]} == 0)); then
    echo "Процесс $ROOT завершился — замеры остановлены."
    break
  fi

  total_pss=0
  py_pss=0
  mpv_pss=0
  total_delta=0
  for pid in "${pids[@]}"; do
    pss=$(pss_kb "$pid")
    [[ -z ${pss:-} ]] && pss=0
    comm=$(comm_of "$pid")
    [[ -z ${COMM[$pid]:-} ]] && COMM[$pid]=$comm
    total_pss=$((total_pss + pss))
    if [[ $comm == "mpv" ]]; then
      mpv_pss=$((mpv_pss + pss))
    else
      py_pss=$((py_pss + pss))
    fi
    cur=$(cpu_ticks "$pid")
    [[ -z ${cur:-} ]] && cur=0
    if [[ -n ${PREV_TICKS[$pid]:-} ]]; then
      delta=$((cur - PREV_TICKS[$pid]))
      ((delta > 0)) && total_delta=$((total_delta + delta))
    fi
    PREV_TICKS[$pid]=$cur
    ((pss > ${PEAK_PSS[$pid]:-0})) && PEAK_PSS[$pid]=$pss
  done

  cpu_pct=$(awk -v d="$total_delta" -v hz="$HZ" -v dt="$INTERVAL" 'BEGIN{printf "%.1f", 100*d/hz/dt}')
  pss_mb=$(awk -v k="$total_pss" 'BEGIN{printf "%.1f", k/1024}')
  py_mb=$(awk -v k="$py_pss" 'BEGIN{printf "%.1f", k/1024}')
  mpv_mb=$(awk -v k="$mpv_pss" 'BEGIN{printf "%.1f", k/1024}')

  printf '[%5ds] PSS %7s МБ (py %s + mpv %s) | CPU %5s%% | pids=%d\n' \
    "$elapsed" "$pss_mb" "$py_mb" "$mpv_mb" "$cpu_pct" "${#pids[@]}"
  if ((AUDIO)); then
    if audio_line=$(probe_audio); then
      echo "        $audio_line"
      audio_samples=$((audio_samples + 1))
      case "$audio_line" in *underrun=1*) underrun_samples=$((underrun_samples + 1)) ;; esac
    else
      audio_fail=$((audio_fail + 1))
      if ((audio_fail == 1 || audio_fail % 30 == 0)); then
        echo "        audio: IPC недоступен (неудачных попыток: $audio_fail) — mpv мог ещё не загрузить трек"
      fi
    fi
  fi

  if [[ -n $CSV ]]; then
    printf '%s,%d,%d,%s,%s,%s,%s\n' "$now" "$elapsed" "${#pids[@]}" "$pss_mb" "$py_mb" "$mpv_mb" "$cpu_pct" >>"$CSV"
  fi

  samples=$((samples + 1))
  pss_sum=$((pss_sum + total_pss))
  ((total_pss > pss_peak)) && pss_peak=$total_pss
  # Первый замер не имеет предыдущих тиков и всегда даёт 0 % — в среднее не идёт.
  if ((samples > 1)); then
    cpu_peak=$(awk -v a="$cpu_peak" -v b="$cpu_pct" 'BEGIN{print (b>a)?b:a}')
    cpu_sum=$(awk -v a="$cpu_sum" -v b="$cpu_pct" 'BEGIN{printf "%.1f", a+b}')
  fi

  if ((SECONDS_TOTAL > 0 && elapsed + INTERVAL > SECONDS_TOTAL)); then
    break
  fi
  sleep "$INTERVAL"
done

# --- итог ---------------------------------------------------------------
echo
echo "=== Итог ($samples замеров) ==="
if ((samples > 0)); then
  awk -v s="$pss_sum" -v n="$samples" -v p="$pss_peak" -v c="$cpu_sum" -v cp="$cpu_peak" 'BEGIN{
    printf "PSS: среднее %.1f МБ, максимум %.1f МБ (ориентир ТЗ: до 200 МБ)\n", s/n/1024, p/1024;
    printf "CPU: среднее %.1f%%, максимум %.1f%% (сумма по дереву процессов)\n", c/(n>1?n-1:1), cp;
  }'
  if ((audio_samples > 0)); then
    printf 'Underrun демуксера mpv: %d из %d замеров\n' "$underrun_samples" "$audio_samples"
  fi
  echo "Пики по процессам:"
  for pid in "${!PEAK_PSS[@]}"; do
    [[ -d /proc/$pid ]] || continue
    awk -v pid="$pid" -v c="${COMM[$pid]:-?}" -v p="${PEAK_PSS[$pid]}" 'BEGIN{
      printf "  pid %-7s %-12s пик PSS %.1f МБ\n", pid, c, p/1024;
    }'
  done | sort -t' ' -k5 -rn
fi
echo "Первый и последний замер PSS сравните между собой: устойчивый рост — повод искать утечку."
