#!/bin/sh
# Regenerate the README images: docs/images/{dashboard,sidebar}.png and docs/images/demo.gif.
# Needs uv, tmux, a headless-capable Chrome (google-chrome or $CHROME), and ImageMagick (convert).
set -eu

repo=$(cd "$(dirname "$0")/.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
chrome=${CHROME:-google-chrome}
images="$repo/docs/images"
mkdir -p "$images"

render() {
    box=$(grep -o 'viewBox="[^"]*"' "$1" | head -1 | tr -d '"' | cut -d' ' -f3,4)
    width=${box% *}
    height=${box#* }
    "$chrome" --headless=new --disable-gpu --no-sandbox --hide-scrollbars --force-device-scale-factor="$3" \
        --window-size="${width%.*},${height%.*}" --screenshot="$2" "file://$1" >/dev/null 2>&1
}

(cd "$repo" && uv run python scripts/demo.py "$work")
render "$work/dashboard.svg" "$images/dashboard.png" 1.5
render "$work/sidebar.svg" "$images/sidebar.png" 1.5

(cd "$repo" && uv run python scripts/demo_tmux.py "$images/demo.gif")
ls -l "$images"
