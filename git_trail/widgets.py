from __future__ import annotations

from textual import events
from textual.binding import Binding
from textual.widget import Widget
from textual.containers import VerticalScroll
from textual.widgets import Static


class DiffScroll(VerticalScroll):
    BINDINGS = [
        Binding("up", "cursor_up", "Previous diff row", show=False),
        Binding("down", "cursor_down", "Next diff row", show=False),
        Binding("enter", "source", "Historical source", show=False),
    ]

    def on_resize(self) -> None:
        if self.app.is_mounted:
            self.app.call_after_refresh(self.app.refresh_diff_layout)

    def action_cursor_up(self) -> None:
        self.app.move_diff_cursor(-1)

    def action_cursor_down(self) -> None:
        self.app.move_diff_cursor(1)

    def action_source(self) -> None:
        if self.app.diff_focused():
            self.app.jump_diff_source()


class DiffContent(Static):
    def on_click(self, event: events.Click) -> None:
        if event.button == 1:
            offset = event.get_content_offset(self)
            if offset is not None:
                self.app.diff_click(offset.y, offset.x)
                event.stop()


class PaneDivider(Widget, can_focus=True):
    BINDINGS = [
        Binding("left", "narrow", "Narrow sidebar"),
        Binding("right", "widen", "Widen sidebar"),
    ]

    def __init__(self, *, tooltip: str = "", **kwargs):
        super().__init__(**kwargs)
        self.tooltip = tooltip
        self._drag_start: tuple[float, int] | None = None

    def render(self) -> str:
        return "│\n" * self.size.height

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button == 1:
            self._drag_start = (event.screen_x, self.app.sidebar_width)
            self.capture_mouse()
            self.focus()
            event.stop()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self._drag_start:
            position, width = self._drag_start
            self.app.resize_sidebar(width + int(event.screen_x - position))
            event.stop()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if self._drag_start:
            self._drag_start = None
            self.release_mouse()
            event.stop()

    def action_narrow(self) -> None:
        self.app.resize_sidebar(self.app.sidebar_width - 2)

    def action_widen(self) -> None:
        self.app.resize_sidebar(self.app.sidebar_width + 2)
