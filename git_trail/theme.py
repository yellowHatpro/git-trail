from __future__ import annotations

from dataclasses import dataclass, fields
from types import MappingProxyType

from pygments.style import Style as PygmentsStyle
from pygments.token import Generic, Token
from rich.style import Style
from rich.syntax import PygmentsSyntaxTheme
from textual.theme import Theme
from textual.widgets.text_area import TextAreaTheme


@dataclass(frozen=True)
class Palette:
    name: str
    label: str
    dark: bool
    background: str
    sidebar: str
    surface: str
    selection: str
    foreground: str
    subtext: str
    muted: str
    border: str
    divider: str
    accent: str
    heading: str
    purple: str
    green: str
    red: str
    yellow: str
    orange: str
    cyan: str
    operator: str
    status: str
    error: str
    commit_foreground: str
    commit_muted: str
    overlay: str

    @property
    def editor_name(self) -> str:
        return "git-trail" if self.name == "tokyo-night" else "git-trail-" + self.name

    def app_theme(self) -> Theme:
        variables = {
            "trail-" + item.name.replace("_", "-"): getattr(self, item.name)
            for item in fields(self) if item.name not in {"name", "label", "dark"}
        }
        variables.update({
            "border": self.border,
            "border-blurred": self.divider,
            "foreground-muted": self.muted,
            "block-cursor-background": self.selection,
            "block-cursor-foreground": self.accent,
            "block-cursor-blurred-background": self.surface,
            "block-cursor-blurred-foreground": self.foreground,
            "input-cursor-background": self.foreground,
            "input-cursor-foreground": self.background,
            "input-selection-background": self.selection,
            "input-selection-foreground": self.foreground,
        })
        return Theme(
            name=self.name, primary=self.accent, secondary=self.purple,
            accent=self.heading, warning=self.yellow, error=self.red,
            success=self.green, foreground=self.foreground,
            background=self.background, surface=self.sidebar, panel=self.surface,
            dark=self.dark, variables=variables,
        )

    def editor_theme(self) -> TextAreaTheme:
        syntax_styles = {
            "comment": Style(color=self.muted, italic=True),
            "keyword": Style(color=self.purple),
            "string": Style(color=self.green),
            "number": Style(color=self.orange),
            "boolean": Style(color=self.orange),
            "function": Style(color=self.accent),
            "function.call": Style(color=self.accent),
            "class": Style(color=self.cyan),
            "type": Style(color=self.cyan),
            "variable": Style(color=self.foreground),
            "operator": Style(color=self.operator),
            "punctuation": Style(color=self.subtext),
        }
        for family, captures in (
            ("keyword", ("keyword.function", "keyword.return", "conditional", "repeat", "include", "import", "exception", "preproc")),
            ("operator", ("keyword.operator",)),
            ("type", ("type.builtin", "type.class", "type.definition", "type.qualifier", "constructor")),
            ("function", ("function.builtin", "function.macro", "function.method", "method", "method.call")),
            ("number", ("float",)),
            ("punctuation", ("punctuation.bracket", "punctuation.delimiter", "punctuation.special")),
        ):
            for capture in captures:
                syntax_styles[capture] = syntax_styles[family]
        return TextAreaTheme(
            name=self.editor_name,
            base_style=Style(color=self.foreground, bgcolor=self.background),
            gutter_style=Style(color=self.muted, bgcolor=self.background),
            cursor_style=Style(color=self.background, bgcolor=self.foreground),
            cursor_line_style=Style(bgcolor=self.surface),
            cursor_line_gutter_style=Style(color=self.accent, bgcolor=self.background),
            selection_style=Style(bgcolor=self.selection),
            bracket_matching_style=Style(color=self.orange, bold=True),
            syntax_styles=syntax_styles,
        )

    def diff_style(self) -> type[PygmentsStyle]:
        return type("TrailDiff_" + self.name.replace("-", "_"), (PygmentsStyle,), {
            "background_color": self.background,
            "styles": {
                Token: self.foreground,
                Generic.Deleted: self.red,
                Generic.Inserted: self.green,
                Generic.Heading: self.muted,
                Generic.Subheading: self.accent,
            },
        })

    def diff_theme(self) -> PygmentsSyntaxTheme:
        return PygmentsSyntaxTheme(self.diff_style())


DEFAULT_PALETTE = Palette(
    name="tokyo-night", label="Tokyo Night", dark=True,
    background="#1a1b26", sidebar="#16161e", surface="#24283b",
    selection="#283457", foreground="#c0caf5", subtext="#a9b1d6",
    muted="#565f89", border="#3b4261", divider="#292e42",
    accent="#7aa2f7", heading="#a5b4fc", purple="#bb9af7",
    green="#9ece6a", red="#f7768e", yellow="#e0af68",
    orange="#ff9e64", cyan="#2ac3de", operator="#89ddff",
    status="#9cafbf", error="#f49b9b", commit_foreground="#e2e8f0",
    commit_muted="#94a3b8", overlay="#000000",
)


def _catppuccin(name: str, label: str, dark: bool, colors: tuple[str, ...]) -> Palette:
    base, mantle, surface0, surface1, surface2, text, subtext, muted, blue, lavender, mauve, green, red, yellow, peach, teal, sky = colors
    return Palette(
        name="catppuccin-" + name, label="Catppuccin " + label, dark=dark,
        background=base, sidebar=mantle, surface=surface0, selection=surface1,
        foreground=text, subtext=subtext, muted=muted, border=surface2,
        divider=surface1, accent=blue, heading=lavender, purple=mauve,
        green=green, red=red, yellow=yellow, orange=peach, cyan=teal,
        operator=sky, status=subtext, error=red, commit_foreground=text,
        commit_muted=muted, overlay=mantle if dark else text,
    )


# Colors from catppuccin/palette palette.json and morhetz/gruvbox colors/gruvbox.vim.
PALETTES = MappingProxyType({palette.name: palette for palette in (
    DEFAULT_PALETTE,
    _catppuccin("latte", "Latte", False, (
        "#eff1f5", "#e6e9ef", "#ccd0da", "#bcc0cc", "#acb0be",
        "#4c4f69", "#5c5f77", "#6c6f85", "#1e66f5", "#7287fd",
        "#8839ef", "#40a02b", "#d20f39", "#df8e1d", "#fe640b",
        "#179299", "#04a5e5",
    )),
    _catppuccin("frappe", "Frappé", True, (
        "#303446", "#292c3c", "#414559", "#51576d", "#626880",
        "#c6d0f5", "#b5bfe2", "#a5adce", "#8caaee", "#babbf1",
        "#ca9ee6", "#a6d189", "#e78284", "#e5c890", "#ef9f76",
        "#81c8be", "#99d1db",
    )),
    _catppuccin("macchiato", "Macchiato", True, (
        "#24273a", "#1e2030", "#363a4f", "#494d64", "#5b6078",
        "#cad3f5", "#b8c0e0", "#a5adcb", "#8aadf4", "#b7bdf8",
        "#c6a0f6", "#a6da95", "#ed8796", "#eed49f", "#f5a97f",
        "#8bd5ca", "#91d7e3",
    )),
    _catppuccin("mocha", "Mocha", True, (
        "#1e1e2e", "#181825", "#313244", "#45475a", "#585b70",
        "#cdd6f4", "#bac2de", "#a6adc8", "#89b4fa", "#b4befe",
        "#cba6f7", "#a6e3a1", "#f38ba8", "#f9e2af", "#fab387",
        "#94e2d5", "#89dceb",
    )),
    Palette(
        name="gruvbox-dark", label="Gruvbox Dark", dark=True,
        background="#282828", sidebar="#1d2021", surface="#3c3836",
        selection="#504945", foreground="#ebdbb2", subtext="#d5c4a1",
        muted="#928374", border="#665c54", divider="#504945",
        accent="#83a598", heading="#fabd2f", purple="#d3869b",
        green="#b8bb26", red="#fb4934", yellow="#fabd2f",
        orange="#fe8019", cyan="#8ec07c", operator="#8ec07c",
        status="#d5c4a1", error="#fb4934", commit_foreground="#ebdbb2",
        commit_muted="#bdae93", overlay="#1d2021",
    ),
    Palette(
        name="gruvbox-light", label="Gruvbox Light", dark=False,
        background="#fbf1c7", sidebar="#f9f5d7", surface="#ebdbb2",
        selection="#d5c4a1", foreground="#3c3836", subtext="#504945",
        muted="#928374", border="#bdae93", divider="#d5c4a1",
        accent="#076678", heading="#b57614", purple="#8f3f71",
        green="#79740e", red="#9d0006", yellow="#b57614",
        orange="#af3a03", cyan="#427b58", operator="#427b58",
        status="#504945", error="#9d0006", commit_foreground="#3c3836",
        commit_muted="#665c54", overlay="#282828",
    ),
)})


def theme_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    name = "-".join(value.strip().casefold().replace("_", " ").replace("-", " ").split()).replace("é", "e")
    aliases = {
        "default": "tokyo-night", "tokyo": "tokyo-night", "tokyonight": "tokyo-night",
        "latte": "catppuccin-latte", "frappe": "catppuccin-frappe",
        "macchiato": "catppuccin-macchiato", "mocha": "catppuccin-mocha",
        "gruvbox": "gruvbox-dark", "gruvboxdark": "gruvbox-dark",
        "gruvboxlight": "gruvbox-light",
    }
    name = aliases.get(name, name)
    return name if name in PALETTES else None


BACKGROUND = DEFAULT_PALETTE.background
FOREGROUND = DEFAULT_PALETTE.foreground
EDITOR_THEME = DEFAULT_PALETTE.editor_theme()
DiffStyle = DEFAULT_PALETTE.diff_style()
DIFF_THEME = DEFAULT_PALETTE.diff_theme()
