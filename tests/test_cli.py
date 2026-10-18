from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pytest

from git_trail.cli import line_range, main


def test_json_cli_reports_the_relevant_timeline_without_starting_tui(range_history, capsys, monkeypatch):
    import git_trail.app

    def forbid_tui(*args, **kwargs):
        pytest.fail("JSON command started the TUI")

    monkeypatch.setattr(git_trail.app.TrailApp, "run", forbid_tui)
    result = main([
        str(range_history.repo.root), range_history.path,
        "--lines", "4:7", "--json",
    ])
    captured = capsys.readouterr()
    assert result == 0
    assert captured.err == ""
    data = json.loads(captured.out)
    assert data["starting_commit"] == range_history.delete
    assert data["selection"]["start"] == 4
    assert data["selection"]["end"] == 7
    assert [entry["commit"] for entry in data["entries"]] == range_history.relevant
    assert "-    b = 2" in data["entries"][0]["patch"]
    assert data["warnings"]


@pytest.mark.parametrize("selector", [
    ["--lines", "4:7"],
    ["--paragraph", "5"],
    ["--symbol", "target"],
    ["--text", "def target():\n    a = 7\n    c = 3\n    return a + b"],
])
def test_cli_selectors_produce_the_same_range_history(range_history, capsys, selector):
    assert main([str(range_history.repo.root), range_history.path, *selector, "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert (data["selection"]["start"], data["selection"]["end"]) == (4, 7)
    assert [entry["commit"] for entry in data["entries"]] == range_history.relevant


def test_cli_text_occurrence_and_unicode_json(repo, capsys):
    repo.write("unicodé.txt", "repeat\nseparator\nrepeat 中文\n")
    commit = repo.commit("Unicode 🐍")
    assert main([
        str(repo.root), "unicodé.txt", "--text", "repeat",
        "--occurrence", "2", "--json",
    ]) == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["path"] == "unicodé.txt"
    assert data["selection"]["start"] == data["selection"]["end"] == 3
    assert data["entries"][0]["commit"] == commit
    assert data["entries"][0]["subject"] == "Unicode 🐍"
    assert "中文" in captured.out


def test_cli_symbols_json_lists_class_and_method_without_history(repo, capsys):
    repo.write("module.py", "class Store:\n    def read(self):\n        return 1\n")
    repo.commit("Symbols")
    assert main([str(repo.root), "module.py", "--symbols", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data == [
        {"name": "Store", "kind": "class", "start": 1, "end": 3},
        {"name": "read", "kind": "function", "start": 2, "end": 3},
    ]


def test_cli_symbols_plain_text_is_actionable(repo, capsys):
    repo.write("module.py", "def run():\n    return 1\n")
    repo.commit("Symbol")
    assert main([str(repo.root), "module.py", "--symbols"]) == 0
    assert capsys.readouterr().out == "1:2\tfunction\trun\n"


def test_cli_ref_reads_old_snapshot(range_history, capsys):
    assert main([
        str(range_history.repo.root), range_history.path,
        "--ref", range_history.edit, "--lines", "3:6", "--json",
    ]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["starting_commit"] == range_history.edit
    assert [entry["commit"] for entry in data["entries"]] == [
        range_history.edit, range_history.origin,
    ]


@pytest.mark.parametrize("arguments", [
    ["--json"],
    ["--lines", "1"],
    ["file.txt", "--json"],
    ["file.txt", "--occurrence", "2", "--lines", "1"],
    ["file.txt", "--symbols", "--lines", "1"],
    ["file.txt", "--lines", "1", "--paragraph", "1"],
    ["file.txt", "--lines", "0"],
    ["file.txt", "--lines", "3:1"],
])
def test_invalid_cli_combinations_exit_two(range_history, capsys, arguments):
    with pytest.raises(SystemExit) as error:
        main([str(range_history.repo.root), *arguments])
    assert error.value.code == 2
    assert "error:" in capsys.readouterr().err


@pytest.mark.parametrize("filename,content,selector", [
    ("empty.txt", "", ["--lines", "1"]),
    ("binary.txt", b"binary\0content", ["--lines", "1"]),
    ("utf16.txt", b"\xff\xfevalue", ["--lines", "1"]),
    ("text.txt", "only one\n", ["--lines", "1:2"]),
    ("text.txt", "only one\n", ["--paragraph", "0"]),
    ("text.txt", "only one\n", ["--text", "missing"]),
    ("README.md", "text\n", ["--symbol", "unknown"]),
])
def test_cli_repository_and_selector_errors_return_two_without_traceback(repo, capsys, filename, content, selector):
    repo.write(filename, content)
    repo.commit("Bad input fixture")
    assert main([str(repo.root), filename, *selector, "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("git-trail: ")
    assert "Traceback" not in captured.err


def test_cli_nonexistent_file_and_ref_return_two(repo, capsys):
    repo.write("exists.txt", "one\n")
    repo.commit("Exists")
    assert main([str(repo.root), "absent.txt", "--lines", "1", "--json"]) == 2
    assert "git-trail:" in capsys.readouterr().err
    assert main([str(repo.root), "exists.txt", "--ref", "absent", "--lines", "1", "--json"]) == 2
    assert "git-trail:" in capsys.readouterr().err


@pytest.mark.parametrize("value,start,end", [("1", 1, 1), ("2:9", 2, 9), ("8:8", 8, 8)])
def test_line_range_cli_syntax(value, start, end):
    selected = line_range(value)
    assert (selected.start, selected.end) == (start, end)


@pytest.mark.parametrize("value", ["", "0", "-1", "2:1", "1:", ":2", "a", "1:2:3", "1.2"])
def test_line_range_cli_rejects_invalid_syntax(value):
    with pytest.raises(argparse.ArgumentTypeError):
        line_range(value)


def test_python_module_entrypoint_json_and_hostile_path(repo):
    filename = "odd : $(touch injected); file.txt"
    repo.write(filename, "fixture\n")
    commit = repo.commit("Entrypoint")
    project = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable, "-m", "git_trail", str(repo.root), filename,
            "--lines", "1", "--json",
        ],
        cwd=project, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    data = json.loads(result.stdout)
    assert data["entries"][0]["commit"] == commit
    assert data["path"] == filename
    assert not (repo.root / "injected").exists()


def test_launcher_symlink_works_from_outside_project(repo, tmp_path):
    repo.write("file.txt", "first\nsecond\n")
    commit = repo.commit("Launcher fixture")
    project = Path(__file__).resolve().parents[1]
    bin_directory = tmp_path / "temporary bin with spaces"
    bin_directory.mkdir()
    launcher = bin_directory / "trail-link"
    launcher.symlink_to(project / "git-trail")
    result = subprocess.run(
        [str(launcher), str(repo.root), "file.txt", "--lines", "2", "--json"],
        cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["entries"][0]["commit"] == commit
    assert data["selection"]["start"] == data["selection"]["end"] == 2


def test_json_cli_preserves_repository_state(range_history, capsys):
    before = range_history.repo.state()
    assert main([
        str(range_history.repo.root), range_history.path, "--lines", "4:7", "--json",
    ]) == 0
    capsys.readouterr()
    assert range_history.repo.state() == before
