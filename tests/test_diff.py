from __future__ import annotations

import asyncio
import json

import pytest
from rich.cells import cell_len
from rich.console import Console
from rich.syntax import Syntax
from rich.text import Text
from textual.widgets import Button, Input, Static

from git_trail.app import InitialSelection, SourceView, TrailApp
from git_trail.cli import main
from git_trail.diff import DiffPatch
from git_trail.git import LineRange, Repository


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("@@ -3 +7 @@\n-old\n+new", [("delete", 3, None), ("add", None, 7)]),
        ("@@ -0,0 +1,2 @@\n+first\n+second", [("add", None, 1), ("add", None, 2)]),
        ("@@ -8,2 +0,0 @@\n-first\n-second", [("delete", 8, None), ("delete", 9, None)]),
        ("@@ -5,2 +6,2 @@\n same\n-old\n+new", [("context", 5, 6), ("delete", 6, None), ("add", None, 7)]),
        (
            "@@ -1 +1 @@\n-old\n\\ No newline at end of file\n+new\n\\ No newline at end of file",
            [("delete", 1, None), ("marker", None, None), ("add", None, 1), ("marker", None, None)],
        ),
    ],
)
def test_hunk_coordinates_follow_the_two_source_sides_and_ignore_markers(raw, expected):
    parsed = DiffPatch.parse(raw)
    assert len(parsed.hunks) == 1
    assert parsed.hunks[0].valid
    assert [(line.kind, line.old, line.new) for line in parsed.lines[1:]] == expected
    assert all(line.hunk == 0 for line in parsed.lines)
    assert parsed.hunks[0].header == 0
    assert parsed.hunks[0].changed == next(
        index for index, line in enumerate(parsed.lines) if line.kind in ("delete", "add")
    )
    assert parsed.hunks[0].end == len(parsed.lines)
    assert "\n".join(line.text for line in parsed.lines) == raw


@pytest.mark.parametrize("ending", ["", "\n\n"])
def test_multiple_hunks_reset_coordinates_and_preserve_every_raw_metadata_line(ending):
    raw = (
        "diff --git a/code.py b/code.py\nindex abc..def 100644\n"
        "--- a/code.py\n+++ b/code.py\n"
        "@@ -2,3 +2,4 @@ target\n"
        " context\n-old\n+new\n+inserted\n tail\n"
        "@@ -12 +13 @@ another\n-old last\n+new last"
        + ending
    )
    parsed = DiffPatch.parse(raw)
    assert len(parsed.hunks) == 2
    assert all(hunk.valid for hunk in parsed.hunks)
    assert all(line.old is line.new is None for line in parsed.lines[:4])
    first, second = parsed.hunks
    assert parsed.lines[first.header].kind == parsed.lines[second.header].kind == "hunk"
    assert parsed.lines[first.changed].old == 3
    assert parsed.lines[second.changed].old == 12
    assert parsed.lines[second.changed + 1].new == 13
    assert first.end == second.header
    assert "\n".join(line.text for line in parsed.lines) == raw


@pytest.mark.parametrize(
    "raw",
    [
        "@@ -1,2 +1,2 @@\n one",
        "@@ -1 +1 @@\n-old",
        "@@ -0 +1 @@\n-old\n+new",
        "@@ -1 +0 @@\n-old\n+new",
        "@@ -2,2 +2,2 @@\n-old\n+new\nunexpected text",
        "@@ -1 +1 @@\n-old\n+new\n+surplus",
        "@@ -oops +1 @@\n-old\n+new",
        "@@ -1,-2 +1,2 @@\n-old\n+new",
    ],
)
def test_malformed_truncated_or_overfull_hunks_never_expose_source_coordinates(raw):
    parsed = DiffPatch.parse(raw)
    assert not any(hunk.valid for hunk in parsed.hunks)
    assert all(line.old is None and line.new is None for line in parsed.lines)
    assert "\n".join(line.text for line in parsed.lines) == raw


def test_split_rows_zip_unequal_changes_keep_context_shared_and_preserve_raw_indices():
    raw = (
        "--- a/code.py\n+++ b/code.py\n@@ -1,4 +1,5 @@\n"
        " first\n-old one\n-old two\n+new one\n+new two\n+extra\n last\n"
        "\\ No newline at end of file"
    )
    parsed = DiffPatch.parse(raw)
    assert parsed.hunks[0].valid
    rows = parsed.split_rows()
    changes = [row for row in rows if row.heading is None]
    assert [(row.old, row.new) for row in changes] == [
        (3, 3), (4, 6), (5, 7), (None, 8), (9, 9),
    ]
    assert {index for row in rows for index in row.indices} == set(range(len(parsed.lines)))
    assert [row.old for row in changes if row.old is not None] == [3, 4, 5, 9]
    assert [row.new for row in changes if row.new is not None] == [3, 6, 7, 8, 9]
    assert rows[-1].heading == 10
    assert parsed.lines[rows[-1].heading].kind == "marker"


def test_context_only_move_retains_both_source_coordinates_without_inventing_changed_lines():
    raw = "--- a/old.py\n+++ b/new.py\n@@ -3,2 +1,2 @@\n def target():\n     return 1"
    parsed = DiffPatch.parse(raw)
    assert parsed.hunks[0].valid
    assert parsed.hunks[0].changed == parsed.hunks[0].header == 2
    assert [(line.old, line.new) for line in parsed.lines[3:]] == [(3, 1), (4, 2)]
    assert all(line.kind == "context" for line in parsed.lines[3:])
    assert not any(line.kind in ("delete", "add") for line in parsed.lines)
    rows = parsed.split_rows()
    assert [(row.old, row.new) for row in rows if row.heading is None] == [(3, 3), (4, 4)]


def test_side_by_side_preserves_long_tabbed_cjk_code_and_uses_cell_aligned_literal_paths():
    old = "-\t[bold]old λ[/] " + "界" * 100
    new = "+\t[red]new λ[/] " + "語" * 120
    raw = f"@@ -1 +1 @@\n{old}\n{new}"
    parsed = DiffPatch.parse(raw)
    rendered, rows, separator = parsed.side_by_side("a[red].py\tpart\nextra", "b[/].py", 60)
    assert isinstance(rendered, Text)
    assert rendered.no_wrap
    assert old[:1] + old[1:].expandtabs(4) in rendered.plain
    assert new[:1] + new[1:].expandtabs(4) in rendered.plain
    assert "BEFORE · a[red].py␉part␤extra" in rendered.plain
    assert "AFTER · b[/].py" in rendered.plain
    assert "\t" not in rendered.plain
    assert "界" * 100 in rendered.plain and "語" * 120 in rendered.plain
    paired = next(line for line in rendered.plain.split("\n") if "[bold]old" in line)
    left, right = paired.split(" │ ", 1)
    assert cell_len(left) + 1 == separator
    header_left = rendered.plain.split("\n", 1)[0].split(" │ ", 1)[0]
    assert cell_len(header_left) + 1 == separator
    assert cell_len(left) > 60
    assert "[red]new" in right
    assert {index for row in rows for index in row.indices} == {0, 1, 2}
    assert "\n".join(line.text for line in parsed.lines) == raw


def test_absent_side_labels_compact_projection_and_active_highlight_keep_correct_raw_identity():
    raw = "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1,2 @@\n+first\n+second"
    parsed = DiffPatch.parse(raw)
    rendered, rows, _ = parsed.side_by_side("", "new.py", 80, active=4, compact=True)
    assert "BEFORE · (absent)" in rendered.plain
    assert "AFTER · new.py" in rendered.plain
    assert "/dev/null" not in rendered.plain
    assert {index for row in rows for index in row.indices} == {2, 3, 4}
    row = next(row for row in rows if 4 in row.indices)
    assert row.old is None and row.new == 4
    offset = rendered.plain.index("+second")
    style = rendered.get_style_at_offset(Console(), offset)
    assert style.bgcolor and style.bgcolor.name.casefold() == "#283457"
    first_style = rendered.get_style_at_offset(Console(), rendered.plain.index("+first"))
    assert first_style.bgcolor is None
    assert parsed.lines[row.new].new == 2


async def settle(app, pilot):
    async def idle():
        for _ in range(10):
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            if not app.workers:
                return
        pytest.fail("Diff UI kept spawning workers.")

    await asyncio.wait_for(idle(), timeout=15)


async def command(app, pilot, value):
    await pilot.press(":")
    field = app.query_one("#command-input", Input)
    assert app.focused is field
    field.value = value
    await pilot.press("enter")
    await settle(app, pilot)


async def test_real_git_split_unified_button_and_resize_preserve_raw_patch_coordinates_and_repo_state(range_history, capsys):
    fixture = range_history
    before = fixture.repo.state()
    repository = Repository(fixture.repo.root)
    original = repository.history(repository.snapshot(fixture.path), LineRange(4, 7))
    raw = original.entries[0].patch
    parsed = DiffPatch.parse(raw)
    assert all(hunk.valid for hunk in parsed.hunks)
    removed = next(line for line in parsed.lines if line.text == "-    b = 2")
    assert removed.old == 6 and removed.new is None
    app = TrailApp(repository, InitialSelection(fixture.path, LineRange(4, 7)))
    async with app.run_test(size=(160, 48)) as pilot:
        await settle(app, pilot)
        assert app.diff_effective_layout == "unified"
        assert isinstance(app.query_one("#diff", Static).content, Syntax)
        assert app.query_one("#diff", Static).content.code == raw
        assert not app._diff_active
        active_index = app._diff_cursor
        assert app._diff_patch.lines[active_index].text == "-    b = 2"
        source = app.query_one("#source", SourceView)
        displayed_source = source.text
        await command(app, pilot, "split")
        assert app.query_one("#diff-scroll").scrollable_content_region.width >= 60
        assert app.diff_layout == app.diff_effective_layout == "split"
        assert isinstance(app.query_one("#diff", Static).content, Text)
        assert "BEFORE ·" in app.query_one("#diff", Static).content.plain
        assert any(active_index in row.indices for row in app._diff_rows)
        assert app.selected_entry.patch == raw
        assert app._diff_cursor == active_index
        assert source.text == displayed_source
        app.resize_sidebar(70)
        await settle(app, pilot)
        assert app.query_one("#diff-scroll").scrollable_content_region.width < 60
        assert app.diff_layout == "split" and app.diff_effective_layout == "unified"
        assert app.selected_entry.patch == raw
        assert app._diff_cursor == active_index
        assert any(active_index in row.indices for row in app._diff_rows)
        app.resize_sidebar(24)
        await settle(app, pilot)
        assert app.query_one("#diff-scroll").scrollable_content_region.width >= 60
        assert app.diff_effective_layout == "split"
        assert app.selected_entry.patch == raw
        assert app._diff_cursor == active_index
        assert source.text == displayed_source
        await pilot.click("#diff-layout")
        await pilot.pause()
        assert app.diff_layout == "unified"
        assert app.query_one("#diff", Static).content.code == raw
        await pilot.click("#diff-layout")
        await pilot.pause()
        assert app.diff_effective_layout == "split"
        await pilot.resize_terminal(80, 24)
        await pilot.pause()
        assert app.query_one("#diff-scroll").scrollable_content_region.width < 60
        assert app.diff_layout == "split" and app.diff_effective_layout == "unified"
        assert str(app.query_one("#diff-layout", Button).label) == "Unified"
        assert "Unified" in app.query_one("#diff-layout", Button).render_line(0).text
        assert "60" in app.query_one("#diff-layout").tooltip
        assert source.scrollable_content_region.height >= 9
        assert app.query_one("#diff-heading").size.height == 1
        assert app._diff_cursor == active_index
        assert any(active_index in row.indices for row in app._diff_rows)
        assert app.selected_entry.patch == raw
        await pilot.press("ctrl+f")
        await pilot.pause()
        assert app.query_one("#prompt-row").size.height == 1
        await pilot.press("escape")
        await pilot.resize_terminal(160, 48)
        await pilot.pause()
        assert app.diff_effective_layout == "split"
        assert app._diff_cursor == active_index
        await command(app, pilot, "unified")
        assert app.diff_effective_layout == "unified"
        assert app.query_one("#diff", Static).content.code == raw
    assert fixture.repo.state() == before
    assert main([str(fixture.repo.root), fixture.path, "--lines", "4:7", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["entries"][0]["patch"] == raw
    assert fixture.repo.state() == before


async def test_split_origin_long_lines_remain_horizontally_scrollable_and_source_is_read_only(repo):
    long_line = "long\t[bold]value[/] " + "界" * 120
    repo.write("long.txt", long_line + "\nsecond line\n")
    commit = repo.commit("Introduce long text")
    before = repo.state()
    app = TrailApp(Repository(repo.root), InitialSelection("long.txt", LineRange(1, 2)))
    async with app.run_test(size=(160, 48)) as pilot:
        await settle(app, pilot)
        await command(app, pilot, "split")
        assert app.diff_effective_layout == "split"
        rendered = app.query_one("#diff", Static).content
        assert isinstance(rendered, Text)
        assert "BEFORE · (absent)" in rendered.plain
        assert "[bold]value[/]" in rendered.plain
        assert "界" * 120 in rendered.plain
        pane = app.query_one("#diff-scroll")
        assert pane.max_scroll_x > 0
        pane.scroll_to(x=pane.max_scroll_x, animate=False, immediate=True)
        await pilot.pause()
        assert pane.scroll_x > 0
        assert app.selected_entry.commit == commit
        assert app.query_one("#source", SourceView).read_only
        assert app.selected_entry.patch == app._diff_key[1]
        assert "\t" in app.selected_entry.patch
    assert repo.state() == before
