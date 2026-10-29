import tempfile
import unittest
from pathlib import Path

from herdr_orch.attention import current_items
from herdr_orch.herdr import Agent
from herdr_orch.runner import Runner
from herdr_orch.state import TopicState
from herdr_orch.topics import Topic
from tests.fakes import FakeHerdr

MIN = 60.0


class FakeWatches:
    def __init__(self) -> None:
        self.pending: list[tuple[str, str]] = []

    def poll(self, now: float) -> None:
        pass

    def lines(self) -> list[tuple[str, str]]:
        out, self.pending = self.pending, []
        return out


class RunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.topic = Topic(name="waki", dir=root / "cfg", heartbeat_window_min=15)
        self.state = TopicState(root, "waki")
        self.herdr = FakeHerdr()
        self.watches = FakeWatches()
        self.notes: list[tuple[str, str]] = []
        self.runner = Runner(self.topic, self.state, self.herdr, self.watches,
                             notify=lambda t, b: self.notes.append((t, b)))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def agents(self, orch: str | None = "idle", **managed: str) -> None:
        self.herdr.agent_list = [Agent(n, s, f"w1:{n}", "w1") for n, s in managed.items()]
        if orch:
            self.herdr.agent_list.append(Agent("orch-waki", orch, "w2:p1", "w2"))

    def texts(self) -> list[str]:
        return [e.text for e in self.state.events_since(0)]

    def test_the_runner_says_a_line_when_a_tick_sees_events_and_is_silent_otherwise(self) -> None:
        said: list[str] = []
        runner = Runner(self.topic, self.state, self.herdr, self.watches, notify=lambda t, b: None, say=said.append)
        self.state.take("w1:waki-1", "waki-1")
        self.agents(**{"waki-1": "working"})
        runner.tick(1000.0)
        self.assertEqual(said, [])
        self.agents(**{"waki-1": "blocked"})
        runner.tick(1010.0)
        self.assertEqual(len(said), 1)
        self.assertIn("1 new event", said[0])

    def test_a_managed_agent_turning_blocked_becomes_an_event(self) -> None:
        self.state.take("w1:waki-1", "waki-1")
        self.agents(orch="working", **{"waki-1": "working"})
        self.runner.tick(now=0)
        self.agents(orch="working", **{"waki-1": "blocked"})
        self.runner.tick(now=10)
        self.assertEqual(self.texts(), ["waki-1 blocked"])

    def test_a_gone_agents_registry_entry_is_removed(self) -> None:
        self.state.take("w1:waki-1", "waki-1")
        self.agents(orch="working", **{"waki-1": "working"})
        self.runner.tick(now=0)
        self.agents(orch="working")
        self.runner.tick(now=10)
        self.assertEqual(self.texts(), ["waki-1 gone"])
        self.assertEqual(self.state.managed(), {})

    def test_a_registry_entry_stays_while_its_pane_still_hosts_the_agent(self) -> None:
        self.state.take("w1:waki-1", "waki-1")
        self.agents(orch="working", **{"waki-1": "working"})
        self.runner.tick(now=0)
        self.runner.tick(now=10)
        self.assertEqual(self.state.managed(), {"w1:waki-1": "waki-1"})

    def test_watch_lines_become_events(self) -> None:
        self.agents(orch="working")
        self.watches.pending = [("jira", "WAKI-7 Done")]
        self.runner.tick(now=5)
        self.assertEqual([(e.source, e.text) for e in self.state.events_since(0)], [("jira", "WAKI-7 Done")])

    def test_events_reach_an_idle_orchestrator_once_and_wait_while_it_works(self) -> None:
        self.agents(orch="working")
        self.watches.pending = [("jira", "WAKI-7 Done")]
        self.runner.tick(now=5)
        self.assertEqual(self.herdr.of("prompt"), [])
        self.agents(orch="idle")
        self.runner.tick(now=10)
        [prompt] = self.herdr.of("prompt")
        self.assertEqual(prompt[1], "orch-waki")
        self.assertIn("jira: WAKI-7 Done", str(prompt[2]))
        self.runner.tick(now=20)
        self.assertEqual(len(self.herdr.of("prompt")), 1)

    def test_events_are_held_while_the_person_is_typing_and_delivered_once_the_box_is_clear(self) -> None:
        self.agents(orch="idle")
        self.watches.pending = [("jira", "WAKI-7 Done")]
        self.herdr.busy_agents.add("orch-waki")
        self.runner.tick(now=10)
        self.runner.tick(now=20)
        self.assertEqual(self.herdr.of("prompt"), [])
        self.herdr.busy_agents.clear()
        self.runner.tick(now=30)
        [prompt] = self.herdr.of("prompt")
        self.assertIn("jira: WAKI-7 Done", str(prompt[2]))

    def test_a_restarted_runner_redelivers_what_was_never_answered(self) -> None:
        self.state.append_event("jira", "answered", now=100)
        self.state.touch_heartbeat(now=150)
        self.state.append_event("jira", "logged before a crash", now=200)
        runner = Runner(self.topic, self.state, self.herdr, self.watches, notify=lambda t, b: None)
        self.agents(orch="idle")
        runner.tick(now=300)
        [prompt] = self.herdr.of("prompt")
        self.assertIn("logged before a crash", str(prompt[2]))
        self.assertNotIn("answered\n", str(prompt[2]) + "\n")

    def test_the_turn_after_a_delivery_records_a_heartbeat(self) -> None:
        self.agents(orch="idle")
        self.watches.pending = [("jira", "x")]
        self.runner.tick(now=10)
        self.assertIsNone(self.state.heartbeat_at())
        self.agents(orch="done")
        self.runner.tick(now=40)
        self.assertEqual(self.state.heartbeat_at(), 40)

    def test_a_missing_orchestrator_notifies_once_until_it_returns(self) -> None:
        self.agents(orch=None)
        self.runner.tick(now=0)
        self.runner.tick(now=10)
        self.assertEqual(len(self.notes), 1)
        self.assertIn("waki", self.notes[0][0])
        self.agents(orch="idle")
        self.runner.tick(now=20)
        self.agents(orch=None)
        self.runner.tick(now=30)
        self.assertEqual(len(self.notes), 2)

    def test_events_waiting_a_whole_window_without_heartbeat_notify(self) -> None:
        self.agents(orch="blocked")
        self.watches.pending = [("jira", "x")]
        self.runner.tick(now=0)
        self.runner.tick(now=14 * MIN)
        self.assertEqual(self.notes, [])
        self.runner.tick(now=15 * MIN)
        self.assertEqual(len(self.notes), 1)

    def test_a_silent_managed_agent_is_reported_stale(self) -> None:
        self.state.take("w1:waki-1", "waki-1")
        self.agents(orch="working", **{"waki-1": "idle"})
        self.herdr.panes["w1:waki-1"] = "same screen"
        self.runner.tick(now=0)
        self.runner.tick(now=40 * MIN)
        self.assertEqual(self.texts(), ["waki-1 stale (idle) 40m"])

    def test_a_missing_attention_pane_is_opened_beside_the_orchestrator_and_not_twice(self) -> None:
        self.herdr.agent_list = [Agent("orch-waki", "idle", "w5:p1", "w5")]
        self.runner.tick(0)
        self.runner.tick(15)
        self.assertEqual(self.herdr.of("attention_open"), [("attention_open", "w5", "w5:p1", "waki")])

    def test_a_detached_orchestrator_is_prompted_in_its_own_session_and_gets_no_attention_pane(self) -> None:
        orch = FakeHerdr()
        orch.agent_list = [Agent("orch-waki", "idle", "w1:p1", "w1")]
        runner = Runner(self.topic, self.state, self.herdr, self.watches, notify=lambda t, b: self.notes.append((t, b)), orch_herdr=orch)
        self.watches.pending = [("jira", "WAKI-7 Done")]
        runner.tick(now=10)
        self.assertEqual([p[1] for p in orch.of("prompt")], ["orch-waki"])
        self.assertEqual(self.herdr.of("prompt"), [])
        self.assertEqual(self.herdr.of("attention_open") + orch.of("attention_open"), [])
        self.assertEqual(self.notes, [])  # not Unattended: the Orchestrator is there, just not in this session

    def test_every_tick_records_the_runner_alive(self) -> None:
        self.herdr.agent_list = [Agent("orch-waki", "idle", "w5:p1", "w5")]
        self.runner.tick(10)
        self.runner.tick(25)
        status = self.state.runner_status()
        assert status is not None
        self.assertEqual((status.at, status.ticks), (25.0, 2))

    def test_the_runner_is_done_once_the_orchestrator_is_gone_a_whole_grace_period(self) -> None:
        self.herdr.agent_list = [Agent("orch-waki", "idle", "w5:p1", "w5")]
        self.runner.tick(0)
        self.herdr.agent_list = []
        self.runner.tick(15)
        self.assertEqual(len(self.notes), 1)  # the Unattended alert goes out before it stops
        self.runner.tick(60)
        self.assertFalse(self.runner.done)
        self.runner.tick(75)
        self.assertTrue(self.runner.done)

    def test_clear_runner_only_removes_its_own_record(self) -> None:
        self.state.record_runner(111, 0, 1)
        self.state.clear_runner(222)
        self.assertIsNotNone(self.state.runner_status())
        self.state.clear_runner(111)
        self.assertIsNone(self.state.runner_status())

    def test_a_question_resolves_when_its_agent_starts_a_turn_again(self) -> None:
        self.state.take("w1:waki-1", "waki-1")
        self.state.escalate("waki-1", "w1:waki-1", "question", "which?", [], 1.0)
        self.state.escalate("waki-1", "w1:waki-1", "decision", "merge?", [], 1.0, to="orchestrator")
        self.agents(**{"waki-1": "blocked"})
        self.runner.tick(10.0)
        self.assertEqual(len(current_items(self.state.inbox_lines())), 2)
        self.agents(**{"waki-1": "working"})
        self.runner.tick(20.0)
        [left] = current_items(self.state.inbox_lines())
        self.assertEqual(left["to"], "orchestrator")


if __name__ == "__main__":
    unittest.main()
