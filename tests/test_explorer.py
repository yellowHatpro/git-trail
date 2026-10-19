from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest
from rich.cells import cell_len
from rich.syntax import Syntax
from rich.text import Text
from textual import events
from textual.worker import WorkerCancelled
from textual.widgets import Input, OptionList, Static

from git_trail.app import InitialSelection, PathScreen, SourceView, TrailApp
from git_trail.explorer import FileExplorer
from git_trail.git import LineRange, Repository


@dataclass(frozen=True)
class ExplorerFixture:
    repo: object
    commit: str
    paths: tuple[str, ...]
    ascii_path: str
    unicode_path: str


@pytest.fixture
def explorer_repo(repo):
    files = {
        "ROOT.txt": "root committed source\nsecond root line\n",
        "zz_root.py": "ROOT_VALUE = 1\n",
        "a_folder/main.py": "VALUE = 'alpha committed'\n",
        "a_folder/nested/README.md": "Nested documentation.\n",
        "A_other/main.py": "VALUE = 'beta committed'\n",
        "spaced folder/literal[red].py": "VALUE = 'literal markup filename'\n",
        "z_chain/one/two/leaf.py": "VALUE = 'compact chain'\n",
    }
    parent = "deep"
    for index in range(12):
        parent += f"/branch_{index:02}"
        files[parent + "/anchor.txt"] = f"Directory {index} anchor.\n"
    ascii_path = parent + "/very_long_shared_prefix_worker.py"
    unicode_path = parent + "/計算処理モジュール.py"
    files[ascii_path] = "VALUE = 'deep ascii source'\n"
    files[unicode_path] = "VALUE = 'deep unicode source'\n"
    for path, text in files.items():
        repo.write(path, text)
    commit = repo.commit("Committed explorer hierarchy")
    repo.write("later.py", "VALUE = 'later committed file'\n")
    repo.commit("Add a later file")
    repo.write("untracked/draft.py", "VALUE = 'local only'\n")
    repo.write("a_folder/main.py", "VALUE = 'local edit'\n")
    return ExplorerFixture(repo, commit, tuple(files), ascii_path, unicode_path)


async def settle(app, pilot, *, path=None):
    async def idle():
        for _ in range(12):
            await pilot.pause()
            workers = tuple(app.workers)
            outcomes = await asyncio.gather(*(worker.wait() for worker in workers), return_exceptions=True)
            for outcome in outcomes:
                if isinstance(outcome, WorkerCancelled):
                    continue
                if isinstance(outcome, BaseException):
                    raise outcome
            await pilot.pause()
            loaded = path is None or (
                app.snapshot is not None and app.snapshot.path == path and app._navigation_ready
            )
            if not app.workers and loaded:
                return
        pytest.fail(f"Explorer UI did not settle with source {path!r}.")

    await asyncio.wait_for(idle(), timeout=15)


async def focus(app, pilot, selector="#files"):
    widget = app.query_one(selector)
    widget.focus()
    await pilot.pause()
    assert app.focused is widget
    return widget


def content(widget):
    value = widget.content
    return value.plain if hasattr(value, "plain") else str(value)


def scene(app):
    source = app.query_one("#source", SourceView)
    diff = app.query_one("#diff", Static).content
    patch = diff.code if isinstance(diff, Syntax) else diff.plain if isinstance(diff, Text) else str(diff)
    return (
        app.snapshot, app.traced_snapshot, app.timeline, app.selected_entry,
        app.source_mode, source.text, source.selection,
        app._diff_key, app._diff_cursor, app._diff_side, app._diff_active,
        app.diff_layout, app.diff_effective_layout, patch,
    )


async def highlight(app, pilot, path, *, directory=False):
    explorer = app.query_one("#files", FileExplorer)
    index = next(index for index, row in enumerate(explorer.rows)
                 if row.path == path and row.directory is directory)
    explorer.highlighted = index
    explorer.scroll_to_highlight()
    await pilot.pause()
    assert explorer.selected_row.path == path and explorer.selected_row.directory is directory
    return explorer


def selected_strip(explorer):
    index = explorer.highlighted
    assert index is not None
    y = index - int(explorer.scroll_y)
    assert 0 <= y < explorer.scrollable_content_region.height
    strip = explorer.render_line(y)
    assert strip.cell_length == cell_len(strip.text)
    assert strip.cell_length <= explorer.scrollable_content_region.width
    return strip


async def click_row(app, pilot, path, *, directory=False):
    explorer = await highlight(app, pilot, path, directory=directory)
    y = explorer.highlighted - int(explorer.scroll_y)
    x = explorer.content_region.x - explorer.region.x + 2
    y += explorer.content_region.y - explorer.region.y
    assert await pilot.click(explorer, offset=(x, y))
    await settle(app, pilot)


async def command(app, pilot, value):
    await pilot.press(":")
    field = app.query_one("#command-input", Input)
    assert app.focused is field
    field.value = value
    await pilot.press("enter")
    await settle(app, pilot)


def observe_file_work(app, monkeypatch):
    groups, snapshots = [], []
    original_worker = app.run_worker
    original_snapshot = app.repository.snapshot

    def worker(work, *args, **kwargs):
        groups.append(kwargs.get("group"))
        return original_worker(work, *args, **kwargs)

    def snapshot(path, *args, **kwargs):
        snapshots.append(str(path))
        return original_snapshot(path, *args, **kwargs)

    monkeypatch.setattr(app, "run_worker", worker)
    monkeypatch.setattr(app.repository, "snapshot", snapshot)
    return groups, snapshots


async def test_committed_hierarchy_sorts_folders_first_compresses_chains_and_uses_captured_ref(explorer_repo):
    fixture = explorer_repo
    before = fixture.repo.state()
    app = TrailApp(Repository(fixture.repo.root, fixture.commit), InitialSelection("ROOT.txt"))
    async with app.run_test(size=(140, 40)) as pilot:
        await settle(app, pilot, path="ROOT.txt")
        explorer = app.query_one("#files", FileExplorer)
        assert isinstance(explorer, OptionList) and not explorer.search_mode
        assert set(app.all_files) == set(fixture.paths)
        assert "later.py" not in app.all_files and "untracked/draft.py" not in app.all_files
        assert [(row.path, row.directory) for row in explorer.rows] == [
            ("a_folder", True), ("A_other", True), ("deep/branch_00", True),
            ("spaced folder", True), ("z_chain/one/two", True),
            ("ROOT.txt", False), ("zz_root.py", False),
        ]
        chain = next(row for row in explorer.rows if row.path == "z_chain/one/two")
        assert chain.name == "z_chain/one/two" and chain.parent == ""
        assert not any(row.path in ("z_chain", "z_chain/one") for row in explorer.rows)
        assert explorer.selected_row.path == "ROOT.txt"
        current = explorer.rows
        explorer.reveal_path("untracked/draft.py")
        explorer.reveal_path("later.py")
        assert explorer.rows == current and explorer.selected_row.path == "ROOT.txt"
        assert app.snapshot.commit == fixture.commit
        assert app.query_one("#source", SourceView).text == "root committed source\nsecond root line\n"
    assert fixture.repo.state() == before


async def test_keyboard_expansion_parent_motion_and_directory_enter_issue_no_file_work(explorer_repo, monkeypatch):
    fixture = explorer_repo
    before = fixture.repo.state()
    app = TrailApp(Repository(fixture.repo.root), InitialSelection("ROOT.txt"))
    async with app.run_test(size=(140, 40)) as pilot:
        await settle(app, pilot, path="ROOT.txt")
        original = scene(app)
        groups, snapshots = observe_file_work(app, monkeypatch)
        explorer = await focus(app, pilot)
        await highlight(app, pilot, "a_folder", directory=True)
        await pilot.press("l")
        assert explorer.selected_row.path == "a_folder"
        assert any(row.path == "a_folder/main.py" for row in explorer.rows)
        await pilot.press("l")
        assert explorer.selected_row.path == "a_folder/nested"
        await pilot.press("h")
        assert explorer.selected_row.path == "a_folder"
        await pilot.press("h")
        assert not any(row.path.startswith("a_folder/") for row in explorer.rows)
        await pilot.press("enter")
        assert any(row.path == "a_folder/main.py" for row in explorer.rows)
        await pilot.press("j")
        assert explorer.selected_row.path == "a_folder/nested"
        await pilot.press("h", "enter")
        assert not any(row.path.startswith("a_folder/") for row in explorer.rows)
        await highlight(app, pilot, "z_chain/one/two", directory=True)
        await pilot.press("l", "j")
        assert explorer.selected_row.path == "z_chain/one/two/leaf.py"
        await pilot.press("h")
        assert explorer.selected_row.path == "z_chain/one/two"
        await pilot.press("h")
        assert not any(row.path == "z_chain/one/two/leaf.py" for row in explorer.rows)
        await pilot.press("G")
        assert explorer.selected_row.path == "zz_root.py"
        await pilot.press("k")
        assert explorer.selected_row.path == "ROOT.txt"
        await pilot.press("g", "g")
        assert explorer.selected_row.path == "a_folder"
        await settle(app, pilot)
        assert scene(app) == original
        assert snapshots == []
        assert not [group for group in groups if group in ("file", "history", "view", "diff-source")]
        assert not app.workers
        await highlight(app, pilot, "zz_root.py")
        await pilot.press("enter")
        await settle(app, pilot, path="zz_root.py")
        assert app.query_one("#source", SourceView).text == "ROOT_VALUE = 1\n"
        assert snapshots == ["zz_root.py"] and groups.count("file") == 1
    assert fixture.repo.state() == before


async def test_mouse_folder_toggle_duplicate_basenames_and_full_path_inspection_dispatch_exact_files(explorer_repo, monkeypatch):
    fixture = explorer_repo
    before = fixture.repo.state()
    app = TrailApp(Repository(fixture.repo.root), InitialSelection("ROOT.txt"))
    async with app.run_test(size=(140, 40)) as pilot:
        await settle(app, pilot, path="ROOT.txt")
        original = scene(app)
        groups, snapshots = observe_file_work(app, monkeypatch)
        await click_row(app, pilot, "a_folder", directory=True)
        explorer = app.query_one("#files", FileExplorer)
        assert any(row.path == "a_folder/main.py" for row in explorer.rows)
        assert scene(app) == original and snapshots == [] and "file" not in groups
        await highlight(app, pilot, "a_folder/main.py")
        await focus(app, pilot)
        assert explorer.tooltip == "a_folder/main.py"
        await command(app, pilot, "path")
        assert isinstance(app.screen, PathScreen) and app.screen.path == "a_folder/main.py"
        assert any(content(widget) == "a_folder/main.py" for widget in app.screen.query(Static))
        await pilot.press("escape")
        await click_row(app, pilot, "a_folder/main.py")
        await settle(app, pilot, path="a_folder/main.py")
        assert app.query_one("#source", SourceView).text == "VALUE = 'alpha committed'\n"
        assert app.snapshot.working_tree_changed
        await click_row(app, pilot, "a_folder", directory=True)
        assert not any(row.path == "a_folder/main.py" for row in explorer.rows)
        assert app.snapshot.path == "a_folder/main.py"
        await click_row(app, pilot, "A_other", directory=True)
        await click_row(app, pilot, "A_other/main.py")
        await settle(app, pilot, path="A_other/main.py")
        assert app.query_one("#source", SourceView).text == "VALUE = 'beta committed'\n"
        assert app.query_one("#source", SourceView).read_only
        assert snapshots == ["a_folder/main.py", "A_other/main.py"]
        assert groups.count("file") == 2
        assert explorer.selected_row.path == "A_other/main.py"
    assert fixture.repo.state() == before


@pytest.mark.parametrize("width", [16, 24])
async def test_deep_ascii_unicode_and_literal_filenames_retain_extensions_in_actual_rendered_rows(explorer_repo, width):
    fixture = explorer_repo
    before = fixture.repo.state()
    app = TrailApp(Repository(fixture.repo.root), InitialSelection("ROOT.txt"))
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot, path="ROOT.txt")
        app.resize_sidebar(width)
        await pilot.pause()
        explorer = app.query_one("#files", FileExplorer)
        assert app.query_one("#files-pane").size.width == width
        for path, visible_prefix in ((fixture.ascii_path, "very"), (fixture.unicode_path, "計算")):
            explorer.reveal_path(path)
            await pilot.pause()
            assert explorer.selected_row.path == path and explorer.selected_row.depth >= 10
            strip = selected_strip(explorer)
            assert visible_prefix in strip.text and ".py" in strip.text
            assert "branch_" not in strip.text
            assert explorer.tooltip == path
        app.resize_sidebar(24)
        explorer.reveal_path("spaced folder/literal[red].py")
        await pilot.pause()
        assert "literal[red].py" in selected_strip(explorer).text
        assert explorer.tooltip == "spaced folder/literal[red].py"
        assert app.snapshot.path == "ROOT.txt"
    assert fixture.repo.state() == before


async def test_file_prompt_has_real_filename_first_matches_and_restores_tree_after_select_or_cancel(explorer_repo):
    fixture = explorer_repo
    before = fixture.repo.state()
    app = TrailApp(Repository(fixture.repo.root), InitialSelection("ROOT.txt"))
    async with app.run_test(size=(140, 40)) as pilot:
        await settle(app, pilot, path="ROOT.txt")
        explorer = await focus(app, pilot)
        original = scene(app)
        await pilot.press("ctrl+o")
        field = app.query_one("#file-filter", Input)
        assert app.focused is field and explorer.search_mode
        field.value = "main ext:py"
        await settle(app, pilot)
        assert app.filtered_files == ("A_other/main.py", "a_folder/main.py")
        assert tuple(row.path for row in explorer.rows) == app.filtered_files
        assert explorer.option_count == 2 and not any(row.directory for row in explorer.rows)
        for index in range(2):
            explorer.highlighted = index
            explorer.scroll_to_highlight()
            await pilot.pause()
            assert selected_strip(explorer).text.lstrip().startswith("main.py")
        explorer.highlighted = 0
        await pilot.press("ctrl+n")
        assert explorer.selected_row.path == "a_folder/main.py" and app.focused is field
        await pilot.press("ctrl+p")
        assert explorer.selected_row.path == "A_other/main.py"
        await pilot.press("down", "up")
        assert explorer.selected_row.path == "A_other/main.py" and app.focused is field
        assert scene(app) == original
        field.value = "af mn ext:py"
        await settle(app, pilot)
        assert app.filtered_files == ("a_folder/main.py",)
        assert explorer.rows[0].path == "a_folder/main.py"
        await pilot.press("enter")
        await settle(app, pilot, path="a_folder/main.py")
        assert not app.query_one("#prompt-row").display and not explorer.search_mode
        assert app.filtered_files == ("a_folder/main.py",)
        assert explorer.selected_row.path == "a_folder/main.py"
        assert any(row.path == "a_folder" and row.directory for row in explorer.rows)
        assert app.query_one("#source", SourceView).text == "VALUE = 'alpha committed'\n"
        await focus(app, pilot)
        await pilot.press("/")
        assert app.focused is field and explorer.search_mode
        assert field.value == "af mn ext:py"
        field.value = "does-not-exist ext:py"
        await settle(app, pilot)
        assert explorer.option_count == 0 and explorer.selected_row is None
        current = scene(app)
        await pilot.press("escape")
        await settle(app, pilot)
        assert not explorer.search_mode and not app.query_one("#prompt-row").display
        assert explorer.selected_row.path == "a_folder/main.py"
        assert scene(app) == current and app.focused is explorer
        await pilot.press("ctrl+o")
        field.value = ""
        await settle(app, pilot)
        assert explorer.option_count == len(app.all_files)
        assert tuple(row.path for row in explorer.rows) == app.filtered_files
        assert not any(row.directory for row in explorer.rows)
    assert fixture.repo.state() == before


async def test_historical_source_in_an_absent_path_never_fabricates_a_committed_explorer_row(repo):
    body = (
        "def target(values):\n"
        "    positive = [value for value in values if value > 0]\n"
        "    total = sum(positive)\n"
        "    count = len(positive)\n"
        "    average = total / max(count, 1)\n"
        "    return round(average, 2)\n"
    )
    repo.write("old.py", "# old source\n\n" + body)
    origin = repo.commit("Old function")
    (repo.root / "old.py").unlink()
    repo.write("new.py", body)
    repo.write("ROOT.txt", "root source\n")
    moved = repo.commit("Move function")
    before = repo.state()
    app = TrailApp(Repository(repo.root), InitialSelection("new.py", LineRange(1, 6), follow_symbol=True))
    async with app.run_test(size=(140, 40)) as pilot:
        await settle(app, pilot, path="new.py")
        explorer = app.query_one("#files", FileExplorer)
        assert app.selected_entry.commit == moved
        await command(app, pilot, "before")
        await settle(app, pilot, path="old.py")
        assert app.snapshot.commit == origin
        assert set(app.all_files) == {"new.py", "ROOT.txt"}
        assert not any(row.path == "old.py" for row in explorer.rows)
        current = scene(app)
        await pilot.press("ctrl+o")
        field = app.query_one("#file-filter", Input)
        field.value = "old ext:py"
        await settle(app, pilot)
        assert app.filtered_files == () and explorer.rows == ()
        await pilot.press("escape")
        await settle(app, pilot)
        assert not explorer.search_mode
        assert not any(row.path == "old.py" for row in explorer.rows)
        assert scene(app) == current
    assert repo.state() == before


async def test_many_tree_rows_scroll_and_keep_exact_selection_through_resize_and_sidebar_visibility(repo):
    repo.write("ROOT.txt", "root source\n")
    for index in range(100):
        repo.write(f"src/file_{index:03}.py", f"VALUE = {index}\n")
    repo.commit("Many committed leaves")
    before = repo.state()
    app = TrailApp(Repository(repo.root), InitialSelection("ROOT.txt"))
    async with app.run_test(size=(140, 35)) as pilot:
        await settle(app, pilot, path="ROOT.txt")
        explorer = await focus(app, pilot)
        original = scene(app)
        await highlight(app, pilot, "src", directory=True)
        await pilot.press("enter")
        assert explorer.option_count == 102
        assert sum(not row.directory for row in explorer.rows) == 101
        await pilot.press("end")
        await pilot.pause()
        assert explorer.selected_row.path == "ROOT.txt" and explorer.scroll_y > 0
        await pilot.press("home")
        await pilot.pause()
        assert explorer.selected_row.path == "src" and explorer.scroll_y == 0
        for _ in range(3):
            explorer.post_message(events.MouseScrollDown(explorer, 1, 1, 0, 0, 0, False, False, False))
        await pilot.pause()
        assert explorer.scroll_y > 0
        await highlight(app, pilot, "src/file_099.py")
        assert "file_099.py" in selected_strip(explorer).text
        for width in (38, 16, 24):
            app.resize_sidebar(width)
            await pilot.pause()
            assert explorer.selected_row.path == "src/file_099.py"
            assert ".py" in selected_strip(explorer).text
        await pilot.press("f2")
        await pilot.pause()
        assert not app.query_one("#files-pane").display
        await pilot.press("f2")
        await pilot.pause()
        assert app.query_one("#files-pane").display
        assert explorer.selected_row.path == "src/file_099.py"
        assert scene(app) == original
    assert repo.state() == before


@pytest.mark.parametrize("size", [(80, 24), (160, 48)])
async def test_explorer_layout_preserves_code_rows_and_transient_single_row_search(range_history, size):
    fixture = range_history
    before = fixture.repo.state()
    app = TrailApp(Repository(fixture.repo.root), InitialSelection(fixture.path, LineRange(4, 7)))
    async with app.run_test(size=size) as pilot:
        await settle(app, pilot, path=fixture.path)
        explorer = app.query_one("#files", FileExplorer)
        source = app.query_one("#source", SourceView)
        assert not explorer.search_mode and explorer.selected_row.path == fixture.path
        assert app.query_one("#files-title").size.height == 1
        assert app.query_one("#workspace-title").size.height == 1
        assert app.query_one("#statusline").size.height == 1
        assert source.scrollable_content_region.height >= 9
        assert explorer.region.right <= size[0]
        original, height = scene(app), source.scrollable_content_region.height
        await focus(app, pilot, "#source")
        await pilot.press("ctrl+o")
        await pilot.pause()
        assert explorer.search_mode and app.focused is app.query_one("#file-filter", Input)
        assert app.query_one("#prompt-row").size.height == 1
        assert source.scrollable_content_region.height >= height - 1
        assert scene(app) == original
        await pilot.press("escape")
        await settle(app, pilot)
        assert not explorer.search_mode and not app.query_one("#prompt-row").display
        assert app.focused is source
        assert source.scrollable_content_region.height == height
        assert scene(app) == original
        assert [entry.commit for entry in app.timeline.entries] == fixture.relevant
    assert fixture.repo.state() == before
