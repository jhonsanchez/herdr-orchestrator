import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from herdr_orch.attention import load
from herdr_orch.cli import main
from herdr_orch.herdr import Agent
from herdr_orch.runner import Runner
from herdr_orch.state import Event, TopicState
from herdr_orch.topics import Topic
from herdr_orch.watches_core import open_obligations
from tests.fakes import FakeHerdr

MIN = 60.0


def item(agent: str, created: float) -> dict[str, Any]:
    return {"agent": agent, "created_at": created}


class ObligationsTest(unittest.TestCase):
    def test_done_gone_and_idle_stale_each_open_an_obligation(self) -> None:
        events = [Event(10, "agents", "a done"), Event(20, "agents", "b gone"), Event(30, "stale", "c stale (idle) 40m")]
        got = open_obligations(events, [], {"a": "done", "c": "idle"}, {"a", "b", "c"})
        self.assertEqual([(o.agent, o.reason) for o in got], [("a", "done"), ("b", "gone"), ("c", "stale (idle)")])

    def test_an_inbox_item_written_after_it_closes_it_and_chat_does_not(self) -> None:
        events = [Event(10, "agents", "a done")]
        self.assertEqual(open_obligations(events, [item("a", 5)], {}, {"a"})[0].agent, "a")
        self.assertEqual(open_obligations(events, [item("a", 11)], {}, {"a"}), [])

    def test_a_repeated_stale_alert_does_not_reopen_it(self) -> None:
        events = [Event(10, "stale", "a stale (idle) 40m"), Event(5000, "stale", "a stale (idle) 100m")]
        self.assertEqual(open_obligations(events, [item("a", 20)], {"a": "idle"}, {"a"}), [])

    def test_working_again_or_given_back_or_a_working_stale_is_not_owed(self) -> None:
        events = [Event(10, "agents", "a done"), Event(10, "stale", "b stale (working) 60m")]
        self.assertEqual(open_obligations(events, [], {"a": "working", "b": "working"}, {"a", "b"}), [])
        self.assertEqual(open_obligations(events, [], {}, set()), [])

    def test_a_pr_of_any_state_resolves_the_followup(self) -> None:
        events = [Event(10, "agents", "a done")]
        for state in ("open", "merged", "closed"):
            branch = {"agent": "a", "pr_state": state, "pr": 5, "checks": "passing", "pr_since": 10.0}
            self.assertEqual(open_obligations(events, [], {"a": "done"}, {"a"}, [branch]), [], state)

    def test_a_gone_agent_is_owed_although_its_registry_entry_is_removed(self) -> None:
        events = [Event(10, "agents", "a gone")]
        self.assertEqual([o.agent for o in open_obligations(events, [], {}, set())], ["a"])
        self.assertEqual(open_obligations(events, [], {"a": "idle"}, set()), [])  # seated again
        self.assertEqual(open_obligations(events, [item("a", 10)], {}, set()), [])

class FollowupFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.topic = Topic(name="waki", dir=self.root / "cfg", heartbeat_window_min=15)
        self.state = TopicState(self.root, "waki")
        self.state.take("w1:p1", "issue-1")
        (self.root / "cfg" / "topics" / "waki").mkdir(parents=True)
        (self.root / "cfg" / "topics" / "waki" / "topic.toml").write_text("")
        self.herdr = FakeHerdr()
        self.notes: list[tuple[str, str]] = []

        class Quiet:
            def poll(self, now: float) -> None: ...
            def lines(self) -> list[tuple[str, str]]:
                return []

        self.runner = Runner(self.topic, self.state, self.herdr, Quiet(), notify=lambda t, b: self.notes.append((t, b)))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def chases(self) -> list[tuple[str, str]]:
        return [n for n in self.notes if "follow-up" in n[0]]

    def set(self, status: str | None) -> None:
        self.herdr.agent_list = [Agent("issue-1", status, "w1:p1", "w1")] if status else []
        self.herdr.agent_list.append(Agent("orch-waki", "working", "w2:p1", "w2"))

    def view(self, now: float) -> Any:
        return load(self.root, [self.topic], self.herdr, now)

    def test_done_then_pr_inbox_item_clears_it(self) -> None:
        self.set("working")
        self.runner.tick(0)
        self.set("done")
        self.runner.tick(60)
        self.runner.tick(120)
        [row] = self.view(120).rows()
        self.assertEqual((row.kind, row.agent), ("followup", "issue-1"))
        self.assertEqual(self.view(120).banners, [])
        self.assertTrue(any("issue-1 done" in b and "no follow-up" in b for b in self.view(20 * MIN).banners))
        self.assertEqual(main(["inbox", "add", "--topic", "waki", "--agent", "issue-1", "--question", "Merge PR #5?",
                               "--option", "Merge (Recommended)", "--option", "Wait"],
                              env={"HERDR_ORCH_STATE": str(self.root), "HERDR_ORCH_CONFIG": str(self.root / "cfg")},
                              now=lambda: 200.0, adapter=self.herdr), 0)
        rows = self.view(300).rows()
        self.assertEqual([(r.kind, r.options[0]) for r in rows], [("decision", "Merge (Recommended)")])

    def test_stale_idle_and_gone_show_and_a_late_runner_notifies_once(self) -> None:
        self.set("idle")
        self.runner.tick(0)
        self.runner.tick(41 * MIN)
        self.assertEqual([r.kind for r in self.view(41 * MIN).rows()], ["followup"])
        self.assertEqual(self.chases(), [])
        self.runner.tick(57 * MIN)
        self.runner.tick(58 * MIN)
        self.assertEqual(len(self.chases()), 1)
        self.set(None)
        self.runner.tick(59 * MIN)
        self.assertEqual(self.view(59 * MIN).rows()[0].kind, "followup")
        self.assertIn("gone", self.view(59 * MIN).rows()[0].text)

    def test_inbox_add_for_an_unknown_agent_fails(self) -> None:
        env = {"HERDR_ORCH_STATE": str(self.root), "HERDR_ORCH_CONFIG": str(self.root / "cfg")}
        self.herdr.agent_list = []
        self.assertEqual(main(["inbox", "add", "--topic", "waki", "--agent", "nobody", "--question", "q"], env=env,
                              adapter=self.herdr), 1)
        self.assertEqual(json.dumps(self.state.inbox_lines()), "[]")


if __name__ == "__main__":
    unittest.main()
