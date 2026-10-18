from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from .git import LineRange, TrailError, source_lines


@dataclass(frozen=True)
class Symbol:
    name: str
    kind: str
    start: int
    end: int

    @property
    def selection(self) -> LineRange:
        return LineRange(self.start, self.end, f"{self.kind} {self.name}")


LANGUAGES = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "tsx",
    ".rs": "rust", ".go": "go", ".java": "java",
    ".c": "c", ".h": "cpp", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp",
    ".cs": "csharp", ".rb": "ruby", ".php": "php", ".swift": "swift",
    ".kt": "kotlin", ".kts": "kotlin", ".sh": "bash",
}

FUNCTION_TYPES = {
    "function_definition", "function_declaration", "function_item",
    "method_definition", "method_declaration", "method", "singleton_method",
    "constructor_declaration", "arrow_function", "function_expression",
}
CLASS_TYPES = {
    "class_definition", "class_declaration", "class", "struct_item",
    "struct_specifier", "class_specifier", "interface_declaration",
    "enum_declaration", "enum_item", "impl_item", "object_declaration", "type_spec",
}


def paragraph(text: str, line: int) -> LineRange:
    lines = source_lines(text)
    LineRange(line, line).validate(len(lines))
    index = line - 1
    if not lines[index].strip():
        raise TrailError("The cursor is on a blank line. Choose a line inside a paragraph.")
    start, end = index, index
    while start > 0 and lines[start - 1].strip():
        start -= 1
    while end + 1 < len(lines) and lines[end + 1].strip():
        end += 1
    return LineRange(start + 1, end + 1, "paragraph")


def exact_text(text: str, value: str, occurrence: int = 1) -> LineRange:
    if not value:
        raise TrailError("The text selector cannot be empty.")
    if occurrence < 1:
        raise TrailError("The text occurrence must be at least 1.")
    offset = -1
    for _ in range(occurrence):
        offset = text.find(value, offset + 1)
        if offset < 0:
            raise TrailError(f"Text occurrence {occurrence} was not found in this snapshot.")
    start = text.count("\n", 0, offset) + 1
    stop = offset + len(value)
    if value.endswith("\n"):
        stop -= 1
    end = text.count("\n", 0, stop) + 1
    return LineRange(start, end, f"text occurrence {occurrence}")


def symbols(text: str, path: str) -> tuple[Symbol, ...]:
    language = LANGUAGES.get(PurePosixPath(path).suffix.lower())
    if language is None:
        raise TrailError("Symbol selection is unavailable for this file type. Select lines or a paragraph.")
    try:
        from tree_sitter_language_pack import get_parser
        parser = get_parser(language)
        tree = parser.parse(text.encode("utf-8"))
    except (ImportError, LookupError, RuntimeError) as error:
        raise TrailError(f"Could not load the {language} parser. Select a line range instead.") from error
    if tree.root_node.has_error:
        raise TrailError("This file has syntax errors. Select a line range instead of a parsed symbol.")
    found = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        stack.extend(reversed(node.children))
        if node.type not in FUNCTION_TYPES | CLASS_TYPES:
            continue
        if node.type == "type_spec":
            type_node = node.child_by_field_name("type")
            if type_node is None or type_node.type not in {"struct_type", "interface_type"}:
                continue
        name_node = node.child_by_field_name("name")
        if name_node is None:
            name_node = node.child_by_field_name("declarator")
        if name_node is None and node.parent is not None:
            parent = node.parent
            if parent.type in {"variable_declarator", "assignment", "pair"}:
                name_node = parent.child_by_field_name("name") or parent.child_by_field_name("left")
            if node.type == "impl_item":
                name_node = node.child_by_field_name("type")
        if name_node is None and language == "kotlin":
            name_node = next(
                (
                    child for child in node.named_children
                    if child.type in {"type_identifier", "simple_identifier"}
                ),
                None,
            )
        if name_node is None:
            continue
        name = name_node.text.decode("utf-8", errors="replace")
        if "(" in name:
            name = name.split("(", 1)[0].strip()
        boundary = node
        if node.parent is not None and node.parent.type in {"decorated_definition", "export_statement"}:
            boundary = node.parent
        start = boundary.start_point.row + 1
        end = boundary.end_point.row + (1 if boundary.end_point.column else 0)
        found.append(Symbol(name, "class" if node.type in CLASS_TYPES else "function", start, max(start, end)))
    return tuple(sorted(found, key=lambda symbol: (symbol.start, -symbol.end)))


def named_symbol(text: str, path: str, name: str) -> LineRange:
    matches = [symbol for symbol in symbols(text, path) if symbol.name == name]
    if not matches:
        raise TrailError(f"Function or class {name!r} was not found. Use --symbols to list them.")
    if len(matches) > 1:
        ranges = ", ".join(f"{symbol.start}:{symbol.end}" for symbol in matches)
        raise TrailError(f"{name!r} is ambiguous. Select one of these ranges with --lines: {ranges}.")
    return matches[0].selection


def enclosing_symbol(text: str, path: str, line: int) -> LineRange:
    LineRange(line, line).validate(len(source_lines(text)))
    matches = [symbol for symbol in symbols(text, path) if symbol.start <= line <= symbol.end]
    if not matches:
        raise TrailError("The cursor is outside a named function or class. Select a range instead.")
    return min(matches, key=lambda symbol: symbol.end - symbol.start).selection
