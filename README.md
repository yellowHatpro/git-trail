# Git Trail

A terminal app for answering: **when did this part of the file change?**

Select a line, several lines, a paragraph, a function, or a class. Browse the commits
that changed that selection and inspect each selected-range diff.

![Git Trail showing source, its commit timeline, and a selected-range diff](docs/images/git-trail.svg)

## Run from source

Requires Git, Python 3.10+, and [uv](https://docs.astral.sh/uv/).

```sh
cd /path/to/git-trail
uv venv .venv
uv pip install --python .venv/bin/python --only-binary :all: \
  'textual==8.2.8' 'tree-sitter==0.25.2' 'tree-sitter-language-pack==0.10.0'
./git-trail /path/to/repository
```

The local launcher runs Python source directly; no packaging build is required.
Add this directory to PATH, or create a symlink to the launcher, to use `git-trail`
from anywhere.

```sh
./git-trail /path/to/repository src/player.py --lines 42
./git-trail /path/to/repository src/player.py --lines 10:35
./git-trail /path/to/repository src/player.py --symbol Player
./git-trail /path/to/repository README.md --paragraph 12
./git-trail /path/to/repository src/player.py --text 'return cached_value'
./git-trail /path/to/repository src/player.py --text 'return None' --occurrence 2
./git-trail /path/to/repository src/player.py --ref v1.2.0 --lines 10:35
```

Paths are relative to the repository root. Absolute paths inside the repository
also work. Filenames containing spaces or shell characters must be quoted as usual.
With no arguments, the current directory is the repository.

## In the terminal

The left pane searches files at your starting commit. The upper pane shows that
commit's source with line numbers. The lower panes show the timeline and selected
commit's diff, newest first.

| Key | Action |
| --- | --- |
| Ctrl+F | Focus file search; type a filter and press Enter to open |
| Shift+arrows or mouse drag | Select source text |
| Enter in source / Ctrl+T | Trace the current line or selected range |
| Ctrl+G | Go to a line number |
| Ctrl+P | Select the paragraph around the source cursor |
| Ctrl+B | Choose a parsed function or class |
| Ctrl+S | Select the smallest named symbol enclosing the source cursor |
| Ctrl+J / Ctrl+K | Older / newer change |
| Up / Down in timeline | Select a change and update its diff |
| Tab / Shift+Tab | Move between panes |
| Escape | Close a selection dialog |
| Ctrl+Q | Quit |

For a whole class, choose it with Ctrl+B. Ctrl+S inside a method selects that method.
Paragraphs are contiguous nonblank lines. Arbitrary text selections track every
whole line touched by the selection.

## CLI output

```sh
./git-trail /path/to/repository src/player.py --symbols
./git-trail /path/to/repository src/player.py --symbols --json
./git-trail /path/to/repository src/player.py --lines 10:35 --json
```

JSON includes the resolved starting commit, selection, warnings, commit metadata,
patches, and old/new hunk coordinates. No TUI starts for these commands.
The CLI returns status 2 for invalid input or Git errors.

## What the history means

Git Trail uses Git's `log -L` range history, rather than just blaming the latest
version of a line. Earlier line numbers can change as lines are inserted or removed.
Unrelated edits are omitted. Git can follow simple file renames.

Selection always uses a **committed snapshot**, HEAD by default. Uncommitted changes
are excluded and called out. A historical `--ref` uses the file as it existed at
that commit, even if the working tree differs.

This is textual history. A copied, moved, split, or renamed function is not
guaranteed to retain its identity. Merge traversal follows Git's range-history
rules; it is not a complete exploration of every parent. Shallow clones may lack
older changes, and this is reported.

The timeline shows commit dates. Author dates are also included in JSON. Git
does not record when a commit was pushed or first received by a remote repository.
Unusual control characters and Unicode line separators display as visible symbols;
line numbers follow Git's LF boundaries.

Functions and classes are selected using bundled Tree-sitter parsers. The initial
extension map supports Python, JavaScript/JSX, TypeScript/TSX, Rust, Go, Java,
C/C++, C#, Ruby, PHP, Swift, Kotlin, and Bash. Named declaration forms vary by
language. Unsupported forms or syntax errors require manual line selection.
Line, range, paragraph, and exact-text selection work for any nonempty UTF-8 text
file. Binary files are rejected.

Repository access is read-only. Git Trail does not fetch, clone, checkout, stage,
commit, or rewrite repository configuration. External diff, text conversion, and
optional index writes are disabled. Lazy fetching is disabled; the needed Git
objects must already be available locally.

## Development

```sh
uv pip install --python .venv/bin/python --only-binary :all: \
  'pytest==9.1.1' 'pytest-asyncio==1.4.0'
.venv/bin/python -m pytest -q
```

Tests create temporary local repositories and exercise real Git range history,
selectors, the CLI, and a headless Textual session. They do not use your
repositories or require network access.

All 166 checks pass with the pinned dependencies. A separate headless check
verified function selection with a long string literal, the symbol picker,
tracing, keyboard history navigation, and unchanged repository state.
