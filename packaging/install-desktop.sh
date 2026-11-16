#!/usr/bin/env bash
# Устанавливает ярлык NeuroWave в меню приложений и на рабочий стол.
#
# Использование:
#   packaging/install-desktop.sh            # меню + рабочий стол
#   packaging/install-desktop.sh --menu     # только меню приложений
#
# Требуется уже установленное окружение проекта:
#   python3 -m venv .venv && .venv/bin/pip install -e .
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LAUNCHER="$PROJECT_DIR/.venv/bin/neurowave"
ICON_FILE="$PROJECT_DIR/packaging/neurowave.svg"

if [ ! -x "$LAUNCHER" ]; then
  echo "Не найден $LAUNCHER" >&2
  echo "Сначала установите окружение:" >&2
  echo "  cd \"$PROJECT_DIR\"" >&2
  echo "  python3 -m venv .venv && .venv/bin/pip install -e ." >&2
  exit 1
fi

if ! command -v mpv >/dev/null 2>&1; then
  echo "Предупреждение: mpv не найден — звук не заработает без него." >&2
  echo "  sudo apt install mpv" >&2
fi

if [ -f "$ICON_FILE" ]; then
  ICON="$ICON_FILE"
else
  ICON="multimedia-audio-player"
fi

APPS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
if command -v xdg-user-dir >/dev/null 2>&1; then
  DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || true)"
fi
DESKTOP_DIR="${DESKTOP_DIR:-$HOME/Desktop}"

MENU_ONLY=0
if [ "${1:-}" = "--menu" ]; then
  MENU_ONLY=1
fi

mkdir -p "$APPS_DIR"

write_entry() {
  local target="$1"
  mkdir -p "$(dirname "$target")"
  cat > "$target" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=NeuroWave
GenericName=Music Player
Comment=Лёгкий плеер Яндекс Музыки для Linux
Exec="$LAUNCHER" wave
Icon=$ICON
Terminal=true
Categories=AudioVideo;Audio;Player;
Keywords=music;audio;yandex;player;neurowave;
StartupNotify=true
EOF
  chmod 755 "$target"
  # Linux Mint / Cinnamon иначе показывает ярлык как недоверенный.
  if command -v gio >/dev/null 2>&1; then
    gio set "$target" metadata::trusted true 2>/dev/null || true
  fi
}

write_entry "$APPS_DIR/neurowave.desktop"
echo "Ярлык в меню: $APPS_DIR/neurowave.desktop"

if [ "$MENU_ONLY" -eq 0 ]; then
  write_entry "$DESKTOP_DIR/neurowave.desktop"
  echo "Ярлык на рабочем столе: $DESKTOP_DIR/neurowave.desktop"
fi

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
fi

echo "Готово. Запуск: «NeuroWave» в меню или двойной клик по ярлыку."
