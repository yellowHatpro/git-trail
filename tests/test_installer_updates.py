from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tarfile
from pathlib import Path

import pytest

from test_installer import (
    InstallerSandbox, PINNED_REF, PROJECT, assert_installed, assert_runtime_contract,
)


SOURCE_OWNER = "yellowHatpro/git-trail source v1"
OTHER_SHA = "a" * 40


@pytest.fixture
def updating_installer(tmp_path):
    installer = InstallerSandbox(tmp_path)
    with tarfile.open(installer.archive, "r:gz") as source:
        files = [(member, source.extractfile(member).read()) for member in source.getmembers()]
    with tarfile.open(installer.archive, "w:gz") as target:
        for member, data in files:
            target.addfile(member, io.BytesIO(data))
        for name in ("app", "selectors"):
            data = b"IMPORTED_WITHOUT_STARTING_APPLICATION = True\n"
            member = tarfile.TarInfo(f"git-trail-fixture/git_trail/{name}.py")
            member.size = len(data)
            member.mode = 0o644
            target.addfile(member, io.BytesIO(data))
    return installer


def fingerprint(root):
    result = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        metadata = path.lstat()
        identity = (metadata.st_mode, metadata.st_ino, metadata.st_mtime_ns)
        if path.is_symlink():
            result[str(relative)] = identity, os.readlink(path)
        elif path.is_file():
            result[str(relative)] = identity, path.read_bytes()
        else:
            result[str(relative)] = (metadata.st_mode,), None
    return result


def link_identity(path):
    metadata = path.lstat()
    assert path.is_symlink()
    return metadata.st_ino, metadata.st_mtime_ns, os.readlink(path)


def file_identity(path):
    metadata = path.stat()
    return metadata.st_mode, metadata.st_ino, metadata.st_mtime_ns, path.read_bytes()


def expected_receipt(ref=PINNED_REF):
    return f"{SOURCE_OWNER}\n{ref}\ngit-trail 0.5.0\n".encode()


def assert_receipt(bundle, ref=PINNED_REF):
    receipt = bundle / ".git-trail-source"
    assert receipt.is_file() and not receipt.is_symlink()
    assert receipt.read_bytes() == expected_receipt(ref)


def install(installer, *arguments, **environment):
    result = installer.run(*arguments, stdin=True, **environment)
    assert result.returncode == 0, (result.stdout, result.stderr)
    bundle = assert_installed(installer)
    assert_runtime_contract(installer, bundle)
    return bundle


def assert_no_install_events(events):
    assert not [item for item in events if item["tool"] in ("curl", "uv", "bootstrap", "git", "checkout-git")]
    assert not [item for item in events if item["tool"] == "runtime" and item["phase"] == "activation"]


def checkout_launcher(installer, repo, *, relative=False, origin="https://github.com/yellowHatpro/git-trail.git"):
    repo.write("git-trail", (PROJECT / "git-trail").read_bytes()).chmod(0o755)
    repo.write("pyproject.toml", (PROJECT / "pyproject.toml").read_bytes())
    repo.write("git_trail/__init__.py", "")
    repo.write("git_trail/cli.py", "def main():\n    raise RuntimeError('Checkout must remain unexecuted')\n")
    repo.write(".venv/bin/python", "#!/bin/sh\nprintf invoked > \"$MOCK_CHECKOUT_EXECUTED\"\nexit 71\n").chmod(0o755)
    repo.write("README.md", "Original checkout documentation.\n")
    repo.commit("Original checkout")
    repo.git("config", "--local", "remote.origin.url", origin)
    repo.write("local untracked notes.txt", "Keep local checkout work.\n")
    repo.write("README.md", "Uncommitted checkout documentation.\n")
    installer.command().parent.mkdir(parents=True)
    target = repo.root / "git-trail"
    installer.command().symlink_to(os.path.relpath(target, installer.command().parent) if relative else target)
    installer.env["MOCK_CHECKOUT_EXECUTED"] = str(installer.base / "checkout-executed")
    real_git = shutil.which("git")
    assert real_git is not None
    wrapper = f"#!{sys.executable}\n" + r'''
import json
import os
import subprocess
import sys

args = sys.argv[1:]
git_env = {key: value for key, value in os.environ.items() if key.startswith("GIT_")}
with open(os.environ["MOCK_LOG"], "a", encoding="utf-8") as output:
    output.write(json.dumps({"tool": "checkout-git", "args": args, "env": git_env}) + "\n")
assert git_env == {
    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_OPTIONAL_LOCKS": "0",
}, git_env
assert args == ["-C", os.environ["MOCK_CHECKOUT"], "rev-parse", "--show-toplevel"] or args == [
    "-C", os.environ["MOCK_CHECKOUT"], "config", "--local", "--get", "remote.origin.url",
], args
result = subprocess.run([os.environ["MOCK_REAL_GIT"], *args], check=False)
raise SystemExit(result.returncode)
'''
    (installer.tools / "git").write_text(wrapper, encoding="utf-8")
    (installer.tools / "git").chmod(0o755)
    installer.env.update(MOCK_CHECKOUT=str(repo.root), MOCK_REAL_GIT=real_git)
    return repo.root


def race_on_dependency_install(installer, path, target):
    wrapper = f"#!{sys.executable}\n" + r'''
import json
import os
import subprocess
import sys
from pathlib import Path

args = sys.argv[1:]
result = subprocess.run(
    [os.environ["MOCK_PYTHON"], os.environ["MOCK_UV_SOURCE"], *args],
    env=dict(os.environ, MOCK_TOOL="uv"), check=False,
)
if result.returncode == 0 and "pip" in args:
    path = Path(os.environ["UPDATE_RACE_PATH"])
    path.unlink()
    path.symlink_to(os.environ["UPDATE_RACE_TARGET"])
    with open(os.environ["MOCK_LOG"], "a", encoding="utf-8") as output:
        output.write(json.dumps({"tool": "fixture-race", "path": str(path)}) + "\n")
raise SystemExit(result.returncode)
'''
    (installer.tools / "uv").write_text(wrapper, encoding="utf-8")
    (installer.tools / "uv").chmod(0o755)
    installer.env.update(UPDATE_RACE_PATH=str(path), UPDATE_RACE_TARGET=str(target))


def test_exact_default_stdin_repeat_is_a_healthy_noop_with_stable_links_and_private_bundle(updating_installer):
    installer = updating_installer
    bundle = install(installer)
    assert_receipt(bundle)
    command_before = link_identity(installer.command())
    current_before = link_identity(installer.root() / "current")
    bundle_before = fingerprint(bundle)
    releases_before = set((installer.root() / "releases").iterdir())
    user_before = {path: file_identity(path) for path in installer.user_files}
    for arguments in ((), (), ("--ref", PINNED_REF.upper())):
        event_count = len(installer.events())
        result = installer.run(*arguments, stdin=True)
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert "already up to date" in result.stdout.casefold()
        assert PINNED_REF in result.stdout
        assert_no_install_events(installer.events()[event_count:])
        assert link_identity(installer.command()) == command_before
        assert link_identity(installer.root() / "current") == current_before
        assert set((installer.root() / "releases").iterdir()) == releases_before
        assert fingerprint(bundle) == bundle_before
        assert {path: file_identity(path) for path in installer.user_files} == user_before
        assert not (installer.root() / ".install-lock").exists()
        installer.assert_user_files()


def test_new_immutable_source_updates_even_when_the_semantic_version_is_identical(updating_installer):
    installer = updating_installer
    previous = install(installer)
    previous_bytes = fingerprint(previous)
    event_count = len(installer.events())
    current = install(installer, "--ref", OTHER_SHA)
    assert current != previous
    assert_receipt(previous)
    assert_receipt(current, OTHER_SHA)
    assert fingerprint(previous) == previous_bytes
    assert set((installer.root() / "releases").iterdir()) == {previous, current}
    assert any(f"https://codeload.github.com/yellowHatpro/git-trail/tar.gz/{OTHER_SHA}" in item["args"]
               for item in installer.events()[event_count:] if item["tool"] == "curl")
    event_count = len(installer.events())
    result = installer.run("--ref", OTHER_SHA, stdin=True)
    assert result.returncode == 0 and "already up to date" in result.stdout.casefold()
    assert_no_install_events(installer.events()[event_count:])
    assert (installer.root() / "current").resolve(strict=True) == current


@pytest.mark.parametrize("ref", ["main", "release/v0.6.0"])
def test_same_mutable_branch_or_tag_refreshes_instead_of_trusting_version(updating_installer, ref):
    installer = updating_installer
    previous = install(installer, "--ref", ref)
    assert not os.path.lexists(previous / ".git-trail-source")
    event_count = len(installer.events())
    current = install(installer, "--ref", ref)
    assert current != previous and previous.is_dir()
    assert not os.path.lexists(current / ".git-trail-source")
    assert any(f"https://codeload.github.com/yellowHatpro/git-trail/tar.gz/{ref}" in item["args"]
               for item in installer.events()[event_count:] if item["tool"] == "curl")
    assert set((installer.root() / "releases").iterdir()) == {previous, current}


@pytest.mark.parametrize("fault", ["missing", "wrong-owner", "wrong-source", "extra-content", "symlink"])
def test_untrusted_or_legacy_receipts_refresh_without_modifying_previous_source(updating_installer, fault):
    installer = updating_installer
    previous = install(installer)
    receipt = previous / ".git-trail-source"
    outside = installer.base / "foreign receipt"
    outside.write_bytes(expected_receipt())
    outside_before = file_identity(outside)
    if fault == "missing":
        receipt.unlink()
    elif fault == "wrong-owner":
        receipt.write_bytes(expected_receipt().replace(SOURCE_OWNER.encode(), b"Other project source v1"))
    elif fault == "wrong-source":
        receipt.write_bytes(expected_receipt(OTHER_SHA))
    elif fault == "extra-content":
        receipt.write_bytes(expected_receipt() + b"unexpected extra field\n")
    else:
        receipt.unlink()
        receipt.symlink_to(outside)
    previous_before = fingerprint(previous)
    event_count = len(installer.events())
    current = install(installer)
    assert current != previous
    assert_receipt(current)
    assert fingerprint(previous) == previous_before
    assert file_identity(outside) == outside_before
    assert any(item["tool"] == "curl" for item in installer.events()[event_count:])
    assert set((installer.root() / "releases").iterdir()) == {previous, current}


@pytest.mark.parametrize("fault", ["missing-command", "app-import", "selectors-import", "launcher-version"])
def test_unhealthy_matching_source_is_repaired_with_a_new_validated_bundle(updating_installer, fault):
    installer = updating_installer
    previous = install(installer)
    if fault == "missing-command":
        installer.command().unlink()
    elif fault.endswith("-import"):
        module = fault.removesuffix("-import")
        (previous / "git_trail" / f"{module}.py").write_text("raise ImportError('Broken installed module')\n")
    else:
        (previous / "git-trail").write_text("#!/bin/sh\nprintf 'another-command 9.9\\n'\n")
    previous_before = fingerprint(previous)
    current = install(installer)
    assert current != previous
    assert_receipt(current)
    assert fingerprint(previous) == previous_before
    assert set((installer.root() / "releases").iterdir()) == {previous, current}


@pytest.mark.parametrize("relative", [False, True], ids=["absolute-link", "relative-link"])
def test_official_checkout_migration_validates_new_install_and_preserves_checkout_and_git_environment(
    updating_installer, repo, relative,
):
    installer = updating_installer
    checkout = checkout_launcher(installer, repo, relative=relative)
    before = fingerprint(checkout)
    original_target = os.readlink(installer.command())
    installer.env.update({
        "GIT_DIR": str(installer.outside / "foreign.git"),
        "GIT_WORK_TREE": str(installer.outside),
        "GIT_INDEX_FILE": str(installer.outside / "foreign-index"),
        "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "remote.origin.url",
        "GIT_CONFIG_VALUE_0": "https://github.com/foreign/project.git",
        "GIT_CONFIG_GLOBAL": str(installer.outside / "foreign-config"),
        "GIT_OPTIONAL_LOCKS": "1",
    })
    current = install(installer)
    assert_receipt(current)
    assert os.readlink(installer.command()) != original_target
    assert installer.command().resolve(strict=True) == current / "git-trail"
    assert fingerprint(checkout) == before
    assert not Path(installer.env["MOCK_CHECKOUT_EXECUTED"]).exists()
    git_events = [item for item in installer.events() if item["tool"] == "checkout-git"]
    assert len(git_events) == 2
    assert git_events[0]["args"][-2:] == ["rev-parse", "--show-toplevel"]
    assert git_events[1]["args"][-4:] == ["config", "--local", "--get", "remote.origin.url"]
    assert all(item["env"] == {
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_OPTIONAL_LOCKS": "0",
    } for item in git_events)


@pytest.mark.parametrize("failure", ["version", "command-replace"])
def test_failed_checkout_migration_keeps_original_link_and_checkout(updating_installer, repo, failure):
    installer = updating_installer
    checkout = checkout_launcher(installer, repo, relative=True)
    before = fingerprint(checkout)
    command_before = link_identity(installer.command())
    result = installer.run(stdin=True, MOCK_FAIL=failure)
    assert result.returncode != 0
    assert link_identity(installer.command()) == command_before
    assert fingerprint(checkout) == before
    assert installer.command().resolve(strict=True) == checkout / "git-trail"
    assert not installer.root().exists()
    assert not list(installer.command().parent.glob(".git-trail-*"))
    assert not Path(installer.env["MOCK_CHECKOUT_EXECUTED"]).exists()
    installer.assert_user_files()


@pytest.mark.parametrize("fault", ["wrong-origin", "missing-project-marker"])
def test_unrelated_checkout_is_not_migrated_or_modified(updating_installer, repo, fault):
    installer = updating_installer
    checkout = checkout_launcher(
        installer, repo,
        origin="https://github.com/foreign/git-trail.git" if fault == "wrong-origin"
        else "https://github.com/yellowHatpro/git-trail.git",
    )
    if fault == "missing-project-marker":
        (checkout / "pyproject.toml").write_text("[project]\nname = 'another-tool'\n")
    before = fingerprint(checkout)
    command_before = link_identity(installer.command())
    result = installer.run(stdin=True)
    assert result.returncode != 0 and "not managed" in result.stderr
    assert link_identity(installer.command()) == command_before
    assert fingerprint(checkout) == before
    assert not installer.root().exists()
    assert not [item for item in installer.events() if item["tool"] in ("curl", "uv", "bootstrap", "runtime")]
    assert not Path(installer.env["MOCK_CHECKOUT_EXECUTED"]).exists()
    installer.assert_user_files()


def test_uninstall_refuses_a_checkout_launcher_instead_of_migrating_or_deleting_it(updating_installer, repo):
    installer = updating_installer
    checkout = checkout_launcher(installer, repo)
    before = fingerprint(checkout)
    command_before = link_identity(installer.command())
    result = installer.run("--uninstall", stdin=True)
    assert result.returncode != 0 and "not managed" in result.stderr
    assert link_identity(installer.command()) == command_before
    assert fingerprint(checkout) == before
    assert not installer.root().exists()
    assert_no_install_events(installer.events())
    installer.assert_user_files()


@pytest.mark.parametrize("race", ["command", "current", "checkout-alias"])
def test_targets_changed_during_dependency_installation_are_preserved(updating_installer, repo, race):
    installer = updating_installer
    replacement = installer.base / "replacement launcher"
    replacement.write_text("#!/bin/sh\nprintf 'foreign command\\n'\n")
    replacement.chmod(0o755)
    replacement_before = replacement.read_bytes()
    checkout_before = None
    if race == "checkout-alias":
        checkout = checkout_launcher(installer, repo)
        alias = installer.base / "checkout alias with spaces"
        alias.symlink_to(checkout / "git-trail")
        installer.command().unlink()
        installer.command().symlink_to(alias)
        original_target = os.readlink(installer.command())
        checkout_before = fingerprint(checkout)
        changing = alias
        target = replacement
        previous = None
        current_before = None
    else:
        previous = install(installer)
        previous_before = fingerprint(previous)
        current_before = link_identity(installer.root() / "current")
        if race == "current":
            target = installer.root() / "releases" / "install.other-owner"
            target.mkdir()
            (target / "keep").write_bytes(b"Replacement release stays untouched.\n")
            changing = installer.root() / "current"
        else:
            changing = installer.command()
            target = replacement
    race_on_dependency_install(installer, changing, target)
    result = installer.run("--ref", OTHER_SHA, stdin=True)
    assert result.returncode != 0
    assert changing.is_symlink() and os.readlink(changing) == str(target)
    assert replacement.read_bytes() == replacement_before
    assert not (installer.root() / ".install-lock").exists()
    assert not list(installer.command().parent.glob(".git-trail-*"))
    if race == "checkout-alias":
        assert os.readlink(installer.command()) == original_target
        assert installer.command().resolve(strict=True) == replacement
        assert fingerprint(checkout) == checkout_before
        assert not installer.root().exists()
        assert not Path(installer.env["MOCK_CHECKOUT_EXECUTED"]).exists()
    else:
        assert fingerprint(previous) == previous_before
        if race == "current":
            assert (target / "keep").read_bytes() == b"Replacement release stays untouched.\n"
            assert set((installer.root() / "releases").iterdir()) == {previous, target}
        else:
            assert link_identity(installer.root() / "current") == current_before
            assert set((installer.root() / "releases").iterdir()) == {previous}
    installer.assert_user_files()
