#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'USAGE'
Install Git Trail on macOS or Linux.

Usage: bash script.sh [--prefix DIR] [--ref REF]
       bash script.sh [--prefix DIR] --uninstall

  --prefix DIR  Installation prefix (default: ~/.local).
  --ref REF     GitHub commit, tag, or branch (default: verified v0.6.0 commit).
  --uninstall   Remove this install and command. Keep user settings.
  --help        Show this help.

Requires Git, curl, tar, and internet access. Python is downloaded privately.
The installed command is PREFIX/bin/git-trail.
USAGE
}

fail() {
    printf 'Git Trail: %s\n' "$*" >&2
    exit 1
}

cleanup() {
    local result=$?
    trap - EXIT
    if [[ -n "$bundle" && -L "$root/current" &&
        "$(readlink "$root/current")" == "$bundle" ]]; then
        activated=1
    fi
    if [[ -n "$bundle" && "$activated" == 0 ]]; then
        rm -rf -- "$bundle"
    fi
    if [[ "$locked" == 1 ]]; then
        rmdir -- "$root/.install-lock" 2>/dev/null || true
    fi
    if [[ "$created_root" == 1 && "$activated" == 0 ]]; then
        rmdir -- "$root/releases" 2>/dev/null || true
        if [[ ! -e "$root/current" && ! -L "$root/current" ]]; then
            rm -f -- "$root/.git-trail-install"
            rmdir -- "$root" 2>/dev/null || true
        fi
    fi
    exit "$result"
}

main() {
    local prefix="${HOME:?HOME must be set}/.local"
    local ref="949f95b6d8c20fc89c3e2984e4209555013d0966"
    local uninstall=0 ref_given=0 tool platform uv_path version current_target
    local owner="yellowHatpro/git-trail installer v1"
    root="" bundle="" locked=0 activated=0 created_root=0

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --prefix|--ref)
                [[ $# -ge 2 && -n "$2" && "$2" != --* ]] ||
                    fail "$1 needs a value."
                if [[ "$1" == --prefix ]]; then
                    prefix="$2"
                else
                    ref="$2"
                    ref_given=1
                fi
                shift 2
                ;;
            --uninstall) uninstall=1; shift ;;
            --help|-h) usage; return ;;
            *) fail "Unknown option: $1. Use --help." ;;
        esac
    done
    [[ "$uninstall" == 0 || "$ref_given" == 0 ]] ||
        fail "--ref and --uninstall cannot be combined."
    [[ "$ref" =~ ^[[:alnum:]][[:alnum:]_./-]*$ &&
        "$ref" != *".."* && "$ref" != *"//"* && "$ref" != */ ]] ||
        fail "Invalid Git reference: $ref."
    platform="$(uname -s)"
    [[ "$platform" == Darwin || "$platform" == Linux ]] ||
        fail "This installer supports macOS and Linux."
    if [[ "$uninstall" == 0 ]]; then
        for tool in git curl tar; do
            command -v "$tool" >/dev/null 2>&1 ||
                fail "Install $tool first, then run this script again."
        done
        mkdir -p -- "$prefix"
    else
        [[ -d "$prefix" ]] || fail "No Git Trail installation at $prefix."
    fi
    prefix="$(CDPATH= cd -P -- "$prefix" && pwd)"
    [[ "$prefix" != / ]] || fail "Choose a prefix other than /."
    root="$prefix/share/git-trail"
    local command_path="$prefix/bin/git-trail"

    if [[ -e "$command_path" || -L "$command_path" ]]; then
        [[ -L "$command_path" &&
            "$(readlink "$command_path")" == "$root/current/git-trail" ]] ||
            fail "Existing command is not managed by this installer: $command_path. Choose another --prefix."
    fi
    if [[ -e "$root" || -L "$root" ]]; then
        [[ -d "$root" && ! -L "$root" &&
            -f "$root/.git-trail-install" &&
            ! -L "$root/.git-trail-install" &&
            "$(cat "$root/.git-trail-install")" == "$owner" ]] ||
            fail "Existing directory is not managed by this installer: $root."
    else
        [[ "$uninstall" == 0 ]] || fail "No managed Git Trail installation at $prefix."
        mkdir -p -- "$prefix/share"
        mkdir -- "$root" || fail "Another installer created $root. Try again."
        created_root=1
    fi
    trap cleanup EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    mkdir -- "$root/.install-lock" 2>/dev/null ||
        fail "Installation is locked: $root/.install-lock. Wait for the other installer to finish."
    locked=1
    if [[ "$created_root" == 1 ]]; then
        printf '%s\n' "$owner" > "$root/.git-trail-install"
    fi
    [[ ! -e "$root/current" || -L "$root/current" ]] ||
        fail "Expected a managed symlink at $root/current."
    if [[ -L "$root/current" ]]; then
        current_target="$(readlink "$root/current")"
        [[ "$current_target" == "$root/releases/install."* &&
            "$(dirname -- "$current_target")" == "$root/releases" ]] ||
            fail "Expected a managed release symlink at $root/current."
    fi
    [[ ! -L "$root/releases" ]] ||
        fail "Expected a managed directory at $root/releases."

    if [[ "$uninstall" == 1 ]]; then
        rm -f -- "$command_path"
        rm -rf -- "$root"
        locked=0
        printf 'Removed Git Trail from %s. User settings were kept.\n' "$prefix"
        return
    fi

    mkdir -p -- "$root/releases" "$prefix/bin"
    bundle="$(mktemp -d "$root/releases/install.XXXXXX")"
    printf 'Downloading Git Trail (%s)...\n' "$ref"
    curl --fail --show-error --silent --location --retry 2 \
        "https://codeload.github.com/yellowHatpro/git-trail/tar.gz/$ref" \
        --output "$bundle/source.tar.gz"
    tar -xzf "$bundle/source.tar.gz" --strip-components=1 -C "$bundle"
    rm -f -- "$bundle/source.tar.gz"
    [[ -f "$bundle/git-trail" && ! -L "$bundle/git-trail" &&
        -f "$bundle/pyproject.toml" && -d "$bundle/git_trail" &&
        ! -L "$bundle/.venv" && ! -L "$bundle/.runtime" ]] ||
        fail "The downloaded archive is missing Git Trail's source."
    chmod +x "$bundle/git-trail"
    mkdir -p -- "$bundle/.runtime"

    if command -v uv >/dev/null 2>&1; then
        uv_path="$(command -v uv)"
    else
        printf 'Downloading the private runtime installer...\n'
        curl --fail --show-error --silent --location --retry 2 \
            https://astral.sh/uv/install.sh --output "$bundle/.runtime/install-uv.sh"
        UV_UNMANAGED_INSTALL="$bundle/.runtime/uv" UV_NO_MODIFY_PATH=1 \
            sh "$bundle/.runtime/install-uv.sh"
        uv_path="$bundle/.runtime/uv/uv"
        [[ -x "$uv_path" ]] || fail "The private uv installation did not produce an executable."
    fi

    export UV_PYTHON_INSTALL_DIR="$bundle/.runtime/python"
    export UV_PYTHON_BIN_DIR="$bundle/.runtime/bin"
    export UV_PYTHON_INSTALL_BIN=0
    export UV_CACHE_DIR="$bundle/.runtime/cache"
    export UV_PYTHON_DOWNLOADS=automatic
    printf 'Installing private Python 3.13 and prebuilt dependencies...\n'
    "$uv_path" --no-config venv --python 3.13 --managed-python "$bundle/.venv"
    "$uv_path" --no-config pip install --python "$bundle/.venv/bin/python" \
        --only-binary :all: --link-mode copy -r "$bundle/pyproject.toml"
    version="$("$bundle/git-trail" --version)"
    [[ "$version" == "git-trail "* ]] || fail "The installed command failed its version check."

    # Activate symlinks with atomic replacements.
    "$bundle/.venv/bin/python" -I -c '
import os
import sys
import tempfile

root, bundle, command = sys.argv[1:]
current = os.path.join(root, "current")
previous = None
if os.path.lexists(current):
    if not os.path.islink(current):
        raise SystemExit("Git Trail: expected a managed current symlink.")
    previous = os.readlink(current)
    if (
        os.path.dirname(previous) != os.path.join(root, "releases")
        or not os.path.basename(previous).startswith("install.")
    ):
        raise SystemExit("Git Trail: expected a managed release symlink.")

def replace_link(target, path):
    fd, temporary = tempfile.mkstemp(prefix=".git-trail-", dir=os.path.dirname(path))
    os.close(fd)
    os.unlink(temporary)
    try:
        os.symlink(target, temporary)
        os.replace(temporary, path)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)

if os.path.lexists(command) and (
    not os.path.islink(command) or os.readlink(command) != current + "/git-trail"
):
    raise SystemExit("Git Trail: another command appeared during installation.")
replace_link(bundle, current)
try:
    if not os.path.lexists(command):
        replace_link(current + "/git-trail", command)
except BaseException:
    if previous is None:
        os.unlink(current)
    else:
        replace_link(previous, current)
    raise
' "$root" "$bundle" "$command_path"
    activated=1
    printf 'Installed %s at %s\n' "$version" "$command_path"
    case ":${PATH:-}:" in
        *":$prefix/bin:"*) ;;
        *)
            printf 'Add the command directory to your shell PATH:\n  export PATH='
            printf '%q' "$prefix/bin"
            printf ':\"$PATH\"\n'
            ;;
    esac
    printf 'Run: git-trail /path/to/repository\n'
}

main "$@"
