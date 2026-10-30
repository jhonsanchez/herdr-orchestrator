import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from herdr_orch import github, seating
from herdr_orch.github import CliGitHub, token_from
from herdr_orch.herdr import Workspace
from herdr_orch.seating import seat
from herdr_orch.state import TopicState
from herdr_orch.topics import load_topic, Topic
from tests.fakes import FakeHerdr


def script(dir: Path, name: str, body: str) -> str:
    path = dir / name
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


class TokenCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_topic_reads_both_commands_from_the_github_table(self) -> None:
        (self.dir / "topic.toml").write_text('[github]\nread_token_cmd = "r"\nwrite_token_cmd = "w"\n')
        topic = load_topic(self.dir)
        self.assertEqual((topic.read_token_cmd, topic.write_token_cmd), ("r", "w"))
        self.assertEqual(Topic(name="x", dir=self.dir).read_token_cmd, "")

    def test_token_is_the_trimmed_output_and_failures_do_not_carry_it(self) -> None:
        self.assertEqual(token_from(script(self.dir, "ok", "echo ghs_abc"), "/h"), "ghs_abc")
        with self.assertRaises(RuntimeError) as e:
            token_from(script(self.dir, "bad", "echo ghs_secret; echo oops >&2; exit 3"), "/h")
        self.assertNotIn("ghs_secret", str(e.exception))
        with self.assertRaises(RuntimeError):
            token_from(script(self.dir, "empty", "true"), "/h")

    def test_gh_runs_with_the_read_token_and_asks_again_after_the_ttl(self) -> None:
        now = [0.0]
        counter = self.dir / "n"
        cmd = script(self.dir, "tok", f'echo x >> {counter}; echo tok$(wc -l < {counter})')
        gh = CliGitHub(cmd, "/h", clock=lambda: now[0])
        seen: list[str] = []
        real = subprocess.run
        def run(args: list[str], **kw: Any) -> Any:
            if args[0] != "gh":
                return real(args, **kw)
            seen.append(kw["env"]["GH_TOKEN"])
            return mock.Mock(returncode=0, stdout="[]", stderr="")
        with mock.patch.object(subprocess, "run", side_effect=run):
            gh.open_pulls("/r")
            gh.open_pulls("/r")
            now[0] = github.TOKEN_TTL_SECONDS + 1
            gh.open_pulls("/r")
        self.assertEqual([t.strip() for t in seen], ["tok1", "tok1", "tok2"])

    def test_without_a_command_gh_inherits_the_environment(self) -> None:
        def run(args: list[str], **kw: Any) -> Any:
            self.assertIsNone(kw["env"])
            return mock.Mock(returncode=0, stdout="[]", stderr="")
        with mock.patch.object(subprocess, "run", side_effect=run):
            CliGitHub().open_pulls("/r")


@mock.patch.object(seating, "POLL_SECONDS", 0)
class SeatExportsWriteTokenTest(unittest.TestCase):
    def test_pane_runs_the_write_command_before_the_agent_starts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            topic = Topic(name="t", dir=root / "cfg/topics/t", github={"write_token_cmd": "~/bin/app token"})
            herdr = FakeHerdr()
            herdr.workspace_list = [Workspace("w3", "r", "/home/u/projects/r")]
            r = seat(topic, TopicState(root / "s", "t"), herdr, workspace_id="w3", branch="b", home="/home/u", cli="/c")
            calls = herdr.calls
            export = ("pane_run", r.pane_id, 'export GH_TOKEN="$(/home/u/bin/app token)" && export GITHUB_TOKEN="$GH_TOKEN"')
            self.assertIn(export, calls)
            self.assertLess(calls.index(export), next(i for i, c in enumerate(calls) if c[0] == "agent_start"))


if __name__ == "__main__":
    unittest.main()
