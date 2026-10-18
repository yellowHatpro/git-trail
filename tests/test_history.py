from __future__ import annotations

from pathlib import Path

import pytest

from git_trail.git import LineRange, Repository, Snapshot, TrailError, parse_history


def timeline_for(repo, path: str, start: int, end: int, ref: str = "HEAD"):
    repository = Repository(repo.root, ref)
    snapshot = repository.snapshot(path)
    return repository.history(snapshot, LineRange(start, end))


def test_range_history_tracks_shifts_and_omits_unrelated_changes(range_history):
    fixture = range_history
    timeline = timeline_for(fixture.repo, fixture.path, 4, 7)
    assert [entry.commit for entry in timeline.entries] == fixture.relevant
    assert fixture.shift not in {entry.commit for entry in timeline.entries}
    assert fixture.unrelated not in {entry.commit for entry in timeline.entries}
    assert timeline.commit == fixture.delete
    assert timeline.selection == LineRange(4, 7)
    assert timeline.path == fixture.path
    assert Path(timeline.repository) == fixture.repo.root


def test_range_patches_show_insertions_deletions_and_origin(range_history):
    timeline = timeline_for(range_history.repo, range_history.path, 4, 7)
    deletion, insertion, change, origin = timeline.entries
    assert "-    b = 2" in deletion.patch
    assert "+    c = 3" in insertion.patch
    assert "-    a = 1" in change.patch
    assert "+    a = 7" in change.patch
    assert "+def target():" in origin.patch
    assert origin.old_count == 0
    assert origin.parents == ()
    assert origin.new_start == 3
    assert change.new_start == 3
    assert deletion.new_start == 4
    assert "footer changed" not in "\n".join(entry.patch for entry in timeline.entries)
    assert all(entry.patch.startswith("diff --git ") for entry in timeline.entries)


def test_single_line_history_contains_its_edits_and_introduction(range_history):
    timeline = timeline_for(range_history.repo, range_history.path, 5, 5)
    assert [entry.commit for entry in timeline.entries] == [
        range_history.edit, range_history.origin,
    ]
    assert all(entry.author == "Trail Fixture Author" for entry in timeline.entries)
    assert timeline.entries[0].subject == "Change target"
    assert timeline.entries[0].authored_at == "2001-01-01T00:00:02+00:00"
    assert timeline.entries[0].parents == (range_history.origin,)


def test_author_and_commit_dates_are_distinct_and_preserved(repo, monkeypatch):
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2020-02-02T12:30:00+05:30")
    repo.write("file.txt", "a historic authored change\n")
    commit = repo.commit("Applied much later")
    timeline = timeline_for(repo, "file.txt", 1, 1)
    entry = timeline.entries[0]
    assert entry.commit == commit
    assert entry.authored_at == "2001-01-01T00:00:01+00:00"
    assert entry.committed_at == "2020-02-02T12:30:00+05:30"
    result = timeline.to_dict()["entries"][0]
    assert result["authored_at"] == entry.authored_at
    assert result["committed_at"] == entry.committed_at


def test_inserted_line_stops_at_its_introduction(range_history):
    timeline = timeline_for(range_history.repo, range_history.path, 6, 6)
    assert [entry.commit for entry in timeline.entries] == [range_history.insert]
    assert "+    c = 3" in timeline.entries[0].patch
    assert timeline.entries[0].old_count == 0


def test_pure_file_rename_preserves_earlier_textual_history(range_history):
    fixture = range_history
    renamed = "src/renamed player.py"
    fixture.repo.git("mv", "--", fixture.path, renamed)
    rename_commit = fixture.repo.commit("Rename file only")
    timeline = timeline_for(fixture.repo, renamed, 4, 7)
    assert [entry.commit for entry in timeline.entries] == fixture.relevant
    assert rename_commit not in {entry.commit for entry in timeline.entries}
    assert any("moves" in warning.lower() and "not guaranteed" in warning.lower()
               for warning in timeline.warnings)


def test_historical_ref_uses_historical_file_and_range(range_history):
    fixture = range_history
    repository = Repository(fixture.repo.root, fixture.edit)
    snapshot = repository.snapshot(fixture.path)
    assert snapshot.lines[2] == "def target():"
    assert "c = 3" not in snapshot.text
    assert snapshot.working_tree_changed
    timeline = repository.history(snapshot, LineRange(3, 6))
    assert [entry.commit for entry in timeline.entries] == [fixture.edit, fixture.origin]
    assert timeline.commit == fixture.edit
    assert any("committed snapshot" in warning for warning in timeline.warnings)


@pytest.mark.parametrize("working_state", ["unstaged", "staged", "deleted"])
def test_working_tree_does_not_replace_committed_source(repo, working_state):
    path = "tracked.txt"
    committed_text = "committed line\nsecond line\n"
    repo.write(path, committed_text)
    commit = repo.commit("Committed snapshot")
    if working_state == "deleted":
        (repo.root / path).unlink()
    else:
        repo.write(path, "UNCOMMITTED replacement\n")
        if working_state == "staged":
            repo.git("add", "--", path)
    repository = Repository(repo.root)
    snapshot = repository.snapshot(path)
    assert snapshot.text == committed_text
    assert snapshot.working_tree_changed
    timeline = repository.history(snapshot, LineRange(1, 2))
    assert [entry.commit for entry in timeline.entries] == [commit]
    assert "UNCOMMITTED" not in timeline.entries[0].patch
    assert any("Working-tree edits" in warning for warning in timeline.warnings)


def test_clean_working_tree_has_no_local_change_warning(range_history):
    repository = Repository(range_history.repo.root)
    snapshot = repository.snapshot(range_history.path)
    assert not snapshot.working_tree_changed
    timeline = repository.history(snapshot, LineRange(4, 7))
    assert not any("Working-tree" in warning for warning in timeline.warnings)


@pytest.mark.parametrize("filename", [
    "name with spaces.txt", "colon:name.txt", ":leading-colon.txt",
    "-leading-option.txt", "semi;$(touch injected).txt", "[literal]*?.txt",
    "quote'\"and`backtick`.txt", "unicodé-文件.txt",
])
def test_literal_paths_cannot_be_treated_as_options_patterns_or_shell(repo, filename):
    repo.write(filename, "old value\n")
    origin = repo.commit("Original")
    repo.write(filename, "new value\n")
    edit = repo.commit("Edit")
    repository = Repository(repo.root)
    assert filename in repository.files()
    snapshot = repository.snapshot(filename)
    assert snapshot.path == filename
    timeline = repository.history(snapshot, LineRange(1, 1))
    assert [entry.commit for entry in timeline.entries] == [edit, origin]
    assert "+new value" in timeline.entries[0].patch
    assert not (repo.root / "injected").exists()


def test_literal_glob_path_changes_are_not_confused_with_other_files(repo):
    repo.write("[literal]*.txt", "selected\n")
    repo.write("l-other.txt", "other\n")
    repo.commit("Two files")
    repo.write("l-other.txt", "dirty other\n")
    snapshot = Repository(repo.root).snapshot("[literal]*.txt")
    assert not snapshot.working_tree_changed


def test_absolute_inside_path_and_subdirectory_repo_location(repo):
    repo.write("nested/file.py", "value = 1\n")
    repo.commit("Nested")
    repository = Repository(repo.root / "nested")
    assert repository.root == repo.root
    snapshot = repository.snapshot(repo.root / "nested/file.py")
    assert snapshot.path == "nested/file.py"
    assert snapshot.text == "value = 1\n"


def test_absolute_path_through_repository_directory_alias_uses_committed_snapshot(repo, tmp_path):
    committed = "def target():\n    return 1\n"
    repo.write("src/player.py", committed)
    commit = repo.commit("Directory alias fixture")
    alias = tmp_path / "repository alias with spaces"
    alias.symlink_to(repo.root, target_is_directory=True)
    before = repo.state()
    repository = Repository(repo.root)
    expected = repository.snapshot("src/player.py")
    actual = repository.snapshot(alias / "src" / "player.py")
    assert repository.root == repo.root.resolve()
    assert actual == expected
    assert actual.path == "src/player.py"
    assert actual.commit == commit
    assert actual.text == committed
    assert not actual.working_tree_changed
    assert alias.is_symlink()
    assert alias.resolve() == repo.root.resolve()
    assert repo.state() == before


def test_absolute_alias_path_preserves_tracked_file_symlink_identity(repo, tmp_path):
    repo.write("src/actual.py", "def actual():\n    return 42\n")
    tracked_link = repo.root / "src" / "link.py"
    tracked_link.symlink_to("actual.py")
    commit = repo.commit("Track a file symlink")
    alias = tmp_path / "repository directory alias"
    alias.symlink_to(repo.root, target_is_directory=True)
    before = repo.state()
    repository = Repository(repo.root)
    snapshot = repository.snapshot(alias / "src" / "link.py")
    assert repository.root == repo.root.resolve()
    assert snapshot.path == "src/link.py"
    assert snapshot.commit == commit
    assert snapshot.text == "actual.py"
    assert snapshot.lines == ["actual.py"]
    assert snapshot.text != repository.snapshot("src/actual.py").text
    assert not snapshot.working_tree_changed
    assert tracked_link.is_symlink()
    assert tracked_link.readlink() == Path("actual.py")
    assert repo.state() == before


@pytest.mark.parametrize("path", [
    "../outside.py", "nested/../../outside.py", ".", "", "bad\npath.py",
    "bad\rpath.py", "bad\0path.py",
])
def test_unsafe_or_empty_path_is_rejected(repo, path):
    repo.write("valid.py", "x = 1\n")
    repo.commit("Valid")
    with pytest.raises(TrailError):
        Repository(repo.root).snapshot(path)


def test_absolute_outside_path_is_rejected(repo, tmp_path):
    repo.write("valid.py", "x = 1\n")
    repo.commit("Valid")
    outside = tmp_path / "outside.py"
    outside.write_text("outside\n")
    with pytest.raises(TrailError, match="inside"):
        Repository(repo.root).snapshot(outside)


@pytest.mark.parametrize("content,expected", [
    (b"", "empty"),
    (b"one\0two", "binary"),
    (b"\xff\xfeinvalid", "UTF-8"),
])
def test_empty_binary_or_non_utf8_snapshot_is_actionable(repo, content, expected):
    repo.write("bad.txt", content)
    repo.commit("Invalid text fixture")
    with pytest.raises(TrailError, match=expected):
        Repository(repo.root).snapshot("bad.txt")


def test_missing_file_does_not_return_an_empty_timeline(repo):
    repo.write("exists.txt", "exists\n")
    repo.commit("Existing file")
    with pytest.raises(TrailError):
        Repository(repo.root).snapshot("absent.txt")


def test_utf8_bom_and_no_terminal_newline_are_supported(repo):
    repo.write("bom.txt", b"\xef\xbb\xbfhello\nsecond")
    origin = repo.commit("BOM file")
    repository = Repository(repo.root)
    snapshot = repository.snapshot("bom.txt")
    assert snapshot.text == "hello\nsecond"
    assert snapshot.lines == ["hello", "second"]
    assert [entry.commit for entry in repository.history(snapshot, LineRange(2, 2)).entries] == [origin]


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\v", "\f", "\r", "\x85", "\x1c"])
def test_valid_utf8_non_lf_separators_do_not_shift_git_line_coordinates(repo, separator):
    repo.write("file.txt", f"one{separator}still one\nold selected\nlast\n")
    origin = repo.commit("Original LF rows")
    repo.write("file.txt", f"one{separator}still one\nnew selected\nlast\n")
    edit = repo.commit("Changed second LF row")
    repository = Repository(repo.root)
    snapshot = repository.snapshot("file.txt")
    assert snapshot.lines == [f"one{separator}still one", "new selected", "last"]
    timeline = repository.history(snapshot, LineRange(2, 2))
    assert [entry.commit for entry in timeline.entries] == [edit, origin]
    assert "-old selected" in timeline.entries[0].patch
    assert "+new selected" in timeline.entries[0].patch
    with pytest.raises(TrailError):
        repository.history(snapshot, LineRange(4, 4))


@pytest.mark.parametrize("start,end", [(0, 1), (-1, 1), (2, 1), (1, 3), (3, 3)])
def test_invalid_ranges_are_rejected_before_git_accepts_them(repo, start, end):
    repo.write("two.txt", "one\ntwo\n")
    repo.commit("Two lines")
    repository = Repository(repo.root)
    snapshot = repository.snapshot("two.txt")
    with pytest.raises(TrailError, match="between 1 and 2"):
        repository.history(snapshot, LineRange(start, end))


@pytest.mark.parametrize("ref", [
    "does-not-exist", "--all", "--help", "HEAD..HEAD", "HEAD\n--all", "",
])
def test_invalid_or_option_like_refs_are_rejected(repo, ref):
    repo.write("file.txt", "text\n")
    repo.commit("Valid")
    with pytest.raises(TrailError):
        Repository(repo.root, ref)


def test_tag_and_parent_refs_resolve_to_single_commit(repo):
    repo.write("file.txt", "original\n")
    first = repo.commit("First")
    repo.git("tag", "fixture-v1")
    repo.write("file.txt", "next\n")
    repo.commit("Second")
    assert Repository(repo.root, "fixture-v1").commit == first
    assert Repository(repo.root, "HEAD^").commit == first


def test_noncommit_blob_ref_is_rejected(repo):
    repo.write("file.txt", "content\n")
    repo.commit("Blob fixture")
    blob = repo.git("rev-parse", "HEAD:file.txt").stdout.decode().strip()
    with pytest.raises(TrailError):
        Repository(repo.root, blob)


def test_nonrepository_and_unborn_repository_are_actionable(repo, tmp_path):
    with pytest.raises(TrailError):
        Repository(tmp_path / "not-created")
    existing = tmp_path / "ordinary-directory"
    existing.mkdir()
    with pytest.raises(TrailError):
        Repository(existing)
    with pytest.raises(TrailError):
        Repository(repo.root)


def test_snapshot_from_another_commit_is_rejected(range_history):
    fixture = range_history
    old = Repository(fixture.repo.root, fixture.origin).snapshot(fixture.path)
    with pytest.raises(TrailError, match="different"):
        Repository(fixture.repo.root).history(old, LineRange(3, 6))


def test_snapshot_path_traversal_is_rejected_even_when_commit_matches(repo):
    repo.write("file.txt", "content\n")
    repo.commit("Valid")
    repository = Repository(repo.root)
    forged = Snapshot("../file.txt", repository.commit, "content\n", False)
    with pytest.raises(TrailError):
        repository.history(forged, LineRange(1, 1))


def test_diff_like_commit_subject_does_not_corrupt_metadata_or_patch(repo):
    subject = "diff --git a/fake b/fake [bold] commit 0123456789"
    repo.write("file.txt", "first\n")
    first = repo.commit("Initial\n\ncommit deadbeef\n@@ -1 +1 @@\n")
    repo.write("file.txt", "second\n")
    last = repo.commit(subject + "\n\nFake patch:\ndiff --git a/fake b/fake\n")
    timeline = timeline_for(repo, "file.txt", 1, 1)
    assert [entry.commit for entry in timeline.entries] == [last, first]
    assert timeline.entries[0].subject == subject
    assert "+second" in timeline.entries[0].patch
    assert "Fake patch" not in timeline.entries[0].patch


def test_browsing_preserves_files_refs_index_and_configuration(range_history):
    repo = range_history.repo
    repo.write("untracked note.txt", "keep this\n")
    repo.write("unrelated.txt", "staged unrelated\n")
    repo.git("add", "--", "unrelated.txt")
    repo.write("unrelated.txt", "staged then unstaged\n")
    before = repo.state()
    repository = Repository(repo.root)
    repository.files()
    snapshot = repository.snapshot(range_history.path)
    repository.history(snapshot, LineRange(4, 7))
    assert repo.state() == before


def test_external_diff_textconv_and_fsmonitor_are_not_executed(repo, tmp_path):
    marker = tmp_path / "external-command-ran"
    script = tmp_path / "external.sh"
    script.write_text(f"#!/bin/sh\nprintf 'executed' > '{marker}'\nexit 0\n")
    script.chmod(0o755)
    repo.write("file.txt", "first\n")
    repo.write(".gitattributes", "*.txt diff=fixture\n")
    first = repo.commit("Initial")
    repo.write("file.txt", "second\n")
    second = repo.commit("Changed")
    repo.git("config", "diff.external", str(script))
    repo.git("config", "diff.fixture.textconv", str(script))
    repo.git("config", "core.fsmonitor", str(script))
    repo.git("config", "core.pager", str(script))
    repo.git("config", "color.ui", "always")
    repo.write("file.txt", "dirty\n")
    timeline = timeline_for(repo, "file.txt", 1, 1)
    assert [entry.commit for entry in timeline.entries] == [second, first]
    assert not marker.exists()
    assert "\x1b" not in timeline.entries[0].patch


def test_shallow_history_is_visibly_labeled(range_history, tmp_path):
    clone = tmp_path / "local shallow clone"
    range_history.repo.git(
        "clone", "--quiet", "--depth", "1", "--no-local",
        range_history.repo.root.as_uri(), str(clone),
    )
    repository = Repository(clone)
    snapshot = repository.snapshot(range_history.path)
    timeline = repository.history(snapshot, LineRange(4, 7))
    assert any("shallow" in warning.lower() and "missing" in warning.lower()
               for warning in timeline.warnings)
    assert len(timeline.entries) == 1
    assert timeline.entries[0].commit == range_history.delete


def test_history_json_contains_coordinates_metadata_and_limitations(range_history):
    timeline = timeline_for(range_history.repo, range_history.path, 4, 7)
    result = timeline.to_dict()
    assert result["starting_commit"] == range_history.delete
    assert result["selection"]["start"] == 4
    assert result["selection"]["end"] == 7
    assert [entry["commit"] for entry in result["entries"]] == range_history.relevant
    assert all({"author", "authored_at", "committed_at", "subject", "patch", "old_start", "old_count",
                "new_start", "new_count", "parents"} <= set(entry)
               for entry in result["entries"])
    assert any("Merge" in warning for warning in result["warnings"])


@pytest.mark.parametrize("output", [
    b"unexpected output",
    b"\0not-enough-fields\0",
    b"\0" + b"a" * 40 + b"\0\0Author\0" + b"2001-01-01\0"
    + b"2020-02-02\0Subject\0no patch",
])
def test_malformed_git_output_is_rejected(output):
    with pytest.raises(TrailError):
        parse_history(output)


def test_history_parser_retains_distinct_dates_and_zero_length_origin():
    commit = b"a" * 40
    patch = b"\ndiff --git a/a.txt b/a.txt\n--- /dev/null\n+++ b/a.txt\n@@ -0,0 +1 @@\n+hello\n"
    fields = [
        b"", commit, b"", b"Fixture Author",
        b"2001-01-01T00:00:00+00:00", b"2020-02-02T12:30:00+05:30", b"Origin", patch,
    ]
    result = parse_history(b"\0".join(fields))
    assert len(result) == 1
    assert result[0].commit == commit.decode()
    assert result[0].parents == ()
    assert result[0].authored_at == "2001-01-01T00:00:00+00:00"
    assert result[0].committed_at == "2020-02-02T12:30:00+05:30"
    assert result[0].old_count == 0
    assert result[0].new_count == 1
    assert "+hello" in result[0].patch
