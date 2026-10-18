from __future__ import annotations

import pytest
from rich.console import Console
from rich.syntax import Syntax
from textual.widgets import Footer, Header, Input, Static

from git_trail.app import InitialSelection, SourceView, TrailApp
from git_trail.git import LineRange, Repository


async def settle(app, pilot):
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


def content(app, selector):
    value = app.query_one(selector, Static).content
    return value.plain if hasattr(value, "plain") else str(value)


@pytest.mark.parametrize("size", [(80, 24), (160, 48)])
async def test_editor_layout_reserves_rows_for_code_and_keeps_bars_single_row(range_history, size):
    app = TrailApp(
        Repository(range_history.repo.root),
        InitialSelection(range_history.path, LineRange(4, 7)),
    )
    async with app.run_test(size=size) as pilot:
        await settle(app, pilot)
        source = app.query_one("#source", SourceView)
        assert not list(app.query(Header))
        assert not list(app.query(Footer))
        assert app.query_one("#workspace-title").size.height == 1
        assert app.query_one("#statusline").size.height == 1
        assert app.query_one("#source-heading").size.height == 1
        assert source.scrollable_content_region.height >= 9
        assert app.query_one("#diff-scroll").scrollable_content_region.height >= 4
        assert source.region.right <= size[0]
        assert app.query_one("#statusline").region.bottom == size[1]
        assert not app.query_one("#prompt-row").display
        assert content(app, "#mode") == "NORMAL"
        for selector in ("#file-filter", "#history-filter", "#find-bar"):
            assert not app.query_one(selector).display
        assert app.query_one("#source-title").region.y == app.query_one("#source-toolbar").region.y


@pytest.mark.parametrize(
    ("keys", "field", "prefix"),
    [
        (("ctrl+o",), "#file-filter", "files >"),
        (("ctrl+l",), "#history-filter", "filter >"),
        (("ctrl+f",), "#find-input", "/"),
    ],
)
async def test_editor_prompts_are_transient_one_row_and_escape_restores_code(range_history, keys, field, prefix):
    app = TrailApp(Repository(range_history.repo.root), InitialSelection(range_history.path))
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        source = app.query_one("#source", SourceView)
        closed_height = source.scrollable_content_region.height
        await pilot.press(*keys)
        await pilot.pause()
        prompt = app.query_one("#prompt-row")
        active = app.query_one(field, Input)
        assert prompt.display
        assert prompt.size.height == active.size.height == 1
        assert app.focused is active
        assert content(app, "#prompt-label").strip() == prefix
        assert content(app, "#mode") == "PROMPT"
        assert source.scrollable_content_region.height >= closed_height - 1
        assert not active.styles.border_top[0]
        assert not active.styles.border_bottom[0]
        await pilot.press("escape")
        await pilot.pause()
        assert not prompt.display
        assert content(app, "#mode") == "NORMAL"
        assert app.focused is source
        assert source.scrollable_content_region.height == closed_height


async def test_compact_diff_starts_at_changed_lines_and_wide_view_retains_metadata(range_history):
    app = TrailApp(
        Repository(range_history.repo.root),
        InitialSelection(range_history.path, LineRange(4, 7)),
    )
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        patch = app.selected_entry.patch
        rendered = app.query_one("#diff", Static).content
        assert isinstance(rendered, Syntax)
        assert rendered.code.startswith("@@ ")
        assert "-    b = 2" in rendered.code
        assert "diff --git" in patch
        assert app.query_one("#diff-scroll").scroll_y == 0
        await pilot.resize_terminal(160, 48)
        await pilot.pause()
        rendered = app.query_one("#diff", Static).content
        assert rendered.code == patch
        assert rendered.code.startswith("diff --git ")
        assert app.selected_entry.patch == patch
        await pilot.resize_terminal(80, 24)
        await pilot.pause()
        assert app.query_one("#diff", Static).content.code.startswith("@@ ")
        assert app.selected_entry.patch == patch


async def test_source_and_diff_render_share_a_readable_editor_background(range_history):
    app = TrailApp(
        Repository(range_history.repo.root),
        InitialSelection(range_history.path, LineRange(4, 7)),
    )
    async with app.run_test(size=(160, 48)) as pilot:
        await settle(app, pilot)
        source = app.query_one("#source", SourceView)
        source_segments = [
            segment for segment in source.render_line(0)
            if segment.text.strip() and segment.style and segment.style.bgcolor
        ]
        assert source_segments
        source_background = source.styles.background.hex.casefold()
        assert any(segment.style.bgcolor.name.casefold() == source_background for segment in source_segments)
        assert all(
            segment.style.color != segment.style.bgcolor
            for segment in source_segments if segment.style.color
        )
        console = Console(width=100)
        rendered = app.query_one("#diff", Static).content
        diff_segments = [
            segment for segment in console.render(rendered)
            if segment.text.strip() and segment.style and segment.style.bgcolor
        ]
        assert diff_segments
        assert all(segment.style.bgcolor.name.casefold() == source_background for segment in diff_segments)
