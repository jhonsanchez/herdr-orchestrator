import os
import tempfile
import unittest
from pathlib import Path

from herdr_orch.state import TopicState, find_managed, state_root


class StateLayoutTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_override_env_wins_over_plugin_state_dir(self) -> None:
        env = {"HERDR_ORCH_STATE": "/a", "HERDR_PLUGIN_STATE_DIR": "/b"}
        self.assertEqual(state_root(env), Path("/a"))
        self.assertEqual(state_root({"HERDR_PLUGIN_STATE_DIR": "/b"}), Path("/b"))
        self.assertEqual(
            state_root({"HOME": "/h"}),
            Path("/h/.local/state/herdr/plugins/jhonsanchez.orchestrator"),
        )

    def test_topic_files_live_under_topics_folder(self) -> None:
        s = TopicState(self.root, "waki")
        self.assertEqual(s.inbox, self.root / "topics/waki/inbox.jsonl")
        self.assertEqual(s.events, self.root / "topics/waki/events.log")
        self.assertEqual(s.heartbeat, self.root / "topics/waki/heartbeat")

    def test_missing_files_read_as_empty(self) -> None:
        s = TopicState(self.root, "waki")
        self.assertEqual(s.managed(), {})
        self.assertIsNone(s.heartbeat_at())
        self.assertEqual(s.events_since(0), [])

    def test_take_and_give_back_an_agent(self) -> None:
        s = TopicState(self.root, "waki")
        s.take("w1:p2", "waki-12")
        self.assertEqual(s.managed(), {"w1:p2": "waki-12"})
        self.assertEqual(find_managed(self.root, "w1:p2"), ("waki", "waki-12"))
        s.give_back("w1:p2")
        self.assertEqual(s.managed(), {})
        self.assertIsNone(find_managed(self.root, "w1:p2"))

    def test_events_are_timestamped_and_read_since(self) -> None:
        s = TopicState(self.root, "waki")
        s.append_event("jira", "WAKI-1 Done", now=100.0)
        s.append_event("jira", "WAKI-2 Done", now=200.0)
        self.assertEqual(
            [(e.at, e.source, e.text) for e in s.events_since(150.0)],
            [(200.0, "jira", "WAKI-2 Done")],
        )

    def test_heartbeat_records_time(self) -> None:
        s = TopicState(self.root, "waki")
        s.touch_heartbeat(now=1234.0)
        self.assertEqual(s.heartbeat_at(), 1234.0)


if __name__ == "__main__":
    unittest.main()


class SocketRecordTest(unittest.TestCase):
    def test_the_topics_herdr_socket_is_remembered(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            s = TopicState(Path(d), "waki")
            self.assertIsNone(s.socket())
            s.record_socket("/run/herdr/waki.sock")
            self.assertEqual(s.socket(), "/run/herdr/waki.sock")
