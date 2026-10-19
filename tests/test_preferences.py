from __future__ import annotations

import asyncio
import json
import stat

import pytest
from textual.widgets import Checkbox, Input, Static

from git_trail.app import HelpScreen, InitialSelection, SourceView, SymbolScreen, TrailApp
from git_trail.git import Repository, TrailError
from git_trail.state import StateStore


def valid_state(**preferences):
    return {"version": 1, "preferences": preferences, "sessions": {}, "bookmarks": {}}


async def settle(app, pilot):
    async def idle():
        for _ in range(10):
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            if not app.workers:
                return
        pytest.fail("The UI kept spawning workers.")

    await asyncio.wait_for(idle(), timeout=15)


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


def test_preferences_round_trip_uses_a_private_file_and_preserves_other_state(tmp_path):
    path = tmp_path / "settings" / "state.json"
    store = StateStore(path)
    assert not path.exists()
    store.data["preferences"].update({
        "sidebar_width": 38, "sidebar_visible": False,
        "find_case": True, "find_regex": True, "keybindings": {"t": "find"},
    })
    store.data["sessions"] = {"example": {"opaque": "session data"}}
    store.data["bookmarks"] = {"example": {"opaque": "bookmark data"}}
    store.save()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    restored = StateStore(path)
    assert not restored.warning
    assert restored.data == store.data
    assert restored.data["preferences"]["keybindings"] == {"t": "find"}
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"{", "ignored"),
        (json.dumps({"version": 2}).encode(), "version"),
        (json.dumps({"version": True}).encode(), "version"),
        (json.dumps(valid_state(sidebar_width=True)).encode(), "width"),
        (json.dumps(valid_state(sidebar_width=71)).encode(), "width"),
        (json.dumps(valid_state(sidebar_visible="false")).encode(), "boolean"),
        (json.dumps(valid_state(find_case=1)).encode(), "boolean"),
        (json.dumps(valid_state(keybindings={"j": "find"})).encode(), "reserved"),
        (json.dumps(valid_state(keybindings={"t": "__class__"})).encode(), "actions"),
        (b" " * 1_000_001, "size"),
    ],
)
def test_invalid_state_is_ignored_read_only_and_preserved_instead_of_overwritten(tmp_path, raw, message):
    path = tmp_path / "state.json"
    path.write_bytes(raw)
    store = StateStore(path)
    assert store.read_only
    assert message in store.warning.casefold()
    assert store.data["preferences"]["sidebar_width"] == 24
    assert store.data["preferences"]["sidebar_visible"] is True
    assert store.data["preferences"]["keybindings"] == {}
    store.data["preferences"]["sidebar_width"] = 40
    with pytest.raises(TrailError):
        store.save()
    assert path.read_bytes() == raw
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("failure", ["replace", "fsync"])
def test_atomic_save_failure_preserves_the_previous_file_and_removes_temporary_files(tmp_path, monkeypatch, failure):
    import git_trail.state

    path = tmp_path / "state.json"
    store = StateStore(path)
    store.save()
    before = path.read_bytes()
    store.data["preferences"]["sidebar_width"] = 35

    def unavailable(*args, **kwargs):
        raise OSError("simulated storage failure")

    monkeypatch.setattr(git_trail.state.os, failure, unavailable)
    with pytest.raises(TrailError, match="simulated storage failure"):
        store.save()
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


def test_state_size_bound_counts_the_complete_written_file(tmp_path, monkeypatch):
    import git_trail.state

    path = tmp_path / "state.json"
    store = StateStore(path)
    store.save()
    before = path.read_bytes()
    monkeypatch.setattr(git_trail.state, "MAX_STATE_BYTES", len(before) - 1)
    with pytest.raises(TrailError, match="size limit"):
        store.save()
    assert path.read_bytes() == before


def test_default_state_location_honors_explicit_override_then_xdg_without_creating_files(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit"
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("GIT_TRAIL_STATE_DIR", str(explicit))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    assert StateStore.default().path == explicit / "state.json"
    monkeypatch.delenv("GIT_TRAIL_STATE_DIR")
    assert StateStore.default().path == xdg / "git-trail" / "state.json"
    assert not explicit.exists()
    assert not xdg.exists()


@pytest.mark.parametrize("location", ["state.json", "nested/state.json", ".git/trail-state.json", "symlink"])
def test_repository_storage_and_symlink_aliases_are_read_only(repo, tmp_path, location):
    repo.write("main.py", "value = 1\n")
    repo.commit("Source")
    target = repo.root / ("nested/state.json" if location == "symlink" else location)
    target.parent.mkdir(parents=True, exist_ok=True)
    original = json.dumps(valid_state(sidebar_width=31)).encode()
    target.write_bytes(original)
    path = target
    if location == "symlink":
        path = tmp_path / "outside-alias.json"
        path.symlink_to(target)
    before = repo.state()
    store = StateStore(path)
    with pytest.raises(TrailError, match="inside"):
        store.allow_repository(repo.root)
    assert store.read_only
    with pytest.raises(TrailError):
        store.save()
    assert target.read_bytes() == original
    assert repo.state() == before


async def test_layout_search_and_custom_keys_survive_two_explicit_store_sessions(repo, tmp_path):
    repo.write("source.py", "def target():\n    return 1\n")
    repo.commit("Source for sessions")
    before = repo.state()
    path = tmp_path / "user-settings" / "state.json"
    app = TrailApp(Repository(repo.root), InitialSelection("source.py"), store=StateStore(path))
    async with app.run_test(size=(140, 40)) as pilot:
        await settle(app, pilot)
        app.resize_sidebar(38)
        await pilot.press("f2")
        await pilot.pause()
        assert not app.query_one("#files-pane").display
        await command(app, pilot, "set noignorecase")
        await command(app, pilot, "set regex")
        await command(app, pilot, "map t find")
        await pilot.press("ctrl+q")
        assert app.return_code == 0
    store = StateStore(path)
    assert store.data["preferences"] == {
        "sidebar_width": 38, "sidebar_visible": False,
        "find_case": True, "find_regex": True, "keybindings": {"t": "find"},
    }
    app = TrailApp(Repository(repo.root), InitialSelection("source.py"), store=store)
    async with app.run_test(size=(140, 40)) as pilot:
        await settle(app, pilot)
        assert app.sidebar_width == 38
        assert not app.query_one("#files-pane").display
        assert app.query_one("#find-case", Checkbox).value
        assert app.query_one("#find-regex", Checkbox).value
        await pilot.press("f2")
        await pilot.pause()
        assert app.query_one("#files-pane").display
        assert app.query_one("#files-pane").size.width == 38
        await pilot.press("t")
        assert app.focused is app.query_one("#find-input", Input)
        assert content(app, "#mode") == "PROMPT"
        await pilot.press("escape")
        await command(app, pilot, "unmap t")
        await pilot.press("ctrl+q")
    assert StateStore(path).data["preferences"]["keybindings"] == {}
    assert repo.state() == before


async def test_custom_normal_keys_preserve_prompt_modal_visual_and_builtin_modes(repo, tmp_path):
    source_text = "def target():\n    return 1\n"
    repo.write("source.py", source_text)
    repo.commit("Source for mapping isolation")
    path = tmp_path / "settings.json"
    app = TrailApp(Repository(repo.root), InitialSelection("source.py"), store=StateStore(path))
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(app, pilot)
        source = app.query_one("#source", SourceView)
        await command(app, pilot, "map t find")
        await pilot.press("t")
        field = app.query_one("#find-input", Input)
        assert app.focused is field
        await pilot.press(*"tqvj")
        assert field.value == "tqvj"
        assert app.focused is field
        assert app.return_code is None
        await pilot.press("escape", "v")
        selected = source.selected_text
        assert content(app, "#mode") == "VISUAL"
        await pilot.press("t")
        assert source.selected_text == selected
        assert content(app, "#mode") == "VISUAL"
        assert not app.query_one("#prompt-row").display
        await pilot.press("escape", "ctrl+b")
        await pilot.pause()
        assert isinstance(app.screen, SymbolScreen)
        modal_input = app.screen.query_one("#symbol-filter", Input)
        await pilot.press("t")
        assert app.focused is modal_input
        assert modal_input.value == "t"
        assert isinstance(app.screen, SymbolScreen)
        await pilot.press("escape")
        await pilot.pause()
        for value, message in (
            ("map j find", "reserved"),
            ("map gg find", "unused"),
            ("map t execute", "actions"),
            ("map t", "Use :map"),
        ):
            await command(app, pilot, value)
            assert message in content(app, "#status")
            assert app.focused is app.query_one("#command-input", Input)
            assert app.preferences["keybindings"] == {"t": "find"}
            await pilot.press("escape")
        await command(app, pilot, "map [ help")
        await command(app, pilot, "maps")
        assert isinstance(app.screen, HelpScreen)
        rendered = "\n".join(
            str(widget.content.plain if hasattr(widget.content, "plain") else widget.content)
            for widget in app.screen.query(Static)
        )
        assert "CUSTOM NORMAL KEYS" in rendered
        assert "t  find" in rendered
        assert "[  help" in rendered
        await pilot.press("escape")
        await pilot.pause()
        await command(app, pilot, "unmap t")
        await pilot.press("t")
        assert app.focused is source
        assert not app.query_one("#prompt-row").display
        assert source.text == source_text
        await pilot.press("ctrl+q")
    assert StateStore(path).data["preferences"]["keybindings"] == {"[": "help"}


async def test_mapping_limit_rejects_a_new_key_and_allows_replacing_an_existing_key(repo, tmp_path):
    repo.write("main.py", "value = 1\n")
    repo.commit("Mapping capacity source")
    path = tmp_path / "state.json"
    store = StateStore(path)
    mappings = {chr(0x3B1 + offset): "help" for offset in range(50)}
    store.data["preferences"]["keybindings"] = mappings.copy()
    app = TrailApp(Repository(repo.root), InitialSelection("main.py"), store=store)
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(app, pilot)
        await command(app, pilot, "map t find")
        assert "50" in content(app, "#status")
        assert app.focused is app.query_one("#command-input", Input)
        assert app.preferences["keybindings"] == mappings
        await pilot.press("escape")
        key = next(iter(mappings))
        await command(app, pilot, f"map {key} find")
        assert app.preferences["keybindings"][key] == "find"
        assert len(app.preferences["keybindings"]) == 50
        await pilot.press("ctrl+q")
    assert StateStore(path).data["preferences"]["keybindings"][key] == "find"


async def test_headless_default_does_not_read_or_create_real_user_state(repo, monkeypatch):
    repo.write("main.py", "value = 1\n")
    repo.commit("Headless source")

    def forbidden_default():
        pytest.fail("Headless app consulted the user's real settings")

    monkeypatch.setattr(StateStore, "default", forbidden_default)
    app = TrailApp(Repository(repo.root), InitialSelection("main.py"))
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(app, pilot)
        assert app.store is None
        await pilot.press("ctrl+q")


async def test_app_reports_corrupt_state_and_exits_without_overwriting_it(repo, tmp_path):
    repo.write("main.py", "value = 1\n")
    repo.commit("Corrupt preference source")
    path = tmp_path / "state.json"
    raw = b'{"version": 2, "private data": "keep"}'
    path.write_bytes(raw)
    app = TrailApp(Repository(repo.root), InitialSelection("main.py"), store=StateStore(path))
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(app, pilot)
        assert "Settings ignored" in content(app, "#status")
        assert app.query_one("#files-pane").display
        await pilot.press("ctrl+q")
        assert app.return_code == 0
    assert path.read_bytes() == raw
