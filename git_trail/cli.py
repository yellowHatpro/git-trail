from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from . import __version__
from .git import LineRange, Repository, TrailError
from .selectors import exact_text, named_symbol, paragraph, symbols


def line_range(value: str) -> LineRange:
    try:
        parts = value.split(":")
        if len(parts) not in {1, 2}:
            raise ValueError
        start = int(parts[0])
        end = int(parts[-1])
        if start < 1 or end < start:
            raise ValueError
        return LineRange(start, end, "line" if start == end else "range")
    except ValueError as error:
        raise argparse.ArgumentTypeError("Use a positive line number or START:END, such as 42 or 10:25.") from error


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="git-trail",
        description="Browse the Git history of a line, paragraph, function, or class.",
        epilog="Without --json or --symbols, open the interactive TUI. Select committed source; local edits are excluded.",
    )
    result.add_argument("repository", nargs="?", default=".", help="Local repository directory (default: current directory)")
    result.add_argument("file", nargs="?", help="Repository-relative file path")
    result.add_argument("--ref", default="HEAD", help="Starting commit, tag, or branch (default: HEAD)")
    selection = result.add_mutually_exclusive_group()
    selection.add_argument("--lines", type=line_range, help="Line number or inclusive START:END")
    selection.add_argument("--paragraph", type=int, metavar="LINE", help="Paragraph containing this line")
    selection.add_argument("--symbol", metavar="NAME", help="Exact function or class name")
    selection.add_argument("--text", help="Exact source text (whole intersecting lines are tracked)")
    result.add_argument("--occurrence", type=int, default=1, help="1-based occurrence for --text")
    result.add_argument("--symbols", action="store_true", help="List parsed functions and classes and exit")
    result.add_argument("--json", action="store_true", help="Print selection history as JSON and exit")
    result.add_argument("--follow-symbol", action="store_true", help="Follow a complete symbol through refactors (first-parent, conservative)")
    result.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return result


def main(argv: list[str] | None = None) -> int:
    argument_parser = parser()
    args = argument_parser.parse_args(argv)
    has_selector = any(
        value is not None for value in (args.lines, args.paragraph, args.symbol, args.text)
    )
    if (has_selector or args.json or args.symbols) and not args.file:
        argument_parser.error("A file path is required for selectors, --symbols, and --json.")
    if args.occurrence != 1 and args.text is None:
        argument_parser.error("--occurrence requires --text.")
    if args.symbols and has_selector:
        argument_parser.error("--symbols cannot be combined with a selection.")
    if args.json and not has_selector and not args.symbols:
        argument_parser.error("--json requires --lines, --paragraph, --symbol, or --text.")
    if args.follow_symbol and (not args.file or args.symbols):
        argument_parser.error("--follow-symbol requires a file and cannot be combined with --symbols.")
    try:
        repository = Repository(args.repository, args.ref)
        snapshot = repository.snapshot(args.file) if args.file else None
        if args.symbols:
            assert snapshot is not None
            items = symbols(snapshot.text, snapshot.path)
            if args.json:
                print(json.dumps([asdict(item) for item in items], ensure_ascii=False, indent=2))
            else:
                for item in items:
                    print(f"{item.start}:{item.end}\t{item.kind}\t{item.name}")
            return 0
        selected = args.lines
        if snapshot:
            if args.paragraph is not None:
                selected = paragraph(snapshot.text, args.paragraph)
            elif args.symbol is not None:
                selected = named_symbol(snapshot.text, snapshot.path, args.symbol)
            elif args.text is not None:
                selected = exact_text(snapshot.text, args.text, args.occurrence)
            if selected:
                selected.validate(len(snapshot.lines))
        if args.json:
            assert snapshot is not None and selected is not None
            timeline = repository.symbol_history(snapshot, selected) if args.follow_symbol else repository.history(snapshot, selected)
            print(json.dumps(timeline.to_dict(), ensure_ascii=False, indent=2))
            return 0
        from .app import InitialSelection, TrailApp
        TrailApp(repository, InitialSelection(snapshot.path if snapshot else None, selected, args.follow_symbol)).run()
        return 0
    except TrailError as error:
        print(f"git-trail: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
