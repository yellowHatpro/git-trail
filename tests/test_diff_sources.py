from __future__ import annotations

from dataclasses import replace

import pytest

from git_trail.diff import DiffPatch
from git_trail.git import HistoryEntry, LineRange, Repository, TrailError


@pytest.mark.parametrize(
    ("raw", "targets"),
    [
        (
            "@@ -1,5 +1,5 @@\n start\n-first\n+FIRST\n middle\n-second\n+SECOND\n end",
            (2, 5),
        ),
        (
            "@@ -1 +1 @@\n-old\n\\ No newline at end of file\n+new\n\\ No newline at end of file",
            (1,),
        ),
        ("@@ -1 +1 @@\n-old\n+new\n@@ -20,2 +30,2 @@\n same\n-other\n+OTHER", (1, 5)),
        ("@@ -3,2 +1,2 @@\n def unchanged():\n     return 1", (1,)),
        ("@@ -1,2 +1,2 @@\n-old\n+new", ()),
        ("@@ -1 +1 @@\n-old\n+new\n+extra\n@@ -10 +20 @@\n valid context", (5,)),
        ("diff --git a/image b/image\nBinary files a/image and b/image differ\n", ()),
    ],
)
def test_change_targets_are_source_edit_blocks_and_skip_untrusted_hunks(raw, targets):
    patch = DiffPatch.parse(raw)
    assert patch.change_targets() == targets
    assert "\n".join(line.text for line in patch.lines) == raw
    for target in targets:
        line = patch.lines[target]
        assert line.kind in ("delete", "add", "context")
        assert line.old is not None or line.new is not None
        assert patch.hunks[line.hunk].valid


def git_entry(repo, commit, old_path, new_path):
    parents = repo.git("rev-list", "--parents", "-n", "1", commit).stdout.decode().split()[1:]
    patch = repo.git(
        "show", "--format=", "--no-ext-diff", "--no-textconv", "--no-color", commit,
    ).stdout.decode()
    return HistoryEntry(
        commit, tuple(parents), "Fixture Author", "2001-01-01T00:00:00+00:00",
        "2001-01-01T00:00:00+00:00", "Fixture change", patch, 0, 0, 0, 0,
        old_path=old_path, new_path=new_path,
    )


def test_diff_source_opens_exact_lines_on_both_sides_and_rejects_invalid_requests(range_history):
    fixture = range_history
    before = fixture.repo.state()
    repository = Repository(fixture.repo.root)
    timeline = repository.history(repository.snapshot(fixture.path), LineRange(4, 7))
    entry = timeline.entries[0]
    assert entry.commit == fixture.delete
    old = repository.diff_source(entry, 6, before=True)
    new = repository.diff_source(entry, 6)
    assert (old.snapshot.commit, old.snapshot.path) == (fixture.insert, fixture.path)
    assert (new.snapshot.commit, new.snapshot.path) == (fixture.delete, fixture.path)
    assert old.snapshot.lines[5] == "    b = 2"
    assert new.snapshot.lines[5] == "    c = 3"
    for view in (old, new):
        assert (view.selection.start, view.selection.end) == (6, 6)
        assert view.snapshot.working_tree_changed is False
        assert f"{fixture.path}:6" in view.note
    origin = timeline.entries[-1]
    introduced = repository.diff_source(origin, 3)
    assert introduced.snapshot.commit == fixture.origin
    assert introduced.snapshot.lines[2] == "def target():"
    with pytest.raises(TrailError, match="first commit|earlier source"):
        repository.diff_source(origin, 3, before=True)
    for bad_entry, line in (
        (entry, 0), (entry, 999),
        (replace(entry, commit="f" * 40), 6),
        (replace(entry, new_path="missing.py"), 6),
        (replace(entry, new_path="../outside.py"), 6),
        (replace(entry, new_path="bad\0.py"), 6),
    ):
        with pytest.raises(TrailError):
            repository.diff_source(bad_entry, line)
    assert fixture.repo.state() == before


def test_diff_source_uses_historical_paths_after_a_git_file_rename(repo):
    old_path, new_path = "old folder/worker.py", "new folder/worker.py"
    old = (
        "def target(values):\n"
        "    count = len(values)\n"
        "    total = sum(values)\n"
        "    minimum = min(values)\n"
        "    maximum = max(values)\n"
        "    average = total / max(count, 1)\n"
        "    return count, total, minimum, maximum, average\n"
    )
    repo.write(old_path, old)
    origin = repo.commit("Original worker")
    (repo.root / new_path).parent.mkdir(parents=True)
    repo.git("mv", old_path, new_path)
    repo.write(new_path, old.replace("total = sum(values)", "total = sum(values) + 1"))
    moved = repo.commit("Move worker and adjust total")
    before = repo.state()
    repository = Repository(repo.root)
    entry = repository.history(repository.snapshot(new_path), LineRange(1, 7)).entries[0]
    assert entry.commit == moved
    assert (entry.old_path, entry.new_path) == (old_path, new_path)
    old_view = repository.diff_source(entry, 3, before=True)
    new_view = repository.diff_source(entry, 3)
    assert (old_view.snapshot.commit, old_view.snapshot.path) == (origin, old_path)
    assert (new_view.snapshot.commit, new_view.snapshot.path) == (moved, new_path)
    assert old_view.snapshot.lines[2] == "    total = sum(values)"
    assert new_view.snapshot.lines[2] == "    total = sum(values) + 1"
    assert repo.state() == before


def test_diff_source_follows_first_parent_for_merge_before_view(repo):
    repo.write("module.txt", "first\nbase\nlast\n")
    repo.commit("Base")
    repo.git("checkout", "--quiet", "-b", "side")
    repo.write("module.txt", "first\nside branch\nlast\n")
    side = repo.commit("Side changes source")
    repo.git("checkout", "--quiet", "main")
    repo.write("notes.txt", "main branch note\n")
    first_parent = repo.commit("Main changes notes")
    repo.git("merge", "--quiet", "--no-ff", "--no-edit", "side")
    merged = repo.git("rev-parse", "HEAD").stdout.decode().strip()
    before = repo.state()
    entry = git_entry(repo, merged, "module.txt", "module.txt")
    assert entry.parents == (first_parent, side)
    repository = Repository(repo.root)
    old = repository.diff_source(entry, 2, before=True)
    new = repository.diff_source(entry, 2)
    assert old.snapshot.commit == first_parent
    assert old.snapshot.lines[1] == "base"
    assert new.snapshot.commit == merged
    assert new.snapshot.lines[1] == "side branch"
    assert "first parent" in old.note
    assert repo.state() == before


def test_diff_source_rejects_absent_and_binary_sides_without_changing_repository(repo):
    repo.write("gone.txt", "first\nsecond\n")
    repo.write("binary.bin", b"first\0second\n")
    origin = repo.commit("Add text and binary")
    (repo.root / "gone.txt").unlink()
    deleted = repo.commit("Delete whole text file")
    before = repo.state()
    repository = Repository(repo.root)
    entry = git_entry(repo, deleted, "gone.txt", "")
    old = repository.diff_source(entry, 2, before=True)
    assert old.snapshot.commit == origin and old.snapshot.lines[1] == "second"
    assert (old.selection.start, old.selection.end) == (2, 2)
    with pytest.raises(TrailError, match="does not exist"):
        repository.diff_source(entry, 1)
    with pytest.raises(TrailError, match="binary"):
        repository.diff_source(git_entry(repo, origin, "", "binary.bin"), 1)
    assert repo.state() == before


@pytest.mark.parametrize("object_kind", ["blob", "commit"])
def test_diff_source_checks_git_objects_again_after_a_revision_cache_hit(range_history, object_kind):
    fixture = range_history
    repository = Repository(fixture.repo.root)
    entry = repository.history(repository.snapshot(fixture.path), LineRange(4, 7)).entries[0]
    view = repository.diff_source(entry, 6, before=True)
    assert repository.revision_text(fixture.path, fixture.insert) == view.snapshot.text
    assert repository.at(fixture.insert).commit == fixture.insert
    expression = f"{fixture.insert}:{fixture.path}" if object_kind == "blob" else fixture.insert
    sha = fixture.repo.git("rev-parse", expression).stdout.decode().strip()
    loose_object = fixture.repo.root / ".git" / "objects" / sha[:2] / sha[2:]
    assert loose_object.is_file()
    loose_object.unlink()
    with pytest.raises(TrailError):
        repository.diff_source(entry, 6, before=True)
