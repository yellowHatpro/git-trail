from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from git_trail.git import LineRange, TrailError
from git_trail.selectors import (
    enclosing_symbol, exact_text, named_symbol, paragraph, symbols,
)


def coordinates(selection):
    return selection.start, selection.end


def test_paragraph_expands_to_contiguous_nonblank_lines():
    text = "first paragraph\nsecond line\n\n  third paragraph\n  fourth line\n \t\nlast\n"
    assert coordinates(paragraph(text, 2)) == (1, 2)
    assert coordinates(paragraph(text, 4)) == (4, 5)
    assert coordinates(paragraph(text, 7)) == (7, 7)


@pytest.mark.parametrize("line", [3, 6])
def test_blank_lines_are_not_treated_as_paragraphs(line):
    with pytest.raises(TrailError, match="blank"):
        paragraph("one\ntwo\n\nthree\nfour\n \t\n", line)


@pytest.mark.parametrize("separator", ["\u2028", "\v", "\f", "\r", "\x85"])
def test_paragraph_uses_git_lf_line_numbers(separator):
    source = f"first{separator}still first\n\nselected\nnext\n"
    assert coordinates(paragraph(source, 1)) == (1, 1)
    assert coordinates(paragraph(source, 3)) == (3, 4)
    with pytest.raises(TrailError, match="blank"):
        paragraph(source, 2)


@pytest.mark.parametrize("line", [0, -1, 4])
def test_paragraph_rejects_out_of_bounds_lines(line):
    with pytest.raises(TrailError):
        paragraph("one\n\ntwo\n", line)


def test_exact_text_selects_every_intersecting_line_and_occurrence():
    text = "before\nalpha beta\nnext line\nalpha beta\nlast\n"
    assert coordinates(exact_text(text, "beta")) == (2, 2)
    assert coordinates(exact_text(text, "beta", occurrence=2)) == (4, 4)
    assert coordinates(exact_text(text, "beta\nnext")) == (2, 3)
    assert coordinates(exact_text(text, "alpha beta\n")) == (2, 2)
    assert coordinates(exact_text(text, "last\n")) == (5, 5)


def test_exact_text_is_literal_and_supports_unicode_and_overlapping_matches():
    assert coordinates(exact_text("π [x].* ${value}\n", "[x].* ${value}")) == (1, 1)
    assert coordinates(exact_text("banana\n", "ana", occurrence=2)) == (1, 1)
    assert coordinates(exact_text("中文\nvalue\n", "中文")) == (1, 1)


@pytest.mark.parametrize("value,occurrence", [
    ("", 1), ("absent", 1), ("found", 0), ("found", -1), ("found", 2),
])
def test_invalid_text_selection_is_actionable(value, occurrence):
    with pytest.raises(TrailError):
        exact_text("found\n", value, occurrence)


PYTHON_SOURCE = (
    "import functools\n"
    "\n"
    "@functools.lru_cache()\n"
    "def outer(value):\n"
    "    def inner():\n"
    "        return value\n"
    "    return inner()\n"
    "\n"
    "class Widget:\n"
    "    def method(self):\n"
    "        return 42\n"
    "\n"
    "async def async_target():\n"
    "    return 7\n"
)


def test_python_functions_classes_methods_nested_symbols_and_decorators():
    result = symbols(PYTHON_SOURCE, "module.py")
    by_name = {symbol.name: symbol for symbol in result}
    assert set(by_name) == {"outer", "inner", "Widget", "method", "async_target"}
    assert coordinates(by_name["outer"].selection) == (3, 7)
    assert coordinates(by_name["inner"].selection) == (5, 6)
    assert coordinates(by_name["Widget"].selection) == (9, 11)
    assert coordinates(by_name["method"].selection) == (10, 11)
    assert coordinates(by_name["async_target"].selection) == (13, 14)
    assert by_name["Widget"].kind == "class"
    assert all(by_name[name].kind == "function"
               for name in ("outer", "inner", "method", "async_target"))
    assert [symbol.start for symbol in result] == sorted(symbol.start for symbol in result)


def test_enclosing_symbol_chooses_smallest_named_scope():
    assert coordinates(enclosing_symbol(PYTHON_SOURCE, "module.py", 6)) == (5, 6)
    assert coordinates(enclosing_symbol(PYTHON_SOURCE, "module.py", 7)) == (3, 7)
    assert coordinates(enclosing_symbol(PYTHON_SOURCE, "module.py", 11)) == (10, 11)
    assert coordinates(enclosing_symbol(PYTHON_SOURCE, "module.py", 9)) == (9, 11)
    assert coordinates(named_symbol(PYTHON_SOURCE, "module.py", "Widget")) == (9, 11)


def test_duplicate_method_names_are_ambiguous_instead_of_guessing():
    source = (
        "class First:\n"
        "    def run(self):\n"
        "        return 1\n"
        "\n"
        "class Second:\n"
        "    def run(self):\n"
        "        return 2\n"
    )
    with pytest.raises(TrailError, match="ambiguous"):
        named_symbol(source, "module.py", "run")
    assert coordinates(enclosing_symbol(source, "module.py", 7)) == (6, 7)


@pytest.mark.parametrize("line", [0, 1, 2, 100])
def test_symbol_selection_rejects_outside_scope_or_file(line):
    with pytest.raises(TrailError):
        enclosing_symbol(PYTHON_SOURCE, "module.py", line)


def test_missing_symbol_name_is_actionable():
    with pytest.raises(TrailError, match="not found"):
        named_symbol(PYTHON_SOURCE, "module.py", "missing")


@pytest.mark.parametrize("source,path", [
    ("def broken(:\n    pass\n", "broken.py"),
    ("function broken( {", "broken.js"),
    ("class Broken {", "broken.ts"),
])
def test_syntax_errors_require_manual_line_selection(source, path):
    with pytest.raises(TrailError, match="syntax"):
        symbols(source, path)


def test_unsupported_filetype_requires_manual_line_selection():
    with pytest.raises(TrailError, match="unavailable"):
        symbols("a document paragraph\n", "README.md")
    assert coordinates(paragraph("a document paragraph\n", 1)) == (1, 1)


@pytest.mark.parametrize("path,source,expected", [
    (
        "main.js",
        "export function compute(x) { return x + 1; }\n"
        "const increment = (x) => x + 1;\n"
        "class Counter {\n  next() { return 1; }\n}\n",
        {"compute": (1, 1, "function"), "increment": (2, 2, "function"),
         "Counter": (3, 5, "class"), "next": (4, 4, "function")},
    ),
    (
        "main.ts",
        "export class Store {\n  read(): number { return 1; }\n}\n"
        "function compute(x: number): number { return x; }\n",
        {"Store": (1, 3, "class"), "read": (2, 2, "function"),
         "compute": (4, 4, "function")},
    ),
    (
        "view.tsx",
        "function View() {\n  return <span>Hello</span>;\n}\n",
        {"View": (1, 3, "function")},
    ),
    (
        "main.rs",
        "struct Store {\n    value: i32,\n}\n"
        "fn compute() -> i32 {\n    42\n}\n",
        {"Store": (1, 3, "class"), "compute": (4, 6, "function")},
    ),
    (
        "main.go",
        "package main\n\n"
        "func compute() int {\n    return 42\n}\n",
        {"compute": (3, 5, "function")},
    ),
    (
        "main.cpp",
        "class Store {\npublic:\n    int read() { return 1; }\n};\n"
        "int compute() {\n    return 42;\n}\n",
        {"Store": (1, 4, "class"), "read": (3, 3, "function"),
         "compute": (5, 7, "function")},
    ),
    (
        "main.c",
        "int compute(void) {\n    return 42;\n}\n",
        {"compute": (1, 3, "function")},
    ),
    (
        "Store.java",
        "class Store {\n    int read() { return 1; }\n}\n",
        {"Store": (1, 3, "class"), "read": (2, 2, "function")},
    ),
    (
        "main.rb",
        "class Store\n  def read\n    1\n  end\nend\n",
        {"Store": (1, 5, "class"), "read": (2, 4, "function")},
    ),
    (
        "main.sh",
        "compute() {\n  printf 'hello\\n'\n}\n",
        {"compute": (1, 3, "function")},
    ),
])
def test_representative_language_declarations_have_exact_ranges(path, source, expected):
    result = {symbol.name: (symbol.start, symbol.end, symbol.kind)
              for symbol in symbols(source, path)}
    for name, bounds in expected.items():
        assert result.get(name) == bounds
        assert coordinates(named_symbol(source, path, name)) == bounds[:2]


def test_symbol_name_bytes_are_decoded_without_ascii_column_assumptions():
    source = "def café():\n    return '中文'\n"
    result = symbols(source, "unicode.py")
    assert [(item.name, item.start, item.end) for item in result] == [("café", 1, 2)]
    assert coordinates(named_symbol(source, "unicode.py", "café")) == (1, 2)


def test_uppercase_extension_uses_the_same_parser():
    assert coordinates(named_symbol("def run():\n    pass\n", "MODULE.PY", "run")) == (1, 2)


def test_large_python_symbol_traversal_does_not_crash_native_parser():
    source = 'def target():\n    return "' + "y" * 500 + '"\n'
    code = (
        "import json, sys\n"
        "from git_trail.selectors import symbols\n"
        "result = symbols(json.load(sys.stdin), 'player.py')\n"
        "json.dump([[item.name, item.kind, item.start, item.end] for item in result], sys.stdout)\n"
    )
    result = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", code],
        input=json.dumps(source), text=True,
        cwd=Path(__file__).resolve().parents[1],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [["target", "function", 1, 2]]
