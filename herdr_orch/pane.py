"""The Attention pane's curses layer: draws the view model and maps keys to actions.

Checked by hand (see #2); everything it decides comes from herdr_orch.attention.
"""

from __future__ import annotations

import curses
import time
import textwrap
from pathlib import Path
from typing import Callable

from herdr_orch.attention import OTHER, Row, View, answer, dismiss, load, open_pane
from herdr_orch.herdr import Herdr
from herdr_orch.topics import Topic

REFRESH_MS = 2000
SCOPE_ENV = "HERDR_ORCH_ATTENTION_TOPIC"  # set when the pane is opened beside a Topic's Orchestrator
ICONS = {"decision": "?", "question": "?", "permission": "!", "blocked": "◐", "done": "✓", "followup": "↻"}


def _age(row: Row, now: float) -> str:
    if row.created_at is None:
        return "—"
    minutes = int((now - row.created_at) // 60)
    return f"{minutes // 60}h" if minutes >= 120 else f"{minutes}m"


class App:
    def __init__(self, states: Path, topics: list[Topic], herdr: Herdr, now: Callable[[], float] = time.time,
                 scope: str | None = None) -> None:
        self.states, self.topics, self.herdr, self.now = states, topics, herdr, now
        self.home_scope = scope  # None: the standalone, all-Topics pane
        self.scope = scope
        self.view = View([], [])
        self.selected = 0
        self.choosing = False
        self.option = 0
        self.message = ""
        self.hidden: set[tuple[str, str]] = set()  # skipped open-only rows, as (pane, status)
        self.hits: list[tuple[int, int, int, str, int]] = []  # drawn click targets: (y, x0, x1, kind, arg)
        self.detail_y = 0  # first screen row of the detail pane
        self.detail_off = 0
        self.detail_for = -1

    def refresh(self) -> None:
        try:
            self.view = load(self.states, self.topics, self.herdr, self.now(), self.hidden, self.scope)
        except Exception as e:  # herdr briefly unreachable: keep the last view
            self.message = f"refresh failed: {e}"
        self.selected = min(self.selected, self.last())

    def last(self) -> int:
        return max(len(self.view.rows()) - 1, 0)

    def current(self) -> Row | None:
        rows = self.view.rows()
        return rows[self.selected] if rows else None

    # Drawing
    def draw(self, scr: curses.window) -> None:
        scr.erase()
        height, width = scr.getmaxyx()
        y = 0
        self.hits = []

        def put(text: str, attr: int = 0) -> None:
            nonlocal y
            if y < height - 1:
                scr.addnstr(y, 0, text, width - 1, attr)
                y += 1

        header = f"Topic {self.scope}" if self.scope else "All Topics"
        label = header + ("  ([a] toggle)" if self.home_scope else "")
        if self.home_scope:
            self.hits.append((0, 0, len(label), "key", ord("a")))
        put(label, curses.A_BOLD)
        for banner in self.view.banners:
            put(f"⚠ {banner}", curses.A_BOLD | curses.color_pair(1))
        for line in self.view.status:
            put(f"· {line}", curses.A_DIM)
        rows = self.view.rows()
        if not rows:
            put("Nothing needs you.", curses.A_DIM)
        now = self.now()
        index = 0
        for group in self.view.groups:
            put(f"▾ {group.label}", curses.A_BOLD)
            for row in group.rows:
                marker = "❯" if index == self.selected else " "
                attr = curses.A_REVERSE if index == self.selected and not self.choosing else 0
                guess = " (guess)" if row.guess else ""
                if y < height - 1:
                    self.hits.append((y, 0, width, "row", index))
                put(f" {marker} {ICONS.get(row.kind, '·')} {row.agent:<16} {row.kind:<10} {_age(row, now):>4}  {row.text}{guess}", attr)
                index += 1

        detail_top = max(y + 1, height // 2)
        y = detail_top
        if y < height - 1:
            scr.hline(y, 0, curses.ACS_HLINE, width)
            y += 1
        self.detail_y = y
        chosen = self.current()
        if chosen is not None:
            row = chosen
            lines: list[tuple[str, int, int | None]] = [
                (line, curses.A_BOLD, None) for line in textwrap.wrap(f"{row.agent} · {row.text}", width - 2) or [""]]
            for i, choice in enumerate(row.choices):
                marker = "❯" if self.choosing and i == self.option else " "
                attr = curses.A_REVERSE if self.choosing and i == self.option else 0
                lines.append((f" {marker} {i + 1}. {choice}", attr, i))
            if not row.answerable:
                lines.append(("   Answer it in the agent's own pane: press o.", curses.A_DIM, None))
            if self.detail_for != self.selected:
                self.detail_for, self.detail_off = self.selected, 0
            self.detail_off = max(min(self.detail_off, len(lines) - (height - 1 - y)), 0)
            for text, attr, option in lines[self.detail_off:]:
                if option is not None and y < height - 1:
                    self.hits.append((y, 0, width, "option", option))
                put(text, attr)
        buttons = [("[↑↓] choose", None), ("[enter] send", 10), ("[esc] back", 27)] if self.choosing else [
            ("[enter] answer", 10), ("[o] open pane", ord("o")), ("[s] skip", ord("s")),
            ("[r] refresh", ord("r")), ("[q] quit", ord("q"))]
        if self.message:
            scr.addnstr(height - 1, 0, self.message[: width - 1], width - 1, curses.A_DIM)
        else:
            x = 0
            for text, key in buttons:
                if key is not None:
                    self.hits.append((height - 1, x, x + len(text), "key", key))
                x += len(text) + 2
            scr.addnstr(height - 1, 0, "  ".join(t for t, _ in buttons)[: width - 1], width - 1, curses.A_DIM)
        scr.refresh()

    def ask_text(self, scr: curses.window, prompt: str) -> str:
        height, width = scr.getmaxyx()
        scr.move(height - 1, 0)
        scr.clrtoeol()
        scr.addnstr(height - 1, 0, prompt, width - 1)
        curses.echo()
        curses.curs_set(1)
        try:
            raw = scr.getstr(height - 1, min(len(prompt), width - 2), 400)
        finally:
            curses.noecho()
            curses.curs_set(0)
        return raw.decode(errors="replace").strip()

    # Mouse
    def handle_mouse(self, scr: curses.window) -> bool:
        """False to quit. A click selects; a double-click or a button click acts."""
        try:
            _, x, y, _, state = curses.getmouse()
        except curses.error:
            return True
        wheel_up, wheel_down = curses.BUTTON4_PRESSED, getattr(curses, "BUTTON5_PRESSED", 0)
        if state & (wheel_up | wheel_down):
            step = -1 if state & wheel_up else 1
            if y >= self.detail_y:
                self.detail_off = max(self.detail_off + step, 0)
            elif not self.choosing:
                self.selected = min(max(self.selected + step, 0), self.last())
            return True
        double = bool(state & curses.BUTTON1_DOUBLE_CLICKED)
        if not state & (curses.BUTTON1_CLICKED | curses.BUTTON1_DOUBLE_CLICKED):
            return True
        hit = next((h for h in self.hits if h[0] == y and h[1] <= x < h[2]), None)
        if hit is None:
            return True
        _, _, _, kind, arg = hit
        if kind == "key":
            return self.handle(scr, arg)
        if kind == "option" and self.choosing:
            self.option = arg
            return self.handle(scr, 10)
        if kind == "row" and not self.choosing:
            self.selected = arg
            if double:
                return self.handle(scr, 10)
        return True

    # Keys
    def handle(self, scr: curses.window, key: int) -> bool:
        """False to quit."""
        row = self.current()
        self.message = ""
        if self.choosing and row is not None:
            choices = row.choices
            if key in (curses.KEY_UP, ord("k")):
                self.option = (self.option - 1) % len(choices)
            elif key in (curses.KEY_DOWN, ord("j")):
                self.option = (self.option + 1) % len(choices)
            elif key == 27:
                self.choosing = False
            elif key in (curses.KEY_ENTER, 10, 13):
                text = choices[self.option]
                if text == OTHER:
                    text = self.ask_text(scr, "Your answer: ")
                if text:
                    try:
                        orch = next((t.orchestrator for t in self.topics if t.name == row.topic), None)
                        sent_to = answer(self.states, row, text, self.herdr, orch)
                        self.message = f"Sent to {sent_to}."
                    except Exception as e:
                        self.message = f"Not sent, item kept: {e}. [s] dismiss  [o] open pane"
                self.choosing = False
                self.refresh()
            return True

        if key in (ord("q"),):
            return False
        if key in (curses.KEY_UP, ord("k")):
            self.selected = max(self.selected - 1, 0)
        elif key in (curses.KEY_DOWN, ord("j")):
            self.selected = min(self.selected + 1, self.last())
        elif key == ord("r"):
            self.refresh()
        elif key == ord("a") and self.home_scope:
            self.scope = None if self.scope else self.home_scope
            self.selected = 0
            self.refresh()
        elif row is None:
            return True
        elif key in (curses.KEY_ENTER, 10, 13) and row.answerable:
            self.choosing, self.option = True, 0
        elif key == ord("o"):
            try:
                open_pane(row, self.herdr)
            except Exception as e:
                self.message = f"Could not open: {e}"
        elif key == ord("s"):
            if row.kind == "followup":
                self.message = "A follow-up stays until the Orchestrator writes an Inbox item."
                return True
            if row.item_id:
                dismiss(self.states, row)
            else:
                self.hidden.add((row.pane, row.kind))  # back once the agent's status changes
            self.message = "Skipped."
            self.refresh()
        return True

    def run(self, scr: curses.window) -> None:
        curses.curs_set(0)
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_RED, -1)
        scr.keypad(True)
        curses.mousemask(curses.ALL_MOUSE_EVENTS)
        scr.timeout(REFRESH_MS)
        self.refresh()
        while True:
            self.draw(scr)
            key = scr.getch()
            if key == -1:
                self.refresh()
                continue
            if not (self.handle_mouse(scr) if key == curses.KEY_MOUSE else self.handle(scr, key)):
                return
