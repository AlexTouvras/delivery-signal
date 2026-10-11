#!/usr/bin/env bash
# One-liner for macOS and Linux. Puts delivery-signal on this machine.
# Uses python3 or python when one is already on PATH. Does not install a runtime.
set -euo pipefail

PAGE="https://htmlpreview.github.io/?https://github.com/AlexTouvras/delivery-signal/blob/main/docs/index.html"
BASE="https://raw.githubusercontent.com/AlexTouvras/delivery-signal/main/delivery-signal"
DEST="${DELIVERY_SIGNAL_HOME:-$HOME/.delivery-signal}"
FILES="pipeline.py extract.py link.py run run.ps1 run.cmd"

source_path="${BASH_SOURCE[0]:-}"
here=""
if [[ -n "$source_path" && -f "$source_path" ]]; then
  here="$(cd "$(dirname "$source_path")" && pwd)"
fi

mkdir -p "$DEST"
for name in $FILES; do
  if [[ -n "$here" && -f "$here/$name" ]]; then
    cp "$here/$name" "$DEST/$name"
  else
    curl -fsSL "$BASE/$name" -o "$DEST/$name"
  fi
done
chmod +x "$DEST/run"

bindir="$HOME/.local/bin"
mkdir -p "$bindir"
cat > "$bindir/delivery-signal" << EOF
#!/usr/bin/env bash
exec "$DEST/run" "\$@"
EOF
chmod +x "$bindir/delivery-signal"

echo "Installed. Open a new terminal and run:"
echo "  delivery-signal"
echo "That opens the configuration page. A CSV is: delivery-signal issues.csv"
if ! command -v delivery-signal >/dev/null 2>&1; then
  echo "Add $bindir to PATH, then open a new terminal."
fi
if ! command -v python3 >/dev/null 2>&1 && ! command -v python >/dev/null 2>&1; then
  echo "Python is not on this machine. Open the page and choose the CSV:" >&2
  echo "$PAGE" >&2
fi
