import io
import json
import os
import signal
import subprocess
import tempfile
import time
import unittest
from unittest import mock
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from herdr_orch import cli
from herdr_orch.cli import main, resolve_topic
from herdr_orch.herdr import Agent, Workspace
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
        self.env["HOME"] = self.tmp.name  # heartbeat keeps the stable herdr-orch link under HOME
        with redirect_stdout(io.StringIO()), mock.patch("herdr_orch.seating.spawn_runner"):
            self.assertEqual(main(["init", "waki"], self.env), 0)
            self.assertEqual(main(["heartbeat", "waki"], self.env, now=lambda: 77.0), 0)
        self.assertTrue((Path(self.env["HERDR_ORCH_CONFIG"]) / "topics/waki/playbook.md").is_file())
        self.assertTrue((Path(self.env["HERDR_ORCH_STATE"]) / "topics/waki").is_dir())
        self.assertEqual(TopicState(Path(self.env["HERDR_ORCH_STATE"]), "waki").heartbeat_at(), 77.0)

    def test_status_prints_the_stored_ledger_as_a_table_or_json(self) -> None:
        with redirect_stdout(io.StringIO()):
            main(["init", "waki"], self.env)
        state = TopicState(Path(self.env["HERDR_ORCH_STATE"]), "waki")
        state.take("w1:p1", "issue-7")
        state.track_branch("issue-7", "issue-7", "/r", "w9", now=0)
        state.set_pr("issue-7", 7, "open", "failing", 0)
        state.set_ledger("issue-7", {"state": "waiting_on", "waiting_on": "ci", "owner": "agent", "mergeable_state": "clean"}, 60)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["status", "waki"], self.env, now=lambda: 360.0), 0)
        self.assertIn("issue-7", out.getvalue())
        self.assertIn("waiting on ci", out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["status", "waki", "--json"], self.env, now=lambda: 360.0), 0)
        (row,) = json.loads(out.getvalue())
        self.assertEqual((row["branch"], row["pr"], row["owner"], row["age_s"], row["checks"]), ("issue-7", 7, "agent", 300, "failing"))

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


class AnswerCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.env = {"HERDR_ORCH_CONFIG": str(root / "cfg"), "HERDR_ORCH_STATE": str(root / "state"), "HOME": "/home/u"}
        with redirect_stdout(io.StringIO()):
            main(["init", "waki"], self.env)
        self.state = TopicState(root / "state", "waki")
        self.state.escalate("waki-1", "w1:p1", "decision", "Which color?", ["Red"], now=1, item_id="a1")
        self.state.escalate("waki-1", "w1:p1", "decision", "Merge?", ["Yes"], now=2, to="orchestrator", item_id="o1")
        self.herdr = FakeHerdr()
        self.herdr.agent_list = [Agent("waki-1", "idle", "w1:p1", "w1"), Agent("orch-waki", "idle", "w2:p1", "w2")]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def last(self, item_id: str) -> dict[str, object]:
        return [i for i in self.state.inbox_lines() if i["id"] == item_id][-1]

    def test_answer_prompts_the_agent_and_resolves_the_item(self) -> None:
        self.assertEqual(main(["answer", "waki", "a1", "Red"], self.env, adapter=self.herdr), 0)
        [(_, target, text)] = self.herdr.of("prompt")
        self.assertEqual(target, "w1:p1")
        self.assertIn("Red", str(text))
        self.assertEqual((self.last("a1")["status"], self.last("a1")["answer"]), ("resolved", "Red"))

    def test_answer_to_an_orchestrator_item_goes_to_the_orchestrator(self) -> None:
        self.assertEqual(main(["answer", "waki", "o1", "Yes"], self.env, adapter=self.herdr), 0)
        self.assertEqual([c[1] for c in self.herdr.of("prompt")], ["orch-waki"])
        self.assertEqual(self.last("o1")["status"], "resolved")

    def test_a_failed_delivery_keeps_the_item_open(self) -> None:
        self.herdr.prompt_error = "never took the prompt"
        with redirect_stderr(io.StringIO()):
            self.assertEqual(main(["answer", "waki", "a1", "Red"], self.env, adapter=self.herdr), 1)
        self.assertEqual(self.last("a1")["status"], "open")

    def test_an_answer_is_refused_when_another_agent_holds_the_pane(self) -> None:
        self.herdr.agent_list = [Agent("waki-2", "idle", "w1:p1", "w1")]
        with redirect_stderr(io.StringIO()):
            self.assertEqual(main(["answer", "waki", "a1", "Red"], self.env, adapter=self.herdr), 1)
        self.assertEqual(self.herdr.of("prompt"), [])
        self.assertEqual(self.last("a1")["status"], "open")

    def test_dismiss_resolves_without_delivering(self) -> None:
        self.assertEqual(main(["answer", "waki", "a1", "--dismiss"], self.env, adapter=self.herdr), 0)
        self.assertEqual(self.herdr.of("prompt"), [])
        self.assertEqual(self.last("a1")["status"], "dismissed")

    def test_an_unknown_or_closed_item_is_an_error(self) -> None:
        with redirect_stderr(io.StringIO()):
            self.assertEqual(main(["answer", "waki", "nope", "x"], self.env, adapter=self.herdr), 1)
            main(["answer", "waki", "a1", "--dismiss"], self.env, adapter=self.herdr)
            self.assertEqual(main(["answer", "waki", "a1", "x"], self.env, adapter=self.herdr), 1)

    def test_an_answer_needs_text_unless_dismissing(self) -> None:
        with redirect_stderr(io.StringIO()):
            self.assertEqual(main(["answer", "waki", "a1"], self.env, adapter=self.herdr), 1)


class RunnerDownTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.env = {"HERDR_ORCH_CONFIG": str(root / "cfg"), "HERDR_ORCH_STATE": str(root / "state"), "HOME": str(root)}
        with redirect_stdout(io.StringIO()):
            main(["init", "waki"], self.env)
        self.state = TopicState(root / "state", "waki")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_cmd(self, *argv: str) -> str:
        out = io.StringIO()
        with redirect_stdout(out), mock.patch("herdr_orch.seating.spawn_runner") as spawn, \
                mock.patch("herdr_orch.cli._alive", return_value=False):
            self.assertEqual(main(list(argv), self.env, now=lambda: 1000.0, adapter=FakeHerdr()), 0)
        self.spawned = spawn.call_count
        return out.getvalue()

    def test_heartbeat_respawns_a_dead_runner_and_says_so(self) -> None:
        self.state.record_runner(4242, 900.0, 3)
        out = self.run_cmd("heartbeat", "waki")
        self.assertEqual(self.spawned, 1)
        self.assertIn("Watch runner for waki was down", out)

    def test_heartbeat_with_a_live_runner_prints_nothing(self) -> None:
        self.state.record_runner(os.getpid(), 990.0, 3)
        with mock.patch("herdr_orch.cli._alive", return_value=True), redirect_stdout(io.StringIO()) as out, \
                mock.patch("herdr_orch.seating.spawn_runner") as spawn:
            main(["heartbeat", "waki"], self.env, now=lambda: 1000.0, adapter=FakeHerdr())
        self.assertEqual((out.getvalue(), spawn.call_count), ("", 0))

    def test_status_says_the_runner_is_down_without_respawning(self) -> None:
        out = self.run_cmd("status", "waki")
        self.assertEqual(self.spawned, 0)
        self.assertIn("Watch runner for waki is not running", out)


class RunnerOwnershipTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = TopicState(self.root / "state", "t")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_reexec_keeps_the_runner_file(self) -> None:
        self.state.record_runner(os.getpid(), 1000.0, 3)
        watches = mock.Mock()
        with mock.patch.object(cli, "newer_install", return_value=True), \
                mock.patch.object(cli, "ensure_link", return_value="/x/herdr-orch"), \
                mock.patch("os.execve") as execve:
            cli._reexec_if_updated(mock.Mock(), {"HOME": str(self.root)}, watches, self.state)
        execve.assert_called_once()
        status = self.state.runner_status()
        assert status is not None
        self.assertEqual(status.pid, os.getpid())

    def test_another_live_fresh_pid_in_the_file_means_superseded(self) -> None:
        self.state.record_runner(4242, 990.0, 1)
        with mock.patch.object(cli, "_alive", return_value=True):
            self.assertEqual(cli._other_runner(self.state, 1000.0), 4242)

    def test_own_dead_or_stale_pids_do_not_supersede(self) -> None:
        self.state.record_runner(os.getpid(), 990.0, 1)
        self.assertIsNone(cli._other_runner(self.state, 1000.0))
        self.state.record_runner(4242, 990.0, 1)
        with mock.patch.object(cli, "_alive", return_value=False):
            self.assertIsNone(cli._other_runner(self.state, 1000.0))
        self.state.record_runner(4242, 100.0, 1)
        with mock.patch.object(cli, "_alive", return_value=True):
            self.assertIsNone(cli._other_runner(self.state, 1000.0))

    def test_a_runner_exits_when_another_takes_the_file_and_leaves_it_alone(self) -> None:
        ticks: list[float] = []
        state = self.state

        class TakenOver:
            done = False

            def __init__(self, *args: object) -> None:
                pass

            def tick(self, at: float) -> None:
                ticks.append(at)
                state.record_runner(4242, at, 1)  # a second runner wins the file

        topic = mock.Mock()
        topic.name = "t"
        topic.watches_dir = self.root / "watches"
        with mock.patch.object(cli, "Runner", TakenOver), mock.patch.object(cli, "reconcile"), \
                mock.patch.object(cli, "notifier_for"), mock.patch.object(cli, "CliGitHub"), \
                mock.patch.object(cli, "_alive", return_value=True), \
                mock.patch.object(cli, "_reexec_if_updated"), mock.patch("time.sleep"), \
                mock.patch("signal.signal"), redirect_stdout(io.StringIO()) as out:
            cli._watch(topic, self.state, mock.Mock(), lambda: 1000.0, {})
        self.assertEqual(len(ticks), 1)
        self.assertIn("4242", out.getvalue())
        status = self.state.runner_status()
        assert status is not None
        self.assertEqual(status.pid, 4242)
