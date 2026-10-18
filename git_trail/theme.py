from rich.style import Style
from textual.widgets.text_area import TextAreaTheme


BACKGROUND = "#1a1b26"
FOREGROUND = "#c0caf5"

EDITOR_THEME = TextAreaTheme(
    name="git-trail",
    base_style=Style(color=FOREGROUND, bgcolor=BACKGROUND),
    gutter_style=Style(color="#565f89", bgcolor=BACKGROUND),
    cursor_style=Style(color=BACKGROUND, bgcolor=FOREGROUND),
    cursor_line_style=Style(bgcolor="#24283b"),
    cursor_line_gutter_style=Style(color="#7aa2f7", bgcolor=BACKGROUND),
    selection_style=Style(bgcolor="#283457"),
    bracket_matching_style=Style(color="#ff9e64", bold=True),
    syntax_styles={
        "comment": Style(color="#565f89", italic=True),
        "keyword": Style(color="#bb9af7"),
        "string": Style(color="#9ece6a"),
        "number": Style(color="#ff9e64"),
        "boolean": Style(color="#ff9e64"),
        "function": Style(color="#7aa2f7"),
        "function.call": Style(color="#7aa2f7"),
        "class": Style(color="#2ac3de"),
        "type": Style(color="#2ac3de"),
        "variable": Style(color=FOREGROUND),
        "operator": Style(color="#89ddff"),
        "punctuation": Style(color="#a9b1d6"),
    },
)
