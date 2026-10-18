from __future__ import annotations

import pytest

from git_trail.git import LineRange, Repository


def test_historical_views_use_correct_revisions_and_shifted_range_coordinates(range_history):
    fixture = range_history
    repository = Repository(fixture.repo.root)
    timeline = repository.history(repository.snapshot(fixture.path), LineRange(4, 7))
    insertion = timeline.entries[1]
    change = timeline.entries[2]
    before = fixture.repo.state()
    insertion_after = repository.historical_source(insertion)
    insertion_before = repository.historical_source(insertion, before=True)
    change_after = repository.historical_source(change)
    assert insertion_after.snapshot.commit == fixture.insert
    assert insertion_before.snapshot.commit == fixture.unrelated
    assert "c = 3" in insertion_after.snapshot.text
    assert "c = 3" not in insertion_before.snapshot.text
    assert "b = 2" in insertion_before.snapshot.text
    assert change_after.snapshot.commit == fixture.edit
    assert change_after.snapshot.lines[2] == "def target():"
    assert change_after.selection.start == 3
    assert change_after.selection.end == 6
    assert change_after.snapshot.text == fixture.repo.git(
        "show", f"{fixture.edit}:{fixture.path}",
    ).stdout.decode()
    root_before = repository.historical_source(timeline.entries[-1], before=True)
    assert root_before.snapshot is None
    assert root_before.selection is None
    assert "No earlier source" in root_before.note
    assert fixture.repo.state() == before


@pytest.mark.parametrize(
    "renamed", ["src/renamed player.py", 'src/quoted "player".py', "src/tab\tplayer.py", "src/é-player.py"],
)
def test_historical_source_tracks_original_path_across_pure_rename(range_history, renamed):
    fixture = range_history
    fixture.repo.git("mv", "--", fixture.path, renamed)
    fixture.repo.commit("Rename file only")
    repository = Repository(fixture.repo.root)
    timeline = repository.history(repository.snapshot(renamed), LineRange(4, 7))
    assert [item.commit for item in timeline.entries] == fixture.relevant
    for item in timeline.entries:
        view = repository.historical_source(item)
        assert view.snapshot.path == fixture.path
        assert view.snapshot.text == fixture.repo.git(
            "show", f"{item.commit}:{fixture.path}",
        ).stdout.decode()


def test_created_line_before_view_keeps_file_and_labels_missing_selected_lines(repo):
    repo.write("file.txt", "first\nlast\n")
    repo.commit("Original file")
    repo.write("file.txt", "first\ninserted\nlast\n")
    repo.commit("Insert selected line")
    repository = Repository(repo.root)
    timeline = repository.history(repository.snapshot("file.txt"), LineRange(2, 2))
    assert len(timeline.entries) == 1
    before = repository.historical_source(timeline.entries[0], before=True)
    assert before.snapshot.text == "first\nlast\n"
    assert before.selection is None
    assert "selected lines do not exist" in before.note


def test_commit_context_contains_full_body_and_every_changed_file(repo):
    repo.write("one.py", "value = 1\n")
    repo.write("notes with spaces.txt", "Some notes\n")
    commit = repo.commit("Introduce value\n\nExplain the reason.\nKeep the full second paragraph.")
    repository = Repository(repo.root)
    before = repo.state()
    details = repository.details(commit)
    assert details.commit == commit
    assert details.message == "Introduce value\n\nExplain the reason.\nKeep the full second paragraph."
    assert set(details.files) == {"one.py", "notes with spaces.txt"}
    assert repo.state() == before


def test_cached_committed_reads_avoid_git_repetition_but_dirty_status_is_live(repo, monkeypatch):
    repo.write("file.txt", "committed value\n")
    commit = repo.commit("Original value")
    repository = Repository(repo.root)
    original = repository._git
    calls = []

    def counted(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(repository, "_git", counted)
    first = repository.snapshot("file.txt")
    assert not first.working_tree_changed
    repo.write("file.txt", "local replacement\n")
    second = repository.snapshot("file.txt")
    assert second.text == first.text == "committed value\n"
    assert second.working_tree_changed
    assert sum(args[0] == "show" for args in calls) == 1
    assert sum(args[0] == "diff" for args in calls) == 2
    calls.clear()
    timeline = repository.history(second, LineRange(1, 1))
    assert repository.history(second, LineRange(1, 1)) is timeline
    assert sum(args[0] == "log" for args in calls) == 1
    calls.clear()
    details = repository.details(commit)
    assert repository.details(commit) is details
    assert sum(args[0] == "show" for args in calls) == 1
    assert sum(args[0] == "diff-tree" for args in calls) == 1
    calls.clear()
    view = repository.historical_source(timeline.entries[0])
    assert repository.historical_source(timeline.entries[0]) is view
    assert not calls
    assert repository.at(commit) is repository.at(commit)
