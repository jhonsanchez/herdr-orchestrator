import unittest

from herdr_orch.attention import TopicStatus, build_view, current_items
from herdr_orch.herdr import Agent, Workspace

MIN = 60.0


def item(id: str, agent: str, pane: str, created: float, kind: str = "decision", status: str = "open", **kw: object) -> dict[str, object]:
    return {"v": 1, "id": id, "topic": "waki", "agent": agent, "pane": pane, "kind": kind, "question": f"q-{id}",
            "options": ["A (Recommended)", "B"], "guess": False, "status": status, "answer": None, "created_at": created, **kw}


class CurrentItemsTest(unittest.TestCase):
    def test_the_last_line_for_an_id_wins_and_only_open_items_remain(self) -> None:
        lines = [item("1", "a", "w1:p1", 10), item("2", "a", "w1:p1", 20), item("1", "a", "w1:p1", 10, status="resolved")]
        self.assertEqual([i["id"] for i in current_items(lines)], ["2"])


class BuildViewTest(unittest.TestCase):
    agents = [Agent("waki-1", "working", "w1:p1", "w1"), Agent("waki-2", "blocked", "w2:p1", "w2"),
              Agent("mine", "blocked", "w3:p1", "w3"), Agent("idle-one", "idle", "w3:p2", "w3")]
    workspaces = [Workspace("w1", "1-login", None), Workspace("w2", "2-signup", None), Workspace("w3", "scratch", None)]

    def test_items_are_grouped_by_workspace_oldest_first(self) -> None:
        items = [item("b", "waki-2", "w2:p1", 100), item("a", "waki-1", "w1:p1", 300), item("c", "waki-2", "w2:p1", 200)]
        view = build_view(items, self.agents, self.workspaces, managed={"w1:p1", "w2:p1"}, topics=[], now=400)
        self.assertEqual([g.label for g in view.groups], ["2-signup", "1-login", "scratch"])
        self.assertEqual([r.item_id for r in view.groups[0].rows], ["b", "c"])
        self.assertEqual(view.groups[0].rows[0].options, ["A (Recommended)", "B"])
        self.assertTrue(view.groups[0].rows[0].answerable)

    def test_unmanaged_agents_that_are_blocked_or_done_are_open_only_rows(self) -> None:
        view = build_view([], self.agents, self.workspaces, managed={"w1:p1", "w2:p1"}, topics=[], now=0)
        [group] = view.groups
        self.assertEqual(group.label, "scratch")
        [row] = group.rows
        self.assertEqual((row.agent, row.kind, row.item_id, row.answerable), ("mine", "blocked", None, False))

    def test_permission_items_can_only_be_opened(self) -> None:
        view = build_view([item("p", "waki-1", "w1:p1", 1, kind="permission", options=[])], self.agents, self.workspaces,
                          managed={"w1:p1"}, topics=[], now=2)
        self.assertFalse(view.groups[0].rows[0].answerable)

    def test_an_item_whose_agent_is_gone_still_shows(self) -> None:
        view = build_view([item("x", "ghost", "w9:p9", 1)], [], [], managed=set(), topics=[], now=2)
        self.assertEqual(view.groups[0].label, "(agent gone)")

    def test_unattended_topics_get_a_banner(self) -> None:
        topics = [TopicStatus("waki", orchestrator_present=False, heartbeat_at=None, pending_since=None, window_min=15),
                  TopicStatus("zebra", orchestrator_present=True, heartbeat_at=0, pending_since=10, window_min=15),
                  TopicStatus("quiet", orchestrator_present=True, heartbeat_at=0, pending_since=None, window_min=15)]
        view = build_view([], [], [], managed=set(), topics=topics, now=16 * MIN)
        self.assertEqual(view.banners, ["Topic waki is Unattended: its Orchestrator is not running",
                                        "Topic zebra is Unattended: Events unanswered for 15m"])


if __name__ == "__main__":
    unittest.main()
