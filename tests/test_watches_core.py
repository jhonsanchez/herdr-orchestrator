import unittest

from herdr_orch.watches_core import StaleTracker, agent_changes, is_unattended

MIN = 60.0


class AgentChangesTest(unittest.TestCase):
    def test_reports_blocked_done_and_gone_for_managed_agents(self) -> None:
        prev = {"a": "working", "b": "working", "c": "idle"}
        now = {"a": "blocked", "b": "done", "x": "blocked"}
        self.assertEqual(
            agent_changes(prev, now, managed={"a", "b", "c"}),
            ["a blocked", "b done", "c gone"],
        )

    def test_first_sighting_and_unchanged_states_are_quiet(self) -> None:
        self.assertEqual(agent_changes({}, {"a": "blocked"}, managed={"a"}), [])
        self.assertEqual(agent_changes({"a": "done"}, {"a": "done"}, managed={"a"}), [])


class StaleTrackerTest(unittest.TestCase):
    def test_idle_agent_silent_past_stale_window_is_reported_once_per_realert(self) -> None:
        t = StaleTracker(stale_min=40, hung_min=60, realert_min=60)
        self.assertIsNone(t.observe("a", "idle", "h1", now=0))
        self.assertIsNone(t.observe("a", "idle", "h1", now=39 * MIN))
        self.assertEqual(t.observe("a", "idle", "h1", now=40 * MIN), "a stale (idle) 40m")
        self.assertIsNone(t.observe("a", "idle", "h1", now=50 * MIN))
        self.assertEqual(t.observe("a", "idle", "h1", now=100 * MIN), "a stale (idle) 100m")

    def test_working_agent_uses_hung_window_and_any_change_resets(self) -> None:
        t = StaleTracker(stale_min=40, hung_min=60, realert_min=60)
        t.observe("a", "working", "h1", now=0)
        self.assertIsNone(t.observe("a", "working", "h1", now=45 * MIN))
        self.assertIsNone(t.observe("a", "working", "h2", now=59 * MIN))
        self.assertIsNone(t.observe("a", "working", "h2", now=100 * MIN))
        self.assertEqual(t.observe("a", "working", "h2", now=119 * MIN), "a stale (working) 60m")


class UnattendedTest(unittest.TestCase):
    def test_missing_orchestrator_is_unattended(self) -> None:
        self.assertTrue(is_unattended(now=0, heartbeat_at=None, pending_since=None, orchestrator_present=False, window_min=15))

    def test_quiet_topic_with_nothing_pending_is_attended(self) -> None:
        self.assertFalse(is_unattended(now=10_000, heartbeat_at=0, pending_since=None, orchestrator_present=True, window_min=15))

    def test_pending_events_older_than_window_without_heartbeat_since(self) -> None:
        kw = dict(heartbeat_at=0.0, orchestrator_present=True, window_min=15)
        self.assertFalse(is_unattended(now=14 * MIN + 100, pending_since=100.0, **kw))  # type: ignore[arg-type]
        self.assertTrue(is_unattended(now=15 * MIN + 100, pending_since=100.0, **kw))  # type: ignore[arg-type]

    def test_heartbeat_after_pending_events_means_attended(self) -> None:
        self.assertFalse(is_unattended(now=100 * MIN, heartbeat_at=200.0, pending_since=100.0, orchestrator_present=True, window_min=15))


if __name__ == "__main__":
    unittest.main()
