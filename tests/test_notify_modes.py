import json
import tempfile
import unittest
from pathlib import Path

from herdr_orch.notify import notifier_for
from herdr_orch.topics import Topic


class StopHookNotifyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.out = self.root / "got"
        settings = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": f"cat >> {self.out}; echo >> {self.out}"}]}]}}
        (self.root / ".claude").mkdir()
        (self.root / ".claude/settings.json").write_text(json.dumps(settings))
        self.env = {"HOME": str(self.root), "HERDR_PANE_ID": "w1:p1"}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_the_alert_runs_the_users_stop_hooks_as_a_turn_ending(self) -> None:
        topic = Topic(name="personal", dir=self.root / "t", notify="stop-hook")
        notifier_for(topic, self.env)("Topic personal is Unattended", "its Orchestrator is not running")
        sent = json.loads(self.out.read_text().splitlines()[0])
        self.assertEqual(sent["last_assistant_message"], "Topic personal is Unattended: its Orchestrator is not running")
        self.assertEqual(sent["cwd"].rsplit("/", 1)[-1], "personal")
        self.assertEqual(sent["hook_event_name"], "Stop")

    def test_the_settings_of_the_shells_claude_account_are_used(self) -> None:
        other = self.root / "other"
        other.mkdir()
        (other / "settings.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
            {"type": "command", "command": f"echo other >> {self.out}"}]}]}}))
        topic = Topic(name="personal", dir=self.root / "t", notify="stop-hook")
        notifier_for(topic, {**self.env, "CLAUDE_CONFIG_DIR": str(other)})("t", "b")
        self.assertEqual(self.out.read_text().strip(), "other")

    def test_no_settings_means_no_alert_and_no_error(self) -> None:
        topic = Topic(name="personal", dir=self.root / "t", notify="stop-hook")
        notifier_for(topic, {"HOME": str(self.root / "nobody")})("t", "b")
        self.assertFalse(self.out.exists())

    def test_a_notify_command_wins_over_the_mode(self) -> None:
        topic = Topic(name="personal", dir=self.root / "t", notify="stop-hook",
                      notify_command=["sh", "-c", f"cat > {self.out}"])
        notifier_for(topic, self.env)("Title", "Body")
        self.assertEqual(self.out.read_text(), "Title\nBody\n")


if __name__ == "__main__":
    unittest.main()
