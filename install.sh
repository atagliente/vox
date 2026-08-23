#!/bin/sh
# VOX preflight installer for Linux, macOS and Termux.
#
#   sh install.sh              interactive install
#   sh install.sh --yes        no questions asked
#   sh install.sh --uninstall  remove the command
#
# POSIX sh only, no sudo, idempotent: running it twice is harmless.

set -eu

REPO_URL="https://github.com/atagliente/vox"
VOX_HOME_DIR="${VOX_HOME:-$HOME/.vox}"
VENV_DIR="$VOX_HOME_DIR/venv"
ASSUME_YES=0
TOUCH_PATH=1
UNINSTALL=0
BIN_DIR=""

RED=""; GREEN=""; YELLOW=""; DIM=""; RESET=""
if [ -t 1 ]; then
    RED=$(printf '\033[31m'); GREEN=$(printf '\033[32m')
    YELLOW=$(printf '\033[33m'); DIM=$(printf '\033[90m'); RESET=$(printf '\033[0m')
fi

say()  { printf '%s\n' "$*"; }
ok()   { printf '%s[ OK ]%s %s\n' "$GREEN" "$RESET" "$*"; }
warn() { printf '%s[WARN]%s %s\n' "$YELLOW" "$RESET" "$*"; }
die()  { printf '%s[FAIL]%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }

banner() {
    say ""
    say "+----------------------------------------------------------+"
    say "|  V O X   -  W.O.P.R. TERMINAL  -  PREFLIGHT INSTALLER     |"
    say "+----------------------------------------------------------+"
    say ""
}

ask() {
    # ask "question" -> 0 for yes, 1 for no
    if [ "$ASSUME_YES" -eq 1 ]; then return 0; fi
    if [ ! -t 0 ]; then return 1; fi
    printf '%s [y/N] ' "$1"
    read -r answer || return 1
    case "$answer" in [yY]|[yY][eE][sS]) return 0 ;; *) return 1 ;; esac
}

usage() {
    say "usage: sh install.sh [--yes] [--no-path] [--prefix DIR] [--uninstall]"
    exit 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        -y|--yes)       ASSUME_YES=1 ;;
        --no-path)      TOUCH_PATH=0 ;;
        --prefix)       shift; [ $# -gt 0 ] || die "--prefix needs a directory"; BIN_DIR="$1" ;;
        --uninstall)    UNINSTALL=1 ;;
        -h|--help)      usage ;;
        *)              die "unknown option: $1 (try --help)" ;;
    esac
    shift
done

# ---------------------------------------------------------------- platform

IS_TERMUX=0
case "${PREFIX:-}" in *com.termux*) IS_TERMUX=1 ;; esac
PLATFORM=$(uname -s 2>/dev/null || echo unknown)
[ "$IS_TERMUX" -eq 1 ] && PLATFORM="Termux"

if [ -z "$BIN_DIR" ]; then
    if [ "$IS_TERMUX" -eq 1 ]; then BIN_DIR="$PREFIX/bin"; else BIN_DIR="$HOME/.local/bin"; fi
fi

# ------------------------------------------------------------------ python

find_python() {
    for candidate in python3.13 python3.12 python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then
            if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 12) else 1)' 2>/dev/null; then
                printf '%s' "$candidate"
                return 0
            fi
        fi
    done
    return 1
}

python_hint() {
    case "$PLATFORM" in
        Termux)  say "  pkg install python" ;;
        Darwin)  say "  brew install python@3.12" ;;
        Linux)
            say "  Debian/Ubuntu: sudo apt install python3.12 python3.12-venv"
            say "  Fedora:        sudo dnf install python3.12"
            say "  Arch:          sudo pacman -S python"
            ;;
        *)       say "  install Python 3.12 or newer from https://python.org" ;;
    esac
}

# --------------------------------------------------------------- uninstall

do_uninstall() {
    removed=0
    if command -v pipx >/dev/null 2>&1; then
        if pipx uninstall vox >/dev/null 2>&1; then ok "pipx package removed"; removed=1; fi
    fi
    if [ -d "$VENV_DIR" ]; then rm -rf "$VENV_DIR"; ok "venv removed: $VENV_DIR"; removed=1; fi
    if [ -f "$BIN_DIR/vox" ]; then rm -f "$BIN_DIR/vox"; ok "launcher removed: $BIN_DIR/vox"; removed=1; fi
    [ "$removed" -eq 0 ] && warn "nothing to remove"
    say ""
    say "${DIM}Your settings and sessions are untouched in $VOX_HOME_DIR${RESET}"
    say "${DIM}Delete them with: rm -rf $VOX_HOME_DIR${RESET}"
    exit 0
}

# ------------------------------------------------------------------ source

script_dir() {
    # Directory of this script, or empty when it was piped into sh.
    case "$0" in
        -|sh|bash|dash|/dev/fd/*) printf '' ;;
        *) (cd "$(dirname "$0")" 2>/dev/null && pwd) ;;
    esac
}

banner
say "PLATFORM: $PLATFORM"

[ "$UNINSTALL" -eq 1 ] && do_uninstall

PY=$(find_python) || {
    printf '%s[FAIL]%s Python 3.12 or newer not found. Install it with:\n' "$RED" "$RESET" >&2
    python_hint
    exit 1
}
ok "python: $PY ($("$PY" -c 'import platform; print(platform.python_version())'))"

SRC=$(script_dir)
if [ -n "$SRC" ] && [ -f "$SRC/pyproject.toml" ]; then
    TARGET="$SRC"
    ok "source: local checkout $SRC"
else
    TARGET="git+$REPO_URL"
    ok "source: $REPO_URL"
fi

# ----------------------------------------------------------------- install

install_with_pipx() {
    command -v pipx >/dev/null 2>&1 || return 1
    say "${DIM}installing with pipx...${RESET}"
    pipx install --force "$TARGET" >/dev/null 2>&1 || return 1
    ok "installed with pipx"
    BIN_DIR="$HOME/.local/bin"
    return 0
}

install_with_venv() {
    say "${DIM}installing into $VENV_DIR ...${RESET}"
    if [ ! -d "$VENV_DIR" ]; then
        "$PY" -m venv "$VENV_DIR" || die "cannot create the virtual environment (on Debian install python3-venv)"
    fi
    "$VENV_DIR/bin/python" -m pip install --upgrade pip >/dev/null 2>&1 || true
    "$VENV_DIR/bin/python" -m pip install --upgrade "$TARGET" >/dev/null || die "pip install failed"
    mkdir -p "$BIN_DIR"
    cat > "$BIN_DIR/vox" <<LAUNCHER
#!/bin/sh
# VOX launcher - generated by install.sh
exec "$VENV_DIR/bin/python" -m vox_chat "\$@"
LAUNCHER
    chmod +x "$BIN_DIR/vox"
    ok "installed in $VENV_DIR"
    ok "launcher: $BIN_DIR/vox"
}

if ! install_with_pipx; then
    warn "pipx not usable, falling back to a dedicated virtual environment"
    install_with_venv
fi

# -------------------------------------------------------------------- PATH

in_path() {
    case ":$PATH:" in *":$1:"*) return 0 ;; *) return 1 ;; esac
}

profile_file() {
    case "${SHELL:-}" in
        */zsh)  printf '%s' "$HOME/.zshrc" ;;
        */bash) [ -f "$HOME/.bashrc" ] && printf '%s' "$HOME/.bashrc" || printf '%s' "$HOME/.profile" ;;
        *)      printf '%s' "$HOME/.profile" ;;
    esac
}

if in_path "$BIN_DIR"; then
    ok "PATH already contains $BIN_DIR"
elif [ "$TOUCH_PATH" -eq 0 ]; then
    warn "$BIN_DIR is not in PATH (--no-path given, add it yourself)"
else
    PROFILE=$(profile_file)
    say ""
    say "$BIN_DIR is not in your PATH."
    if ask "Append it to $PROFILE?"; then
        printf '\n# added by VOX install.sh\nexport PATH="%s:$PATH"\n' "$BIN_DIR" >> "$PROFILE"
        ok "PATH updated in $PROFILE"
        say "${DIM}open a new shell, or run: export PATH=\"$BIN_DIR:\$PATH\"${RESET}"
    else
        warn "add it manually: export PATH=\"$BIN_DIR:\$PATH\""
    fi
fi

# ------------------------------------------------------------- final check

say ""
if [ -x "$BIN_DIR/vox" ]; then
    VOX_CMD="$BIN_DIR/vox"
elif command -v vox >/dev/null 2>&1; then
    VOX_CMD="vox"
else
    die "installation finished but the vox command was not found"
fi

set +e
"$VOX_CMD" doctor --plain --timeout 5
STATUS=$?
set -e

say ""
case "$STATUS" in
    0) ok "VOX IS READY - run: vox" ;;
    1) warn "VOX is installed; the provider is unreachable. Fix it with: vox --help, then edit the config"
       say "${DIM}config file: $VOX_HOME_DIR/config.json${RESET}" ;;
    *) die "system check failed - see the report above" ;;
esac
exit 0
