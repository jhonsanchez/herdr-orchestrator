import tempfile
import unittest
from pathlib import Path

from herdr_orch.attention import load
from herdr_orch.herdr import Agent
from herdr_orch.runner import Runner
from herdr_orch.state import TopicState
from herdr_orch.topics import Topic
from herdr_orch.watchdog import DEFAULT_LIMITS_MIN, limit_min
from tests.fakes import FakeHerdr

MIN = 60


class Quiet:
    def poll(self, now: float) -> None: ...
    def lines(self) -> list[tuple[str, str]]:
        return []


class WatchdogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = TopicState(self.root, "waki")
        self.state.take("w1:p1", "issue-1")
        self.state.track_branch("issue-1", "issue-1", "/r", "w9", now=0)
        self.herdr = FakeHerdr()
        self.herdr.agent_list = [Agent("orch-waki", "working", "w2:p1", "w2"), Agent("issue-1", "idle", "w1:p1", "w9")]
        self.topic = Topic(name="waki", dir=self.root / "cfg", match=["/r"])
        self.runner = self.make_runner(self.topic)

    def make_runner(self, topic: Topic) -> Runner:
        return Runner(topic, self.state, self.herdr, Quiet(), notify=lambda t, b: None)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def wait(self, what: str, owner: str, since: float) -> None:
        self.state.set_ledger("issue-1", {"state": "waiting_on", "waiting_on": what, "owner": owner}, since)

    def stuck_events(self) -> list[str]:
        return [e.text for e in self.state.events_since(float("-inf")) if e.source == "stuck"]

    def prompts(self) -> list[tuple[object, ...]]:
        return [c for c in self.herdr.calls if c[0] == "prompt"]

    def test_defaults_and_overrides(self) -> None:
        self.assertEqual(DEFAULT_LIMITS_MIN, {"ci": 20, "review": 60, "conflict": 15, "deploy": 15, "validate": 10, "agent": 10})
        self.assertEqual((limit_min("ci", {}), limit_min("ci", {"ci": 5}), limit_min("merge", {})), (20, 5, None))

    def test_nothing_before_the_limit(self) -> None:
        self.wait("ci", "agent", 0)
        self.runner.tick(20 * MIN - 1)
        self.assertEqual((self.stuck_events(), self.prompts()), ([], []))

    def test_past_the_limit_one_event_and_one_nudge_to_the_owning_agent(self) -> None:
        self.wait("ci", "agent", 0)
        for t in (20 * MIN, 25 * MIN, 30 * MIN):
            self.runner.tick(t)
        self.assertEqual(self.stuck_events(), ["issue-1 has been waiting on ci (agent) for 20m"])
        (call,) = self.prompts()
        self.assertEqual(call[1], "issue-1")
        self.assertIn("You have been waiting on ci for 20 minutes: say what you are waiting on, or continue.", str(call[2]))

    def test_a_runner_restart_repeats_neither_the_event_nor_the_nudge(self) -> None:
        self.wait("ci", "agent", 0)
        self.runner.tick(21 * MIN)
        again = self.make_runner(self.topic)
        again.tick(22 * MIN)
        self.assertEqual((len(self.stuck_events()), len(self.prompts())), (1, 1))

    def test_a_non_agent_owner_gets_an_event_but_no_nudge(self) -> None:
        self.wait("review", "human", 0)
        self.runner.tick(61 * MIN)
        self.assertEqual(len(self.stuck_events()), 1)
        self.assertEqual(self.prompts(), [])

    def test_a_blocked_agent_is_nudged_on_a_later_tick(self) -> None:
        self.wait("ci", "agent", 0)
        self.herdr.blocked_agents.add("issue-1")
        self.runner.tick(21 * MIN)
        self.assertEqual(self.prompts(), [])
        self.assertEqual(len(self.stuck_events()), 1)
        self.herdr.blocked_agents.clear()
        self.runner.tick(22 * MIN)
        self.runner.tick(23 * MIN)
        self.assertEqual((len(self.stuck_events()), len(self.prompts())), (1, 1))

    def test_still_stuck_after_a_second_limit_it_becomes_an_attention_followup_once(self) -> None:
        self.wait("ci", "agent", 0)
        self.runner.tick(20 * MIN)
        self.runner.tick(39 * MIN)
        self.assertEqual(len(self.stuck_events()), 1)
        self.runner.tick(40 * MIN)
        self.runner.tick(50 * MIN)
        self.assertEqual(self.stuck_events()[1], "issue-1 is still waiting on ci after 40m: raised to you")
        self.assertEqual(len(self.stuck_events()), 2)
        self.state.touch_heartbeat(50 * MIN)
        view = load(self.root, [self.topic], self.herdr, 50 * MIN)
        self.assertIn("issue-1 stuck on ci for 50m: needs a follow-up (re-seat issue-1 or tell the user)",
                      [r.text for g in view.groups for r in g.rows])

    def test_a_state_that_changes_resets_the_watchdog(self) -> None:
        self.wait("ci", "agent", 0)
        self.runner.tick(41 * MIN)
        self.wait("conflict", "agent", 45 * MIN)
        self.runner.tick(50 * MIN)
        view = load(self.root, [self.topic], self.herdr, 50 * MIN)
        self.assertEqual([r for g in view.groups for r in g.rows if r.kind == "followup"], [])
        self.runner.tick(60 * MIN)
        self.assertEqual(self.stuck_events()[-1], "issue-1 has been waiting on conflict (agent) for 15m")
        self.assertEqual(len(self.prompts()), 2)

    def test_the_topics_stuck_table_overrides_a_limit(self) -> None:
        runner = self.make_runner(Topic(name="waki", dir=self.root / "cfg", match=["/r"], stuck={"ci": 5}))
        self.wait("ci", "agent", 0)
        runner.tick(5 * MIN)
        self.assertEqual(len(self.stuck_events()), 1)

    def test_a_working_branch_is_never_stuck(self) -> None:
        self.state.set_ledger("issue-1", {"state": "working", "waiting_on": None, "owner": "agent"}, 0)
        self.runner.tick(500 * MIN)
        self.assertEqual(self.stuck_events(), [])
