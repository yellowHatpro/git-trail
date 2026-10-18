from __future__ import annotations

import pytest

from git_trail.filters import HistoryFilter, filter_files, find_matches
from git_trail.git import HistoryEntry, TrailError


def entry(
    *, subject="Round tax", author="Jane Doe", committed_at="2024-03-04T23:30:00-08:00",
    authored_at="2001-01-01T00:00:01+00:00", patch="+return total_price\n",
):
    return HistoryEntry(
        "abcdef0123456789" + "0" * 24, (), author, authored_at, committed_at,
        subject, patch, 1, 1, 1, 1,
    )


def test_file_filter_combines_fuzzy_words_and_case_insensitive_extensions():
    paths = (
        "src/price_calculator.py", "src/player.py", "src/PLAYER.TS",
        "docs/player.md", "README", "src/other.py",
    )
    assert filter_files(paths, "src pcr ext:PY") == ("src/price_calculator.py",)
    assert filter_files(paths, "player ext:.py ext:ts") == ("src/PLAYER.TS", "src/player.py")
    assert filter_files(paths, "readme") == ("README",)
    assert filter_files(paths, "ext:md") == ("docs/player.md",)
    assert filter_files(paths, "") == tuple(sorted(paths))
    assert filter_files(paths, "not-found ext:py") == ()


def test_file_filter_ranks_contiguous_match_before_subsequence_match():
    assert filter_files(("src/abc.py", "src/a_big_class.py"), "abc") == (
        "src/abc.py", "src/a_big_class.py",
    )


def test_history_filter_combines_author_hash_patch_and_inclusive_commit_dates():
    selected = entry()
    predicate = HistoryFilter.parse(
        'round total_price author:"JANE DOE" hash:ABCDEF after:2024-03-04 before:2024-03-04'
    )
    assert predicate.matches(selected)
    assert not predicate.matches(entry(author="John Doe"))
    assert not predicate.matches(entry(patch="+return subtotal\n"))
    assert not predicate.matches(entry(committed_at="2024-03-03T23:59:59-08:00"))
    assert not predicate.matches(entry(committed_at="2024-03-05T00:00:00+05:30"))
    assert not HistoryFilter.parse("after:2001-01-01 before:2001-01-01").matches(selected)


@pytest.mark.parametrize(
    "query",
    [
        'author:"unclosed', "author:", "hash:", "after:", "before:",
        "after:2024-02-30", "before:20240304", "after:2024-03-05 before:2024-03-04",
    ],
)
def test_invalid_history_filter_reports_actionable_input_error(query):
    with pytest.raises(TrailError) as raised:
        HistoryFilter.parse(query)
    assert str(raised.value)


def test_find_literal_regex_case_and_multiline_coordinates():
    text = "def Target():\n    return target + target()\n"
    assert find_matches(text, "target") == (
        ((0, 4), (0, 10)), ((1, 11), (1, 17)), ((1, 20), (1, 26)),
    )
    assert find_matches(text, "Target", case_sensitive=True) == (((0, 4), (0, 10)),)
    assert find_matches(text, "target()") == (
        ((0, 4), (0, 12)), ((1, 20), (1, 28)),
    )
    assert find_matches(text, "target()", case_sensitive=True) == (((1, 20), (1, 28)),)
    assert find_matches(text, r"target(?=\()", regex=True) == (
        ((0, 4), (0, 10)), ((1, 20), (1, 26)),
    )
    assert find_matches(text, "():\n    return") == (((0, 10), (1, 10)),)
    assert find_matches(text, r"^", regex=True) == ()
    assert find_matches(text, "") == ()
    assert find_matches(text, "absent") == ()
    with pytest.raises(TrailError, match="Invalid expression"):
        find_matches(text, "[", regex=True)


def test_find_coordinates_use_lf_rows_and_unicode_character_columns():
    assert find_matches("é🙂\u2028same row\nmatch\n", "same row") == (((0, 3), (0, 11)),)
    assert find_matches("é🙂\u2028same row\nmatch\n", "match") == (((1, 0), (1, 5)),)
