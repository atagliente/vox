#!/bin/sh
# VOX preflight installer for Linux, macOS and Termux.
#
#   sh install.sh              interactive install
#   sh install.sh --yes        no questions asked
#   sh install.sh --uninstall  remove the command
#
# POSIX sh only, idempotent: running it twice is harmless. It never uses sudo
# without asking, and always shows the exact command it would run.

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

MIN_PYTHON="3.11"

find_python() {
    for candidate in python3.14 python3.13 python3.12 python3.11 python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then
            if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 11) else 1)' 2>/dev/null; then
                printf '%s' "$candidate"
                return 0
            fi
        fi
    done
    return 1
}

report_python_found() {
    # Say what is there, so "not found" never looks like a mystery.
    for candidate in python3 python; do
        command -v "$candidate" >/dev/null 2>&1 || continue
        version=$("$candidate" -c 'import platform; print(platform.python_version())' 2>/dev/null)
        [ -n "$version" ] && say "  ${DIM}$candidate is $version, older than $MIN_PYTHON${RESET}"
    done
}

# ------------------------------------------------------- package management

PACKAGE_MANAGER=""
SUDO=""

detect_package_manager() {
    for manager in apt-get dnf yum pacman zypper apk brew pkg; do
        if command -v "$manager" >/dev/null 2>&1; then
            PACKAGE_MANAGER="$manager"
            break
        fi
    done
    # Termux and root need no sudo; everyone else does, if it is there.
    if [ "$IS_TERMUX" -eq 0 ] && [ "$(id -u 2>/dev/null || echo 1)" -ne 0 ]; then
        case "$PACKAGE_MANAGER" in
            brew|pkg|"") SUDO="" ;;
            *) command -v sudo >/dev/null 2>&1 && SUDO="sudo" ;;
        esac
    fi
}

install_command() {
    # install_command <package>... -> prints the command that would install them
    case "$PACKAGE_MANAGER" in
        apt-get) printf '%s apt-get install -y %s' "$SUDO" "$*" ;;
        dnf)     printf '%s dnf install -y %s' "$SUDO" "$*" ;;
        yum)     printf '%s yum install -y %s' "$SUDO" "$*" ;;
        pacman)  printf '%s pacman -S --needed --noconfirm %s' "$SUDO" "$*" ;;
        zypper)  printf '%s zypper --non-interactive install %s' "$SUDO" "$*" ;;
        apk)     printf '%s apk add %s' "$SUDO" "$*" ;;
        brew)    printf 'brew install %s' "$*" ;;
        pkg)     printf 'pkg install -y %s' "$*" ;;
        *)       printf '' ;;
    esac
}

run_install() {
    # Ask before touching the system, and always show the exact command.
    command=$(install_command "$@")
    if [ -z "$command" ]; then
        warn "no package manager found; install these yourself: $*"
        return 1
    fi
    say ""
    say "Missing packages: $*"
    say "  ${DIM}$command${RESET}"
    if [ -n "$SUDO" ] && ! command -v sudo >/dev/null 2>&1; then
        warn "sudo is not available; run that as root and start again"
        return 1
    fi
    # sudo without a terminal waits for a password nobody can type, which
    # looks exactly like a hang. Check first, and say so instead.
    if [ -n "$SUDO" ] && ! sudo -n true 2>/dev/null && [ ! -t 0 ]; then
        warn "sudo needs a password and there is no terminal to type it in"
        say "  ${DIM}run the command above yourself, then start this again${RESET}"
        return 1
    fi
    if ! ask "Run it now?"; then
        # What being skipped costs depends on the caller, so it says so.
        warn "skipped"
        return 1
    fi
    # shellcheck disable=SC2086
    if [ "$PACKAGE_MANAGER" = "apt-get" ]; then
        ${SUDO} apt-get update >/dev/null 2>&1 || true
    fi
    if eval "$command"; then
        ok "installed: $*"
        return 0
    fi
    warn "the package manager refused; see its output above"
    return 1
}

python_packages() {
    # What this platform calls a usable Python, venv and pip included.
    case "$PACKAGE_MANAGER" in
        apt-get) printf 'python3 python3-venv python3-pip' ;;
        dnf|yum) printf 'python3 python3-pip' ;;
        pacman)  printf 'python python-pip' ;;
        zypper)  printf 'python3 python3-pip' ;;
        apk)     printf 'python3 py3-pip' ;;
        brew)    printf 'python' ;;
        pkg)     printf 'python' ;;
        *)       printf '' ;;
    esac
}

venv_packages() {
    # Debian splits venv and ensurepip out of the interpreter package.
    version=$("$1" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)
    case "$PACKAGE_MANAGER" in
        apt-get)
            if [ -n "$version" ]; then
                # python3.11-venv on Debian 12, python3.12-venv on Ubuntu 24.04.
                printf 'python3-venv python%s-venv python3-pip' "$version"
            else
                printf 'python3-venv python3-pip'
            fi
            ;;
        *) printf '' ;;
    esac
}

clipboard_packages() {
    # Copy and paste need a helper on Linux; without one the keys report a
    # failure instead of working. Not required to run VOX.
    case "$PACKAGE_MANAGER" in
        apt-get|dnf|yum|zypper) printf 'xclip' ;;
        pacman) printf 'xclip' ;;
        apk)    printf 'xclip' ;;
        pkg)    printf 'termux-api' ;;
        *)      printf '' ;;
    esac
}

offer_clipboard_helper() {
    [ "$PLATFORM" = "Darwin" ] && return 0          # pbcopy is always there
    for helper in xclip xsel wl-copy termux-clipboard-set; do
        command -v "$helper" >/dev/null 2>&1 && return 0
    done
    packages=$(clipboard_packages)
    [ -z "$packages" ] && return 0
    say ""
    warn "no clipboard helper found: ctrl+c and ctrl+v will report a failure"
    run_install $packages || warn "carry on without it; everything else works"
}

ensure_venv_support() {
    # A Python that cannot build a venv is no use to us; on Debian that is a
    # missing package rather than a broken interpreter, so offer to add it.
    "$1" -c 'import venv, ensurepip' 2>/dev/null && return 0
    warn "$1 has no venv or ensurepip module"
    packages=$(venv_packages "$1")
    if [ -z "$packages" ]; then
        warn "install the venv module for $1 and start again"
        return 1
    fi
    # Try the version-specific name too; apt ignores the ones that do not exist.
    for package in $packages; do
        run_install "$package" >/dev/null 2>&1 && break
    done
    if "$1" -c 'import venv, ensurepip' 2>/dev/null; then
        ok "venv is available now"
        return 0
    fi
    if ! run_install $packages; then
        warn "VOX needs the venv module to install itself"
        return 1
    fi
    "$1" -c 'import venv, ensurepip' 2>/dev/null
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

detect_package_manager
[ -n "$PACKAGE_MANAGER" ] && say "${DIM}package manager: $PACKAGE_MANAGER${RESET}"

PY=$(find_python) || {
    warn "no Python $MIN_PYTHON or newer on this system"
    report_python_found
    packages=$(python_packages)
    if [ -n "$packages" ] && run_install $packages; then
        PY=$(find_python) || true
    fi
}

if [ -z "${PY:-}" ]; then
    printf '%s[FAIL]%s VOX needs Python %s or newer.\n' "$RED" "$RESET" "$MIN_PYTHON" >&2
    say ""
    say "Your distribution may not carry one. Two ways that always work:"
    say "  ${DIM}curl -LsSf https://astral.sh/uv/install.sh | sh && uv python install 3.12${RESET}"
    say "  ${DIM}or pyenv: https://github.com/pyenv/pyenv${RESET}"
    say "On Debian 12 the stock python3 is 3.11, which is enough — install it with:"
    say "  ${DIM}sudo apt-get install python3 python3-venv python3-pip${RESET}"
    exit 1
fi
ok "python: $PY ($("$PY" -c 'import platform; print(platform.python_version())'))"

ensure_venv_support "$PY" || die "cannot create virtual environments with $PY"
offer_clipboard_helper

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
    pipx_bin=$(pipx environment --value PIPX_BIN_DIR 2>/dev/null)
    BIN_DIR="${pipx_bin:-$HOME/.local/bin}"
    return 0
}

install_with_venv() {
    say "${DIM}installing into $VENV_DIR ...${RESET}"
    if [ ! -d "$VENV_DIR" ]; then
        if ! "$PY" -m venv "$VENV_DIR"; then
            # Almost always a missing python3-venv on Debian and friends.
            ensure_venv_support "$PY" || die "cannot create a virtual environment"
            "$PY" -m venv "$VENV_DIR" || die "cannot create a virtual environment"
        fi
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
    if ask "Append it to $PROFILE, so vox works from any directory?"; then
        printf '\n# added by VOX install.sh\nexport PATH="%s:$PATH"\n' "$BIN_DIR" >> "$PROFILE"
        ok "PATH updated in $PROFILE - open a new shell and just type: vox"
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
