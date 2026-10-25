#!/usr/bin/env bash
# Install GPU Bar Verifier into ~/.local (or $PREFIX).
#
# Installs the application, every icon size into the hicolor theme, and a
# desktop entry so the app shows up in the menu / launcher and is searchable.
set -euo pipefail

SRC="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
PREFIX="${PREFIX:-$HOME/.local}"
BIN_DIR="$PREFIX/bin"
LIB_DIR="$PREFIX/lib/gpu-bar-verifier"
APP_DIR="$PREFIX/share/applications"
ICON_ROOT="$PREFIX/share/icons/hicolor"

uninstall() {
    echo "Removing GPU Bar Verifier from $PREFIX"
    rm -rf "$LIB_DIR"
    rm -f "$BIN_DIR/gpu-bar-verifier"
    rm -f "$APP_DIR/gpu-bar-verifier.desktop"
    rm -f "$ICON_ROOT/scalable/apps/gpu-bar-verifier.svg"
    for size in 16 24 32 48 64 128 256 512; do
        rm -f "$ICON_ROOT/${size}x${size}/apps/gpu-bar-verifier.png"
    done
    command -v update-desktop-database >/dev/null 2>&1 && \
        update-desktop-database "$APP_DIR" 2>/dev/null || true
    command -v gtk-update-icon-cache >/dev/null 2>&1 && \
        gtk-update-icon-cache -f -t "$ICON_ROOT" 2>/dev/null || true
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

# ---------------------------------------------------------------- application
mkdir -p "$BIN_DIR" "$LIB_DIR"
rm -rf "$LIB_DIR"
mkdir -p "$LIB_DIR"
cp -r "$SRC/gpu_bar_verifier" "$LIB_DIR/"
cp -r "$SRC/assets" "$LIB_DIR/"
find "$LIB_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} +

cat > "$BIN_DIR/gpu-bar-verifier" <<EOF
#!/usr/bin/env bash
export PYTHONPATH="$LIB_DIR\${PYTHONPATH:+:\$PYTHONPATH}"
exec python3 -m gpu_bar_verifier.cli "\$@"
EOF
chmod +x "$BIN_DIR/gpu-bar-verifier"

# ---------------------------------------------------------------- icons
# Every size in the hicolor theme: launchers pick the right one for the menu,
# the taskbar and the window switcher instead of scaling one bitmap.
for size in 16 24 32 48 64 128 256 512; do
    src_png="$SRC/assets/icons/gpu-bar-verifier-${size}.png"
    [[ -f "$src_png" ]] || continue
    mkdir -p "$ICON_ROOT/${size}x${size}/apps"
    install -m 644 "$src_png" "$ICON_ROOT/${size}x${size}/apps/gpu-bar-verifier.png"
done
mkdir -p "$ICON_ROOT/scalable/apps"
install -m 644 "$SRC/assets/gpu-bar-verifier.svg" \
    "$ICON_ROOT/scalable/apps/gpu-bar-verifier.svg"

# An icon cache speeds up menu searches; it needs a theme index to work.
if [[ ! -f "$ICON_ROOT/index.theme" && -f /usr/share/icons/hicolor/index.theme ]]; then
    cp /usr/share/icons/hicolor/index.theme "$ICON_ROOT/index.theme" 2>/dev/null || true
fi
command -v gtk-update-icon-cache >/dev/null 2>&1 && \
    gtk-update-icon-cache -f -t "$ICON_ROOT" 2>/dev/null || true

# ---------------------------------------------------------------- menu entry
mkdir -p "$APP_DIR"
sed "s|@EXEC@|$BIN_DIR/gpu-bar-verifier|g" \
    "$SRC/packaging/gpu-bar-verifier.desktop" > "$APP_DIR/gpu-bar-verifier.desktop"
chmod 644 "$APP_DIR/gpu-bar-verifier.desktop"

command -v update-desktop-database >/dev/null 2>&1 && \
    update-desktop-database "$APP_DIR" 2>/dev/null || true

echo "  binary : $BIN_DIR/gpu-bar-verifier"
echo "  desktop: $APP_DIR/gpu-bar-verifier.desktop"
echo "  icons  : $ICON_ROOT/{16..512}x/apps/gpu-bar-verifier.png"
echo
echo "Run it with: gpu-bar-verifier   (or search 'GPU' in the menu)"
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo "note: $BIN_DIR is not on your PATH" ;;
esac
