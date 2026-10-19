from __future__ import annotations

import copy
import re
from dataclasses import FrozenInstanceError, dataclass, replace

import pytest

from git_trail.diff import DiffPatch
from git_trail.git import LineRange, Repository, TrailError


BODY = (
    "def calculate(value):\n"
    "    base = value + 1\n"
    "\n"
    "    # Normalize the input.\n"
    "    normalized = max(0, base)\n"
    "\n"
    "    # Keep an intermediate result.\n"
    "    subtotal = normalized\n"
    "\n"
    "    # Apply the tax.\n"
    "    tax = 0\n"
    "\n"
    "    return subtotal + tax\n"
)


@dataclass(frozen=True)
class MergeFixture:
    repo: object
    path: str
    origin: str
    first: str
    second: str
    merge: str
    first_text: str
    second_text: str
    merged_text: str


@pytest.fixture
def two_parent_merge(repo):
    path = "src/calculate.py"
    repo.write(path, BODY)
    origin = repo.commit("Introduce calculation")
    repo.git("checkout", "--quiet", "-b", "feature")
    second_text = BODY.replace("tax = 0", "tax = 2")
    repo.write(path, second_text)
    second = repo.commit("Add tax on feature")
    repo.git("checkout", "--quiet", "main")
    first_text = BODY.replace("value + 1", "value + 3")
    repo.write(path, first_text)
    first = repo.commit("Adjust base on main")
    repo.git("merge", "--quiet", "--no-ff", "-m", "Merge tax feature", "feature")
    merge = repo.git("rev-parse", "HEAD").stdout.decode().strip()
    merged_text = first_text.replace("tax = 0", "tax = 2")
    assert (repo.root / path).read_text() == merged_text
    return MergeFixture(repo, path, origin, first, second, merge, first_text, second_text, merged_text)


def raw_parents(repo, commit):
    raw = repo.git("--no-replace-objects", "cat-file", "-p", commit).stdout
    headers = raw.partition(b"\n\n")[0].splitlines()
    return tuple(line.removeprefix(b"parent ").decode() for line in headers if line.startswith(b"parent "))


def pair_patch(repo, parent, commit, *paths):
    return repo.git(
        "--no-replace-objects", "--literal-pathspecs", "-c", "color.ui=false",
        "-c", "core.quotepath=false", "-c", "diff.suppressBlankEmpty=false",
        "diff", "--no-ext-diff", "--no-textconv", "--no-color", "--no-relative",
        "--find-renames", "--src-prefix=a/", "--dst-prefix=b/", "--unified=3",
        "--output-indicator-new=+", "--output-indicator-old=-", "--output-indicator-context= ",
        parent, commit, "--", *dict.fromkeys(paths),
    ).stdout.decode("utf-8", errors="replace").strip("\n")


def expected_ranges(patch, side):
    hunks = re.findall(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", patch, re.M)
    return tuple((int(hunk[side]), int(hunk[side]) + int(hunk[side + 1] or "1") - 1)
                 for hunk in hunks if int(hunk[side + 1] or "1"))


def assert_pair(repository, repo, merge, path, parent, index, old_path, new_path, old_text, new_text):
    comparison = repository.parent_comparison(merge, path, index)
    assert comparison.commit == merge and comparison.parent_commit == parent
    assert comparison.parent_index == index and comparison.path == path
    assert comparison.parents == raw_parents(repo, merge)
    assert comparison.entry.parents == comparison.parents
    assert comparison.old_path == old_path and comparison.new_path == new_path
    patch = pair_patch(repo, parent, merge, *(name for name in (old_path, new_path) if name))
    assert comparison.patch == patch and comparison.entry.patch == patch
    assert tuple((item.start, item.end) for item in comparison.old_ranges) == expected_ranges(patch, 0)
    assert tuple((item.start, item.end) for item in comparison.new_ranges) == expected_ranges(patch, 2)
    assert "FULL-FILE" in comparison.note and parent in comparison.note and merge in comparison.note
    assert f"parent {index}/{len(comparison.parents)}" in comparison.note
    for before, expected_path, expected_text, expected_commit in (
        (True, old_path, old_text, parent), (False, new_path, new_text, merge),
    ):
        view = repository.parent_source(comparison, before=before)
        available = comparison.old_available if before else comparison.new_available
        assert available is (expected_text is not None)
        if expected_text is None:
            assert view.snapshot is None and view.selection is None
            continue
        assert view.snapshot.commit == expected_commit
        assert view.snapshot.path == expected_path and view.snapshot.text == expected_text
        assert not view.snapshot.working_tree_changed
        blob = repo.git("--no-replace-objects", "rev-parse", f"{expected_commit}:{expected_path}").stdout.decode().strip()
        assert (comparison.old_blob if before else comparison.new_blob) == blob
        ranges = comparison.old_ranges if before else comparison.new_ranges
        if ranges:
            assert (view.selection.start, view.selection.end) == (
                min(item.start for item in ranges), max(item.end for item in ranges),
            )
        else:
            assert view.selection is None
    parsed = DiffPatch.parse(patch)
    for before, kind, coordinate in ((True, "delete", "old"), (False, "add", "new")):
        row = next((line for line in parsed.lines if line.kind == kind and getattr(line, coordinate) is not None), None)
        if row is None:
            continue
        number = getattr(row, coordinate)
        view = repository.parent_diff_source(comparison, number, before=before)
        assert view.snapshot.commit == (parent if before else merge)
        assert view.snapshot.path == (old_path if before else new_path)
        assert view.snapshot.lines[number - 1] == (old_text if before else new_text).splitlines()[number - 1]
        assert (view.selection.start, view.selection.end) == (number, number)
    return comparison


def test_canonical_parent_metadata_and_full_file_comparisons_remain_discoverable_when_range_history_omits_merge(two_parent_merge):
    fixture = two_parent_merge
    repo = fixture.repo
    before = repo.state()
    repository = Repository(repo.root)
    metadata = repository.commit_metadata()
    assert metadata.commit == fixture.merge
    assert metadata.parents == (fixture.first, fixture.second) == raw_parents(repo, fixture.merge)
    assert metadata.tree == repo.git("rev-parse", f"{fixture.merge}^{{tree}}").stdout.decode().strip()
    assert metadata.subject == metadata.message == "Merge tax feature"
    author, authored, committed = repo.git("show", "-s", "--format=%an%x00%aI%x00%cI", fixture.merge).stdout.decode().strip().split("\0")
    assert (metadata.author, metadata.authored_at, metadata.committed_at) == (author, authored, committed)
    timeline = repository.history(repository.snapshot(fixture.path), LineRange(2, 2))
    assert [entry.commit for entry in timeline.entries] == [fixture.first, fixture.origin]
    assert fixture.merge not in {entry.commit for entry in timeline.entries}
    discovered = repository.reachable_merges()
    assert discovered.starting_commit == fixture.merge and not discovered.truncated
    assert [item.commit for item in discovered.merges] == [fixture.merge]
    first = assert_pair(repository, repo, fixture.merge, fixture.path, fixture.first, 1,
                        fixture.path, fixture.path, fixture.first_text, fixture.merged_text)
    second = assert_pair(repository, repo, fixture.merge, fixture.path, fixture.second, 2,
                         fixture.path, fixture.path, fixture.second_text, fixture.merged_text)
    assert "-    tax = 0" in first.patch and "+    tax = 2" in first.patch
    assert "-    base = value + 1" in second.patch and "+    base = value + 3" in second.patch
    assert first.patch != second.patch
    assert first.before.selection.start > timeline.selection.end
    assert repository.parent_comparison(fixture.merge, fixture.path).parent_commit == fixture.first
    assert repository.parent_comparison(fixture.merge, fixture.path, fixture.second.upper()) == second
    with pytest.raises(FrozenInstanceError):
        second.parent_index = 1
    assert repo.state() == before


def test_conflict_resolution_compares_actual_parent_lines_and_merge_result(repo):
    path = "conflict.txt"
    repo.write(path, "begin\nbase value\nend\n")
    repo.commit("Base")
    repo.git("checkout", "--quiet", "-b", "side")
    side_text = "begin\nside value\nend\n"
    repo.write(path, side_text)
    side = repo.commit("Side conflict")
    repo.git("checkout", "--quiet", "main")
    main_text = "begin\nmain value\nend\n"
    repo.write(path, main_text)
    main = repo.commit("Main conflict")
    conflict = repo.git("merge", "--no-ff", "--no-commit", "side", check=False)
    assert conflict.returncode == 1
    merged_text = "begin\nresolved value\nextra resolved row\nend\n"
    repo.write(path, merged_text)
    merged = repo.commit("Resolve both branches")
    assert raw_parents(repo, merged) == (main, side)
    before = repo.state()
    repository = Repository(repo.root)
    for index, parent, text in ((1, main, main_text), (2, side, side_text)):
        comparison = assert_pair(repository, repo, merged, path, parent, index, path, path, text, merged_text)
        old = repository.parent_diff_source(comparison, 2, before=True)
        new = repository.parent_diff_source(comparison, 3)
        assert old.snapshot.lines[1] in ("main value", "side value")
        assert new.snapshot.lines[2] == "extra resolved row"
        assert old.snapshot.commit == parent and new.snapshot.commit == merged
    assert repo.state() == before


def test_octopus_merge_exposes_all_three_ordered_parents_and_independent_comparisons(repo):
    path = "values.txt"
    base = "left\nkeep one\nmiddle\nkeep two\nright\n"
    repo.write(path, base)
    origin = repo.commit("Base values")
    repo.git("checkout", "--quiet", "-b", "left")
    left_text = base.replace("left\n", "LEFT\n")
    repo.write(path, left_text)
    left = repo.commit("Left branch")
    repo.git("checkout", "--quiet", "-b", "right", origin)
    right_text = base.replace("right\n", "RIGHT\n")
    repo.write(path, right_text)
    right = repo.commit("Right branch")
    repo.git("checkout", "--quiet", "main")
    main_text = base.replace("middle\n", "MAIN middle\n")
    repo.write(path, main_text)
    main = repo.commit("Main middle")
    repo.git("merge", "--quiet", "--no-ff", "-m", "Octopus contribution", "left", "right")
    merged = repo.git("rev-parse", "HEAD").stdout.decode().strip()
    merged_text = main_text.replace("left\n", "LEFT\n").replace("right\n", "RIGHT\n")
    assert (repo.root / path).read_text() == merged_text
    before = repo.state()
    repository = Repository(repo.root)
    assert repository.commit_metadata().parents == (main, left, right)
    assert repository.reachable_merges().merges[0].parents == (main, left, right)
    comparisons = [assert_pair(repository, repo, merged, path, parent, index, path, path, text, merged_text)
                   for index, (parent, text) in enumerate(((main, main_text), (left, left_text), (right, right_text)), 1)]
    assert len({item.patch for item in comparisons}) == 3
    assert [item.parent_index for item in comparisons] == [1, 2, 3]
    assert repo.state() == before


def test_parent_file_mapping_handles_rename_addition_deletion_retained_copy_and_unchanged_side(repo, tmp_path, monkeypatch):
    old_path, new_path = "old folder/literal[red]*.py", "new folder/literal[red]*.py"
    copy_text = BODY.replace("calculate", "retained_calculate")
    repo.write(old_path, BODY)
    repo.write("retained.py", copy_text)
    repo.write("gone.txt", "first deleted row\nsecond deleted row\n")
    repo.write("notes.txt", "base notes\n")
    repo.commit("Original files")
    repo.git("checkout", "--quiet", "-b", "side")
    repo.write("notes.txt", "side notes\n")
    side = repo.commit("Side notes")
    repo.git("checkout", "--quiet", "main")
    (repo.root / new_path).parent.mkdir(parents=True)
    repo.git("mv", "--", old_path, new_path)
    (repo.root / "gone.txt").unlink()
    repo.write("added.txt", "newly added row\n")
    repo.write("copied.py", copy_text)
    main = repo.commit("Move add delete and copy")
    repo.git("merge", "--quiet", "--no-ff", "-m", "Merge file changes", "side")
    merged = repo.git("rev-parse", "HEAD").stdout.decode().strip()
    marker = tmp_path / "external-executed"
    poison = tmp_path / "external-command"
    poison.write_text(f"#!/bin/sh\nprintf executed > '{marker}'\nexit 93\n")
    poison.chmod(0o755)
    repo.write(".gitattributes", "*.py diff=hostile\n")
    repo.git("config", "diff.hostile.textconv", str(poison))
    repo.git("config", "core.hooksPath", str(tmp_path / "hooks"))
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "post-checkout").write_bytes(poison.read_bytes())
    (hooks / "post-checkout").chmod(0o755)
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", str(poison))
    before = repo.state()
    repository = Repository(repo.root)
    rename = assert_pair(repository, repo, merged, new_path, side, 2, old_path, new_path, BODY, BODY)
    assert "rename" in rename.note.casefold() and "no semantic lineage" in rename.note
    added = assert_pair(repository, repo, merged, "added.txt", side, 2, "", "added.txt", None, "newly added row\n")
    assert not added.old_available and "does not exist" in added.before.note
    deleted = assert_pair(repository, repo, merged, "gone.txt", side, 2, "gone.txt", "", "first deleted row\nsecond deleted row\n", None)
    assert not deleted.new_available and "does not exist" in deleted.after.note
    copied = assert_pair(repository, repo, merged, "copied.py", side, 2, "", "copied.py", None, copy_text)
    assert not copied.old_available and not copied.old_path
    assert repository.parent_source(copied, before=True).snapshot is None
    unchanged = assert_pair(repository, repo, merged, new_path, main, 1, new_path, new_path, BODY, BODY)
    assert unchanged.patch == "" and unchanged.old_ranges == unchanged.new_ranges == ()
    assert unchanged.before.selection is None and unchanged.after.selection is None
    assert "No file changes" in unchanged.note
    for comparison, is_before in ((added, True), (deleted, False), (unchanged, True)):
        with pytest.raises(TrailError):
            repository.parent_diff_source(comparison, 1, before=is_before)
    assert not marker.exists()
    assert repo.state() == before


def test_identical_rename_destinations_reject_gits_arbitrary_selected_pair(repo):
    repo.write("old.py", BODY)
    repo.write("notes.txt", "base notes\n")
    repo.commit("One predecessor")
    repo.git("checkout", "--quiet", "-b", "side")
    repo.write("notes.txt", "side notes\n")
    side = repo.commit("Side notes")
    repo.git("checkout", "--quiet", "main")
    (repo.root / "old.py").unlink()
    repo.write("new-one.py", BODY)
    repo.write("new-two.py", BODY)
    repo.commit("Two identical destinations")
    repo.git("merge", "--quiet", "--no-ff", "-m", "Merge destinations", "side")
    merged = repo.git("rev-parse", "HEAD").stdout.decode().strip()
    records = repo.git(
        "--no-replace-objects", "diff-tree", "--no-commit-id", "--name-status",
        "-r", "-z", "--find-renames", "--no-ext-diff", "--no-textconv", side, merged, "--",
    ).stdout.split(b"\0")
    assert records.pop() == b""
    renames = []
    index = 0
    while index < len(records):
        status = records[index].decode()
        count = 2 if status.startswith(("R", "C")) else 1
        if status.startswith("R"):
            renames.append((status, records[index + 1].decode(), records[index + 2].decode()))
        index += count + 1
    assert len(renames) == 1
    status, predecessor, destination = renames[0]
    assert status == "R100" and predecessor == "old.py"
    assert destination in {"new-one.py", "new-two.py"}
    before = repo.state()
    with pytest.raises(TrailError, match="[Aa]mbiguous"):
        Repository(repo.root).parent_comparison(merged, destination, 2)
    assert repo.state() == before


@pytest.mark.parametrize("fault", ["ambiguous", "candidate-limit"])
def test_uncertain_rename_mapping_stops_instead_of_selecting_an_arbitrary_predecessor(repo, monkeypatch, fault):
    import git_trail.merge

    repo.write("old-one.py", BODY)
    repo.write("old-two.py", BODY if fault == "ambiguous" else "Different predecessor.\n")
    repo.write("old-three.py", "Third predecessor.\n")
    repo.write("notes.txt", "base notes\n")
    repo.commit("Predecessors")
    repo.git("checkout", "--quiet", "-b", "side")
    repo.write("notes.txt", "side notes\n")
    side = repo.commit("Side notes")
    repo.git("checkout", "--quiet", "main")
    for name in ("old-one.py", "old-two.py", "old-three.py"):
        (repo.root / name).unlink()
    repo.write("new.py", BODY)
    repo.commit("Move predecessor")
    repo.git("merge", "--quiet", "--no-ff", "-m", "Merge predecessors", "side")
    merged = repo.git("rev-parse", "HEAD").stdout.decode().strip()
    if fault == "candidate-limit":
        monkeypatch.setattr(git_trail.merge, "MAX_RENAME_CANDIDATES", 2)
    before = repo.state()
    repository = Repository(repo.root)
    with pytest.raises(TrailError, match="Ambiguous rename" if fault == "ambiguous" else "candidate limit"):
        repository.parent_comparison(merged, "new.py", side)
    assert repo.state() == before


@pytest.mark.parametrize("old", [b"binary\0parent\n", b"invalid \xff parent\n"], ids=["binary", "non-utf8"])
def test_unreadable_parent_side_is_explicit_while_after_source_remains_exact(repo, old):
    repo.write("source.txt", old)
    repo.write("notes.txt", "notes\n")
    repo.commit("Unreadable source")
    repo.git("checkout", "--quiet", "-b", "side")
    repo.write("notes.txt", "side notes\n")
    side = repo.commit("Side notes")
    repo.git("checkout", "--quiet", "main")
    repo.write("source.txt", "readable merge source\n")
    repo.commit("Convert source to UTF-8")
    repo.git("merge", "--quiet", "--no-ff", "-m", "Merge readable source", "side")
    merged = repo.git("rev-parse", "HEAD").stdout.decode().strip()
    before = repo.state()
    repository = Repository(repo.root)
    comparison = repository.parent_comparison(merged, "source.txt", 2)
    assert comparison.parent_commit == side and not comparison.old_available
    assert comparison.before.snapshot is None and comparison.before.selection is None
    assert ("binary" if b"\0" in old else "UTF-8") in comparison.before.note
    assert comparison.after.snapshot.commit == merged
    assert comparison.after.snapshot.text == "readable merge source\n"
    with pytest.raises(TrailError, match="binary" if b"\0" in old else "UTF-8"):
        repository.parent_diff_source(comparison, 1, before=True)
    assert repo.state() == before


def test_discovery_uses_captured_ref_excludes_unreachable_merges_and_honors_historical_anchor(two_parent_merge):
    fixture = two_parent_merge
    repo = fixture.repo
    captured = Repository(repo.root)
    tree = repo.git("rev-parse", f"{fixture.merge}^{{tree}}").stdout.decode().strip()
    unreachable = repo.git("commit-tree", tree, "-p", fixture.first, "-p", fixture.second, "-m", "Unreachable merge").stdout.decode().strip()
    repo.write("later.txt", "later source\n")
    later = repo.commit("Later nonmerge")
    before = repo.state()
    assert captured.reachable_merges().starting_commit == fixture.merge
    assert [item.commit for item in captured.reachable_merges().merges] == [fixture.merge]
    current = Repository(repo.root)
    result = current.reachable_merges()
    assert result.starting_commit == later
    assert [item.commit for item in result.merges] == [fixture.merge]
    assert unreachable not in {item.commit for item in result.merges}
    assert current.reachable_merges(commit=fixture.origin).merges == ()
    assert current.commit_metadata(fixture.first).parents == (fixture.origin,)
    assert current.parent_comparison(fixture.merge, fixture.path, 2).parent_commit == fixture.second
    assert repo.state() == before


def test_default_discovery_has_exact_100_merge_bound_and_explicit_truncation(repo):
    repo.write("source.txt", "stable source\n")
    origin = repo.commit("Origin")
    tree = repo.git("rev-parse", f"{origin}^{{tree}}").stdout.decode().strip()
    side = repo.git("commit-tree", tree, "-p", origin, "-m", "Side tip").stdout.decode().strip()
    commits = []
    current = origin
    for number in range(101):
        current = repo.git("commit-tree", tree, "-p", current, "-p", side,
                           "-m", f"Reachable merge {number}").stdout.decode().strip()
        commits.append(current)
    repo.git("update-ref", "refs/heads/main", current)
    before = repo.state()
    repository = Repository(repo.root)
    result = repository.reachable_merges()
    assert result.starting_commit == current and result.truncated
    assert [item.commit for item in result.merges] == list(reversed(commits[-100:]))
    assert all(len(item.parents) == 2 for item in result.merges)
    shorter = repository.reachable_merges(limit=2)
    assert shorter.truncated and [item.commit for item in shorter.merges] == list(reversed(commits[-2:]))
    assert repo.state() == before


def test_shallow_merge_retains_actual_parent_headers_and_is_discoverable_without_parent_objects(two_parent_merge, tmp_path):
    fixture = two_parent_merge
    clone = tmp_path / "shallow merge with spaces"
    fixture.repo.git("clone", "--quiet", "--depth=1", fixture.repo.root.as_uri(), str(clone))
    clone_repo = copy.copy(fixture.repo)
    clone_repo.root = clone
    before = clone_repo.state()
    shallow = (clone / ".git" / "shallow").read_bytes()
    repository = Repository(clone)
    metadata = repository.commit_metadata()
    assert metadata.parents == (fixture.first, fixture.second)
    assert [item.commit for item in repository.reachable_merges().merges] == [fixture.merge]
    assert not repository.reachable_merges().truncated
    for index, parent in enumerate(metadata.parents, 1):
        assert clone_repo.git("cat-file", "-e", parent, check=False).returncode != 0
        with pytest.raises(TrailError, match=f"Parent {index}.*unavailable"):
            repository.parent_comparison(fixture.merge, fixture.path, index)
    assert clone_repo.state() == before and (clone / ".git" / "shallow").read_bytes() == shallow


@pytest.mark.parametrize("kind", ["parent-commit", "parent-tree", "parent-blob", "merge-blob"])
def test_warm_comparisons_reject_pruned_objects_instead_of_returning_cached_sources(two_parent_merge, kind):
    fixture = two_parent_merge
    repo = fixture.repo
    repository = Repository(repo.root)
    comparison = repository.parent_comparison(fixture.merge, fixture.path, 1)
    repository.parent_source(comparison, before=True)
    line = comparison.old_ranges[0].start
    repository.parent_diff_source(comparison, line, before=True)
    sha = {
        "parent-commit": comparison.parent_commit,
        "parent-tree": comparison.parent_metadata.tree,
        "parent-blob": comparison.old_blob,
        "merge-blob": comparison.new_blob,
    }[kind]
    object_path = repo.root / ".git" / "objects" / sha[:2] / sha[2:]
    assert object_path.is_file()
    object_path.unlink()
    head = (repo.root / ".git" / "HEAD").read_bytes()
    refs = repo.git("show-ref", "--heads", "--tags").stdout
    for request in (
        lambda: repository.parent_comparison(fixture.merge, fixture.path, 1),
        lambda: repository.parent_source(comparison, before=True),
        lambda: repository.parent_diff_source(comparison, line, before=True),
    ):
        with pytest.raises(TrailError):
            request()
    assert (repo.root / ".git" / "HEAD").read_bytes() == head
    assert repo.git("show-ref", "--heads", "--tags").stdout == refs
    assert not object_path.exists()


def test_parent_inputs_source_rows_and_forged_comparisons_are_rejected_without_repository_changes(two_parent_merge):
    fixture = two_parent_merge
    repo = fixture.repo
    before = repo.state()
    repository = Repository(repo.root)
    comparison = repository.parent_comparison(fixture.merge, fixture.path, 2)
    for parent in (0, 3, -1, True, None, "main", fixture.second[:12], fixture.origin, "--help"):
        with pytest.raises(TrailError):
            repository.parent_comparison(fixture.merge, fixture.path, parent)
    for limit in (0, 101, -1, True, "2"):
        with pytest.raises(TrailError):
            repository.reachable_merges(limit=limit)
    for commit in ("HEAD", "--help", "f" * 40):
        with pytest.raises(TrailError):
            repository.commit_metadata(commit)
    with pytest.raises(TrailError, match="merge commit"):
        repository.parent_comparison(fixture.first, fixture.path)
    for line in (0, 10_000, True, "2"):
        with pytest.raises(TrailError):
            repository.parent_diff_source(comparison, line, before=True)
    for forged in (
        replace(comparison, parent_index=1),
        replace(comparison, patch="forged patch"),
        replace(comparison, old_path="another.py"),
    ):
        with pytest.raises(TrailError, match="no longer matches"):
            repository.parent_source(forged, before=True)
    with pytest.raises(TrailError, match="exact merge-parent"):
        repository.parent_source(comparison.entry)
    assert repo.state() == before


def test_default_range_json_and_historical_source_contract_remain_unchanged(range_history):
    fixture = range_history
    before = fixture.repo.state()
    repository = Repository(fixture.repo.root)
    timeline = repository.history(repository.snapshot(fixture.path), LineRange(4, 7))
    assert [entry.commit for entry in timeline.entries] == fixture.relevant
    encoded = timeline.to_dict()
    assert set(encoded) == {"repository", "path", "starting_commit", "selection", "warnings", "entries", "mode"}
    assert set(encoded["entries"][0]) == {
        "commit", "parents", "author", "authored_at", "committed_at", "subject", "patch",
        "old_start", "old_count", "new_start", "new_count", "old_path", "new_path",
        "old_range", "new_range", "match_method", "similarity", "tracking_note",
    }
    entry = timeline.entries[0]
    assert repository.historical_source(entry, before=True).snapshot.commit == entry.parents[0]
    assert repository.diff_source(entry, 6, before=True).snapshot.commit == entry.parents[0]
    assert fixture.repo.state() == before


def test_canonical_merge_comparisons_ignore_replace_refs_and_legacy_revision_cache(two_parent_merge):
    fixture = two_parent_merge
    repo = fixture.repo
    warm = Repository(repo.root)
    previous = warm.parent_comparison(fixture.merge, fixture.path, 1)
    metadata = warm.commit_metadata(fixture.merge)
    origin_tree = repo.git("rev-parse", f"{fixture.origin}^{{tree}}").stdout.decode().strip()
    replacement = repo.git("commit-tree", origin_tree, "-p", fixture.origin, "-m", "Replacement commit").stdout.decode().strip()
    repo.git("replace", fixture.merge, replacement)
    repo.git("replace", previous.parent_metadata.tree, origin_tree)
    before = repo.state()
    replacements = repo.git("for-each-ref", "--format=%(refname) %(objectname)", "refs/replace").stdout
    assert warm.commit_metadata(fixture.merge) == metadata
    assert warm.parent_source(previous, before=True).snapshot.text == fixture.first_text
    cold = Repository(repo.root)
    assert cold.revision_text(fixture.path, fixture.first) == BODY
    comparison = assert_pair(cold, repo, fixture.merge, fixture.path, fixture.first, 1,
                             fixture.path, fixture.path, fixture.first_text, fixture.merged_text)
    assert cold.commit_metadata(fixture.merge).parents == (fixture.first, fixture.second)
    assert [item.commit for item in cold.reachable_merges().merges] == [fixture.merge]
    assert cold.parent_source(comparison, before=True).snapshot.text == fixture.first_text
    assert cold.parent_diff_source(comparison, 11, before=True).snapshot.lines[10] == "    tax = 0"
    assert cold.revision_text(fixture.path, fixture.first) == BODY
    assert repo.state() == before
    assert repo.git("for-each-ref", "--format=%(refname) %(objectname)", "refs/replace").stdout == replacements


def test_configured_suppression_of_blank_context_cannot_break_diff_coordinates(two_parent_merge):
    fixture = two_parent_merge
    repo = fixture.repo
    repo.git("config", "diff.suppressBlankEmpty", "true")
    before = repo.state()
    repository = Repository(repo.root)
    comparison = assert_pair(repository, repo, fixture.merge, fixture.path, fixture.first, 1,
                             fixture.path, fixture.path, fixture.first_text, fixture.merged_text)
    assert "\n \n" in comparison.patch
    parsed = DiffPatch.parse(comparison.patch)
    assert parsed.hunks and all(hunk.valid for hunk in parsed.hunks)
    assert parsed.change_targets()
    for row in parsed.lines:
        if row.kind == "context" and row.text == " ":
            assert row.old is not None and row.new is not None
            old = repository.parent_diff_source(comparison, row.old, before=True)
            new = repository.parent_diff_source(comparison, row.new)
            assert old.snapshot.lines[row.old - 1] == new.snapshot.lines[row.new - 1] == ""
            break
    else:
        pytest.fail("Expected an actual blank context row in this parent patch.")
    assert repo.state() == before


def test_reachable_historical_merge_at_shallow_boundary_is_not_hidden_by_an_ordinary_tip(two_parent_merge, tmp_path):
    fixture = two_parent_merge
    repo = fixture.repo
    repo.write("later.txt", "ordinary tip\n")
    tip = repo.commit("Ordinary tip above merge")
    clone = tmp_path / "shallow boundary below tip"
    repo.git("clone", "--quiet", "--depth=2", repo.root.as_uri(), str(clone))
    clone_repo = copy.copy(repo)
    clone_repo.root = clone
    before = clone_repo.state()
    shallow = (clone / ".git" / "shallow").read_bytes()
    assert fixture.merge.encode() in shallow
    repository = Repository(clone)
    assert repository.commit_metadata().parents == (fixture.merge,)
    result = repository.reachable_merges()
    assert result.starting_commit == tip and not result.truncated
    assert [item.commit for item in result.merges] == [fixture.merge]
    assert result.merges[0].parents == (fixture.first, fixture.second)
    for index in (1, 2):
        with pytest.raises(TrailError, match="unavailable"):
            repository.parent_comparison(fixture.merge, fixture.path, index)
    assert clone_repo.state() == before and (clone / ".git" / "shallow").read_bytes() == shallow
