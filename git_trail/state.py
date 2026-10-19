from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .git import TrailError

DEFAULTS = {
    "sidebar_width": 24, "sidebar_visible": True,
    "find_case": False, "find_regex": False, "keybindings": {},
}
NORMAL_ACTIONS = {
    "files": "find_file", "find": "find_code", "symbols": "browse_symbols",
    "filter": "find_history", "follow": "follow", "help": "help",
    "sidebar": "toggle_sidebar",
    "back": "back", "forward": "forward", "bookmarks": "bookmarks",
    "nextchange": "nextchange", "prevchange": "prevchange", "diffsource": "diffsource",
}
RESERVED_KEYS = set("hjklwbvVgGnNq?/ :$0123456789")
MAX_STATE_BYTES = 1_000_000


def validate_mapping(key: str, action: str) -> None:
    if len(key) != 1 or not key.isprintable() or key.isspace() or key in RESERVED_KEYS:
        raise TrailError("Choose one unused printable normal-mode key; built-in Vim keys are reserved.")
    if action not in NORMAL_ACTIONS:
        raise TrailError("Mapping actions: " + ", ".join(NORMAL_ACTIONS))


def preferences(value: dict) -> dict:
    if not isinstance(value, dict):
        raise TrailError("Preferences must be an object.")
    result = dict(DEFAULTS, keybindings={})
    for key in DEFAULTS:
        if key in value:
            result[key] = value[key]
    if type(result["sidebar_width"]) is not int or not 16 <= result["sidebar_width"] <= 70:
        raise TrailError("Saved sidebar width must be between 16 and 70.")
    for key in ("sidebar_visible", "find_case", "find_regex"):
        if type(result[key]) is not bool:
            raise TrailError(f"Saved {key} must be a boolean.")
    bindings = result["keybindings"]
    if not isinstance(bindings, dict) or len(bindings) > 50:
        raise TrailError("Save at most 50 normal-mode mappings.")
    for key, action in bindings.items():
        if not isinstance(key, str) or not isinstance(action, str):
            raise TrailError("Mapping keys and actions must be strings.")
        validate_mapping(key, action)
    return result


class StateStore:
    """Versioned user settings outside inspected repositories."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self.warning = ""
        self.read_only = False
        self.data = {"version": 1, "preferences": preferences({}), "sessions": {}, "bookmarks": {}}
        try:
            with self.path.open("rb") as stream:
                raw = stream.read(MAX_STATE_BYTES + 1)
            if len(raw) > MAX_STATE_BYTES:
                raise TrailError("Settings file exceeds its size limit.")
            value = json.loads(raw)
            if not isinstance(value, dict) or type(value.get("version")) is not int or value.get("version") != 1:
                raise TrailError("Unsupported settings version.")
            value["preferences"] = preferences(value.get("preferences", {}))
            for key in ("sessions", "bookmarks"):
                if not isinstance(value.get(key, {}), dict):
                    raise TrailError(f"Saved {key} must be an object.")
                value.setdefault(key, {})
            self.data = value
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TrailError) as error:
            self.warning = f"Settings ignored: {error}. Repair or rename {self.path} to enable saving."
            self.read_only = True

    @classmethod
    def default(cls):
        directory = os.environ.get("GIT_TRAIL_STATE_DIR")
        if not directory:
            directory = str(Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "git-trail")
        return cls(Path(directory) / "state.json")

    def allow_repository(self, root: Path) -> None:
        if self.path.is_relative_to(root.resolve()):
            self.read_only = True
            raise TrailError("Settings path is inside the inspected repository. Set GIT_TRAIL_STATE_DIR outside it.")

    def save(self) -> None:
        if self.read_only:
            raise TrailError(self.warning or "Settings are read-only.")
        self.data["preferences"] = preferences(self.data["preferences"])
        raw = json.dumps(self.data, ensure_ascii=False, indent=2) + "\n"
        if len(raw.encode("utf-8")) > MAX_STATE_BYTES:
            raise TrailError("Settings size limit reached.")
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except OSError as error:
            raise TrailError(f"Could not save settings: {error}") from error
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
