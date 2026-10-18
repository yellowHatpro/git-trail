from __future__ import annotations

from textual.widgets import Input, OptionList, Static, TabbedContent

from git_trail.app import HelpScreen, InitialSelection, SourceView, SymbolScreen, TrailApp
from git_trail.git import LineRange, Repository


async def settle(app, pilot):
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


def content(app, selector):
    value = app.query_one(selector, Static).content
    return value.plain if hasattr(value, "plain") else str(value)


async def command(app, pilot, value):
    await pilot.press(":")
    field = app.query_one("#command-input", Input)
    assert app.focused is field
    field.value = value
    await pilot.press("enter")
    await settle(app, pilot)


async def test_command_prompt_literal_keys_jump_errors_help_and_quit_keep_source_read_only(range_history):
    fixture = range_history
    before = fixture.repo.state()
    app = TrailApp(Repository(fixture.repo.root), InitialSelection(fixture.path))
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(app, pilot)
        source = app.query_one("#source", SourceView)
        source_text = source.text
        await pilot.press(":")
        field = app.query_one("#command-input", Input)
        assert app.query_one("#prompt-row").size.height == field.size.height == 1
        assert content(app, "#prompt-label").strip() == ":"
        await pilot.press(*"qjkv/")
        assert field.value == "qjkv/"
        assert app.return_code is None
        assert source.text == source_text
        await pilot.press("enter")
        await pilot.pause()
        assert "Unknown command" in content(app, "#status")
        assert app.focused is field
        assert content(app, "#mode") == "PROMPT"
        for value, message in (
            ("0", "1 to 9"),
            ("10", "1 to 9"),
            ("99999999999999999999", "too large"),
            ("²", "Unknown command"),
            ("set mystery", "Settings"),
        ):
            field.value = value
            await pilot.press("enter")
            await pilot.pause()
            assert message in content(app, "#status")
            assert app.focused is field
            assert source.text == source_text
        field.value = "5"
        await pilot.press("enter")
        await pilot.pause()
        assert source.cursor_location == (4, 0)
        assert app.focused is source
        assert not app.query_one("#prompt-row").display
        assert content(app, "#mode") == "NORMAL"
        await command(app, pilot, "help")
        assert isinstance(app.screen, HelpScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, HelpScreen)
        assert app.focused is source
        await pilot.press(":", "q", "enter")
        assert app.return_code == 0
    assert fixture.repo.state() == before


async def test_case_and_regex_commands_change_search_results_and_recover_invalid_queries(repo):
    text = 'def target():\n    value = "TARGET"\n    return target\n'
    repo.write("search.py", text)
    repo.commit("Command search corpus")
    app = TrailApp(Repository(repo.root), InitialSelection("search.py"))
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(app, pilot)
        source = app.query_one("#source", SourceView)
        await command(app, pilot, "set noignorecase")
        await pilot.press("/")
        field = app.query_one("#find-input", Input)
        field.value = "target"
        await pilot.pause()
        assert content(app, "#find-count") == "1/2"
        await pilot.press("enter", "n")
        assert source.selected_text == "target"
        assert source.selection.start == (2, 11)
        await command(app, pilot, "set ignorecase")
        await pilot.press("/")
        field.value = "target"
        await pilot.pause()
        assert content(app, "#find-count") == "1/3"
        await pilot.press("enter")
        await command(app, pilot, "set regex")
        await pilot.press("/")
        field.value = r"target(?=\()"
        await pilot.pause()
        assert content(app, "#find-count") == "1/1"
        assert source.selection.start == (0, 4)
        field.value = "["
        await pilot.pause()
        assert content(app, "#find-count") == "Invalid"
        assert "Invalid expression" in content(app, "#status")
        await pilot.press("escape")
        await command(app, pilot, "set noregex")
        await pilot.press("/")
        field.value = "["
        await pilot.pause()
        assert content(app, "#find-count") == "0 matches"
        field.value = "target"
        await pilot.pause()
        assert content(app, "#find-count") == "1/3"
        await pilot.press("enter", "N")
        assert source.selection.start == (2, 11)
        assert source.text == text
        assert not app.query_one("#prompt-row").display


async def test_history_filter_and_view_commands_preserve_commit_source_mapping_and_recovery(range_history):
    fixture = range_history
    before = fixture.repo.state()
    app = TrailApp(Repository(fixture.repo.root), InitialSelection(fixture.path, LineRange(4, 7)))
    async with app.run_test(size=(140, 45)) as pilot:
        await settle(app, pilot)
        day = app.timeline.entries[1].committed_at[:10]
        query = f'author:"Trail Fixture" hash:{fixture.insert[:10]} after:{day} before:{day} c = 3'
        await command(app, pilot, f"filter {query}")
        assert [entry.commit for entry in app.filtered_entries] == [fixture.insert]
        assert app.selected_entry.commit == app.snapshot.commit == fixture.insert
        assert app.query_one("#timeline", OptionList).get_option_at_index(0).id == fixture.insert
        assert "+    c = 3" in app.query_one("#diff", Static).content.code
        assert app.focused is app.query_one("#timeline")
        await command(app, pilot, "filter absent-commit")
        assert app.selected_entry is None
        assert app.snapshot is None
        assert app.query_one("#source", SourceView).text == ""
        assert "No matching changes" in content(app, "#diff")
        await command(app, pilot, "filter")
        assert [entry.commit for entry in app.filtered_entries] == fixture.relevant
        assert app.selected_entry.commit == fixture.delete
        await command(app, pilot, "filter after:2024-02-30")
        field = app.query_one("#command-input", Input)
        assert app.focused is field
        assert "YYYY-MM-DD" in content(app, "#status")
        assert [entry.commit for entry in app.filtered_entries] == fixture.relevant
        field.value = "before"
        await pilot.press("enter")
        await settle(app, pilot)
        assert app.source_mode == "before"
        assert app.snapshot.commit == fixture.insert
        assert "b = 2" in app.query_one("#source", SourceView).text
        await command(app, pilot, "starting")
        assert app.source_mode == "starting"
        assert app.snapshot.commit == fixture.delete
        await command(app, pilot, "after")
        assert app.source_mode == "after"
        assert app.snapshot.commit == fixture.delete
        await command(app, pilot, "commit")
        assert app.query_one("#review-tabs", TabbedContent).active == "commit-tab"
        assert fixture.delete in content(app, "#commit-details")
        await command(app, pilot, "diff")
        assert app.query_one("#review-tabs", TabbedContent).active == "diff-tab"
        await command(app, pilot, "sidebar")
        assert not app.query_one("#files-pane").display
        await command(app, pilot, "sidebar")
        assert app.query_one("#files-pane").display
    assert fixture.repo.state() == before


async def test_leader_file_and_symbol_pickers_support_result_keys_without_stealing_text(repo):
    source_text = "def alpha():\n    pass\n\ndef beta():\n    pass\n\nclass Gamma:\n    pass\n"
    repo.write("a.txt", "notes\n")
    repo.write("code.py", source_text)
    repo.write("z.txt", "last\n")
    repo.commit("Picker choices")
    app = TrailApp(Repository(repo.root), InitialSelection("code.py"))
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(app, pilot)
        source = app.query_one("#source", SourceView)
        source.move_cursor((0, 0))
        await pilot.press("space", "f")
        assert not app.query_one("#prompt-row").display
        assert app.query_one("#statusline").size.height == 1
        assert "files" in content(app, "#status")
        await pilot.press("escape", "f")
        assert not app.query_one("#prompt-row").display
        assert source.cursor_location == (0, 0)
        await pilot.press("space", "f", "f")
        field = app.query_one("#file-filter", Input)
        options = app.query_one("#files", OptionList)
        assert app.focused is field
        assert options.highlighted == 0
        await pilot.press("ctrl+n")
        assert options.highlighted == 1
        await pilot.press("down")
        assert options.highlighted == 2
        await pilot.press("ctrl+p")
        assert options.highlighted == 1
        await pilot.press("up")
        assert options.highlighted == 0
        assert app.focused is field
        assert field.value == ""
        await pilot.press("ctrl+n", "enter")
        await settle(app, pilot)
        assert app.snapshot.path == "code.py"
        assert app.focused is source
        await pilot.press("space", "f", "s")
        assert isinstance(app.screen, SymbolScreen)
        symbol_field = app.screen.query_one("#symbol-filter", Input)
        symbol_options = app.screen.query_one("#symbol-options", OptionList)
        await pilot.press("ctrl+n", "down")
        assert symbol_options.highlighted == 2
        await pilot.press("ctrl+p", "up")
        assert symbol_options.highlighted == 0
        assert app.focused is symbol_field
        assert symbol_field.value == ""
        await pilot.press(*"beta")
        await pilot.pause()
        assert symbol_field.value == "beta"
        assert symbol_options.option_count == 1
        await pilot.press("enter")
        await pilot.pause()
        assert not isinstance(app.screen, SymbolScreen)
        assert (app.selected_range().start, app.selected_range().end) == (4, 5)
        assert source.selected_text == "def beta():\n    pass"
        assert source.text == source_text
        await command(app, pilot, "symbols")
        assert isinstance(app.screen, SymbolScreen)
        await pilot.press("escape")
        await pilot.pause()
        await command(app, pilot, "file cd ext:py")
        field = app.query_one("#file-filter", Input)
        assert app.focused is field
        assert field.value == "cd ext:py"
        assert app.filtered_files == ("code.py",)
        await pilot.press("escape")
        await command(app, pilot, "files")
        assert app.query_one("#file-filter", Input).value == "cd ext:py"
        assert app.filtered_files == ("code.py",)
        await pilot.press("enter")
        await settle(app, pilot)
        assert app.snapshot.path == "code.py"


async def test_leader_history_prompt_navigation_keeps_input_and_selected_sha_consistent(range_history):
    fixture = range_history
    app = TrailApp(Repository(fixture.repo.root), InitialSelection(fixture.path, LineRange(4, 7)))
    async with app.run_test(size=(140, 45)) as pilot:
        await settle(app, pilot)
        source = app.query_one("#source", SourceView)
        source.focus()
        await pilot.pause()
        assert app.focused is source
        await pilot.press("space", "f", "h")
        field = app.query_one("#history-filter", Input)
        options = app.query_one("#timeline", OptionList)
        assert app.focused is field
        await pilot.press("ctrl+n")
        await settle(app, pilot)
        assert options.highlighted == 1
        assert app.selected_entry.commit == app.snapshot.commit == fixture.insert
        await pilot.press("down")
        await settle(app, pilot)
        assert options.highlighted == 2
        assert app.selected_entry.commit == fixture.edit
        await pilot.press("ctrl+p", "up")
        await settle(app, pilot)
        assert options.highlighted == 0
        assert app.selected_entry.commit == fixture.delete
        assert app.focused is field
        assert field.value == ""
        assert content(app, "#mode") == "PROMPT"
        assert app.query_one("#prompt-row").size.height == 1
        field.value = f"hash:{fixture.insert[:10]}"
        await settle(app, pilot)
        assert len(app.filtered_entries) == 1
        assert app.selected_entry.commit == fixture.insert
        await pilot.press("enter")
        await settle(app, pilot)
        assert not app.query_one("#prompt-row").display
        assert app.focused is options
        assert options.get_option_at_index(0).id == fixture.insert
        assert app.selected_entry.commit == app.snapshot.commit == fixture.insert
