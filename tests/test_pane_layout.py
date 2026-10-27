import curses
import unittest
from unittest import mock

from herdr_orch.attention import Group, Row, View, ledger_summary
from herdr_orch.pane import App, scroll_top


class FakeScreen:
    def __init__(self, height: int, width: int = 80) -> None:
        self.size = (height, width)
        self.cells: dict[int, str] = {}

    def getmaxyx(self) -> tuple[int, int]:
        return self.size

    def erase(self) -> None:
        self.cells = {}

    def addnstr(self, y: int, x: int, text: str, n: int, attr: int = 0) -> None:
        assert 0 <= y < self.size[0], f"drew off-screen at row {y}"
        self.cells[y] = text[:n]

    def hline(self, y: int, x: int, ch: int, n: int) -> None:
        self.cells[y] = "-" * n

    def refresh(self) -> None:
        pass

    def text(self) -> str:
        return "\n".join(self.cells.values())


def row(i: int) -> Row:
    return Row(agent=f"agent{i}", pane=f"w{i}:p1", kind="decision", text=f"question {i}", options=["A", "B"],
               created_at=0.0, item_id=str(i), topic="waki")


def app(n_rows: int, n_ledger: int) -> App:
    a = App.__new__(App)
    App.__init__(a, None, [], None, now=lambda: 600.0)  # type: ignore[arg-type]
    a.view = View([Group("g", [row(i) for i in range(n_rows)])], [], ["Topic waki: watching"],
                  ledger=[f"b{i}: waiting on review (x) 5m" for i in range(n_ledger)])
    return a


@mock.patch.object(curses, "color_pair", lambda n: 0)
@mock.patch.object(curses, "ACS_HLINE", 0, create=True)
class DrawTest(unittest.TestCase):
    def draw(self, a: App, height: int = 35) -> FakeScreen:
        scr = FakeScreen(height)
        a.draw(scr)  # type: ignore[arg-type]
        return scr

    def test_forty_rows_thirty_ledger_lines_keep_the_selection_and_its_options_visible(self) -> None:
        a = app(40, 30)
        a.selected = 39
        a.choosing = True
        scr = self.draw(a)
        text = scr.text()
        self.assertIn("question 39", text)
        self.assertIn("1. A", text)
        self.assertIn("2. B", text)
        self.assertNotIn("b0: waiting", text)  # the ledger is one summary line
        self.assertIn("30 waiting on review", text)

    def test_hits_cover_only_what_is_drawn(self) -> None:
        a = app(40, 30)
        a.selected = 25
        a.choosing = True
        scr = self.draw(a)
        for y, _, _, kind, arg in a.hits:
            self.assertIn(y, scr.cells)
            if kind == "row":
                self.assertIn(f"question {arg}", scr.cells[y])
            if kind == "option":
                self.assertIn(f"{arg + 1}.", scr.cells[y])
        self.assertEqual(len([h for h in a.hits if h[3] == "option"]), 3)

    def test_expanding_the_ledger_shows_its_lines(self) -> None:
        a = app(2, 3)
        a.ledger_open = True
        self.assertIn("b0: waiting on review", self.draw(a).text())

    def test_the_ledger_toggle_is_clickable(self) -> None:
        a = app(2, 3)
        self.draw(a)
        self.assertTrue(any(h[3] == "key" and h[4] == ord("l") for h in a.hits))

    def test_a_short_pane_still_shows_a_row_and_the_detail(self) -> None:
        a = app(5, 30)
        a.ledger_open = True
        a.selected = 2
        text = self.draw(a, height=12).text()
        self.assertIn("question 2", text)
        self.assertIn("agent2 · question 2", text)

    def test_wheel_scrolls_the_rows(self) -> None:
        a = app(40, 0)
        self.draw(a)
        a.scroll_rows(30)
        scr = self.draw(a)
        self.assertEqual(a.selected, 30)
        self.assertIn("question 30", scr.text())
        a.scroll_rows(-100)
        self.assertEqual(a.selected, 0)


class ScrollTopTest(unittest.TestCase):
    def test_keeps_the_selection_inside_the_window(self) -> None:
        self.assertEqual(scroll_top(0, 0, 10, 40), 0)
        self.assertEqual(scroll_top(0, 12, 10, 40), 3)
        self.assertEqual(scroll_top(20, 5, 10, 40), 5)
        self.assertEqual(scroll_top(30, 0, 10, 40), 0)

    def test_never_scrolls_past_the_end(self) -> None:
        self.assertEqual(scroll_top(35, 39, 10, 40), 30)
        self.assertEqual(scroll_top(5, 1, 10, 3), 0)


class LedgerSummaryTest(unittest.TestCase):
    def test_counts_by_state(self) -> None:
        lines = ["a: waiting on review (x) 5m", "b: waiting on review (y) 1m", "c: waiting on ci (z) 2m",
                 "untracked PR #1 (h): working (r) 3m"]
        self.assertEqual(ledger_summary(lines), "Ledger: 2 waiting on review, 1 waiting on ci, 1 working")

    def test_empty(self) -> None:
        self.assertEqual(ledger_summary([]), "")
