from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest


PROJECT = Path(__file__).resolve().parents[1]
INSTALLER = PROJECT / "script.sh"
PINNED_REF = "949f95b6d8c20fc89c3e2984e4209555013d0966"
OWNER = "yellowHatpro/git-trail installer v1"

MOCK_TOOL = r'''
import json
import os
import shutil
import sys
import time
from pathlib import Path

tool = os.environ.get("MOCK_TOOL") or Path(sys.argv[0]).name
args = sys.argv[1:]
environment = {key: value for key, value in os.environ.items() if key.startswith("UV_")}
def record(name, values):
    with open(os.environ["MOCK_LOG"], "a", encoding="utf-8") as output:
        output.write(json.dumps({"tool": name, "args": values, "env": environment}) + "\n")
record(tool, args)
failure = os.environ.get("MOCK_FAIL", "")
if tool == "uname":
    print(os.environ.get("MOCK_SYSTEM", "Linux"))
elif tool == "git":
    raise SystemExit("The installer unexpectedly invoked Git.")
elif tool == "curl":
    url = next(value for value in args if value.startswith("https://"))
    destination = Path(args[args.index("--output") + 1])
    if url.startswith("https://codeload.github.com/"):
        if failure == "download":
            raise SystemExit(22)
        shutil.copyfile(os.environ["MOCK_ARCHIVE"], destination)
    elif url == "https://astral.sh/uv/install.sh":
        destination.write_text(
            'MOCK_TOOL=bootstrap "$MOCK_PYTHON" "$MOCK_DISPATCH"\n', encoding="utf-8")
    else:
        raise SystemExit("Unexpected download URL: " + url)
elif tool == "bootstrap":
    destination = Path(os.environ["UV_UNMANAGED_INSTALL"]) / "uv"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(os.environ["MOCK_UV_SOURCE"], destination)
    destination.chmod(0o755)
elif tool == "uv":
    if "venv" in args:
        if os.environ.get("MOCK_WAIT") == "venv":
            Path(os.environ["MOCK_STARTED"]).touch()
            deadline = time.monotonic() + 10
            while not Path(os.environ["MOCK_RELEASED"]).exists():
                if time.monotonic() >= deadline:
                    raise SystemExit("Test did not release the mocked runtime installer.")
                time.sleep(0.02)
        if failure == "venv":
            raise SystemExit(31)
        environment_path = Path(args[-1])
        python = Path(os.environ["UV_PYTHON_INSTALL_DIR"]) / "mock-python"
        python.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(os.environ["MOCK_RUNTIME"], python)
        python.chmod(0o755)
        (environment_path / "bin").mkdir(parents=True)
        (environment_path / "bin" / "python").symlink_to(python)
        for key in ("UV_PYTHON_BIN_DIR", "UV_CACHE_DIR"):
            Path(os.environ[key]).mkdir(parents=True, exist_ok=True)
    elif "pip" in args and "install" in args:
        if "--only-binary" in args and "--no-build" in args:
            print("error: --only-binary cannot be combined with --no-build", file=sys.stderr)
            raise SystemExit(2)
        if failure == "pip":
            raise SystemExit(32)
        manifest = Path(args[args.index("-r") + 1])
        assert manifest.is_file()
        if os.environ.get("MOCK_FOREIGN_PATH"):
            foreign = Path(os.environ["MOCK_FOREIGN_PATH"])
            if os.environ.get("MOCK_FOREIGN_SYMLINK"):
                foreign.symlink_to(os.environ["MOCK_FOREIGN_SYMLINK"], target_is_directory=True)
            else:
                foreign.write_bytes(b"Foreign content appeared during installation.\n")
    else:
        raise SystemExit("Unexpected uv invocation.")
else:
    raise SystemExit("Unexpected mock tool: " + tool)
'''

MOCK_RUNTIME = r'''
import json
import os
import subprocess
import sys
from pathlib import Path

args = sys.argv[1:]
phase = "version" if args[-1:] == ["--version"] else "activation"
with open(os.environ["MOCK_LOG"], "a", encoding="utf-8") as output:
    output.write(json.dumps({"tool": "runtime", "phase": phase, "args": args}) + "\n")
failure = os.environ.get("MOCK_FAIL", "")
if phase == "version" and failure == "version":
    print("Another Tool 1.0")
    raise SystemExit(0)
if phase == "activation" and failure in ("activation", "command-replace"):
    assert args[:2] == ["-I", "-c"]
    injection = """
import os
_fixture_replace = os.replace
def _installer_replace(source, destination):
    if os.environ.get("MOCK_FAIL") == "activation" or os.path.basename(destination) == "git-trail":
        raise PermissionError("Mock atomic activation failure")
    return _fixture_replace(source, destination)
os.replace = _installer_replace
"""
    args[2] = injection + args[2]
result = subprocess.run([os.environ["MOCK_PYTHON"], *args], check=False)
raise SystemExit(result.returncode)
'''

MOCK_CLI = '''
import json
import sys
from pathlib import Path

def main():
    if sys.argv[1:] == ["--version"]:
        print("git-trail 0.5.0")
    else:
        print(json.dumps({"args": sys.argv[1:], "module": str(Path(__file__).resolve())}))
    return 0
'''


class InstallerSandbox:
    def __init__(self, temporary):
        self.base = temporary
        self.home = temporary / "home with spaces"
        self.home.mkdir()
        self.tools = temporary / "mock tools"
        self.tools.mkdir()
        self.archive = temporary / "source.tar.gz"
        self.log = temporary / "calls.jsonl"
        self.outside = temporary / "unrelated runtime"
        self.outside.mkdir()
        (self.outside / "keep.txt").write_text("Foreign runtime stays intact.\n")
        self.dispatch = temporary / "dispatch"
        self.uv_source = temporary / "uv-template"
        self.runtime = temporary / "mock-runtime"
        for path, code in ((self.dispatch, MOCK_TOOL), (self.uv_source, MOCK_TOOL), (self.runtime, MOCK_RUNTIME)):
            path.write_text(f"#!{sys.executable}\n" + code, encoding="utf-8")
            path.chmod(0o755)
        for name in ("curl", "git", "uname", "uv"):
            shutil.copyfile(self.dispatch, self.tools / name)
            (self.tools / name).chmod(0o755)
        self.bash = "/bin/bash" if Path("/bin/bash").is_file() else shutil.which("bash")
        assert self.bash is not None
        for name in ("bash", "sh", "cat", "mkdir", "rmdir", "rm", "mktemp", "tar", "gzip",
                     "chmod", "dirname", "readlink"):
            executable = self.bash if name == "bash" else shutil.which(name)
            assert executable is not None, f"Fixture needs {name}"
            (self.tools / name).symlink_to(executable)
        self.settings = self.home / ".config" / "git-trail" / "state.json"
        self.settings.parent.mkdir(parents=True)
        self.settings.write_bytes(b'{"version":1,"bookmarks":{"fixture":"keep"}}\n')
        for name in (".bashrc", ".zshrc", ".profile"):
            (self.home / name).write_text(f"# Existing {name}\nexport KEEP_USER_SETTING=1\n")
        self.user_files = {path: path.read_bytes() for path in [
            self.settings, *(self.home / name for name in (".bashrc", ".zshrc", ".profile")),
            self.outside / "keep.txt",
        ]}
        self.env = os.environ.copy()
        for key in tuple(self.env):
            if key.startswith("UV_"):
                self.env.pop(key)
        self.env.update({
            "HOME": str(self.home), "PATH": str(self.tools), "MOCK_ARCHIVE": str(self.archive),
            "MOCK_LOG": str(self.log), "MOCK_PYTHON": sys.executable,
            "MOCK_DISPATCH": str(self.dispatch), "MOCK_UV_SOURCE": str(self.uv_source),
            "MOCK_RUNTIME": str(self.runtime), "UV_PYTHON_INSTALL_DIR": str(self.outside),
            "UV_PYTHON_BIN_DIR": str(self.outside), "UV_CACHE_DIR": str(self.outside),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
        })
        with tarfile.open(self.archive, "w:gz") as archive:
            files = {
                "git-trail": (PROJECT / "git-trail").read_bytes(),
                "pyproject.toml": (PROJECT / "pyproject.toml").read_bytes(),
                "git_trail/__init__.py": b"",
                "git_trail/cli.py": MOCK_CLI.encode(),
            }
            for path, data in files.items():
                member = tarfile.TarInfo("git-trail-fixture/" + path)
                member.size = len(data)
                member.mode = 0o755 if path == "git-trail" else 0o644
                archive.addfile(member, io.BytesIO(data))

    @property
    def prefix(self):
        return self.home / ".local"

    def root(self, prefix=None):
        return (prefix or self.prefix) / "share" / "git-trail"

    def command(self, prefix=None):
        return (prefix or self.prefix) / "bin" / "git-trail"

    def run(self, *arguments, stdin=False, **environment):
        command = [self.bash, "-s", "--", *arguments] if stdin else [self.bash, str(INSTALLER), *arguments]
        return subprocess.run(
            command, input=INSTALLER.read_text() if stdin else None, text=True,
            cwd=self.base, env={**self.env, **environment}, capture_output=True, timeout=20,
        )

    def invoke(self, *arguments, prefix=None):
        return subprocess.run(
            [str(self.command(prefix)), *arguments], text=True, cwd=self.outside,
            env=self.env, capture_output=True, timeout=10,
        )

    def events(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def assert_user_files(self):
        assert {path: path.read_bytes() for path in self.user_files} == self.user_files
        assert list(self.outside.iterdir()) == [self.outside / "keep.txt"]


@pytest.fixture
def installer(tmp_path):
    return InstallerSandbox(tmp_path)


def assert_installed(installer, prefix=None):
    root, command = installer.root(prefix), installer.command(prefix)
    assert root.is_dir() and not root.is_symlink()
    marker = root / ".git-trail-install"
    assert marker.is_file() and not marker.is_symlink()
    assert (root / "current").is_symlink() and command.is_symlink()
    bundle = (root / "current").resolve(strict=True)
    assert bundle.parent == root / "releases"
    assert bundle.name.startswith("install.")
    assert os.readlink(command) == str(root / "current" / "git-trail")
    assert command.resolve(strict=True) == bundle / "git-trail"
    assert os.access(command, os.X_OK)
    assert (bundle / "git_trail" / "cli.py").is_file()
    assert (bundle / ".venv" / "bin" / "python").resolve(strict=True).is_relative_to(bundle / ".runtime")
    result = installer.invoke("--version", prefix=prefix)
    assert result.returncode == 0 and result.stdout == "git-trail 0.5.0\n", result.stderr
    assert not (root / ".install-lock").exists()
    assert not [path for path in root.glob(".git-trail-*") if path != marker]
    assert not list(command.parent.glob(".git-trail-*"))
    installer.assert_user_files()
    return bundle


def assert_runtime_contract(installer, bundle):
    calls = [item for item in installer.events() if item["tool"] == "uv"]
    venv = next(item for item in calls if "venv" in item["args"] and str(bundle / ".venv") in item["args"])
    pip = next(item for item in calls if "pip" in item["args"] and str(bundle / "pyproject.toml") in item["args"])
    assert venv["args"][:2] == ["--no-config", "venv"]
    assert venv["args"][venv["args"].index("--python") + 1] == "3.13"
    assert "--managed-python" in venv["args"]
    assert pip["args"][:3] == ["--no-config", "pip", "install"]
    for option, value in (("--python", str(bundle / ".venv" / "bin" / "python")),
                          ("--only-binary", ":all:"), ("--link-mode", "copy"),
                          ("-r", str(bundle / "pyproject.toml"))):
        assert pip["args"][pip["args"].index(option) + 1] == value
    assert "--no-build" not in pip["args"]
    assert "." not in pip["args"] and str(bundle) not in pip["args"]
    for item in (venv, pip):
        for key in ("UV_PYTHON_INSTALL_DIR", "UV_PYTHON_BIN_DIR", "UV_CACHE_DIR"):
            assert Path(item["env"][key]).is_relative_to(bundle / ".runtime")
        assert item["env"]["UV_PYTHON_INSTALL_BIN"] == "0"
        assert item["env"]["UV_PYTHON_DOWNLOADS"] == "automatic"
    runtime = [item for item in installer.events() if item["tool"] == "runtime"
               and str(bundle) in item["args"]]
    assert [item["phase"] for item in runtime[:2]] == ["version", "activation"]
    assert all(item["args"][:2] == ["-I", "-c"] for item in runtime)


def test_installer_help_invalid_options_platform_and_missing_tools_are_read_only(installer):
    help_result = installer.run("--help")
    assert help_result.returncode == 0
    assert "--prefix" in help_result.stdout and "--uninstall" in help_result.stdout
    assert not installer.prefix.exists() and not installer.events()
    for args in (
        ("--prefix",), ("--ref", ""), ("--unknown",),
        ("--ref", "../main"), ("--ref", "main;echo injected"),
        ("--ref", "main", "--uninstall"),
    ):
        result = installer.run(*args)
        assert result.returncode != 0 and "Git Trail:" in result.stderr
        assert not installer.prefix.exists() and not installer.events()
    unsupported = installer.run(MOCK_SYSTEM="OtherOS")
    assert unsupported.returncode != 0 and "macOS and Linux" in unsupported.stderr
    assert not installer.prefix.exists()
    for tool in ("git", "curl", "tar"):
        original = installer.tools / tool
        renamed = installer.tools / (tool + "-hidden")
        original.rename(renamed)
        try:
            result = installer.run()
            assert result.returncode != 0 and f"Install {tool} first" in result.stderr
            assert not installer.prefix.exists()
        finally:
            renamed.rename(original)
    assert not [item for item in installer.events() if item["tool"] in ("curl", "uv")]
    installer.assert_user_files()


def test_default_install_and_update_keep_permanent_bundles_and_run_from_another_directory(installer):
    result = installer.run(MOCK_SYSTEM="Darwin")
    assert result.returncode == 0, result.stderr
    assert str(installer.command()) in result.stdout and "export PATH=" in result.stdout
    first = assert_installed(installer)
    assert_runtime_contract(installer, first)
    downloads = [item for item in installer.events() if item["tool"] == "curl"]
    assert len(downloads) == 1
    assert f"https://codeload.github.com/yellowHatpro/git-trail/tar.gz/{PINNED_REF}" in downloads[0]["args"]
    assert not [item for item in installer.events() if item["tool"] == "bootstrap"]
    invoked = installer.invoke("repository with spaces", "--lines", "2:3", "--json")
    assert invoked.returncode == 0, invoked.stderr
    data = json.loads(invoked.stdout)
    assert data["args"] == ["repository with spaces", "--lines", "2:3", "--json"]
    assert Path(data["module"]) == first / "git_trail" / "cli.py"
    update = installer.run("--ref", "release/v0.5.0")
    assert update.returncode == 0, update.stderr
    second = assert_installed(installer)
    assert second != first and first.is_dir()
    assert set((installer.root() / "releases").iterdir()) == {first, second}
    assert_runtime_contract(installer, second)
    assert any("https://codeload.github.com/yellowHatpro/git-trail/tar.gz/release/v0.5.0" in item["args"]
               for item in installer.events() if item["tool"] == "curl")


def test_complete_stdin_script_bootstraps_uv_privately_and_handles_prefix_spaces(installer):
    (installer.tools / "uv").unlink()
    prefix = installer.base / "custom prefix with spaces"
    result = installer.run("--prefix", str(prefix), "--ref", "main", stdin=True)
    assert result.returncode == 0, result.stderr
    bundle = assert_installed(installer, prefix)
    assert_runtime_contract(installer, bundle)
    bootstrap = [item for item in installer.events() if item["tool"] == "bootstrap"]
    assert len(bootstrap) == 1
    assert bootstrap[0]["env"]["UV_NO_MODIFY_PATH"] == "1"
    assert bootstrap[0]["env"]["UV_UNMANAGED_INSTALL"] == str(bundle / ".runtime" / "uv")
    assert (bundle / ".runtime" / "uv" / "uv").is_file()
    assert any("https://astral.sh/uv/install.sh" in item["args"]
               for item in installer.events() if item["tool"] == "curl")
    assert not installer.prefix.exists()


@pytest.mark.parametrize("failure", ["download", "venv", "pip", "version", "activation"])
def test_failed_update_preserves_the_previous_working_command_and_removes_failed_bundle(installer, failure):
    initial = installer.run()
    assert initial.returncode == 0, initial.stderr
    previous = assert_installed(installer)
    command_target = os.readlink(installer.command())
    current_target = os.readlink(installer.root() / "current")
    source_files = {path.relative_to(previous): path.read_bytes() for path in previous.rglob("*")
                    if path.is_file() and not path.is_symlink()}
    failed = installer.run("--ref", "next-release", MOCK_FAIL=failure)
    assert failed.returncode != 0
    assert os.readlink(installer.command()) == command_target
    assert os.readlink(installer.root() / "current") == current_target
    assert assert_installed(installer) == previous
    assert list((installer.root() / "releases").iterdir()) == [previous]
    assert {path.relative_to(previous): path.read_bytes() for path in previous.rglob("*")
            if path.is_file() and not path.is_symlink()} == source_files


@pytest.mark.parametrize(
    "foreign",
    ["command-file", "command-symlink", "root-directory", "root-symlink",
     "marker-symlink", "releases-symlink", "current-file", "current-symlink"],
)
def test_foreign_paths_are_preserved_by_install_and_uninstall(installer, foreign):
    prefix, root, command = installer.prefix, installer.root(), installer.command()
    (prefix / "bin").mkdir(parents=True)
    (prefix / "share").mkdir()
    protected = installer.base / "foreign-data"
    protected.mkdir()
    (protected / "keep.txt").write_bytes(b"foreign contents\n")
    if foreign == "command-file":
        command.write_bytes(b"foreign executable\n")
        tracked = command
    elif foreign == "command-symlink":
        command.symlink_to(protected / "missing-command")
        tracked = command
    elif foreign == "root-directory":
        root.mkdir()
        (root / "keep.txt").write_bytes(b"unmanaged install\n")
        tracked = root / "keep.txt"
    elif foreign == "root-symlink":
        root.symlink_to(protected, target_is_directory=True)
        tracked = root
    else:
        root.mkdir()
        marker = root / ".git-trail-install"
        if foreign == "marker-symlink":
            owner_file = protected / "owner"
            owner_file.write_text(OWNER + "\n")
            marker.symlink_to(owner_file)
            tracked = marker
        else:
            marker.write_text(OWNER + "\n")
            if foreign == "releases-symlink":
                tracked = root / "releases"
                tracked.symlink_to(protected, target_is_directory=True)
            elif foreign == "current-file":
                tracked = root / "current"
                tracked.write_bytes(b"foreign current file\n")
            else:
                tracked = root / "current"
                tracked.symlink_to(protected, target_is_directory=True)
    original = os.readlink(tracked) if tracked.is_symlink() else tracked.read_bytes()
    protected_files = {path.name: path.read_bytes() for path in protected.iterdir()}
    for arguments in ((), ("--uninstall",)):
        result = installer.run(*arguments)
        assert result.returncode != 0
        assert "managed" in result.stderr
        assert os.path.lexists(tracked)
        assert (os.readlink(tracked) if tracked.is_symlink() else tracked.read_bytes()) == original
        assert {path.name: path.read_bytes() for path in protected.iterdir()} == protected_files
        assert not [item for item in installer.events() if item["tool"] in ("curl", "uv")]
        assert not (root / ".install-lock").exists()
        installer.assert_user_files()


def test_concurrent_update_is_refused_without_removing_the_active_installers_lock(installer):
    initial = installer.run()
    assert initial.returncode == 0, initial.stderr
    previous = assert_installed(installer)
    started, released = installer.base / "started", installer.base / "released"
    process = subprocess.Popen(
        [installer.bash, str(INSTALLER), "--ref", "first-update"],
        cwd=installer.base, env={**installer.env, "MOCK_WAIT": "venv",
                                 "MOCK_STARTED": str(started), "MOCK_RELEASED": str(released)},
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 5
        while not started.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert started.exists(), "First installer did not reach the controlled runtime step."
        lock = installer.root() / ".install-lock"
        assert lock.is_dir()
        second = installer.run("--ref", "second-update")
        assert second.returncode != 0 and "locked" in second.stderr
        assert lock.is_dir()
        assert (installer.root() / "current").resolve(strict=True) == previous
        working = installer.invoke("--version")
        assert working.returncode == 0 and working.stdout == "git-trail 0.5.0\n"
    finally:
        released.touch()
        stdout, stderr = process.communicate(timeout=20)
    assert process.returncode == 0, (stdout, stderr)
    current = assert_installed(installer)
    assert current != previous and previous.is_dir()
    assert not any("second-update" in argument for item in installer.events()
                   if item["tool"] == "curl" for argument in item["args"])


def test_uninstall_removes_only_managed_install_and_keeps_preferences_profiles_and_other_commands(installer):
    result = installer.run()
    assert result.returncode == 0, result.stderr
    assert_installed(installer)
    unrelated = installer.prefix / "bin" / "other-tool"
    unrelated.write_bytes(b"another command\n")
    (installer.tools / "uv").unlink()
    (installer.tools / "curl").unlink()
    (installer.tools / "tar").unlink()
    before = len(installer.events())
    removed = installer.run("--uninstall")
    assert removed.returncode == 0, removed.stderr
    assert "User settings were kept" in removed.stdout
    assert not os.path.lexists(installer.command()) and not installer.root().exists()
    assert unrelated.read_bytes() == b"another command\n"
    assert not [item for item in installer.events()[before:] if item["tool"] in ("curl", "uv", "git")]
    installer.assert_user_files()
    again = installer.run("--uninstall")
    assert again.returncode != 0
    assert unrelated.read_bytes() == b"another command\n"
    installer.assert_user_files()


def test_first_install_rolls_back_a_current_switch_when_command_activation_fails(installer):
    result = installer.run(MOCK_FAIL="command-replace")
    assert result.returncode != 0 and "Mock atomic activation failure" in result.stderr
    assert not os.path.lexists(installer.command())
    assert not installer.root().exists()
    assert not list((installer.prefix / "bin").glob(".git-trail-*"))
    installer.assert_user_files()
    retry = installer.run()
    assert retry.returncode == 0, retry.stderr
    assert_installed(installer)


@pytest.mark.parametrize("foreign", ["current-file", "current-symlink", "command-file"])
def test_foreign_paths_appearing_during_dependency_installation_are_preserved(installer, foreign):
    path = installer.command() if foreign == "command-file" else installer.root() / "current"
    environment = {"MOCK_FOREIGN_PATH": str(path)}
    if foreign == "current-symlink":
        environment["MOCK_FOREIGN_SYMLINK"] = str(installer.outside)
    result = installer.run(**environment)
    assert result.returncode != 0
    assert os.path.lexists(path)
    if foreign == "current-symlink":
        assert path.is_symlink() and os.readlink(path) == str(installer.outside)
    else:
        assert not path.is_symlink()
        assert path.read_bytes() == b"Foreign content appeared during installation.\n"
    assert not list((installer.root() / "releases").glob("install.*"))
    assert not (installer.root() / ".install-lock").exists()
    if foreign != "command-file":
        assert not os.path.lexists(installer.command())
    installer.assert_user_files()
