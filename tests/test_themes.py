from __future__ import annotations

import asyncio
import copy
import json

import pytest
from rich.console import Console
from rich.syntax import Syntax
from rich.text import Text
from textual.worker import WorkerCancelled
from textual.widgets import Input, OptionList, Static

from git_trail.app import InitialSelection, SourceView, ThemeScreen, TrailApp
from git_trail.git import LineRange, Repository
from git_trail.state import DEFAULTS, StateStore
from git_trail.theme import PALETTES


# Colors come from the official Catppuccin palette and Gruvbox color files.
COLORS = {
    "tokyo-night": dict(
        dark=True, background="#1a1b26", sidebar="#16161e", foreground="#c0caf5",
        accent="#7aa2f7", purple="#bb9af7", green="#9ece6a", red="#f7768e",
        orange="#ff9e64", muted="#565f89", selection="#283457",
        heading="#a5b4fc", status="#9cafbf",
    ),
    "catppuccin-latte": dict(
        dark=False, background="#eff1f5", sidebar="#e6e9ef", foreground="#4c4f69",
        accent="#1e66f5", purple="#8839ef", green="#40a02b", red="#d20f39",
        orange="#fe640b", muted="#6c6f85", selection="#bcc0cc",
        heading="#7287fd", status="#5c5f77",
    ),
    "catppuccin-frappe": dict(
        dark=True, background="#303446", sidebar="#292c3c", foreground="#c6d0f5",
        accent="#8caaee", purple="#ca9ee6", green="#a6d189", red="#e78284",
        orange="#ef9f76", muted="#a5adce", selection="#51576d",
        heading="#babbf1", status="#b5bfe2",
    ),
    "catppuccin-macchiato": dict(
        dark=True, background="#24273a", sidebar="#1e2030", foreground="#cad3f5",
        accent="#8aadf4", purple="#c6a0f6", green="#a6da95", red="#ed8796",
        orange="#f5a97f", muted="#a5adcb", selection="#494d64",
        heading="#b7bdf8", status="#b8c0e0",
    ),
    "catppuccin-mocha": dict(
        dark=True, background="#1e1e2e", sidebar="#181825", foreground="#cdd6f4",
        accent="#89b4fa", purple="#cba6f7", green="#a6e3a1", red="#f38ba8",
        orange="#fab387", muted="#a6adc8", selection="#45475a",
        heading="#b4befe", status="#bac2de",
    ),
    "gruvbox-dark": dict(
        dark=True, background="#282828", sidebar="#1d2021", foreground="#ebdbb2",
        accent="#83a598", purple="#d3869b", green="#b8bb26", red="#fb4934",
        orange="#fe8019", muted="#928374", selection="#504945",
        heading="#fabd2f", status="#d5c4a1",
    ),
    "gruvbox-light": dict(
        dark=False, background="#fbf1c7", sidebar="#f9f5d7", foreground="#3c3836",
        accent="#076678", purple="#8f3f71", green="#79740e", red="#9d0006",
        orange="#af3a03", muted="#928374", selection="#d5c4a1",
        heading="#b57614", status="#504945",
    ),
}

LEGACY_PREFERENCES = {
    "sidebar_width": 28, "sidebar_visible": True,
    "find_case": True, "find_regex": False, "keybindings": {"t": "files"},
}


@pytest.fixture
def themed_history(repo):
    path = "src/target.py"
    repo.write(path, 'def target():\n    # theme fixture\n    count = 1\n    message = "old"\n    return count, message\n')
    repo.commit("Introduce target")
    repo.write(path, 'def target():\n    # theme fixture\n    count = 2\n    message = "new"\n    return count, message\n')
    repo.commit("Change target")
    return repo, path


async def settle(app, pilot, *, path=None):
    async def idle():
        for _ in range(12):
            await pilot.pause()
            outcomes = await asyncio.gather(
                *(worker.wait() for worker in tuple(app.workers)),
                return_exceptions=True,
            )
            for outcome in outcomes:
                if isinstance(outcome, WorkerCancelled):
                    continue
                if isinstance(outcome, BaseException):
                    raise outcome
            await pilot.pause()
            ready = path is None or (
                app.snapshot is not None and app.snapshot.path == path and app._navigation_ready
            )
            if not app.workers and ready:
                return
        pytest.fail(f"Theme UI did not settle with source {path!r}.")

    await asyncio.wait_for(idle(), timeout=15)


async def focus(app, pilot, selector="#source"):
    widget = app.query_one(selector)
    widget.focus()
    await pilot.pause()
    assert app.focused is widget
    return widget


async def command(app, pilot, value):
    await focus(app, pilot)
    await pilot.press(":")
    field = app.query_one("#command-input", Input)
    assert app.focused is field
    field.value = value
    await pilot.press("enter")
    await settle(app, pilot)


def rgb(color):
    assert color is not None
    return color.get_truecolor().hex.casefold()


def text_style(text, offset):
    return text.get_style_at_offset(Console(), offset)


def strip_style(strip, offset):
    assert 0 <= offset < len(strip.text)
    position = 0
    for segment in strip:
        end = position + len(segment.text)
        if position <= offset < end:
            assert segment.style is not None
            return segment.style
        position = end
    pytest.fail(f"No rendered style at offset {offset}.")


def source_style(source, row, column):
    y = row - int(source.scroll_y)
    assert 0 <= y < source.scrollable_content_region.height
    return strip_style(source.render_line(y), source.gutter_width + column)


def scene(app):
    source = app.query_one("#source", SourceView)
    diff = app.query_one("#diff", Static).content
    raw = diff.code if isinstance(diff, Syntax) else diff.plain
    return (
        app.snapshot, app.traced_snapshot, app.snapshot_repository, app.timeline,
        app.selected_entry, app.filtered_entries, app.source_mode,
        source.text, source.selection, source.language,
        app._diff_key, app._diff_cursor, app._diff_side, app._diff_active, app._diff_rows,
        app.diff_layout, app.diff_effective_layout, raw,
        app._search_matches, app._match_index, app.query_one("#find-input", Input).value,
        app._valid_history_filter, tuple(app.journal.visits), app.journal.index,
        app.current_visit(), tuple(app.workers),
    )


def forbid_io(*args, **kwargs):
    pytest.fail("Changing colors must use the displayed source and cached commit data.")


def assert_source_colors(source, expected):
    assert source.styles.background.hex.casefold() == expected["background"]
    assert source.styles.color.hex.casefold() == expected["foreground"]
    assert source.read_only and source.show_cursor
    assert rgb(source_style(source, 0, 0).color) == expected["purple"]
    assert rgb(source_style(source, 0, 4).color) == expected["accent"]
    assert rgb(source_style(source, 1, 4).color) == expected["muted"]
    assert rgb(source_style(source, 2, 12).color) == expected["orange"]
    assert rgb(source_style(source, 3, 15).color) == expected["green"]
    assert rgb(source_style(source, 4, 4).color) == expected["purple"]
    gutter = strip_style(source.render_line(0), 0)
    assert rgb(gutter.color) == expected["muted"]
    assert rgb(gutter.bgcolor) == expected["background"]
    cursor = source_style(source, 4, 0)
    assert rgb(cursor.color) == expected["background"]
    assert rgb(cursor.bgcolor) == expected["foreground"]


@pytest.mark.parametrize("name", tuple(COLORS))
async def test_all_palettes_recolor_real_views_without_reloading_and_persist_the_choice(
    themed_history, tmp_path, monkeypatch, name,
):
    repo, path = themed_history
    before_repository = repo.state()
    store = StateStore(tmp_path / "settings" / "state.json")
    store.data["preferences"] = copy.deepcopy(LEGACY_PREFERENCES)
    store.data["sessions"]["other-repository"] = {"keep": "session"}
    store.data["bookmarks"]["other-repository"] = {"keep": "bookmark"}
    app = TrailApp(Repository(repo.root), InitialSelection(path, LineRange(1, 5)), store=store)
    expected = COLORS[name]
    async with app.run_test(size=(180, 48)) as pilot:
        await settle(app, pilot, path=path)
        source = await focus(app, pilot)
        app.query_one("#find-input", Input).value = "message"
        await settle(app, pilot)
        assert len(app._search_matches) == 2
        app.show_match(1)
        source.move_cursor((4, 0))
        changed = next(i for i, line in enumerate(app._diff_patch.lines) if line.kind == "delete")
        app.select_diff_index(changed, "before")
        await settle(app, pilot)
        original = scene(app)
        context_plain = app.query_one("#commit-details", Static).content.plain
        monkeypatch.setattr(Repository, "_git", forbid_io)
        monkeypatch.setattr(source, "load_text", forbid_io)
        monkeypatch.setattr(app, "refresh_search", forbid_io)
        monkeypatch.setattr(app, "run_worker", forbid_io)
        await command(app, pilot, "theme " + name)
        assert scene(app) == original
        assert app.theme == name
        assert app.get_theme(name).dark is expected["dark"]
        assert app.has_class("-dark-mode") is expected["dark"]
        assert app.has_class("-light-mode") is not expected["dark"]
        assert_source_colors(source, expected)
        assert app.screen.styles.background.hex.casefold() == expected["background"]
        assert app.query_one("#files-pane").styles.background.hex.casefold() == expected["sidebar"]
        assert app.query_one("#workspace-title").styles.color.hex.casefold() == expected["accent"]
        assert app.query_one("#mode").styles.background.hex.casefold() == expected["accent"]
        status = app.query_one("#status", Static).content
        assert rgb(text_style(status, 0).color) == expected["status"]
        context = app.query_one("#commit-details", Static).content
        assert context.plain == context_plain
        assert rgb(text_style(context, 0).color) == expected["heading"]
        author_color = "#e2e8f0" if name == "tokyo-night" else expected["foreground"]
        assert rgb(text_style(context, len(app.selected_entry.commit) + 1).color) == author_color
        unified = app.query_one("#diff", Static).content
        assert isinstance(unified, Syntax)
        highlighted = unified.highlight(unified.code)
        deletion = highlighted.plain.index("-    count = 1")
        addition = highlighted.plain.index("+    count = 2")
        assert rgb(text_style(highlighted, deletion).color) == expected["red"]
        assert rgb(text_style(highlighted, addition).color) == expected["green"]
        assert rgb(text_style(highlighted, deletion).bgcolor) == expected["selection"]
        assert rgb(text_style(highlighted, 0).bgcolor) == expected["background"]
        app.set_diff_layout("split")
        await settle(app, pilot)
        assert app.diff_effective_layout == "split"
        split = app.query_one("#diff", Static).content
        assert isinstance(split, Text)
        deletion = split.plain.index("-    count = 1")
        addition = split.plain.index("+    count = 2")
        assert rgb(text_style(split, deletion).color) == expected["red"]
        assert rgb(text_style(split, addition).color) == expected["green"]
        assert rgb(text_style(split, deletion).bgcolor) == expected["selection"]
        assert app._diff_cursor == changed and app._diff_side == "before"
        split_scene = scene(app)
        assert app.apply_theme(name)
        await settle(app, pilot)
        assert scene(app) == split_scene
        await focus(app, pilot)
        await pilot.press(":")
        field = app.query_one("#command-input", Input)
        assert app.focused is field
        assert field.styles.background.hex.casefold() == expected["sidebar"]
        assert field.styles.color.hex.casefold() == expected["foreground"]
        assert app.query_one("#prompt-label").styles.color.hex.casefold() == expected["accent"]
        await pilot.press("escape")
        await settle(app, pilot)
        app.exit()
    saved = StateStore(store.path)
    assert not saved.warning and not saved.read_only
    assert saved.data["preferences"] == dict(LEGACY_PREFERENCES, theme=name)
    assert saved.data["sessions"]["other-repository"] == {"keep": "session"}
    assert saved.data["bookmarks"]["other-repository"] == {"keep": "bookmark"}
    assert saved.data["sessions"][str(repo.root.resolve())]["display_start"] == 5
    assert repo.state() == before_repository


async def test_theme_picker_filters_keeps_input_literal_and_supports_keyboard_mouse_cancel(themed_history):
    repo, path = themed_history
    before_repository = repo.state()
    app = TrailApp(Repository(repo.root), InitialSelection(path, LineRange(1, 5)))
    async with app.run_test(size=(140, 42)) as pilot:
        await settle(app, pilot, path=path)
        original = scene(app)
        assert "theme" not in app.preferences
        await command(app, pilot, "theme")
        assert isinstance(app.screen, ThemeScreen)
        field = app.screen.query_one("#theme-filter", Input)
        options = app.screen.query_one("#theme-options", OptionList)
        assert app.focused is field
        assert options.option_count == 7
        await pilot.press("j", "k")
        assert field.value == "jk"
        assert app.screen.filtered_names == ()
        field.value = "catppuccin"
        await settle(app, pilot)
        assert app.screen.filtered_names == (
            "catppuccin-latte", "catppuccin-frappe", "catppuccin-macchiato", "catppuccin-mocha",
        )
        await pilot.press("ctrl+n", "down", "ctrl+p", "up")
        assert options.highlighted == 0 and app.focused is field
        await pilot.press("ctrl+n", "enter")
        await settle(app, pilot)
        assert not isinstance(app.screen, ThemeScreen)
        assert app.theme == "catppuccin-frappe"
        assert scene(app) == original
        await command(app, pilot, "theme")
        field = app.screen.query_one("#theme-filter", Input)
        options = app.screen.query_one("#theme-options", OptionList)
        assert app.screen.query_one("#theme-dialog").styles.background.hex.casefold() == "#292c3c"
        assert field.styles.color.hex.casefold() == "#c6d0f5"
        assert rgb(strip_style(options.render_line(1), 0).color) == "#c6d0f5"
        options.focus()
        await pilot.pause()
        assert app.focused is options
        await pilot.press("j", "k")
        assert options.highlighted is not None
        assert options.get_option_at_index(options.highlighted).id == "catppuccin-frappe"
        assert field.value == ""
        await pilot.press("escape")
        await settle(app, pilot)
        assert app.theme == "catppuccin-frappe"
        assert scene(app) == original
        await command(app, pilot, "theme")
        field = app.screen.query_one("#theme-filter", Input)
        field.value = "gruvbox light"
        await settle(app, pilot)
        options = app.screen.query_one("#theme-options", OptionList)
        assert options.option_count == 1
        assert options.get_option_at_index(0).id == "gruvbox-light"
        await pilot.click("#theme-options", offset=(1, 0))
        await settle(app, pilot)
        assert app.theme == "gruvbox-light"
        assert app.screen.styles.background.hex.casefold() == "#fbf1c7"
        assert scene(app) == original
    assert repo.state() == before_repository


@pytest.mark.parametrize("native", [False, True], ids=["apply-theme", "native-theme"])
async def test_switching_while_picker_is_open_preserves_compact_main_diff_projection(themed_history, native):
    repo, path = themed_history
    before_repository = repo.state()
    app = TrailApp(Repository(repo.root), InitialSelection(path, LineRange(1, 5)))
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot, path=path)
        main = app.screen
        assert main.has_class("compact")
        source = app.query_one("#source", SourceView)
        source.move_cursor((2, 4))
        source.move_cursor((3, 11), select=True)
        changed = next(i for i, line in enumerate(app._diff_patch.lines) if line.kind == "delete")
        app.select_diff_index(changed, "before")
        await settle(app, pilot)
        compact = app.query_one("#diff", Static).content
        assert isinstance(compact, Syntax) and compact.code.startswith("@@")
        assert app.selected_entry.patch.startswith("diff")
        before = scene(app)
        await command(app, pilot, "theme")
        modal = app.screen
        assert isinstance(modal, ThemeScreen) and modal is not main
        field = modal.query_one("#theme-filter", Input)
        assert app.focused is field
        if native:
            app.theme = "catppuccin-latte"
        else:
            assert app.apply_theme("catppuccin-latte")
        await settle(app, pilot)
        assert app.screen is modal and app.focused is field
        assert modal.query_one("#theme-dialog").styles.background.hex.casefold() == "#e6e9ef"
        assert field.styles.color.hex.casefold() == "#4c4f69"
        assert main.styles.background.hex.casefold() == "#eff1f5"
        assert rgb(source_style(source, 2, 6).bgcolor) == "#bcc0cc"
        assert app.query_one("#diff", Static).content.code.startswith("@@")
        assert scene(app) == before
        await pilot.press("escape")
        await settle(app, pilot)
        assert app.screen is main
        assert scene(app) == before
    assert repo.state() == before_repository


async def test_theme_command_aliases_invalid_names_and_native_switch_are_safe(themed_history):
    repo, path = themed_history
    app = TrailApp(Repository(repo.root), InitialSelection(path, LineRange(1, 5)))
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(app, pilot, path=path)
        original = scene(app)
        for alias, canonical in (
            ("LATTE", "catppuccin-latte"), ("Frappé", "catppuccin-frappe"),
            ("catppuccin_macchiato", "catppuccin-macchiato"), ("Mocha", "catppuccin-mocha"),
            ("Gruvbox", "gruvbox-dark"), ("GRUVBOX LIGHT", "gruvbox-light"),
            ("Tokyo Night", "tokyo-night"), ("default", "tokyo-night"),
        ):
            await command(app, pilot, "theme " + alias)
            assert app.theme == canonical
            assert scene(app) == original
        preferences_before = copy.deepcopy(app.preferences)
        await command(app, pilot, "theme [red]unknown")
        field = app.query_one("#command-input", Input)
        assert app.focused is field and app.query_one("#prompt-row").display
        assert field.value == "theme [red]unknown"
        assert "Unknown theme" in app.query_one("#status", Static).content.plain
        assert app.preferences == preferences_before and scene(app) == original
        field.value = "theme gruvbox-light"
        await pilot.press("enter")
        await settle(app, pilot)
        assert app.theme == "gruvbox-light"
        assert not app.query_one("#prompt-row").display
        app.theme = "catppuccin-mocha"
        await settle(app, pilot)
        assert app.query_one("#source", SourceView).styles.background.hex.casefold() == "#1e1e2e"
        assert app.preferences["theme"] == "catppuccin-mocha"
        assert scene(app) == original


@pytest.mark.parametrize("value", [None, "", "unknown-theme", 7, [], {"name": "latte"}])
def test_unknown_saved_theme_drops_only_optional_key_and_preserves_legacy_state(tmp_path, value):
    path = tmp_path / "state.json"
    data = {
        "version": 1, "preferences": dict(LEGACY_PREFERENCES, theme=value),
        "sessions": {"keep": {"session": 1}}, "bookmarks": {"keep": {"bookmark": 2}},
        "future": {"preserve": True},
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    store = StateStore(path)
    assert not store.warning and not store.read_only
    assert store.data["preferences"] == LEGACY_PREFERENCES
    store.save()
    restored = json.loads(path.read_text(encoding="utf-8"))
    assert restored["preferences"] == LEGACY_PREFERENCES
    assert restored["sessions"] == data["sessions"]
    assert restored["bookmarks"] == data["bookmarks"]
    assert restored["future"] == data["future"]


@pytest.mark.parametrize("name", ["catppuccin-latte", "gruvbox-dark"])
async def test_saved_theme_is_applied_at_startup_without_resetting_other_preferences(themed_history, tmp_path, name):
    repo, path = themed_history
    store = StateStore(tmp_path / "state.json")
    store.data["preferences"] = dict(LEGACY_PREFERENCES, theme=name)
    store.save()
    restored = StateStore(store.path)
    app = TrailApp(Repository(repo.root), InitialSelection(path), store=restored)
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(app, pilot, path=path)
        assert app.theme == name
        assert app.query_one("#source", SourceView).styles.background.hex.casefold() == COLORS[name]["background"]
        assert app.screen.styles.background.hex.casefold() == COLORS[name]["background"]
        assert app.preferences == dict(LEGACY_PREFERENCES, theme=name)
        assert app.sidebar_width == 28
        app.exit()
    assert StateStore(store.path).data["preferences"] == dict(LEGACY_PREFERENCES, theme=name)


def test_theme_objects_are_independent_and_legacy_defaults_remain_exact():
    assert tuple(PALETTES) == tuple(COLORS)
    assert DEFAULTS == {
        "sidebar_width": 24, "sidebar_visible": True,
        "find_case": False, "find_regex": False, "keybindings": {},
    }
    for name, palette in PALETTES.items():
        first_app, second_app = palette.app_theme(), palette.app_theme()
        first_editor, second_editor = palette.editor_theme(), palette.editor_theme()
        assert first_app is not second_app
        assert first_app.variables is not second_app.variables
        first_app.variables["trail-background"] = "#010203"
        assert second_app.variables["trail-background"] == COLORS[name]["background"]
        assert first_editor is not second_editor
        assert first_editor.syntax_styles is not second_editor.syntax_styles
        first_editor.syntax_styles.clear()
        assert second_editor.syntax_styles
        first_diff, second_diff = palette.diff_theme(), palette.diff_theme()
        assert first_diff is not second_diff
        assert palette.diff_style() is not palette.diff_style()
    assert DEFAULTS.get("theme") is None


async def test_two_apps_keep_editor_and_diff_colors_independent(themed_history):
    repo, path = themed_history
    first = TrailApp(Repository(repo.root), InitialSelection(path, LineRange(1, 5)))
    second = TrailApp(Repository(repo.root), InitialSelection(path, LineRange(1, 5)))
    async with first.run_test(size=(140, 40)) as first_pilot:
        await settle(first, first_pilot, path=path)
        assert first.apply_theme("catppuccin-latte")
        await settle(first, first_pilot)
        first_source = first.query_one("#source", SourceView)
        first_render = first.query_one("#diff", Static).content
        first_colors = first_render.highlight(first_render.code)
        deletion = first_colors.plain.index("-    count = 1")
        assert rgb(text_style(first_colors, deletion).color) == "#d20f39"
        async with second.run_test(size=(140, 40)) as second_pilot:
            await settle(second, second_pilot, path=path)
            assert second.apply_theme("gruvbox-dark")
            await settle(second, second_pilot)
            assert first_source.styles.background.hex.casefold() == "#eff1f5"
            assert second.query_one("#source", SourceView).styles.background.hex.casefold() == "#282828"
            assert first.preferences["theme"] == "catppuccin-latte"
            assert second.preferences["theme"] == "gruvbox-dark"
            second_render = second.query_one("#diff", Static).content
            second_colors = second_render.highlight(second_render.code)
            deletion = second_colors.plain.index("-    count = 1")
            assert rgb(text_style(second_colors, deletion).color) == "#fb4934"
        await settle(first, first_pilot)
        rendered = first.query_one("#diff", Static).content
        colors = rendered.highlight(rendered.code)
        assert rgb(text_style(colors, colors.plain.index("-    count = 1")).color) == "#d20f39"
        assert first_source.styles.background.hex.casefold() == "#eff1f5"
