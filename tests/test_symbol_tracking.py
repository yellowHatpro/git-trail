from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass

import pytest
from textual.widgets import Input, Static

from git_trail.app import InitialSelection, SourceView, TrailApp
from git_trail.cli import main
from git_trail.git import LineRange, Repository, TrailError
from git_trail.selectors import named_symbol


def function(name, precision=2):
    return (
        f"def {name}(values):\n"
        "    filtered = [value for value in values if value >= 0]\n"
        "    total = sum(filtered)\n"
        "    count = len(filtered)\n"
        "    average = total / max(count, 1)\n"
        f"    return round(average, {precision})\n"
    )


def follow(repo, path, name, *, ref="HEAD", cancel=None):
    repository = Repository(repo.root, ref)
    snapshot = repository.snapshot(path)
    selection = named_symbol(snapshot.text, path, name)
    return repository, repository.symbol_history(snapshot, selection, cancel=cancel)


def warning_text(timeline):
    return "\n".join(timeline.warnings).casefold()


@dataclass(frozen=True)
class Refactors:
    repo: object
    old_path: str
    new_path: str
    origin: str
    rename: str
    edit: str
    shift: str
    move: str
    unrelated: str

    @property
    def relevant(self):
        return [self.move, self.edit, self.rename, self.origin]


@pytest.fixture
def refactors(repo):
    old_path, new_path = "src/original.py", "pkg/average new.py"
    repo.write(old_path, function("original"))
    origin = repo.commit("Introduce original")
    repo.write(old_path, function("calculate_average"))
    rename = repo.commit("Rename function")
    repo.write(old_path, function("calculate_average", 3))
    edit = repo.commit("Edit same named function")
    repo.write(old_path, "# package prefix\n\n" + function("calculate_average", 3))
    shift = repo.commit("Shift source with unrelated prefix")
    (repo.root / old_path).unlink()
    repo.write(new_path, function("mean_price", 4))
    move = repo.commit("Move rename and refine function")
    repo.write("notes.txt", "unrelated notes\n")
    unrelated = repo.commit("Unrelated later change")
    return Refactors(repo, old_path, new_path, origin, rename, edit, shift, move, unrelated)


def test_symbol_history_follows_rename_body_edit_shift_and_cross_file_move_with_exact_views(refactors):
    fixture = refactors
    fixture.repo.write(fixture.new_path, "LOCAL WORK IN PROGRESS\n")
    fixture.repo.write("notes.txt", "staged local notes\n")
    fixture.repo.git("add", "--", "notes.txt")
    before = fixture.repo.state()
    repository, timeline = follow(fixture.repo, fixture.new_path, "mean_price")
    assert timeline.mode == "symbol"
    assert timeline.commit == fixture.unrelated
    assert [entry.commit for entry in timeline.entries] == fixture.relevant
    assert fixture.shift not in [entry.commit for entry in timeline.entries]
    moved, edited, renamed, origin = timeline.entries
    assert [entry.match_method for entry in timeline.entries] == [
        "similarity", "name", "body", "introduction",
    ]
    assert moved.old_path == fixture.old_path and moved.new_path == fixture.new_path
    assert (moved.old_range.start, moved.old_range.end) == (3, 8)
    assert (moved.new_range.start, moved.new_range.end) == (1, 6)
    assert 0.90 <= moved.similarity < 1.0
    assert edited.similarity is None
    assert renamed.similarity == 1.0
    assert "-def calculate_average" in moved.patch and "+def mean_price" in moved.patch
    assert "# package prefix" not in moved.patch
    assert "working-tree" in warning_text(timeline)
    assert "first parent" in warning_text(timeline)
    assert "not semantic proof" in warning_text(timeline)
    old = repository.historical_source(moved, before=True)
    new = repository.historical_source(moved)
    assert old.snapshot.path == fixture.old_path and old.snapshot.commit == fixture.shift
    assert new.snapshot.path == fixture.new_path and new.snapshot.commit == fixture.move
    assert old.selection == moved.old_range and new.selection == moved.new_range
    assert "def calculate_average" in old.snapshot.text
    assert "def mean_price" in new.snapshot.text
    assert origin.parents == () and origin.old_count == 0 and origin.old_range is None
    assert repository.historical_source(origin, before=True).snapshot is None
    assert fixture.repo.state() == before


@pytest.mark.parametrize("kind", ["function", "class"])
def test_exact_body_rename_tracks_functions_and_classes_through_literal_unicode_paths(repo, kind):
    old_path, new_path = "old'λ.py", "new [literal];$(touch injected).py"
    body = function("Original")
    if kind == "class":
        body = "class Original:\n" + "".join("    " + line + "\n" for line in function("average").splitlines())
    repo.write(old_path, body)
    origin = repo.commit("Original symbol")
    (repo.root / old_path).unlink()
    repo.write(new_path, body.replace("Original", "Renamed"))
    moved = repo.commit("Rename and move")
    before = repo.state()
    repository, timeline = follow(repo, new_path, "Renamed")
    assert [entry.commit for entry in timeline.entries] == [moved, origin]
    assert timeline.entries[0].match_method == "body"
    assert timeline.entries[0].similarity == 1.0
    assert repository.historical_source(timeline.entries[0], before=True).snapshot.path == old_path
    assert repository.historical_source(timeline.entries[0]).snapshot.path == new_path
    assert not (repo.root / "injected").exists()
    assert repo.state() == before


def test_named_continuity_is_qualified_by_scope_and_explicitly_labeled(repo):
    old = "class Left:\n    def read(self):\n        return 1\n\nclass Right:\n    def read(self):\n        return 2\n"
    new = old.replace("return 1", 'raise RuntimeError("replacement implementation")')
    repo.write("scope.py", old)
    origin = repo.commit("Two qualified methods")
    repo.write("scope.py", new)
    edit = repo.commit("Replace Left implementation")
    repository = Repository(repo.root)
    snapshot = repository.snapshot("scope.py")
    timeline = repository.symbol_history(snapshot, LineRange(2, 3))
    assert [entry.commit for entry in timeline.entries] == [edit, origin]
    assert timeline.entries[0].match_method == "name"
    assert timeline.entries[0].similarity is None
    assert "-        return 1" in timeline.entries[0].patch
    assert "return 2" not in "\n".join(entry.patch for entry in timeline.entries)
    assert "named continuity" in warning_text(timeline)
    assert "not proof of semantic identity" in warning_text(timeline)


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("duplicate", "ambiguous"), ("copy", "copy-like"), ("copy-edited", "copy-like"),
        ("weak", "no confident"), ("repeated", "repeated"),
    ],
)
def test_ambiguous_copied_repeated_and_weak_predecessors_stop_without_inventing_origin(repo, case, expected):
    parent = function("original")
    if case == "duplicate":
        parent += "\n" + function("other")
    elif case == "repeated":
        parent += "\n" + function("original", 3)
    repo.write("candidate.py", parent)
    origin = repo.commit("Potential predecessors")
    current = function("renamed")
    name = "renamed"
    if case == "copy":
        current = parent + "\n" + current
    elif case == "copy-edited":
        current = function("original", 3) + "\n" + current
    elif case == "weak":
        current = "def unrelated():\n    return None\n"
        name = "unrelated"
    elif case == "repeated":
        current = function("original", 4)
        name = "original"
    repo.write("candidate.py", current)
    repo.commit("Uncertain transition")
    before = repo.state()
    _, timeline = follow(repo, "candidate.py", name)
    assert timeline.entries == ()
    assert origin not in [entry.commit for entry in timeline.entries]
    assert "partial" in warning_text(timeline)
    assert expected in warning_text(timeline)
    assert repo.state() == before


@pytest.mark.parametrize(
    ("parent", "expected"),
    [('def broken(:\n    pass\n', "syntax"), (b"binary\0parent", "binary")],
)
def test_unreadable_historical_source_stops_with_a_partial_warning(repo, parent, expected):
    repo.write("historic.py", parent)
    origin = repo.commit("Unreadable earlier source")
    repo.write("historic.py", function("current"))
    repo.commit("Valid current source")
    before = repo.state()
    _, timeline = follow(repo, "historic.py", "current")
    assert timeline.entries == ()
    assert "partial" in warning_text(timeline)
    assert expected in warning_text(timeline)
    assert origin not in [entry.commit for entry in timeline.entries]
    assert repo.state() == before


@pytest.mark.parametrize(
    ("path", "text", "selection", "expected"),
    [
        ("file.txt", function("current"), LineRange(1, 6), "unavailable"),
        ("file.py", "def current(:\n    pass\n", LineRange(1, 2), "syntax"),
        ("file.py", function("current"), LineRange(2, 3), "complete"),
    ],
)
def test_follow_requires_a_complete_parseable_supported_symbol(repo, path, text, selection, expected):
    repo.write(path, text)
    repo.commit("Starting selection")
    repository = Repository(repo.root)
    snapshot = repository.snapshot(path)
    with pytest.raises(TrailError, match=expected):
        repository.symbol_history(snapshot, selection)


def test_first_parent_merge_does_not_invent_a_linear_feature_branch_history(repo):
    repo.write("merge.py", function("target"))
    origin = repo.commit("Introduce target")
    repo.git("checkout", "-q", "-b", "feature")
    repo.write("merge.py", function("target", 3))
    feature = repo.commit("Feature edits target")
    repo.git("checkout", "-q", "main")
    repo.write("notes.txt", "main-side notes\n")
    main = repo.commit("Main changes another file")
    repo.git("merge", "--no-ff", "-m", "Merge feature", "feature")
    merge = repo.git("rev-parse", "HEAD").stdout.decode().strip()
    before = repo.state()
    repository, timeline = follow(repo, "merge.py", "target")
    assert [entry.commit for entry in timeline.entries] == [merge, origin]
    assert feature not in [entry.commit for entry in timeline.entries]
    assert timeline.entries[0].parents == (main, feature)
    old = repository.historical_source(timeline.entries[0], before=True)
    assert old.snapshot.commit == main
    assert "round(average, 2)" in old.snapshot.text
    assert "first parent" in old.note.casefold()
    assert repo.state() == before


def test_shallow_symbol_history_marks_a_boundary_instead_of_a_known_origin(refactors, tmp_path):
    clone = tmp_path / "shallow refactors"
    fixture = refactors
    fixture.repo.git("clone", "--quiet", "--depth", "1", "--no-local", fixture.repo.root.as_uri(), str(clone))
    repository = Repository(clone)
    snapshot = repository.snapshot(fixture.new_path)
    before = (clone / ".git" / "index").read_bytes()
    timeline = repository.symbol_history(snapshot, named_symbol(snapshot.text, snapshot.path, "mean_price"))
    assert [entry.commit for entry in timeline.entries] == [fixture.unrelated]
    assert timeline.entries[0].match_method == "boundary"
    assert "shallow" in warning_text(timeline)
    assert "boundary" in timeline.entries[0].tracking_note.casefold()
    assert (clone / ".git" / "index").read_bytes() == before


@pytest.mark.parametrize(
    ("limit", "value", "expected"),
    [("MAX_COMMITS", 1, "stopped after 1"), ("MAX_FILES", 0, "file limit"), ("MAX_SECONDS", 0, "time limit")],
)
def test_bounded_history_returns_explicit_partial_results(refactors, monkeypatch, limit, value, expected):
    import git_trail.tracking

    monkeypatch.setattr(git_trail.tracking, limit, value)
    _, timeline = follow(refactors.repo, refactors.new_path, "mean_price")
    assert expected in warning_text(timeline)
    assert "partial" in warning_text(timeline)
    assert all(entry.match_method != "introduction" for entry in timeline.entries)


@pytest.mark.parametrize(("limit", "value", "expected"), [("MAX_BYTES", 20, "byte limit"), ("MAX_TOKENS", 5, "token limit")])
def test_oversized_starting_symbol_is_rejected_before_lineage_inference(repo, monkeypatch, limit, value, expected):
    import git_trail.tracking

    repo.write("large.py", function("target"))
    repo.commit("Larger than configured bound")
    monkeypatch.setattr(git_trail.tracking, limit, value)
    with pytest.raises(TrailError, match=expected):
        follow(repo, "large.py", "target")


def test_cancelled_tracking_raises_without_returning_a_misleading_partial_timeline(refactors):
    cancellation = threading.Event()
    cancellation.set()
    before = refactors.repo.state()
    with pytest.raises(TrailError, match="cancelled"):
        follow(refactors.repo, refactors.new_path, "mean_price", cancel=cancellation)
    assert refactors.repo.state() == before


def test_opt_in_json_reports_symbol_evidence_while_default_history_stays_range_mode(refactors, range_history, capsys, monkeypatch):
    def forbidden_run(*args, **kwargs):
        pytest.fail("JSON tracking started a TUI")

    monkeypatch.setattr(TrailApp, "run", forbidden_run)
    fixture = refactors
    assert main([str(fixture.repo.root), fixture.new_path, "--symbol", "mean_price", "--follow-symbol", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["mode"] == "symbol"
    assert [entry["commit"] for entry in data["entries"]] == fixture.relevant
    assert data["entries"][0]["old_range"]["start"] == 3
    assert data["entries"][0]["new_path"] == fixture.new_path
    assert data["entries"][0]["match_method"] == "similarity"
    assert data["warnings"]
    assert main([str(fixture.repo.root), fixture.old_path, "--ref", fixture.rename,
                 "--symbol", "calculate_average", "--follow-symbol", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [entry["commit"] for entry in data["entries"]] == [fixture.rename, fixture.origin]
    fixture = range_history
    assert main([str(fixture.repo.root), fixture.path, "--lines", "4:7", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["mode"] == "range"
    assert [entry["commit"] for entry in data["entries"]] == fixture.relevant
    assert main([str(fixture.repo.root), fixture.path, "--lines", "5", "--follow-symbol", "--json"]) == 2
    assert "complete" in capsys.readouterr().err


async def settle(app, pilot):
    async def wait_for_idle():
        for _ in range(10):
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            if not app.workers:
                return
        pytest.fail("Workers kept spawning while waiting for the completed UI state.")

    await asyncio.wait_for(wait_for_idle(), timeout=15)


def content(app, selector):
    value = app.query_one(selector, Static).content
    return value.plain if hasattr(value, "plain") else str(value)


async def test_follow_command_expands_containing_function_and_before_view_follows_the_old_file(refactors):
    fixture = refactors
    before = fixture.repo.state()
    app = TrailApp(Repository(fixture.repo.root), InitialSelection(fixture.new_path))
    async with app.run_test(size=(140, 45)) as pilot:
        await settle(app, pilot)
        source = app.query_one("#source", SourceView)
        source.move_cursor((3, 4))
        await pilot.press(":")
        app.query_one("#command-input", Input).value = "follow"
        await pilot.press("enter")
        await settle(app, pilot)
        assert app.timeline.mode == "symbol"
        assert [entry.commit for entry in app.timeline.entries] == fixture.relevant
        assert (app.timeline.selection.start, app.timeline.selection.end) == (1, 6)
        assert app.snapshot.path == fixture.new_path
        assert "using similarity" in content(app, "#source-note")
        assert "SYMBOL TRACKING" in content(app, "#commit-details")
        await pilot.press(":")
        app.query_one("#command-input", Input).value = "before"
        await pilot.press("enter")
        await settle(app, pilot)
        assert app.snapshot.path == fixture.old_path and app.snapshot.commit == fixture.shift
        assert (app.selected_range().start, app.selected_range().end) == (3, 8)
        assert "def calculate_average" in source.text
        await pilot.press("ctrl+j")
        await settle(app, pilot)
        assert app.selected_entry.commit == fixture.edit
        assert app.snapshot.path == fixture.old_path
        assert "round(average, 2)" in source.text
    assert fixture.repo.state() == before


async def test_new_file_load_cancels_pending_symbol_worker_and_preserves_newer_source(refactors, monkeypatch):
    fixture = refactors
    repository = Repository(fixture.repo.root)
    started = threading.Event()
    observed_cancel = threading.Event()

    def delayed(snapshot, selection, *, cancel):
        started.set()
        if not cancel.wait(timeout=10):
            raise RuntimeError("Test did not cancel the pending symbol scan")
        observed_cancel.set()
        raise TrailError("Symbol history cancelled.")

    monkeypatch.setattr(repository, "symbol_history", delayed)
    app = TrailApp(repository, InitialSelection(fixture.new_path))
    async with app.run_test(size=(140, 45)) as pilot:
        await settle(app, pilot)
        app.action_follow()
        assert await asyncio.to_thread(started.wait, 5)
        await app.load_file("notes.txt")
        await settle(app, pilot)
        assert observed_cancel.is_set()
        assert app.snapshot.path == "notes.txt"
        assert app.query_one("#source", SourceView).text == "unrelated notes\n"
        assert app.timeline is None
        assert "cancelled" not in content(app, "#status").casefold()


async def test_partial_follow_without_entries_keeps_the_starting_symbol_visible(repo):
    repo.write("copy.py", function("original"))
    repo.commit("Original definition")
    text = function("original") + "\n" + function("copied")
    repo.write("copy.py", text)
    repo.commit("Copy original")
    app = TrailApp(Repository(repo.root), InitialSelection("copy.py"))
    async with app.run_test(size=(140, 45)) as pilot:
        await settle(app, pilot)
        source = app.query_one("#source", SourceView)
        source.move_cursor((8, 4))
        app.action_follow()
        await settle(app, pilot)
        assert app.timeline.mode == "symbol"
        assert app.timeline.entries == ()
        assert app.snapshot.path == "copy.py"
        assert source.text == text
        assert (app.selected_range().start, app.selected_range().end) == (8, 13)
        assert "partial" in content(app, "#status").casefold()
        assert "copy-like" in content(app, "#source-note").casefold()
