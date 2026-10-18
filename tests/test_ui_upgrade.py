from __future__ import annotations

import asyncio
import threading

import pytest
from textual import events
from textual.widgets import Checkbox, Input, OptionList, Static, TabbedContent

from git_trail.app import InitialSelection, SourceView, SymbolScreen, TrailApp
from git_trail.git import LineRange, Repository, TrailError
from git_trail.widgets import PaneDivider


async def settle(app, pilot):
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


def content(app, selector):
    value = app.query_one(selector, Static).content
    return value.plain if hasattr(value, "plain") else str(value)


def selection(source):
    return source.selection.start, source.selection.end


async def test_sidebar_mount_keyboard_and_mouse_scroll_reach_offscreen_files(repo):
    for index in range(100):
        repo.write(f"src/file_{index:03}.py", f"value = {index}\n")
    repo.commit("Many source files")
    before = repo.state()
    app = TrailApp(Repository(repo.root))
    async with app.run_test(size=(140, 35)) as pilot:
        await settle(app, pilot)
        files = app.query_one("#files", OptionList)
        assert app.query_one("#sidebar-divider", PaneDivider).is_mounted
        assert files.option_count == 100
        assert files.size.height < 100
        files.focus()
        await pilot.press("end")
        await pilot.pause()
        assert files.highlighted == 99
        assert files.scroll_y > 0
        await pilot.press("home")
        await pilot.pause()
        assert files.highlighted == 0
        assert files.scroll_y == 0
        for _ in range(3):
            files.post_message(events.MouseScrollDown(files, 1, 1, 0, 0, 0, False, False, False))
        await pilot.pause()
        assert files.scroll_y > 0
        await pilot.press("end", "enter")
        await settle(app, pilot)
        assert app.snapshot.path == "src/file_099.py"
        assert app.query_one("#source", SourceView).text == "value = 99\n"
    assert repo.state() == before


async def test_sidebar_drag_keyboard_resize_hide_and_narrow_terminal_clamp(repo):
    repo.write("one.py", "value = 1\n")
    repo.commit("One source file")
    app = TrailApp(Repository(repo.root), InitialSelection("one.py"))
    async with app.run_test(size=(140, 40)) as pilot:
        await settle(app, pilot)
        divider = app.query_one("#sidebar-divider", PaneDivider)
        pane = app.query_one("#files-pane")
        initial = pane.size.width
        start_x, start_y = divider.region.x, divider.region.y + 3
        assert await pilot.mouse_down("#sidebar-divider", offset=(0, 3))
        await pilot.hover(offset=(start_x + 12, start_y))
        await pilot.mouse_up(offset=(start_x + 12, start_y))
        await pilot.pause()
        assert pane.size.width == initial + 12
        divider.focus()
        await pilot.press("left")
        await pilot.pause()
        assert pane.size.width == initial + 10
        await pilot.press("ctrl+shift+left")
        await pilot.pause()
        assert pane.size.width == initial + 8
        await pilot.press("ctrl+shift+right")
        await pilot.pause()
        assert pane.size.width == initial + 10
        await pilot.press("f2")
        await pilot.pause()
        assert not pane.display
        assert not divider.display
        await pilot.press("ctrl+o")
        await pilot.pause()
        assert pane.display and divider.display
        assert app.focused is app.query_one("#file-filter", Input)
        await pilot.resize_terminal(80, 24)
        await pilot.pause()
        assert 16 <= pane.size.width <= 25
        assert app.query_one("#source", SourceView).size.width > 0
        assert app.screen.has_class("compact")


async def test_source_find_function_name_case_regex_wrap_and_invalid_recovery(repo):
    repo.write("code.py", 'def target():\n    value = "TARGET"\n    return target\n')
    repo.commit("Searchable source")
    app = TrailApp(Repository(repo.root), InitialSelection("code.py"))
    async with app.run_test(size=(140, 42)) as pilot:
        await settle(app, pilot)
        source = app.query_one("#source", SourceView)
        assert source.language == "python"
        await pilot.press("ctrl+f")
        await pilot.pause()
        field = app.query_one("#find-input", Input)
        assert app.focused is field
        assert app.query_one("#find-bar").display
        field.value = "target"
        await pilot.pause()
        assert content(app, "#find-count") == "1/3"
        assert selection(source) == ((0, 4), (0, 10))
        await pilot.press("f3")
        await pilot.pause()
        assert selection(source) == ((1, 13), (1, 19))
        await pilot.press("shift+f3", "shift+f3")
        await pilot.pause()
        assert content(app, "#find-count") == "3/3"
        assert selection(source) == ((2, 11), (2, 17))
        app.query_one("#find-case", Checkbox).value = True
        await pilot.pause()
        assert content(app, "#find-count") == "1/2"
        app.query_one("#find-regex", Checkbox).value = True
        field.value = r"target(?=\()"
        await pilot.pause()
        assert content(app, "#find-count") == "1/1"
        assert selection(source) == ((0, 4), (0, 10))
        field.value = "["
        await pilot.pause()
        assert content(app, "#find-count") == "Invalid"
        assert "Invalid expression" in content(app, "#status")
        field.value = "absent"
        await pilot.pause()
        assert content(app, "#find-count") == "0 matches"
        await pilot.press("enter")
        await pilot.pause()
        assert app.timeline is None
        field.value = ""
        await pilot.pause()
        assert content(app, "#find-count") == ""
        await pilot.press("escape")
        await pilot.pause()
        assert not app.query_one("#find-bar").display
        assert app.focused is source
        assert source.text == 'def target():\n    value = "TARGET"\n    return target\n'


async def test_fuzzy_file_filter_and_symbol_search_open_the_matching_selection(repo):
    repo.write("src/price_calculator.py", "def total_price():\n    return 42\n\nclass Receipt:\n    pass\n")
    repo.write("src/player.py", "def play():\n    return None\n")
    repo.write("src/price_calculator.ts", "export const totalPrice = 42;\n")
    repo.commit("Source choices")
    app = TrailApp(Repository(repo.root))
    async with app.run_test(size=(140, 42)) as pilot:
        await settle(app, pilot)
        field = app.query_one("#file-filter", Input)
        field.value = "pcr ext:py"
        await pilot.pause()
        assert app.filtered_files == ("src/price_calculator.py",)
        await pilot.press("enter")
        await settle(app, pilot)
        assert app.snapshot.path == "src/price_calculator.py"
        await pilot.press("ctrl+b")
        await pilot.pause()
        assert isinstance(app.screen, SymbolScreen)
        symbol_filter = app.screen.query_one("#symbol-filter", Input)
        symbol_filter.value = "receipt"
        await pilot.pause()
        assert app.screen.query_one("#symbol-options", OptionList).option_count == 1
        await pilot.press("enter")
        await pilot.pause()
        assert not isinstance(app.screen, SymbolScreen)
        assert app.selected_range().start == 4
        assert app.selected_range().end == 5


async def test_combined_history_filter_preserves_sha_mapping_and_recovers_from_no_matches(range_history):
    fixture = range_history
    app = TrailApp(Repository(fixture.repo.root), InitialSelection(fixture.path, LineRange(4, 7)))
    async with app.run_test(size=(140, 45)) as pilot:
        await settle(app, pilot)
        day = app.timeline.entries[1].committed_at[:10]
        await pilot.press("ctrl+l")
        await pilot.pause()
        field = app.query_one("#history-filter", Input)
        assert app.focused is field
        field.value = f'author:"Trail Fixture" hash:{fixture.insert[:10]} after:{day} before:{day} c = 3'
        await settle(app, pilot)
        assert [item.commit for item in app.filtered_entries] == [fixture.insert]
        assert app.selected_entry.commit == fixture.insert
        assert app.query_one("#timeline", OptionList).get_option_at_index(0).id == fixture.insert
        assert app.snapshot.commit == fixture.insert
        assert "+    c = 3" in app.query_one("#diff", Static).content.code
        await pilot.press("ctrl+j", "ctrl+k")
        await settle(app, pilot)
        assert app.selected_entry.commit == fixture.insert
        field.value = "no-such-change"
        await settle(app, pilot)
        assert not app.filtered_entries
        assert app.selected_entry is None
        assert app.query_one("#source", SourceView).text == ""
        assert "No matching changes" in content(app, "#diff")
        field.value = ""
        await settle(app, pilot)
        assert [item.commit for item in app.filtered_entries] == fixture.relevant
        assert app.selected_entry.commit == fixture.delete
        field.value = "after:2024-02-30"
        await pilot.pause()
        assert "YYYY-MM-DD" in content(app, "#status")
        assert [item.commit for item in app.filtered_entries] == fixture.relevant


async def test_historical_source_modes_refresh_find_and_show_root_absence(range_history):
    fixture = range_history
    app = TrailApp(Repository(fixture.repo.root), InitialSelection(fixture.path, LineRange(4, 7)))
    async with app.run_test(size=(140, 45)) as pilot:
        await settle(app, pilot)
        source = app.query_one("#source", SourceView)
        await pilot.press("ctrl+f")
        await pilot.pause()
        app.query_one("#find-input", Input).value = "b = 2"
        await pilot.pause()
        assert content(app, "#find-count") == "0 matches"
        await pilot.press("ctrl+j")
        await settle(app, pilot)
        assert app.snapshot.commit == fixture.insert
        assert "b = 2" in source.text and "c = 3" in source.text
        assert content(app, "#find-count") == "1/1"
        await pilot.press("f4")
        await settle(app, pilot)
        assert app.source_mode == "before"
        assert app.snapshot.commit == fixture.unrelated
        assert "b = 2" in source.text and "c = 3" not in source.text
        await pilot.press("f4")
        await settle(app, pilot)
        assert app.source_mode == "starting"
        assert app.snapshot.commit == fixture.delete
        assert "c = 3" in source.text and "b = 2" not in source.text
        await pilot.press("f4", "ctrl+j", "ctrl+j")
        await settle(app, pilot)
        assert app.selected_entry.commit == fixture.origin
        assert app.snapshot.commit == fixture.origin
        await pilot.click("#view-before")
        await settle(app, pilot)
        assert app.snapshot is None
        assert source.text == ""
        assert "No earlier source" in content(app, "#source-note")
        await pilot.click("#view-starting")
        await settle(app, pilot)
        assert app.snapshot.commit == fixture.delete
        assert app.traced_snapshot.commit == fixture.delete


async def test_retracing_historical_source_uses_its_revision_without_future_changes(range_history):
    fixture = range_history
    app = TrailApp(Repository(fixture.repo.root), InitialSelection(fixture.path, LineRange(4, 7)))
    async with app.run_test(size=(140, 45)) as pilot:
        await settle(app, pilot)
        await pilot.press("ctrl+j", "ctrl+j")
        await settle(app, pilot)
        assert app.snapshot.commit == fixture.edit
        assert app.snapshot_repository.commit == fixture.edit
        assert app.selected_range().start == 3
        app.query_one("#source", SourceView).focus()
        await pilot.press("ctrl+t")
        await settle(app, pilot)
        assert app.timeline.commit == fixture.edit
        assert [item.commit for item in app.timeline.entries] == [fixture.edit, fixture.origin]
        assert app.traced_snapshot.commit == fixture.edit


async def test_commit_tab_displays_full_message_and_changed_files(repo):
    repo.write("main.py", "value = 1\n")
    repo.write("context.txt", "context\n")
    commit = repo.commit("Introduce value\n\nExplain the business reason.\nKeep the second sentence.")
    before = repo.state()
    app = TrailApp(Repository(repo.root), InitialSelection("main.py", LineRange(1, 1)))
    async with app.run_test(size=(140, 42)) as pilot:
        await settle(app, pilot)
        await pilot.press("ctrl+d")
        await pilot.pause()
        assert app.query_one("#review-tabs", TabbedContent).active == "commit-tab"
        context = content(app, "#commit-details")
        assert commit in context
        assert "Explain the business reason.\nKeep the second sentence." in context
        assert "main.py" in context and "context.txt" in context
        assert "Committed" in context and "Authored" in context
        await pilot.press("ctrl+d")
        await pilot.pause()
        assert app.query_one("#review-tabs", TabbedContent).active == "diff-tab"
    assert repo.state() == before


async def test_opening_new_file_resets_before_control_to_displayed_source(range_history):
    fixture = range_history
    app = TrailApp(Repository(fixture.repo.root), InitialSelection(fixture.path, LineRange(4, 7)))
    async with app.run_test(size=(140, 45)) as pilot:
        await settle(app, pilot)
        await pilot.click("#view-before")
        await settle(app, pilot)
        assert app.source_mode == "before"
        assert app.query_one("#view-before").has_class("active")
        await app.load_file("unrelated.txt")
        await pilot.pause()
        assert app.snapshot.path == "unrelated.txt"
        assert app.snapshot.commit == fixture.delete
        assert app.query_one("#source", SourceView).text == "Different file\n"
        assert app.source_mode == "after"
        assert app.query_one("#view-after").has_class("active")
        assert not app.query_one("#view-before").has_class("active")


async def test_failed_trace_clears_previous_commit_context_and_title(range_history, monkeypatch):
    fixture = range_history
    app = TrailApp(Repository(fixture.repo.root), InitialSelection(fixture.path, LineRange(4, 7)))
    async with app.run_test(size=(140, 45)) as pilot:
        await settle(app, pilot)
        assert fixture.delete in content(app, "#commit-details")

        def unavailable(snapshot, selected):
            raise TrailError("The repository became unavailable.")

        monkeypatch.setattr(app.snapshot_repository, "history", unavailable)
        app.query_one("#source", SourceView).focus()
        await pilot.press("ctrl+t")
        await settle(app, pilot)
        assert app.timeline is None
        assert app.selected_entry is None
        assert fixture.delete not in content(app, "#commit-details")
        assert fixture.delete[:10] not in content(app, "#diff-title")
        assert "became unavailable" in content(app, "#status")


async def test_pending_new_commit_source_prevents_tracing_previous_revision(range_history, monkeypatch):
    fixture = range_history
    repository = Repository(fixture.repo.root)
    app = TrailApp(repository, InitialSelection(fixture.path, LineRange(4, 7)))
    async with app.run_test(size=(140, 45)) as pilot:
        await settle(app, pilot)
        original = repository.historical_source
        started = threading.Event()
        release = threading.Event()

        def delayed(entry, before=False):
            if entry.commit == fixture.insert:
                started.set()
                if not release.wait(timeout=10):
                    raise RuntimeError("Test did not release pending source")
            return original(entry, before)

        monkeypatch.setattr(repository, "historical_source", delayed)
        original_timeline = app.timeline
        app.show_entry(1)
        try:
            assert await asyncio.to_thread(started.wait, 5)
            await pilot.pause()
            assert app.selected_entry.commit == fixture.insert
            assert app.snapshot is None
            assert app.query_one("#source", SourceView).text == ""
            assert "Loading" in content(app, "#source-note")
            await pilot.press("ctrl+t")
            await pilot.pause()
            assert app.timeline is original_timeline
            assert app.traced_snapshot.commit == fixture.delete
        finally:
            release.set()
            await settle(app, pilot)
        assert app.snapshot.commit == fixture.insert
        assert app.snapshot_repository.commit == fixture.insert


@pytest.mark.parametrize("newer_target", ["entry", "file"])
async def test_delayed_old_historical_view_cannot_replace_newer_selection(range_history, monkeypatch, newer_target):
    fixture = range_history
    repository = Repository(fixture.repo.root)
    app = TrailApp(repository, InitialSelection(fixture.path, LineRange(4, 7)))
    async with app.run_test(size=(140, 45)) as pilot:
        await settle(app, pilot)
        original = repository.historical_source
        started = threading.Event()
        release = threading.Event()

        def delayed(entry, before=False):
            if entry.commit == fixture.edit:
                started.set()
                if not release.wait(timeout=10):
                    raise RuntimeError("Test did not release historical source")
            return original(entry, before)

        monkeypatch.setattr(repository, "historical_source", delayed)
        entry = app.timeline.entries[2]
        app._view_generation += 1
        generation = app._view_generation
        old_task = asyncio.create_task(app.load_entry(entry, generation, "after"))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            if newer_target == "entry":
                app.show_entry(1)
                await settle(app, pilot)
                assert app.snapshot.commit == fixture.insert
            else:
                await app.load_file("unrelated.txt")
                await pilot.pause()
                assert app.snapshot.path == "unrelated.txt"
            release.set()
            await old_task
            await pilot.pause()
            if newer_target == "entry":
                assert app.selected_entry.commit == fixture.insert
                assert app.snapshot.commit == fixture.insert
                assert app.snapshot_repository.commit == fixture.insert
                assert fixture.insert in content(app, "#commit-details")
            else:
                assert app.snapshot.path == "unrelated.txt"
                assert app.query_one("#source", SourceView).text == "Different file\n"
                assert app.timeline is None
                assert fixture.edit not in content(app, "#commit-details")
        finally:
            release.set()
            await old_task
