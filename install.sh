#!/usr/bin/env bash
# One-shot setup for macOS and Linux: creates .venv and installs onscreen into it.
set -euo pipefail
cd "$(dirname "$0")"

PY=${PYTHON:-python3}
"$PY" -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ required"'

if [[ "$(uname)" == "Linux" ]]; then
  # sounddevice needs the PortAudio shared library on Linux (macOS wheels bundle it)
  if ! ldconfig -p 2>/dev/null | grep -q libportaudio; then
    echo ">> PortAudio missing. Install it first, e.g.:"
    echo "   sudo apt install libportaudio2      # Debian/Ubuntu"
    echo "   sudo dnf install portaudio          # Fedora"
    echo "   sudo pacman -S portaudio            # Arch"
    exit 1
  fi
fi

"$PY" -m venv .venv
.venv/bin/pip install -U pip wheel
EXTRAS="ai"
if [[ "$(uname)" == "Darwin" && "$(uname -m)" == "arm64" ]]; then
  EXTRAS="ai,mlx"   # Whisper on the Apple Silicon GPU
fi
.venv/bin/pip install -e ".[${EXTRAS}]"

echo
echo "Done. Run it with:  .venv/bin/onscreen"
