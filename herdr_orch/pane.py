"""The Attention pane's curses layer: draws the view model and maps keys to actions.

Checked by hand (see #2); everything it decides comes from herdr_orch.attention.
"""

from __future__ import annotations

import curses
import time
import textwrap
from pathlib import Path
from typing import Callable

from herdr_orch.attention import Row, View, answer, dismiss, load, open_pane
from herdr_orch.herdr import Herdr
from herdr_orch.topics import Topic

REFRESH_MS = 2000
OTHER = "Other…"
ICONS = {"decision": "?", "question": "?", "permission": "!", "blocked": "◐", "done": "✓"}


def _age(row: Row, now: float) -> str:
    minutes = int((now - row.created_at) // 60)
    return f"{minutes // 60}h" if minutes >= 120 else f"{minutes}m"


class App:
    def __init__(self, states: Path, topics: list[Topic], herdr: Herdr, now: Callable[[], float] = time.time) -> None:
        self.states, self.topics, self.herdr, self.now = states, topics, herdr, now
        self.view = View([], [])
        self.selected = 0
        self.choosing = False
        self.option = 0
        self.message = ""

    def refresh(self) -> None:
        try:
            self.view = load(self.states, self.topics, self.herdr, self.now())
        except Exception as e:  # herdr briefly unreachable: keep the last view
            self.message = f"refresh failed: {e}"
        self.selected = min(self.selected, max(len(self.view.rows()) - 1, 0))

    def current(self) -> Row | None:
        rows = self.view.rows()
        return rows[self.selected] if rows else None

    def choices(self, row: Row) -> list[str]:
        return row.options + [OTHER] if row.answerable else []

    # Drawing
    def draw(self, scr: curses.window) -> None:
        scr.erase()
        height, width = scr.getmaxyx()
        y = 0

        def put(text: str, attr: int = 0) -> None:
            nonlocal y
            if y < height - 1:
                scr.addnstr(y, 0, text, width - 1, attr)
                y += 1

        for banner in self.view.banners:
            put(f"⚠ {banner}", curses.A_BOLD | curses.color_pair(1))
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
                put(f" {marker} {ICONS.get(row.kind, '·')} {row.agent:<16} {row.kind:<10} {_age(row, now):>4}  {row.question}{guess}", attr)
                index += 1

        detail_top = max(y + 1, height // 2)
        y = detail_top
        if y < height - 1:
            scr.hline(y, 0, curses.ACS_HLINE, width)
            y += 1
        chosen = self.current()
        if chosen is not None:
            row = chosen
            for line in textwrap.wrap(f"{row.agent} · {row.question}", width - 2) or [""]:
                put(line, curses.A_BOLD)
            for i, choice in enumerate(self.choices(row)):
                marker = "❯" if self.choosing and i == self.option else " "
                attr = curses.A_REVERSE if self.choosing and i == self.option else 0
                put(f" {marker} {i + 1}. {choice}", attr)
            if not row.answerable:
                put("   Answer it in the agent's own pane: press o.", curses.A_DIM)
        footer = "[enter] answer  [o] open pane  [s] skip  [r] refresh  [q] quit"
        if self.choosing:
            footer = "[↑↓] choose  [enter] send  [esc] back"
        scr.addnstr(height - 1, 0, (self.message or footer)[: width - 1], width - 1, curses.A_DIM)
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

    # Keys
    def handle(self, scr: curses.window, key: int) -> bool:
        """False to quit."""
        row = self.current()
        self.message = ""
        if self.choosing and row is not None:
            choices = self.choices(row)
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
                        answer(self.states, row, text, self.herdr)
                        self.message = f"Sent to {row.agent}."
                    except Exception as e:
                        self.message = f"Not sent: {e}"
                self.choosing = False
                self.refresh()
            return True

        if key in (ord("q"),):
            return False
        if key in (curses.KEY_UP, ord("k")):
            self.selected = max(self.selected - 1, 0)
        elif key in (curses.KEY_DOWN, ord("j")):
            self.selected = min(self.selected + 1, max(len(self.view.rows()) - 1, 0))
        elif key == ord("r"):
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
            dismiss(self.states, row)
            self.message = "Skipped." if row.item_id else "Nothing to skip: it clears when the agent moves on."
            self.refresh()
        return True

    def run(self, scr: curses.window) -> None:
        curses.curs_set(0)
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_RED, -1)
        scr.timeout(REFRESH_MS)
        self.refresh()
        while True:
            self.draw(scr)
            key = scr.getch()
            if key == -1:
                self.refresh()
                continue
            if not self.handle(scr, key):
                return
