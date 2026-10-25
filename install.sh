#!/usr/bin/env bash
# Install GPU Bar Verifier into ~/.local (or $PREFIX).
set -euo pipefail

SRC="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
PREFIX="${PREFIX:-$HOME/.local}"
BIN_DIR="$PREFIX/bin"
LIB_DIR="$PREFIX/lib/gpu-bar-verifier"
APP_DIR="$PREFIX/share/applications"
ICON_DIR="$PREFIX/share/icons/hicolor/scalable/apps"

uninstall() {
    echo "Removing GPU Bar Verifier from $PREFIX"
    rm -rf "$LIB_DIR"
    rm -f "$BIN_DIR/gpu-bar-verifier"
    rm -f "$APP_DIR/gpu-bar-verifier.desktop"
    rm -f "$ICON_DIR/gpu-bar-verifier.svg"
    command -v update-desktop-database >/dev/null 2>&1 && \
        update-desktop-database "$APP_DIR" 2>/dev/null || true
    echo "Done."
}

if [[ "${1:-}" == "--uninstall" ]]; then
    uninstall
    exit 0
fi

echo "Installing GPU Bar Verifier into $PREFIX"

missing=0
check() {
    if ! "$@" >/dev/null 2>&1; then
        echo "  missing: $*"
        missing=1
    fi
}
check python3 -c "import PIL"
check python3 -c "import gi; gi.require_version('Gtk','3.0')"
if [[ $missing -eq 1 ]]; then
    echo
    echo "Install the missing pieces first, e.g.:"
    echo "  sudo apt install python3-gi gir1.2-gtk-3.0 python3-pil"
    exit 1
fi

if ! command -v nvidia-smi >/dev/null 2>&1; then
    echo "  note: nvidia-smi not found - dedicated-GPU comparison will be unavailable"
fi

mkdir -p "$BIN_DIR" "$APP_DIR" "$ICON_DIR"
rm -rf "$LIB_DIR"
mkdir -p "$LIB_DIR"
cp -r "$SRC/gpu_bar_verifier" "$LIB_DIR/"
find "$LIB_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} +

cat > "$BIN_DIR/gpu-bar-verifier" <<EOF
#!/usr/bin/env bash
export PYTHONPATH="$LIB_DIR\${PYTHONPATH:+:\$PYTHONPATH}"
exec python3 -m gpu_bar_verifier.cli "\$@"
EOF
chmod +x "$BIN_DIR/gpu-bar-verifier"

sed "s|@EXEC@|$BIN_DIR/gpu-bar-verifier|g" \
    "$SRC/packaging/gpu-bar-verifier.desktop" > "$APP_DIR/gpu-bar-verifier.desktop"
cp "$SRC/assets/gpu-bar-verifier.svg" "$ICON_DIR/gpu-bar-verifier.svg"

command -v update-desktop-database >/dev/null 2>&1 && \
    update-desktop-database "$APP_DIR" 2>/dev/null || true

echo "  binary : $BIN_DIR/gpu-bar-verifier"
echo "  desktop: $APP_DIR/gpu-bar-verifier.desktop"
echo "  icon   : $ICON_DIR/gpu-bar-verifier.svg"
echo
echo "Run it with: gpu-bar-verifier"
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo "note: $BIN_DIR is not on your PATH" ;;
esac
