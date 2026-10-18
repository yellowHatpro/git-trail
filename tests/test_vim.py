from __future__ import annotations

from textual.widgets import Input, OptionList, Static

from git_trail.app import InitialSelection, SourceView, TrailApp
from git_trail.git import LineRange, Repository


async def settle(app, pilot):
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


def content(app, selector):
    value = app.query_one(selector, Static).content
    return value.plain if hasattr(value, "plain") else str(value)


async def test_normal_motions_words_counts_short_lines_and_real_end_of_file(repo):
    text = "alpha beta\n  gamma\n\nμword next\nz\n"
    repo.write("motion.txt", text)
    repo.commit("Motion corpus")
    before = repo.state()
    app = TrailApp(Repository(repo.root), InitialSelection("motion.txt"))
    async with app.run_test(size=(140, 40)) as pilot:
        await settle(app, pilot)
        source = app.query_one("#source", SourceView)
        source.move_cursor((0, 0))
        await pilot.press("h")
        assert source.cursor_location == (0, 0)
        await pilot.press("l", "2", "l")
        assert source.cursor_location == (0, 3)
        await pilot.press("0", "w")
        assert source.cursor_location == (0, 6)
        await pilot.press("w")
        assert source.cursor_location == (1, 2)
        await pilot.press("b")
        assert source.cursor_location == (0, 6)
        source.move_cursor((0, 2))
        await pilot.press("w", "b")
        assert source.cursor_location == (0, 0)
        source.move_cursor((0, 8))
        await pilot.press("b")
        assert source.cursor_location == (0, 6)
        await pilot.press("g", "g", "3", "j", "$")
        assert source.cursor_location == (3, 9)
        await pilot.press("j")
        assert source.cursor_location == (4, 0)
        await pilot.press("k")
        assert source.cursor_location == (3, 9)
        await pilot.press("k")
        assert source.cursor_location == (2, 0)
        await pilot.press("k")
        assert source.cursor_location == (1, 6)
        await pilot.press("k")
        assert source.cursor_location == (0, 9)
        source.move_cursor((2, 0))
        await pilot.press("w")
        assert source.cursor_location == (3, 0)
        await pilot.press("w", "b")
        assert source.cursor_location == (3, 0)
        await pilot.press("G")
        assert source.cursor_location == (4, 0)
        assert app.selected_range().start == app.selected_range().end == 5
        await pilot.press("2", "G")
        assert source.cursor_location == (1, 0)
        await pilot.press("4", "g", "g")
        assert source.cursor_location == (3, 0)
        await pilot.press("g", "g", "$", "l", "l")
        assert source.cursor_location == (0, 9)
        source.move_cursor((0, 0))
        await pilot.press("²", "j")
        assert source.cursor_location == (1, 0)
        source.move_cursor((0, 0))
        await pilot.press("٢", "j")
        assert source.cursor_location == (1, 0)
        await pilot.press("x", "backspace", "delete")
        assert source.text == text
        assert source.read_only
        assert content(app, "#mode") == "NORMAL"
    assert repo.state() == before


async def test_word_motions_treat_unicode_words_and_punctuation_as_separate_targets(repo):
    repo.write("words.txt", "μword.foo + next\n")
    repo.commit("Word boundaries")
    app = TrailApp(Repository(repo.root), InitialSelection("words.txt"))
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(app, pilot)
        source = app.query_one("#source", SourceView)
        for column in (5, 6, 10, 12):
            await pilot.press("w")
            assert source.cursor_location == (0, column)
        await pilot.press("3", "b")
        assert source.cursor_location == (0, 5)
        await pilot.press("b")
        assert source.cursor_location == (0, 0)
        await pilot.press("2", "w")
        assert source.cursor_location == (0, 6)
        await pilot.press("2", "b")
        assert source.cursor_location == (0, 0)


async def test_visual_character_and_reverse_line_selection_are_inclusive_and_trace_correct_rows(range_history):
    fixture = range_history
    app = TrailApp(Repository(fixture.repo.root), InitialSelection(fixture.path))
    async with app.run_test(size=(140, 45)) as pilot:
        await settle(app, pilot)
        source = app.query_one("#source", SourceView)
        source.move_cursor((3, 1))
        await pilot.press("v")
        assert source.selected_text == "e"
        assert content(app, "#mode") == "VISUAL"
        await pilot.press("l")
        assert source.selected_text == "ef"
        await pilot.press("h", "h")
        assert source.selected_text == "de"
        await pilot.press("escape")
        assert source.selected_text == ""
        assert source.cursor_location == (3, 0)
        assert content(app, "#mode") == "NORMAL"
        await pilot.press("v", "right")
        await pilot.pause()
        assert source.selected_text == ""
        assert content(app, "#mode") == "NORMAL"
        await pilot.press("v")
        assert content(app, "#mode") == "VISUAL"
        assert await pilot.click("#source", offset=(8, 1))
        await pilot.pause()
        assert source.selected_text == ""
        assert content(app, "#mode") == "NORMAL"
        source.move_cursor((6, 4))
        await pilot.press("V", "3", "k")
        assert content(app, "#mode") == "V-LINE"
        assert source.selected_text == "def target():\n    a = 7\n    c = 3\n    return a + b\n"
        assert (app.selected_range().start, app.selected_range().end) == (4, 7)
        await pilot.press("j")
        assert (app.selected_range().start, app.selected_range().end) == (5, 7)
        await pilot.press("k", "enter")
        await settle(app, pilot)
        assert (app.timeline.selection.start, app.timeline.selection.end) == (4, 7)
        assert [entry.commit for entry in app.timeline.entries] == fixture.relevant
        assert content(app, "#mode") == "NORMAL"
        assert app.query_one("#timeline", OptionList).option_count == 4


async def test_visual_line_mode_selects_final_lf_row_without_extending_git_range(repo):
    repo.write("last.txt", "first\nsecond\n")
    commit = repo.commit("Two committed lines")
    app = TrailApp(Repository(repo.root), InitialSelection("last.txt"))
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(app, pilot)
        source = app.query_one("#source", SourceView)
        await pilot.press("G", "V")
        assert source.selected_text == "second\n"
        assert (app.selected_range().start, app.selected_range().end) == (2, 2)
        await pilot.press("enter")
        await settle(app, pilot)
        assert app.timeline.selection.start == app.timeline.selection.end == 2
        assert [entry.commit for entry in app.timeline.entries] == [commit]


async def test_slash_find_enters_prompt_then_normal_n_and_N_wrap_with_query_closed(repo):
    text = 'def target():\n    value = "TARGET"\n    return target\n'
    repo.write("search.py", text)
    repo.commit("Search corpus")
    before = repo.state()
    app = TrailApp(Repository(repo.root), InitialSelection("search.py"))
    async with app.run_test(size=(140, 40)) as pilot:
        await settle(app, pilot)
        source = app.query_one("#source", SourceView)
        await pilot.press("/")
        field = app.query_one("#find-input", Input)
        assert app.focused is field
        assert content(app, "#mode") == "PROMPT"
        await pilot.press(*"target")
        await pilot.pause()
        assert field.value == "target"
        assert (source.selection.start, source.selection.end) == ((0, 4), (0, 10))
        assert content(app, "#find-count") == "1/3"
        await pilot.press("enter")
        await pilot.pause()
        assert app.focused is source
        assert content(app, "#mode") == "NORMAL"
        assert not app.query_one("#prompt-row").display
        assert app.timeline is None
        await pilot.press("n")
        assert source.selected_text == "TARGET"
        assert content(app, "#find-count") == "2/3"
        await pilot.press("n", "n")
        assert (source.selection.start, source.selection.end) == ((0, 4), (0, 10))
        await pilot.press("N")
        assert (source.selection.start, source.selection.end) == ((2, 11), (2, 17))
        assert content(app, "#find-count") == "3/3"
        assert not app.query_one("#prompt-row").display
        assert source.text == text
    assert repo.state() == before


async def test_prompt_typing_keeps_normal_commands_literal_and_clears_unfinished_motion(repo):
    text = "first\nsecond\nthird\n"
    repo.write("input.txt", text)
    repo.commit("Prompt corpus")
    app = TrailApp(Repository(repo.root), InitialSelection("input.txt"))
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(app, pilot)
        source = app.query_one("#source", SourceView)
        source.move_cursor((2, 0))
        await pilot.press("g", "/")
        field = app.query_one("#find-input", Input)
        await pilot.press(*"jkqvVhnN/:")
        await pilot.pause()
        assert field.value == "jkqvVhnN/:"
        assert app.focused is field
        assert content(app, "#mode") == "PROMPT"
        assert source.text == text
        assert source.cursor_location == (2, 0)
        await pilot.press("escape", "g")
        assert source.cursor_location == (2, 0)
        assert app.focused is source
        await pilot.press("g")
        assert source.cursor_location == (0, 0)
        await pilot.press("2", "ctrl+w", "h", "ctrl+w", "l", "j")
        assert source.cursor_location == (1, 0)
        assert source.text == text


async def test_ctrl_w_spatial_and_cycle_navigation_and_list_vim_motions(range_history):
    fixture = range_history
    app = TrailApp(Repository(fixture.repo.root), InitialSelection(fixture.path, LineRange(4, 7)))
    async with app.run_test(size=(140, 45)) as pilot:
        await settle(app, pilot)
        await pilot.press("ctrl+w", "k")
        assert app.focused is app.query_one("#source")
        for key, selector in (
            ("h", "#files"), ("l", "#source"), ("j", "#timeline"),
            ("l", "#diff-scroll"), ("h", "#timeline"), ("k", "#source"),
            ("j", "#timeline"),
        ):
            await pilot.press("ctrl+w", key)
            assert app.focused is app.query_one(selector)
            assert content(app, "#mode") == "NORMAL"
        await pilot.press("j")
        await settle(app, pilot)
        assert app.selected_entry.commit == fixture.insert
        await pilot.press("G")
        await settle(app, pilot)
        assert app.selected_entry.commit == fixture.origin
        await pilot.press("g", "g")
        await settle(app, pilot)
        assert app.selected_entry.commit == fixture.delete
        await pilot.press("ctrl+w", "l", "ctrl+w", "k")
        assert app.focused is app.query_one("#source")
        for selector in ("#timeline", "#diff-scroll", "#files", "#source"):
            await pilot.press("ctrl+w", "w")
            assert app.focused is app.query_one(selector)
        await pilot.press("f2", "ctrl+w", "h")
        assert app.query_one("#files-pane").display
        assert app.focused is app.query_one("#files")


async def test_file_and_historical_reload_reset_visual_mode_and_pending_motion(range_history):
    fixture = range_history
    app = TrailApp(Repository(fixture.repo.root), InitialSelection(fixture.path, LineRange(4, 7)))
    async with app.run_test(size=(140, 45)) as pilot:
        await settle(app, pilot)
        source = app.query_one("#source", SourceView)
        source.focus()
        source.move_cursor((4, 4))
        await pilot.press("V", "j")
        assert content(app, "#mode") == "V-LINE"
        await pilot.press("ctrl+j")
        await settle(app, pilot)
        assert app.snapshot.commit == fixture.insert
        assert content(app, "#mode") == "NORMAL"
        source.focus()
        source.move_cursor((5, 4))
        await pilot.press("g", "ctrl+o")
        field = app.query_one("#file-filter", Input)
        field.value = "unrelated"
        await pilot.press("enter")
        await settle(app, pilot)
        assert app.snapshot.path == "unrelated.txt"
        assert source.text == "Different file\n"
        assert content(app, "#mode") == "NORMAL"
        await pilot.press("g")
        assert source.cursor_location == (0, 0)
        await pilot.press("g", "V")
        assert source.selected_text == "Different file\n"
        await pilot.press("escape", "right")
        assert source.cursor_location == (0, 1)
        assert source.selected_text == ""
