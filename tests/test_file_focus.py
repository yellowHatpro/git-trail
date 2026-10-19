from __future__ import annotations

import asyncio
import threading

import pytest
from textual.widgets import Input, Static

from git_trail.app import InitialSelection, SourceView, TrailApp
from git_trail.explorer import FileExplorer
from git_trail.git import Repository
from test_explorer import (
    click_row, explorer_repo, focus, highlight, observe_file_work, scene, settle,
)


def assert_source(app, path, commit, text):
    assert app.snapshot is not None
    assert app.snapshot.path == path and app.snapshot.commit == commit
    source = app.query_one("#source", SourceView)
    assert source.read_only and source.text == text
    title = app.query_one("#source-title", Static).content
    assert path in title.plain and commit[:7] in title.plain
    assert app._navigation_ready


async def click_pending(app, pilot, path):
    explorer = await highlight(app, pilot, path)
    x = explorer.content_region.x - explorer.region.x + 2
    y = explorer.highlighted - int(explorer.scroll_y)
    y += explorer.content_region.y - explorer.region.y
    assert await pilot.click(explorer, offset=(x, y))
    await pilot.pause()
    return explorer


async def wait_started(event):
    assert await asyncio.wait_for(asyncio.to_thread(event.wait, 2), timeout=3), "The controlled load did not start."


def delay_snapshot(app, monkeypatch, path):
    started, released, finished = threading.Event(), threading.Event(), threading.Event()
    original = app.repository.snapshot

    def snapshot(requested, *args, **kwargs):
        if str(requested) != path:
            return original(requested, *args, **kwargs)
        started.set()
        assert released.wait(8), "The test did not release the controlled snapshot."
        try:
            return original(requested, *args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(app.repository, "snapshot", snapshot)
    return started, released, finished


async def test_tree_mouse_clicks_keep_browser_focus_and_keyboard_or_direct_loads_focus_source(explorer_repo, monkeypatch):
    fixture = explorer_repo
    before = fixture.repo.state()
    app = TrailApp(Repository(fixture.repo.root, fixture.commit), InitialSelection("ROOT.txt"))
    async with app.run_test(size=(140, 40)) as pilot:
        await settle(app, pilot, path="ROOT.txt")
        groups, snapshots = observe_file_work(app, monkeypatch)
        current = scene(app)
        await click_row(app, pilot, "a_folder", directory=True)
        explorer = app.query_one("#files", FileExplorer)
        assert app.focused is explorer
        assert scene(app) == current and not groups and not snapshots
        for _ in range(2):
            await click_row(app, pilot, "a_folder/main.py")
            assert app.focused is explorer
            assert_source(app, "a_folder/main.py", fixture.commit, "VALUE = 'alpha committed'\n")
        assert snapshots == ["a_folder/main.py", "a_folder/main.py"]
        assert groups == ["file", "file"]
        current = scene(app)
        await click_row(app, pilot, "a_folder", directory=True)
        assert app.focused is explorer and scene(app) == current
        assert snapshots == ["a_folder/main.py", "a_folder/main.py"]
        assert groups == ["file", "file"]
        await highlight(app, pilot, "A_other", directory=True)
        await pilot.press("enter")
        await settle(app, pilot)
        assert app.focused is explorer and scene(app) == current
        await highlight(app, pilot, "A_other/main.py")
        await pilot.press("enter")
        await settle(app, pilot, path="A_other/main.py")
        assert app.focused is app.query_one("#source", SourceView)
        assert_source(app, "A_other/main.py", fixture.commit, "VALUE = 'beta committed'\n")
        assert snapshots == ["a_folder/main.py", "a_folder/main.py", "A_other/main.py"]
        assert groups == ["file", "file", "file"]
        await focus(app, pilot)
        await app.load_file("ROOT.txt")
        await settle(app, pilot, path="ROOT.txt")
        assert app.focused is app.query_one("#source", SourceView)
        assert_source(app, "ROOT.txt", fixture.commit, "root committed source\nsecond root line\n")
    assert fixture.repo.state() == before


async def test_clicking_file_search_result_closes_prompt_keeps_browser_and_preserves_keyboard_submission(explorer_repo):
    fixture = explorer_repo
    before = fixture.repo.state()
    app = TrailApp(Repository(fixture.repo.root, fixture.commit), InitialSelection("ROOT.txt"))
    async with app.run_test(size=(140, 40)) as pilot:
        await settle(app, pilot, path="ROOT.txt")
        await pilot.press("ctrl+o")
        field = app.query_one("#file-filter", Input)
        assert app.focused is field
        field.value = "main ext:py"
        await settle(app, pilot)
        explorer = app.query_one("#files", FileExplorer)
        assert explorer.search_mode
        assert set(app.filtered_files) == {"a_folder/main.py", "A_other/main.py"}
        await click_row(app, pilot, "a_folder/main.py")
        assert app.focused is explorer
        assert not app.query_one("#prompt-row").display and not explorer.search_mode
        assert explorer.selected_row.path == "a_folder/main.py"
        assert field.value == "main ext:py"
        assert_source(app, "a_folder/main.py", fixture.commit, "VALUE = 'alpha committed'\n")
        await pilot.press("ctrl+o")
        assert app.focused is field
        field.value = "A_other ext:py"
        await settle(app, pilot)
        assert app.filtered_files == ("A_other/main.py",)
        await pilot.press("enter")
        await settle(app, pilot, path="A_other/main.py")
        assert app.focused is app.query_one("#source", SourceView)
        assert not app.query_one("#prompt-row").display and not explorer.search_mode
        assert explorer.selected_row.path == "A_other/main.py"
        assert_source(app, "A_other/main.py", fixture.commit, "VALUE = 'beta committed'\n")
    assert fixture.repo.state() == before


@pytest.mark.parametrize("destination", ["source", "timeline"])
async def test_delayed_mouse_load_preserves_pane_selected_by_ctrl_w(explorer_repo, monkeypatch, destination):
    fixture = explorer_repo
    before = fixture.repo.state()
    app = TrailApp(Repository(fixture.repo.root, fixture.commit), InitialSelection("ROOT.txt"))
    async with app.run_test(size=(140, 40)) as pilot:
        await settle(app, pilot, path="ROOT.txt")
        await click_row(app, pilot, "a_folder", directory=True)
        started, released, finished = delay_snapshot(app, monkeypatch, "a_folder/main.py")
        try:
            explorer = await click_pending(app, pilot, "a_folder/main.py")
            await wait_started(started)
            assert app.focused is explorer and app.snapshot is None
            await pilot.press("ctrl+w", "l")
            await pilot.pause()
            assert app.focused is app.query_one("#source", SourceView)
            if destination == "timeline":
                await pilot.press("ctrl+w", "j")
                await pilot.pause()
            chosen = app.query_one("#" + destination)
            assert app.focused is chosen
            released.set()
            await settle(app, pilot, path="a_folder/main.py")
            assert finished.is_set()
            assert app.focused is chosen
            assert_source(app, "a_folder/main.py", fixture.commit, "VALUE = 'alpha committed'\n")
        finally:
            released.set()
    assert fixture.repo.state() == before


async def test_newer_keyboard_file_open_wins_over_an_older_delayed_mouse_request(explorer_repo, monkeypatch):
    fixture = explorer_repo
    before = fixture.repo.state()
    app = TrailApp(Repository(fixture.repo.root, fixture.commit), InitialSelection("ROOT.txt"))
    async with app.run_test(size=(140, 40)) as pilot:
        await settle(app, pilot, path="ROOT.txt")
        await click_row(app, pilot, "a_folder", directory=True)
        await click_row(app, pilot, "A_other", directory=True)
        started, released, finished = delay_snapshot(app, monkeypatch, "a_folder/main.py")
        try:
            explorer = await click_pending(app, pilot, "a_folder/main.py")
            await wait_started(started)
            assert app.focused is explorer
            await highlight(app, pilot, "A_other/main.py")
            await pilot.press("enter")
            await settle(app, pilot, path="A_other/main.py")
            source = app.query_one("#source", SourceView)
            assert app.focused is source
            assert_source(app, "A_other/main.py", fixture.commit, "VALUE = 'beta committed'\n")
            latest = scene(app), tuple(app.journal.visits), app.journal.index, app.current_visit()
            assert not any(visit.path == "a_folder/main.py" for visit in app.journal.visits)
            released.set()
            await wait_started(finished)
            await settle(app, pilot, path="A_other/main.py")
            assert app.focused is source
            assert (scene(app), tuple(app.journal.visits), app.journal.index, app.current_visit()) == latest
            assert explorer.selected_row.path == "A_other/main.py"
            assert_source(app, "A_other/main.py", fixture.commit, "VALUE = 'beta committed'\n")
        finally:
            released.set()
    assert fixture.repo.state() == before


async def test_mouse_open_in_follow_session_keeps_browser_and_delayed_history_respects_new_focus(repo, monkeypatch):
    starter = (
        "def starter(values):\n"
        "    positive = [value for value in values if value > 0]\n"
        "    total = sum(positive)\n"
        "    count = len(positive)\n"
        "    average = total / max(count, 1)\n"
        "    return round(average, 2)\n"
    )
    chosen = starter.replace("starter", "chosen")
    repo.write("starter.py", starter)
    repo.write("chosen.py", chosen)
    origin = repo.commit("Introduce functions")
    chosen = chosen.replace("round(average, 2)", "round(average, 3)")
    repo.write("chosen.py", chosen)
    edited = repo.commit("Change chosen function")
    before = repo.state()
    app = TrailApp(Repository(repo.root), InitialSelection("starter.py", follow_symbol=True))
    async with app.run_test(size=(140, 40)) as pilot:
        await settle(app, pilot, path="starter.py")
        assert app.timeline.mode == "symbol"
        assert app.focused is app.query_one("#timeline")
        await click_row(app, pilot, "chosen.py")
        explorer = app.query_one("#files", FileExplorer)
        assert app.focused is explorer
        assert app.timeline.mode == "symbol"
        assert [entry.commit for entry in app.timeline.entries] == [edited, origin]
        assert app.selected_entry.commit == edited
        assert_source(app, "chosen.py", edited, chosen)
        started, released = threading.Event(), threading.Event()
        original = app.repository.symbol_history

        def history(snapshot, selection, **kwargs):
            started.set()
            assert released.wait(8), "The test did not release controlled symbol history."
            return original(snapshot, selection, **kwargs)

        monkeypatch.setattr(app.repository, "symbol_history", history)
        try:
            await click_pending(app, pilot, "chosen.py")
            await wait_started(started)
            assert app.focused is explorer and app.timeline is None
            await pilot.press("ctrl+w", "l")
            await pilot.pause()
            source = app.query_one("#source", SourceView)
            assert app.focused is source
            released.set()
            await settle(app, pilot, path="chosen.py")
            assert app.focused is source
            assert app.timeline.mode == "symbol"
            assert [entry.commit for entry in app.timeline.entries] == [edited, origin]
            assert app.selected_entry.commit == edited
            assert_source(app, "chosen.py", edited, chosen)
        finally:
            released.set()
    assert repo.state() == before
