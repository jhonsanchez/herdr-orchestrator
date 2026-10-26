import io
import json
import os
import signal
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from herdr_orch.cli import main, resolve_topic
from herdr_orch.herdr import Workspace
from herdr_orch.notify import notifier
from herdr_orch.state import TopicState
from herdr_orch.topics import load_topics
from tests.fakes import FakeHerdr


class NotifyTest(unittest.TestCase):
    def test_command_gets_title_and_body_on_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "got"
            notifier(["sh", "-c", f"cat > {out}"])("Topic waki unattended", "3 events unanswered")
            self.assertEqual(out.read_text(), "Topic waki unattended\n3 events unanswered\n")

    def test_no_command_means_no_notification(self) -> None:
        notifier([])("t", "b")  # must not raise


class WatchShutdownTest(unittest.TestCase):
    def test_sigterm_stops_the_runner_and_its_watches(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            env = dict(os.environ, HERDR_ORCH_CONFIG=str(root / "cfg"), HERDR_ORCH_STATE=str(root / "state"),
                       HERDR_BIN_PATH="/bin/false")
            cli = str(Path(__file__).resolve().parent.parent / "bin" / "herdr-orch")
            subprocess.run([cli, "init", "t"], env=env, check=True, capture_output=True)
            pidfile = root / "watch.pid"
            w = root / "cfg/topics/t/watches/w"
            w.write_text(f"#!/bin/sh\necho $$ > {pidfile}\nexec sleep 60\n")
            w.chmod(0o755)
            proc = subprocess.Popen([cli, "watch", "t"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + 10
            while not pidfile.exists() and time.monotonic() < deadline:
                time.sleep(0.1)
            watch_pid = int(pidfile.read_text())
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=10)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    os.kill(watch_pid, 0)
                except ProcessLookupError:
                    return
                time.sleep(0.05)
            self.fail(f"watch {watch_pid} still running after the runner stopped")


class CliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.env = {"HERDR_ORCH_CONFIG": str(root / "cfg"), "HERDR_ORCH_STATE": str(root / "state"), "HOME": "/home/u"}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_init_then_heartbeat(self) -> None:
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["init", "waki"], self.env), 0)
            self.assertEqual(main(["heartbeat", "waki"], self.env, now=lambda: 77.0), 0)
        self.assertTrue((Path(self.env["HERDR_ORCH_CONFIG"]) / "topics/waki/playbook.md").is_file())
        self.assertTrue((Path(self.env["HERDR_ORCH_STATE"]) / "topics/waki").is_dir())
        self.assertEqual(TopicState(Path(self.env["HERDR_ORCH_STATE"]), "waki").heartbeat_at(), 77.0)

    def test_unknown_topic_is_an_error(self) -> None:
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["heartbeat", "nope"], self.env), 2)

    def test_action_context_resolves_the_topic_by_membership(self) -> None:
        with redirect_stdout(io.StringIO()):
            main(["init", "waki"], self.env)
        toml = Path(self.env["HERDR_ORCH_CONFIG"]) / "topics/waki/topic.toml"
        toml.write_text('match = ["~/projects/waki/*"]\n')
        topics = load_topics(Path(self.env["HERDR_ORCH_CONFIG"]))
        herdr = FakeHerdr()
        herdr.workspace_list = [Workspace("w4", "waki-platform", "/home/u/projects/waki/waki-platform")]
        ctx = json.dumps({"workspace_id": "w4"})
        t = resolve_topic(ctx, "", topics, herdr, home="/home/u")
        self.assertEqual(t.name if t else None, "waki")
        self.assertIsNone(resolve_topic(json.dumps({"workspace_id": "w9"}), "", topics, herdr, home="/home/u"))


if __name__ == "__main__":
    unittest.main()


class LinkedLauncherTest(unittest.TestCase):
    def test_symlinked_launcher_still_finds_the_package(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            link = Path(d) / "herdr-orch"
            link.symlink_to(Path(__file__).resolve().parent.parent / "bin" / "herdr-orch")
            r = subprocess.run([str(link), "--help"], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
