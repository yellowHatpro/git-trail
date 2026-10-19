#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'USAGE'
Install Git Trail on macOS or Linux.

Usage: bash script.sh [--prefix DIR] [--ref REF]
       bash script.sh [--prefix DIR] --uninstall

  --prefix DIR  Installation prefix (default: ~/.local).
  --ref REF     GitHub commit, tag, or branch (default: verified v0.7.0 commit).
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

require_tools() {
    local tool
    for tool in git curl tar; do
        command -v "$tool" >/dev/null 2>&1 ||
            fail "Install $tool first, then run this script again."
    done
}

canonical_file() {
    local path="$1" directory target count=0
    while [[ -L "$path" ]]; do
        [[ "$count" -lt 40 ]] || return 1
        directory="$(CDPATH= cd -P -- "$(dirname -- "$path")" && pwd)" || return 1
        target="$(readlink "$path")" || return 1
        case "$target" in
            /*) path="$target" ;;
            *) path="$directory/$target" ;;
        esac
        count=$((count + 1))
    done
    [[ -f "$path" ]] || return 1
    directory="$(CDPATH= cd -P -- "$(dirname -- "$path")" && pwd)" || return 1
    printf '%s/%s\n' "$directory" "${path##*/}"
}

checkout_source() (
    local source directory top origin line section="" project=0 entry=0 launcher=0 variable
    source="$(canonical_file "$1")" || return 1
    [[ "${source##*/}" == git-trail && -x "$source" ]] || return 1
    directory="${source%/*}"
    [[ -f "$directory/pyproject.toml" && ! -L "$directory/pyproject.toml" &&
        -d "$directory/git_trail" && ! -L "$directory/git_trail" &&
        -f "$directory/git_trail/cli.py" && ! -L "$directory/git_trail/cli.py" &&
        ( -d "$directory/.git" || -f "$directory/.git" ) ]] || return 1
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line// /}"
        line="${line//$'\t'/}"
        line="${line//$'\r'/}"
        case "$line" in
            \[*\]) section="$line" ;;
            'name="git-trail-tui"'|"name='git-trail-tui'")
                [[ "$section" != "[project]" ]] || project=1 ;;
            'git-trail="git_trail.cli:main"'|"git-trail='git_trail.cli:main'")
                [[ "$section" != "[project.scripts]" ]] || entry=1 ;;
        esac
    done < "$directory/pyproject.toml"
    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ "$line" != *"from git_trail.cli import main"* ]] || launcher=1
    done < "$source"
    [[ "$project" == 1 && "$entry" == 1 && "$launcher" == 1 ]] || return 1
    command -v git >/dev/null 2>&1 || return 1
    for variable in ${!GIT_@}; do unset "$variable"; done
    export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_OPTIONAL_LOCKS=0
    top="$(git -C "$directory" rev-parse --show-toplevel </dev/null 2>/dev/null)" || return 1
    [[ "$(CDPATH= cd -P -- "$top" && pwd)" == "$directory" ]] || return 1
    origin="$(git -C "$directory" config --local --get remote.origin.url </dev/null 2>/dev/null)" || return 1
    case "$origin" in
        git@github.com:yellowHatpro/git-trail|git@github.com:yellowHatpro/git-trail.git|\
        https://github.com/yellowHatpro/git-trail|https://github.com/yellowHatpro/git-trail.git|\
        ssh://git@github.com/yellowHatpro/git-trail|ssh://git@github.com/yellowHatpro/git-trail.git)
            printf '%s\n' "$source" ;;
        *) return 1 ;;
    esac
)

installed_launcher() {
    cat <<'LAUNCHER'
#!/usr/bin/env bash
set -euo pipefail
if [[ -z "${TEXTUAL_COLOR_SYSTEM:-}" ]]; then
    unset TEXTUAL_COLOR_SYSTEM
    if [[ -z "${NO_COLOR+x}" && "${TERM_PROGRAM:-}" != Apple_Terminal ]]; then
        case "${TERM:-}" in
            ""|dumb|linux|ansi|vt100|vt102|vt220|cons25|cons25-*|xterm|screen) ;;
            xterm-256color|xterm-direct|xterm-kitty|kitty|alacritty|alacritty-*|\
            wezterm|wezterm-*|foot|foot-*|tmux-256color|screen-256color|\
            screen-256color-*|rxvt-unicode-256color|st-256color|*-direct|*-truecolor|*-24bit)
                export TEXTUAL_COLOR_SYSTEM=truecolor ;;
            *)
                case "${COLORTERM:-}" in
                    truecolor|24bit) export TEXTUAL_COLOR_SYSTEM=truecolor ;;
                    *)
                        case "${TERM_PROGRAM:-}" in
                            iTerm.app|WezTerm|vscode|Hyper|ghostty|kitty)
                                export TEXTUAL_COLOR_SYSTEM=truecolor ;;
                        esac
                        if [[ -n "${KITTY_WINDOW_ID:-}${KONSOLE_VERSION:-}${VTE_VERSION:-}" ]]; then
                            export TEXTUAL_COLOR_SYSTEM=truecolor
                        fi
                        ;;
                esac
                ;;
        esac
    fi
fi
git_trail_source="${BASH_SOURCE[0]}"
while [[ -L "$git_trail_source" ]]; do
    git_trail_link_directory="$(CDPATH= cd -P -- "$(dirname -- "$git_trail_source")" && pwd)"
    git_trail_source="$(readlink "$git_trail_source")"
    if [[ "$git_trail_source" != /* ]]; then
        git_trail_source="$git_trail_link_directory/$git_trail_source"
    fi
done
git_trail_directory="$(CDPATH= cd -P -- "$(dirname -- "$git_trail_source")" && pwd)"
git_trail_python="$git_trail_directory/.venv/bin/python"
if [[ ! -x "$git_trail_python" ]]; then
    printf '%s\n' "Git Trail needs its Python environment. See $git_trail_directory/README.md." >&2
    exit 2
fi
exec "$git_trail_python" -I -c \
    'import sys; sys.dont_write_bytecode = True; sys.path.insert(0, sys.argv.pop(1)); from git_trail.cli import main; raise SystemExit(main())' \
    "$git_trail_directory" "$@"
LAUNCHER
}

launcher_is_current() {
    local actual=""
    if IFS= read -r -d '' -n $((${#launcher_text} + 1)) actual < "$current_target/git-trail"; then
        return 1
    fi
    [[ "$actual" == "$launcher_text" ]]
}

same_source_is_healthy() {
    local receipt="" receipt_after="" receipt_version expected runtime physical version
    [[ -n "$immutable_ref" && -n "$current_target" &&
        -L "$command_path" && "$(readlink "$command_path")" == "$root/current/git-trail" &&
        -d "$current_target" && ! -L "$current_target" &&
        -f "$current_target/.git-trail-source" && ! -L "$current_target/.git-trail-source" &&
        -f "$current_target/git-trail" && ! -L "$current_target/git-trail" &&
        -x "$current_target/git-trail" && -d "$current_target/git_trail" &&
        ! -L "$current_target/git_trail" ]] || return 1
    if IFS= read -r -d '' -n 4097 receipt < "$current_target/.git-trail-source"; then
        return 1
    fi
    expected="$source_owner"$'\n'"$immutable_ref"$'\n'
    [[ "$receipt" == "$expected"* ]] || return 1
    receipt_version="${receipt#"$expected"}"
    receipt_version="${receipt_version%$'\n'}"
    [[ "$receipt_version" == "git-trail "* &&
        "$receipt_version" != *$'\n'* && "$receipt_version" != *$'\r'* &&
        "$receipt" == "$expected$receipt_version"$'\n' ]] || return 1
    physical="$(CDPATH= cd -P -- "$current_target" && pwd)" || return 1
    runtime="$(canonical_file "$current_target/.venv/bin/python")" || return 1
    [[ -x "$runtime" && "$runtime" == "$physical/.runtime/python/"* ]] || return 1
    version="$("$current_target/.venv/bin/python" -I -c '
import sys
sys.dont_write_bytecode = True
import os
import signal
import subprocess

bundle, expected = sys.argv[1:3]
def check(arguments, capture=False):
    child = subprocess.Popen(arguments, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        output, _ = child.communicate(timeout=8)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.communicate(timeout=2)
        raise SystemExit(1)
    if child.returncode:
        raise SystemExit(1)
    return output
check([os.path.join(bundle, ".venv", "bin", "python"), "-I", "-c",
    "import sys; sys.dont_write_bytecode = True; sys.path.insert(0, sys.argv[1]); import git_trail.app; import git_trail.selectors",
    bundle, "--version"])
if check([os.path.join(bundle, "git-trail"), "--version"], True) != (expected + "\n").encode():
    raise SystemExit(1)
print(expected)
' "$current_target" "$receipt_version" --version </dev/null 2>/dev/null)" || return 1
    [[ -f "$current_target/.git-trail-source" && ! -L "$current_target/.git-trail-source" ]] || return 1
    if IFS= read -r -d '' -n 4097 receipt_after < "$current_target/.git-trail-source"; then
        return 1
    fi
    [[ "$version" == "$receipt_version" &&
        "$receipt_after" == "$receipt" &&
        -L "$root/current" && "$(readlink "$root/current")" == "$current_target" &&
        -L "$command_path" && "$(readlink "$command_path")" == "$command_target" ]] || return 1
    installed_version="$receipt_version"
}

repair_launcher() {
    "$current_target/.venv/bin/python" -I -c '
import os
import signal
import stat
import subprocess
import sys
import tempfile

root, bundle, command, command_target, source_owner, source_ref, version, owner, launcher = sys.argv[1:]
current = os.path.join(root, "current")
path = os.path.join(bundle, "git-trail")
canonical_path = os.path.realpath(path)
python = os.path.join(bundle, ".venv", "bin", "python")

def identity(metadata):
    return metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_size, metadata.st_mtime_ns

def read_regular(filename, limit):
    fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as source:
        metadata = os.fstat(source.fileno())
        data = source.read(limit + 1)
    if not stat.S_ISREG(metadata.st_mode) or len(data) > limit:
        raise SystemExit("Git Trail: invalid managed launcher data.")
    if identity(os.lstat(filename)) != identity(metadata):
        raise SystemExit("Git Trail: managed files changed during launcher repair.")
    return identity(metadata), data

files = {
    os.path.join(root, ".git-trail-install"): read_regular(os.path.join(root, ".git-trail-install"), 256),
    os.path.join(bundle, ".git-trail-source"): read_regular(os.path.join(bundle, ".git-trail-source"), 4096),
    path: read_regular(path, 65536),
}
if files[os.path.join(root, ".git-trail-install")][1] != (owner + "\n").encode():
    raise SystemExit("Git Trail: installation ownership changed.")
if files[os.path.join(bundle, ".git-trail-source")][1] != (
    source_owner + "\n" + source_ref + "\n" + version + "\n"
).encode():
    raise SystemExit("Git Trail: installed source changed.")
directories = [root, os.path.join(root, "releases"), bundle, os.path.join(bundle, "git_trail")]
directory_ids = {}
for directory in directories:
    metadata = os.lstat(directory)
    if not stat.S_ISDIR(metadata.st_mode):
        raise SystemExit("Git Trail: managed directory changed.")
    directory_ids[directory] = metadata.st_dev, metadata.st_ino, metadata.st_mode
runtime = os.path.realpath(python)
if not runtime.startswith(os.path.realpath(bundle) + "/.runtime/python/") or not os.access(runtime, os.X_OK):
    raise SystemExit("Git Trail: private runtime changed.")
runtime_id = identity(os.stat(runtime))

def guard():
    for filename, expected in files.items():
        if read_regular(filename, 65536) != expected:
            raise SystemExit("Git Trail: managed files changed during launcher repair.")
    for directory, expected in directory_ids.items():
        metadata = os.lstat(directory)
        if (metadata.st_dev, metadata.st_ino, metadata.st_mode) != expected:
            raise SystemExit("Git Trail: managed directory changed.")
    if (
        not os.path.islink(current) or os.readlink(current) != bundle
        or not os.path.islink(command) or os.readlink(command) != command_target
        or os.path.realpath(command) != canonical_path or os.path.realpath(path) != canonical_path
        or os.path.realpath(python) != runtime or identity(os.stat(runtime)) != runtime_id
    ):
        raise SystemExit("Git Trail: installation changed during launcher repair.")

guard()
fd, temporary = tempfile.mkstemp(prefix=".git-trail-launcher-", dir=bundle)
try:
    with os.fdopen(fd, "wb") as output:
        output.write(launcher.encode())
    os.chmod(temporary, stat.S_IMODE(files[path][0][2]))
    staged = read_regular(temporary, 65536)
    child = subprocess.Popen([temporary, "--version"], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        output, _ = child.communicate(timeout=8)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.communicate(timeout=2)
        raise SystemExit("Git Trail: repaired launcher version check timed out.")
    if child.returncode or output != (version + "\n").encode():
        raise SystemExit("Git Trail: repaired launcher failed its version check.")
    guard()
    if read_regular(temporary, 65536) != staged:
        raise SystemExit("Git Trail: staged launcher changed.")
    os.replace(temporary, path)
finally:
    if os.path.lexists(temporary):
        os.unlink(temporary)
' "$root" "$current_target" "$command_path" "$command_target" "$source_owner" \
        "$immutable_ref" "$installed_version" "$owner" "$launcher_text" </dev/null
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
    local ref="103b38317ffe3733a1001ee5afb182f2c635a85e"
    local uninstall=0 ref_given=0 platform uv_path version current_target=""
    local owner="yellowHatpro/git-trail installer v1"
    local source_owner="yellowHatpro/git-trail source v1" immutable_ref=""
    local command_target="" legacy_source=""
    local launcher_text="" installed_version=""
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
    if [[ "$ref" =~ ^[0-9a-fA-F]{40}$ ]]; then
        immutable_ref="${ref//A/a}"
        immutable_ref="${immutable_ref//B/b}"
        immutable_ref="${immutable_ref//C/c}"
        immutable_ref="${immutable_ref//D/d}"
        immutable_ref="${immutable_ref//E/e}"
        immutable_ref="${immutable_ref//F/f}"
    fi
    platform="$(uname -s)"
    [[ "$platform" == Darwin || "$platform" == Linux ]] ||
        fail "This installer supports macOS and Linux."
    if [[ "$uninstall" == 0 ]]; then
        if [[ ! -d "$prefix" ]]; then
            require_tools
            mkdir -p -- "$prefix"
        fi
    else
        [[ -d "$prefix" ]] || fail "No Git Trail installation at $prefix."
    fi
    prefix="$(CDPATH= cd -P -- "$prefix" && pwd)"
    [[ "$prefix" != / ]] || fail "Choose a prefix other than /."
    root="$prefix/share/git-trail"
    local command_path="$prefix/bin/git-trail"

    if [[ -e "$command_path" || -L "$command_path" ]]; then
        [[ -L "$command_path" ]] ||
            fail "Existing command is not managed by this installer: $command_path. Choose another --prefix."
        command_target="$(readlink "$command_path")"
        if [[ "$command_target" != "$root/current/git-trail" ]]; then
            [[ "$uninstall" == 0 ]] &&
                legacy_source="$(checkout_source "$command_path")" ||
                fail "Existing command is not managed by this installer: $command_path. Choose another --prefix."
        fi
    fi
    if [[ -e "$root" || -L "$root" ]]; then
        [[ -d "$root" && ! -L "$root" &&
            -f "$root/.git-trail-install" &&
            ! -L "$root/.git-trail-install" &&
            "$(cat "$root/.git-trail-install")" == "$owner" ]] ||
            fail "Existing directory is not managed by this installer: $root."
    else
        [[ "$uninstall" == 0 ]] || fail "No managed Git Trail installation at $prefix."
        require_tools
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
    if [[ -n "$command_target" ]]; then
        [[ -L "$command_path" && "$(readlink "$command_path")" == "$command_target" ]] ||
            fail "The command changed during installation: $command_path."
    else
        [[ ! -e "$command_path" && ! -L "$command_path" ]] ||
            fail "Another command appeared during installation: $command_path."
    fi

    if [[ "$uninstall" == 1 ]]; then
        rm -f -- "$command_path"
        rm -rf -- "$root"
        locked=0
        printf 'Removed Git Trail from %s. User settings were kept.\n' "$prefix"
        return
    fi

    launcher_text="$(installed_launcher)"$'\n'
    if same_source_is_healthy; then
        if ! launcher_is_current; then
            repair_launcher
            printf 'Updated Git Trail terminal support at %s\n' "$command_path"
            return
        fi
        printf 'Git Trail is already up to date at source %s: %s\n' "$immutable_ref" "$command_path"
        return
    fi
    require_tools
    mkdir -p -- "$root/releases" "$prefix/bin"
    bundle="$(mktemp -d "$root/releases/install.XXXXXX")"
    printf 'Downloading Git Trail (%s)...\n' "$ref"
    curl --fail --show-error --silent --location --retry 2 \
        "https://codeload.github.com/yellowHatpro/git-trail/tar.gz/$ref" \
        --output "$bundle/source.tar.gz" </dev/null
    tar -xzf "$bundle/source.tar.gz" --strip-components=1 -C "$bundle" </dev/null
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
            https://astral.sh/uv/install.sh --output "$bundle/.runtime/install-uv.sh" </dev/null
        UV_UNMANAGED_INSTALL="$bundle/.runtime/uv" UV_NO_MODIFY_PATH=1 \
            sh "$bundle/.runtime/install-uv.sh" </dev/null
        uv_path="$bundle/.runtime/uv/uv"
        [[ -x "$uv_path" ]] || fail "The private uv installation did not produce an executable."
    fi

    export UV_PYTHON_INSTALL_DIR="$bundle/.runtime/python"
    export UV_PYTHON_BIN_DIR="$bundle/.runtime/bin"
    export UV_PYTHON_INSTALL_BIN=0
    export UV_CACHE_DIR="$bundle/.runtime/cache"
    export UV_PYTHON_DOWNLOADS=automatic
    printf 'Installing private Python 3.13 and prebuilt dependencies...\n'
    "$uv_path" --no-config venv --python 3.13 --managed-python "$bundle/.venv" </dev/null
    "$uv_path" --no-config pip install --python "$bundle/.venv/bin/python" \
        --only-binary :all: --link-mode copy -r "$bundle/pyproject.toml" </dev/null
    printf '%s' "$launcher_text" > "$bundle/git-trail"
    version="$("$bundle/git-trail" --version </dev/null)"
    [[ "$version" == "git-trail "* && "$version" != *$'\n'* && "$version" != *$'\r'* ]] ||
        fail "The installed command failed its version check."

    # Activate symlinks with atomic replacements.
    "$bundle/.venv/bin/python" -I -c '
import os
import sys
import tempfile

root, bundle, command, command_target, legacy_source, current_target, source_owner, source_ref, version, owner = sys.argv[1:]
current = os.path.join(root, "current")
marker = os.path.join(root, ".git-trail-install")
if (
    not os.path.isdir(root) or os.path.islink(root)
    or not os.path.isfile(marker) or os.path.islink(marker)
    or open(marker, encoding="utf-8").read() != owner + "\n"
    or os.path.islink(os.path.join(root, "releases"))
):
    raise SystemExit("Git Trail: installation ownership changed.")

def check_current(expected):
    if os.path.lexists(current):
        if not os.path.islink(current) or os.readlink(current) != expected:
            raise SystemExit("Git Trail: current changed during installation.")
    elif expected:
        raise SystemExit("Git Trail: current disappeared during installation.")

def check_command():
    if os.path.lexists(command):
        if not os.path.islink(command) or os.readlink(command) != command_target:
            raise SystemExit("Git Trail: another command appeared during installation.")
        if legacy_source and os.path.realpath(command) != legacy_source:
            raise SystemExit("Git Trail: the checkout launcher changed during installation.")
    elif command_target:
        raise SystemExit("Git Trail: the command disappeared during installation.")

check_current(current_target)
check_command()
previous = current_target or None
if source_ref:
    with open(os.path.join(bundle, ".git-trail-source"), "x", encoding="utf-8", newline="\n") as receipt:
        receipt.write(source_owner + "\n" + source_ref + "\n" + version + "\n")

def replace_link(target, path, guard):
    fd, temporary = tempfile.mkstemp(prefix=".git-trail-", dir=os.path.dirname(path))
    os.close(fd)
    os.unlink(temporary)
    try:
        os.symlink(target, temporary)
        guard()
        os.replace(temporary, path)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)

replace_link(bundle, current, lambda: check_current(current_target))
try:
    if command_target != current + "/git-trail":
        replace_link(current + "/git-trail", command, check_command)
    else:
        check_command()
except BaseException:
    if os.path.islink(current) and os.readlink(current) == bundle:
        if previous is None:
            os.unlink(current)
        else:
            replace_link(previous, current, lambda: check_current(bundle))
    raise
' "$root" "$bundle" "$command_path" "$command_target" "$legacy_source" "$current_target" \
        "$source_owner" "$immutable_ref" "$version" "$owner" </dev/null
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
