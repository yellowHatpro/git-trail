from __future__ import annotations

import asyncio
import threading
from dataclasses import replace

import pytest
from rich.syntax import Syntax
from textual.worker import WorkerCancelled
from textual.widgets import Button, Input, OptionList, Static

from git_trail.app import HelpScreen, InitialSelection, SourceView, TrailApp
from git_trail.git import LineRange, Repository, TrailError
from git_trail.merge_navigation import MergePickerScreen, ParentPickerScreen
from git_trail.navigation import Journal, Visit
from git_trail.state import StateStore
from test_merge_parents import BODY, pair_patch, raw_parents, two_parent_merge


async def settle(app, pilot):
    async def idle():
        for _ in range(12):
            await pilot.pause()
            outcomes = await asyncio.gather(
                *(worker.wait() for worker in tuple(app.workers)), return_exceptions=True,
            )
            for outcome in outcomes:
                if isinstance(outcome, WorkerCancelled):
                    continue
                if isinstance(outcome, BaseException):
                    raise outcome
            await pilot.pause()
            if not app.workers:
                return
        pytest.fail("Merge navigation did not finish its queued UI work.")

    await asyncio.wait_for(idle(), timeout=20)


async def focus(app, pilot, selector):
    widget = app.query_one(selector)
    widget.focus()
    await pilot.pause()
    assert app.focused is widget
    return widget


async def command(app, pilot, value):
    await pilot.press(":")
    field = app.query_one("#command-input", Input)
    assert app.focused is field
    field.value = value
    await pilot.press("enter")
    await settle(app, pilot)


def content(app, selector):
    value = app.query_one(selector, Static).content
    return value.code if isinstance(value, Syntax) else value.plain if hasattr(value, "plain") else str(value)


def scene(app):
    source = app.query_one("#source", SourceView)
    return (
        app.repository.commit, app.snapshot, app.traced_snapshot, app.timeline,
        app.selected_entry, app.comparison, app.review_entry, app.source_mode,
        source.text, source.selection, app.filtered_entries,
        app.query_one("#history-filter", Input).value,
        content(app, "#source-title"), content(app, "#source-note"), content(app, "#diff-title"),
        content(app, "#diff"), content(app, "#commit-details"),
        app._diff_key, app._diff_cursor, app._diff_side, app._diff_active, app._diff_rows,
        app.diff_layout, app.diff_effective_layout,
        tuple(app.journal.visits), app.journal.index, app.current_visit(), app._navigation_ready,
    )


def assert_source(app, commit, path, text, line=None):
    assert app.snapshot is not None
    assert (app.snapshot.commit, app.snapshot.path, app.snapshot.text) == (commit, path, text)
    source = app.query_one("#source", SourceView)
    assert source.text == text and source.read_only
    assert app._navigation_ready and app.current_visit() is not None
    if line is not None:
        assert (app.selected_range().start, app.selected_range().end) == (line, line)
        assert source.selected_text.rstrip("\n") == text.splitlines()[line - 1]


def assert_comparison(app, repo, commit, path, index):
    comparison = app.comparison
    parents = raw_parents(repo, commit)
    assert comparison is not None
    assert (comparison.commit, comparison.path, comparison.parent_index, comparison.parent_commit) == (
        commit, path, index, parents[index - 1],
    )
    assert comparison.parents == parents == app.review_entry.parents
    expected = pair_patch(repo, parents[index - 1], commit,
                          *(name for name in (comparison.old_path, comparison.new_path) if name))
    assert comparison.patch == app.review_entry.patch == expected
    assert "\n".join(line.text for line in app._diff_patch.lines) == expected
    assert "FULL-FILE" in content(app, "#diff-title")
    assert parents[index - 1][:10] in content(app, "#diff-title")
    assert f"P{index}/{len(parents)}" in content(app, "#diff-title")
    assert str(app.query_one("#merge-parent", Button).label) == f"P{index}/{len(parents)}"
    assert "ALL PARENTS" in content(app, "#commit-details")
    visit = app.current_visit()
    assert visit is not None
    assert (visit.comparison_commit, visit.comparison_path, visit.comparison_parent) == (
        commit, path, parents[index - 1],
    )
    return comparison


def raw_index(app, text):
    return next(index for index, line in enumerate(app._diff_patch.lines) if line.text == text)


def assert_parent_footer(app, merge, parents, index):
    prefix = f"FULL-FILE {merge[:7]} · parent {index}/{len(parents)} {parents[index - 1][:7]} · "
    assert content(app, "#status").startswith(prefix)


async def click_diff(app, pilot, raw, side):
    pane = app.query_one("#diff-scroll")
    row = next(index for index, item in enumerate(app._diff_rows) if raw in item.indices)
    pane.scroll_to(x=0, y=0, animate=False, immediate=True)
    await pilot.pause()
    assert row < pane.scrollable_content_region.height
    column = 6 if side == "before" else app._diff_separator + 3
    assert await pilot.click("#diff", offset=(column, row))
    await pilot.pause()
    assert app.focused is pane
    assert app._diff_side == side


def legacy_visit(**changes):
    value = dict(
        repository="/tmp/merge-review-repository", commit="a" * 40, path="src/module.py",
        start=2, end=3, follow=False, traced=False, selected_commit="", mode="starting",
        history_filter="", display_commit="", display_path="", display_start=0, display_end=0,
    )
    value.update(changes)
    return value


def test_visit_merge_fields_are_optional_and_parent_identity_survives_round_trip():
    legacy = legacy_visit()
    assert Visit.from_dict(legacy).to_dict() == legacy
    assert Visit.from_dict({**legacy, "canonical": False}).to_dict() == legacy
    canonical = {**legacy, "canonical": True}
    assert Visit.from_dict(canonical).to_dict() == canonical
    first = Visit.from_dict({
        **legacy, "mode": "after", "comparison_commit": "b" * 40,
        "comparison_path": "moved/source.py", "comparison_parent": "c" * 40,
        "display_commit": "b" * 40, "display_path": "moved/source.py",
        "display_start": 7, "display_end": 7,
    })
    second = replace(first, comparison_parent="d" * 40)
    assert Visit.from_dict(first.to_dict()) == first
    assert Visit.from_dict(second.to_dict()) == second
    assert first != second and first.display_commit == second.display_commit
    journal = Journal()
    journal.record(first)
    journal.record(second)
    assert journal.visits == [first, second] and journal.index == 1
    unavailable = replace(first, mode="before", display_commit="", display_path="",
                          display_start=0, display_end=0)
    assert Visit.from_dict(unavailable.to_dict()) == unavailable
    assert (unavailable.start, unavailable.end) == (2, 3)


@pytest.mark.parametrize("changes", [
    {"comparison_path": "source.py"},
    {"comparison_parent": "c" * 40},
    {"comparison_path": "source.py", "comparison_parent": "c" * 40},
])
def test_direct_partial_visit_cannot_lose_invalid_comparison_fields_during_serialization(changes):
    malformed = replace(Visit.from_dict(legacy_visit()), **changes)
    encoded = malformed.to_dict()
    for name, value in changes.items():
        assert encoded[name] == value
    with pytest.raises(TrailError, match="Invalid saved visit"):
        Visit.from_dict(encoded)


@pytest.mark.parametrize("changes", [
    {"comparison_commit": "b" * 40},
    {"comparison_path": "source.py"},
    {"comparison_parent": "c" * 40},
    {"comparison_commit": "b" * 40, "comparison_path": "../source.py", "comparison_parent": "c" * 40},
    {"comparison_commit": "HEAD", "comparison_path": "source.py", "comparison_parent": "c" * 40},
    {"comparison_commit": "b" * 40, "comparison_path": "source.py", "comparison_parent": 2},
    {"comparison_commit": "b" * 40, "comparison_path": "source.py", "comparison_parent": "c" * 40},
    {"canonical": "true"},
    {"canonical": 1},
])
def test_saved_merge_visit_rejects_partial_unsafe_or_unresolved_context(changes):
    with pytest.raises(TrailError, match="Invalid saved visit"):
        Visit.from_dict(legacy_visit(**changes))


async def test_historical_merge_picker_preserves_pruned_timeline_and_jumps_exact_parent(two_parent_merge):
    fixture = two_parent_merge
    repo = fixture.repo
    repo.write("later.txt", "later committed source\n")
    tip = repo.commit("Ordinary tip above the merge")
    before = repo.state()
    app = TrailApp(Repository(repo.root), InitialSelection(fixture.path, LineRange(2, 2)))
    async with app.run_test(size=(180, 48)) as pilot:
        await settle(app, pilot)
        assert app.repository.commit == tip
        entries = tuple(item.commit for item in app.timeline.entries)
        assert fixture.merge not in entries
        original_timeline = app.timeline
        original_count = app.query_one("#timeline", OptionList).option_count
        await command(app, pilot, "merges")
        assert isinstance(app.screen, MergePickerScreen)
        options = app.screen.query_one("#merge-options", OptionList)
        assert [options.get_option_at_index(i).id for i in range(options.option_count)] == [fixture.merge]
        field = app.screen.query_one("#merge-filter", Input)
        assert app.screen.focused is field
        field.value = "tax feature"
        await pilot.pause()
        assert options.option_count == 1
        await pilot.press("enter")
        await settle(app, pilot)
        assert isinstance(app.screen, ParentPickerScreen)
        options = app.screen.query_one("#parent-options", OptionList)
        assert [options.get_option_at_index(i).id for i in range(options.option_count)] == [
            fixture.first, fixture.second,
        ]
        await pilot.press("ctrl+n", "enter")
        await settle(app, pilot)
        assert app.screen is app.default_screen
        assert_comparison(app, repo, fixture.merge, fixture.path, 2)
        assert app.timeline is original_timeline
        assert tuple(item.commit for item in app.timeline.entries) == entries
        assert app.query_one("#timeline", OptionList).option_count == original_count
        assert_source(app, fixture.merge, fixture.path, fixture.merged_text)
        await command(app, pilot, "before")
        assert_source(app, fixture.second, fixture.path, fixture.second_text)
        await command(app, pilot, "split")
        assert app.diff_effective_layout == "split"
        deleted = raw_index(app, "-    base = value + 1")
        await click_diff(app, pilot, deleted, "before")
        await pilot.press("enter")
        await settle(app, pilot)
        assert_source(app, fixture.second, fixture.path, fixture.second_text, 2)
        app.save_bookmark("Exact second-parent line")
        await click_diff(app, pilot, deleted, "after")
        await pilot.press("enter")
        await settle(app, pilot)
        assert_source(app, fixture.merge, fixture.path, fixture.merged_text, 2)
        await command(app, pilot, "open Exact second-parent line")
        assert_source(app, fixture.second, fixture.path, fixture.second_text, 2)
        assert_comparison(app, repo, fixture.merge, fixture.path, 2)
        assert_parent_footer(app, fixture.merge, (fixture.first, fixture.second), 2)
        await command(app, pilot, "starting")
        assert app.comparison is None
        assert_source(app, tip, fixture.path, fixture.merged_text, 2)
        expected_restored = replace(
            original_timeline, selection=replace(original_timeline.selection, label="saved selection"),
        )
        assert app.timeline.to_dict() == expected_restored.to_dict()
    assert repo.state() == before


async def test_octopus_parent_commands_bookmarks_and_journal_keep_distinct_identical_after_views(repo, tmp_path):
    path = "values.txt"
    base = "left\nkeep one\nmiddle\nkeep two\nright\n"
    repo.write(path, base)
    origin = repo.commit("Base values")
    repo.git("checkout", "--quiet", "-b", "left")
    repo.write(path, base.replace("left\n", "LEFT\n"))
    left = repo.commit("Left branch")
    repo.git("checkout", "--quiet", "-b", "right", origin)
    repo.write(path, base.replace("right\n", "RIGHT\n"))
    right = repo.commit("Right branch")
    repo.git("checkout", "--quiet", "main")
    repo.write(path, base.replace("middle\n", "MAIN middle\n"))
    main = repo.commit("Main middle")
    repo.git("merge", "--quiet", "--no-ff", "-m", "Octopus contribution", "left", "right")
    merge = repo.git("rev-parse", "HEAD").stdout.decode().strip()
    merged = base.replace("left\n", "LEFT\n").replace("middle\n", "MAIN middle\n").replace("right\n", "RIGHT\n")
    before = repo.state()
    store = StateStore(tmp_path / "user-state" / "state.json")
    app = TrailApp(Repository(repo.root), InitialSelection(path), store=store)
    visits = []
    async with app.run_test(size=(160, 48)) as pilot:
        await settle(app, pilot)
        await pilot.press("f8")
        await settle(app, pilot)
        assert isinstance(app.screen, ParentPickerScreen)
        options = app.screen.query_one("#parent-options", OptionList)
        assert tuple(options.get_option_at_index(i).id for i in range(3)) == (main, left, right)
        await pilot.press("escape")
        await settle(app, pilot)
        for index in range(1, 4):
            await command(app, pilot, f"parent {index}")
            assert_comparison(app, repo, merge, path, index)
            assert_source(app, merge, path, merged)
            app.select_range(LineRange(2, 2))
            await pilot.pause()
            visits.append(app.current_visit())
            app.save_bookmark(f"Parent {index}")
        assert len(set(visits)) == 3
        assert {(item.display_commit, item.display_start, item.display_end) for item in visits} == {(merge, 2, 2)}
        await command(app, pilot, "back")
        assert app.current_visit() == visits[1]
        assert_comparison(app, repo, merge, path, 2)
        assert_parent_footer(app, merge, (main, left, right), 2)
        await command(app, pilot, "forward")
        assert app.current_visit() == visits[2]
        assert_parent_footer(app, merge, (main, left, right), 3)
        await command(app, pilot, f"parent {left}")
        assert_comparison(app, repo, merge, path, 2)
        await command(app, pilot, "nextparent")
        assert_comparison(app, repo, merge, path, 3)
        await command(app, pilot, "nextparent")
        assert_comparison(app, repo, merge, path, 1)
        await command(app, pilot, "prevparent")
        assert_comparison(app, repo, merge, path, 3)
        await command(app, pilot, "open Parent 1")
        assert app.current_visit() == visits[0]
        assert_parent_footer(app, merge, (main, left, right), 1)
        settled = scene(app)
        journal = tuple(app.journal.visits), app.journal.index
        await focus(app, pilot, "#source")
        focused = app.focused
        app.choose_parent(4)
        await settle(app, pilot)
        assert scene(app) == settled
        assert "Could not compare" in content(app, "#status")
        assert app.focused is focused
        bad = replace(visits[0], comparison_parent="f" * 40)
        assert not await app.restore_visit(bad, index=0)
        await settle(app, pilot)
        assert scene(app) == settled
        assert (tuple(app.journal.visits), app.journal.index) == journal
        assert app.focused is focused and app.current_visit() is not None
        await pilot.press("ctrl+q")
    persisted = StateStore(store.path)
    assert not persisted.read_only
    for index, visit in enumerate(visits, 1):
        assert persisted.data["bookmarks"][str(repo.root.resolve())][f"Parent {index}"] == visit.to_dict()
    assert Visit.from_dict(persisted.data["sessions"][str(repo.root.resolve())]) == visits[0]
    assert repo.state() == before


async def test_absent_empty_unchanged_and_missing_parent_views_remain_honest_and_restorable(repo):
    repo.write("stable.py", "VALUE = 1\n")
    repo.write("deleted.py", "old first\nold second\n")
    repo.write("empty.txt", "")
    origin = repo.commit("Base files")
    repo.git("checkout", "--quiet", "-b", "feature")
    repo.write("added.py", "new first\nnew second\n")
    (repo.root / "deleted.py").unlink()
    second = repo.commit("Add and remove feature files")
    repo.git("checkout", "--quiet", "main")
    repo.write("stable.py", "VALUE = 2\n")
    first = repo.commit("Main change")
    repo.git("merge", "--quiet", "--no-ff", "-m", "Merge files", "feature")
    merge = repo.git("rev-parse", "HEAD").stdout.decode().strip()
    before = repo.state()
    app = TrailApp(Repository(repo.root), InitialSelection("added.py"))
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        assert "Parents" in app.query_one("#merge-parent", Button).render_line(0).text
        for mode, label in (("after", "After"), ("before", "Before"), ("starting", "Start")):
            assert label in app.query_one(f"#view-{mode}", Button).render_line(0).text
        assert app.query_one("#source", SourceView).scrollable_content_region.height >= 4
        await command(app, pilot, "parent 1")
        assert_comparison(app, repo, merge, "added.py", 1)
        assert "P1/2" in app.query_one("#merge-parent", Button).render_line(0).text
        await command(app, pilot, "before")
        assert app.snapshot is None and app.query_one("#source", SourceView).text == ""
        assert "does not exist" in content(app, "#source-note").lower()
        absent = app.current_visit()
        assert absent is not None
        assert (absent.display_commit, absent.display_path, absent.display_start, absent.display_end) == ("", "", 0, 0)
        app.save_bookmark("Absent old file")
        await command(app, pilot, "after")
        assert_source(app, merge, "added.py", "new first\nnew second\n")
        await command(app, pilot, "open Absent old file")
        assert app.current_visit() == absent and app.snapshot is None
        assert_parent_footer(app, merge, (first, second), 1)
        await command(app, pilot, "parent 2")
        assert_comparison(app, repo, merge, "added.py", 2)
        assert app.comparison.patch == "" and not app._diff_patch.change_targets()
        assert_source(app, second, "added.py", "new first\nnew second\n")
        assert "No" in content(app, "#source-note") or "unchanged" in content(app, "#source-note").lower()
        app.request_parent(merge, "deleted.py", 1, mode="after")
        await settle(app, pilot)
        assert_comparison(app, repo, merge, "deleted.py", 1)
        assert app.snapshot is None
        app.save_bookmark("Deleted after")
        await command(app, pilot, "before")
        assert_source(app, first, "deleted.py", "old first\nold second\n")
        await command(app, pilot, "open Deleted after")
        assert app.snapshot is None and app.comparison.old_path == "deleted.py"
        app.request_parent(merge, "empty.txt", 1, mode="before")
        await settle(app, pilot)
        assert_comparison(app, repo, merge, "empty.txt", 1)
        assert app.snapshot is not None and app.snapshot.text == ""
        empty = app.current_visit()
        assert empty is not None and empty.display_commit == "" and empty.display_start == 0
        app.save_bookmark("Empty old file")
        await app.load_file("stable.py")
        await settle(app, pilot)
        await command(app, pilot, "open Empty old file")
        assert app.current_visit() == empty and app.snapshot.text == ""
        assert_parent_footer(app, merge, (first, second), 1)
        object_path = repo.root / ".git" / "objects" / second[:2] / second[2:]
        assert object_path.is_file()
        raw_object = object_path.read_bytes()
        try:
            object_path.unlink()
            settled = scene(app)
            await command(app, pilot, "parents")
            assert isinstance(app.screen, ParentPickerScreen)
            choices = app.screen.query_one("#parent-options", OptionList)
            assert choices.option_count == 2
            assert choices.get_option_at_index(1).id == second
            assert "Unavailable" in choices.get_option_at_index(1).prompt.plain
            await pilot.press("ctrl+n", "enter")
            await settle(app, pilot)
            assert scene(app) == settled
            assert "unavailable" in content(app, "#status").lower()
            assert app._navigation_ready and app.current_visit() == empty
            assert not object_path.exists()
        finally:
            object_path.write_bytes(raw_object)
    assert repo.state() == before
    assert origin != merge


async def test_slow_parent_or_picker_cannot_replace_newer_selection_navigation_or_modal(two_parent_merge, monkeypatch):
    fixture = two_parent_merge
    repo = fixture.repo
    repo.write("other.txt", "other first\nother second\n")
    repo.commit("Other source")
    before = repo.state()
    app = TrailApp(Repository(repo.root, fixture.merge), InitialSelection(fixture.path, LineRange(2, 2)))
    async with app.run_test(size=(160, 48)) as pilot:
        await settle(app, pilot)
        assert fixture.merge not in {entry.commit for entry in app.timeline.entries}
        await command(app, pilot, "parents")
        assert isinstance(app.screen, ParentPickerScreen)
        assert app.screen.metadata.commit == fixture.merge
        await pilot.press("escape")
        await settle(app, pilot)
        original = app.prepare_comparison
        for newer in ("parent", "source", "filter", "timeline", "trace", "file", "modal", "invalid-filter"):
            app.query_one("#history-filter", Input).value = ""
            await settle(app, pilot)
            await app.load_file(fixture.path, LineRange(2, 2))
            await settle(app, pilot)
            assert app.query_one("#history-filter", Input).value == ""
            if newer == "trace":
                await command(app, pilot, "starting")
                assert_source(app, fixture.merge, fixture.path, fixture.merged_text, 2)
                trace_anchor = app.snapshot
            started, released = threading.Event(), threading.Event()

            def delayed(repository, commit, path, parent, mode):
                if parent == 1:
                    started.set()
                    assert released.wait(10), "Test did not release parent preparation."
                return original(repository, commit, path, parent, mode)

            monkeypatch.setattr(app, "prepare_comparison", delayed)
            await focus(app, pilot, "#source")
            settled = scene(app)
            app.request_parent(fixture.merge, fixture.path, 1)
            try:
                assert await asyncio.to_thread(started.wait, 5)
                if newer == "parent":
                    app.request_parent(fixture.merge, fixture.path, 2)
                elif newer == "source":
                    app.select_range(LineRange(5, 5))
                elif newer == "filter":
                    app.query_one("#history-filter", Input).value = f"hash:{fixture.origin[:10]}"
                elif newer == "invalid-filter":
                    app.open_prompt("#history-filter")
                    await pilot.pause()
                    field = app.query_one("#history-filter", Input)
                    assert app.focused is field
                    field.value = "after:2024-02-30"
                elif newer == "timeline":
                    app.show_entry(len(app.filtered_entries) - 1)
                elif newer == "trace":
                    app.select_range(LineRange(11, 11))
                    await pilot.pause()
                    app.action_trace()
                elif newer == "file":
                    await app.load_file(fixture.path)
                else:
                    app.action_help()
                await pilot.pause()
                released.set()
                await settle(app, pilot)
                if newer == "parent":
                    assert_comparison(app, repo, fixture.merge, fixture.path, 2)
                    assert_source(app, fixture.merge, fixture.path, fixture.merged_text)
                else:
                    assert app.comparison is None and app._navigation_ready
                    if newer == "source":
                        assert app.selected_range().start == app.selected_range().end == 5
                        assert app.focused is app.query_one("#source")
                    elif newer in ("filter", "timeline"):
                        assert app.selected_entry.commit == fixture.origin
                        assert_source(app, fixture.origin, fixture.path, BODY)
                    elif newer == "trace":
                        assert app.timeline.selection.start == app.timeline.selection.end == 11
                        assert app.traced_snapshot.commit == fixture.merge
                        assert app.traced_snapshot == trace_anchor
                        assert app.timeline.commit == trace_anchor.commit
                    elif newer == "file":
                        assert app.timeline is None
                        assert_source(app, fixture.merge, fixture.path, fixture.merged_text)
                    elif newer == "invalid-filter":
                        expected = list(settled)
                        expected[11] = "after:2024-02-30"
                        assert scene(app) == tuple(expected)
                        assert app.query_one("#prompt-row").display
                        assert app.focused is app.query_one("#history-filter", Input)
                        assert "YYYY-MM-DD" in content(app, "#status")
                        assert app.current_visit() == settled[26]
                        app.save_bookmark("Retained source after invalid filter")
                        assert app.saved_bookmarks()["Retained source after invalid filter"] == settled[26].to_dict()
                        app.query_one("#history-filter", Input).value = ""
                        await pilot.press("escape")
                        await settle(app, pilot)
                    else:
                        assert isinstance(app.screen, HelpScreen)
                        await pilot.press("escape")
                        await settle(app, pilot)
            finally:
                released.set()
                await settle(app, pilot)
            monkeypatch.setattr(app, "prepare_comparison", original)
        await app.load_file(fixture.path, LineRange(2, 2))
        await settle(app, pilot)
        started, released = threading.Event(), threading.Event()
        reachable = app.repository.reachable_merges

        def slow_picker(*args, **kwargs):
            started.set()
            assert released.wait(10), "Test did not release merge discovery."
            return reachable(*args, **kwargs)

        monkeypatch.setattr(app.repository, "reachable_merges", slow_picker)
        app.action_merges()
        try:
            assert await asyncio.to_thread(started.wait, 5)
            app.select_range(LineRange(5, 5))
            await pilot.pause()
            released.set()
            await settle(app, pilot)
            assert app.screen is app.default_screen and app.comparison is None
            assert app.selected_range().start == app.selected_range().end == 5
            assert app._navigation_ready and app.current_visit() is not None
        finally:
            released.set()
            await settle(app, pilot)
    assert repo.state() == before


async def test_parent_diff_jumps_are_transactional_and_newer_side_source_parent_or_modal_wins(two_parent_merge, monkeypatch):
    fixture = two_parent_merge
    repo = fixture.repo
    before = repo.state()
    app = TrailApp(Repository(repo.root), InitialSelection(fixture.path))
    async with app.run_test(size=(160, 48)) as pilot:
        await settle(app, pilot)
        await command(app, pilot, "parent 2")
        await command(app, pilot, "split")
        original = app.repository.parent_diff_source
        for newer in ("side", "source", "parent", "modal", "failure"):
            await focus(app, pilot, "#source")
            app.request_parent(fixture.merge, fixture.path, 2, mode="after")
            await settle(app, pilot)
            raw = raw_index(app, "-    base = value + 1")
            app.select_diff_index(raw, "before")
            await focus(app, pilot, "#diff-scroll")
            settled = scene(app)
            journal = tuple(app.journal.visits), app.journal.index
            started, released = threading.Event(), threading.Event()

            def delayed(comparison, line, before=False):
                started.set()
                assert released.wait(10), "Test did not release diff source."
                if newer == "failure":
                    raise TrailError("Chosen parent blob became unavailable.")
                return original(comparison, line, before)

            monkeypatch.setattr(app.repository, "parent_diff_source", delayed)
            app.jump_diff_source()
            try:
                assert await asyncio.to_thread(started.wait, 5)
                if newer == "side":
                    app.select_diff_index(raw, "after")
                elif newer == "source":
                    await focus(app, pilot, "#source")
                    app.select_range(LineRange(5, 5))
                elif newer == "parent":
                    app.request_parent(fixture.merge, fixture.path, 1)
                elif newer == "modal":
                    app.action_help()
                await pilot.pause()
                released.set()
                await settle(app, pilot)
                assert app._navigation_ready and app.current_visit() is not None
                if newer == "parent":
                    assert_comparison(app, repo, fixture.merge, fixture.path, 1)
                elif newer == "source":
                    assert_source(app, fixture.merge, fixture.path, fixture.merged_text, 5)
                    assert app.focused is app.query_one("#source")
                    assert app.comparison == settled[5]
                elif newer == "side":
                    assert app.comparison == settled[5] and app.snapshot == settled[1]
                    assert app._diff_cursor == raw and app._diff_side == "after"
                    assert app.focused is app.query_one("#diff-scroll")
                    assert (tuple(app.journal.visits), app.journal.index) == journal
                else:
                    assert scene(app) == settled
                    if newer == "modal":
                        assert isinstance(app.screen, HelpScreen)
                        await pilot.press("escape")
                        await settle(app, pilot)
                    else:
                        assert "Chosen parent blob" in content(app, "#status")
                        assert app.focused is app.query_one("#diff-scroll")
                app.save_bookmark("Settled after pending jump")
            finally:
                released.set()
                await settle(app, pilot)
            monkeypatch.setattr(app.repository, "parent_diff_source", original)
        app.request_parent(fixture.merge, fixture.path, 2)
        await settle(app, pilot)
        app.select_diff_index(raw_index(app, "-    base = value + 1"), "before")
        await command(app, pilot, "diffbefore")
        assert_source(app, fixture.second, fixture.path, fixture.second_text, 2)
        assert_comparison(app, repo, fixture.merge, fixture.path, 2)
    assert repo.state() == before


async def test_retracing_chosen_before_and_restoring_bookmarks_use_canonical_objects(two_parent_merge, tmp_path):
    fixture = two_parent_merge
    repo = fixture.repo
    tree = repo.git("rev-parse", f"{fixture.origin}^{{tree}}").stdout.decode().strip()
    replacement = repo.git("commit-tree", tree, "-p", fixture.origin, "-m", "Replacement source").stdout.decode().strip()
    repo.git("replace", fixture.second, replacement)
    before = repo.state()
    replacement_refs = repo.git("for-each-ref", "--format=%(refname) %(objectname)", "refs/replace").stdout
    ordinary = Repository(repo.root)
    assert ordinary.revision_text(fixture.path, fixture.second) == BODY
    store = StateStore(tmp_path / "canonical-settings" / "state.json")
    app = TrailApp(ordinary, InitialSelection(fixture.path), store=store)
    async with app.run_test(size=(160, 48)) as pilot:
        await settle(app, pilot)
        await command(app, pilot, "parent 2")
        await command(app, pilot, "before")
        assert_source(app, fixture.second, fixture.path, fixture.second_text)
        await focus(app, pilot, "#source")
        app.select_range(LineRange(11, 11))
        await pilot.pause()
        await pilot.press("enter")
        await settle(app, pilot)
        assert app.comparison is None
        assert (app.timeline.commit, app.timeline.path) == (fixture.second, fixture.path)
        assert [item.commit for item in app.timeline.entries] == [fixture.second, fixture.origin]
        assert app.traced_snapshot.text == fixture.second_text
        await command(app, pilot, "after")
        assert_source(app, fixture.second, fixture.path, fixture.second_text, 11)
        traced = app.current_visit()
        assert traced.canonical is True and traced.to_dict()["canonical"] is True
        assert "comparison_commit" not in traced.to_dict()
        app.save_bookmark("Canonical tax history")
        await app.load_file(fixture.path)
        await settle(app, pilot)
        assert app.current_visit().canonical is False
        await command(app, pilot, "open Canonical tax history")
        assert app.current_visit() == traced
        assert_source(app, fixture.second, fixture.path, fixture.second_text, 11)
        await command(app, pilot, "before")
        assert_source(app, fixture.origin, fixture.path, BODY, 11)
        await command(app, pilot, "after")
        assert_source(app, fixture.second, fixture.path, fixture.second_text, 11)
        assert app.current_visit() == traced
        await pilot.press("ctrl+q")
    saved = StateStore(store.path)
    assert saved.data["bookmarks"][str(repo.root.resolve())]["Canonical tax history"] == traced.to_dict()
    assert saved.data["sessions"][str(repo.root.resolve())] == traced.to_dict()
    assert ordinary.revision_text(fixture.path, fixture.second) == BODY
    assert repo.git("for-each-ref", "--format=%(refname) %(objectname)", "refs/replace").stdout == replacement_refs
    assert repo.state() == before
