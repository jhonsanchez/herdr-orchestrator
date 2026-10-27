import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from herdr_orch.deploy import holds, installed_checkout, is_deployed, runner_on
from herdr_orch.github import PullRequest, parse_pr
from herdr_orch.herdr import Agent
from herdr_orch.runner import DEPLOY_POLL_SECONDS, PR_POLL_SECONDS, Runner
from herdr_orch.state import TopicState
from herdr_orch.topics import Topic
from tests.fakes import FakeHerdr
from tests.test_prs import FakeGitHub


class Quiet:
    def poll(self, now: float) -> None: ...
    def lines(self) -> list[tuple[str, str]]:
        return []


class DeployGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.flag = self.root / "deployed"
        self.state = TopicState(self.root, "waki")
        self.state.take("w1:p1", "issue-1")
        self.state.track_branch("issue-1", "issue-1", "/r", "w9", now=0)
        self.herdr = FakeHerdr()
        self.herdr.agent_list = [Agent("issue-1", "idle", "w1:p1", "w1"), Agent("orch-waki", "working", "w2:p1", "w2")]
        self.github = FakeGitHub()
        check = ["sh", "-c", f'test -e {self.flag} && test "$HERDR_ORCH_MERGE_SHA" = abc123def456']
        self.topic = Topic(name="waki", dir=self.root / "cfg", deploy_check=check)
        self.runner = Runner(self.topic, self.state, self.herdr, Quiet(), notify=lambda t, b: None, github=self.github)

    def prompts(self) -> list[object]:
        return [c for c in self.herdr.of("prompt") if c[1] == "issue-1"]

    def merge(self) -> None:
        self.github.prs["issue-1"] = PullRequest(20, "merged", "passing", merge_sha="abc123def456")
        self.runner.tick(1)

    def test_agent_is_not_prompted_until_the_check_passes_then_once(self) -> None:
        self.merge()
        self.runner.tick(1 + DEPLOY_POLL_SECONDS)
        self.assertEqual(self.prompts(), [])
        self.flag.touch()
        self.runner.tick(1 + 2 * DEPLOY_POLL_SECONDS)
        self.runner.tick(1 + 3 * DEPLOY_POLL_SECONDS)
        self.assertEqual(len(self.prompts()), 1)
        self.assertIn("deploy", [e.source for e in self.state.events_since(0)])

    def test_the_check_gets_the_merge_sha_and_a_new_runner_does_not_prompt_twice(self) -> None:
        self.merge()
        self.flag.touch()
        self.runner.tick(1 + DEPLOY_POLL_SECONDS)
        again = Runner(self.topic, self.state, self.herdr, Quiet(), notify=lambda t, b: None, github=self.github)
        again.tick(1 + 5 * PR_POLL_SECONDS)
        self.assertEqual(len(self.prompts()), 1)

    def test_no_deploy_check_means_no_gate(self) -> None:
        plain = Runner(Topic(name="waki", dir=self.root / "cfg"), self.state, self.herdr, Quiet(),
                       notify=lambda t, b: None, github=self.github)
        self.github.prs["issue-1"] = PullRequest(20, "merged", "passing", merge_sha="abc123def456")
        plain.tick(1)
        plain.tick(1 + DEPLOY_POLL_SECONDS)
        self.assertEqual(self.prompts(), [])

    def test_parse_pr_reads_the_merge_commit(self) -> None:
        raw = '[{"number":3,"state":"closed","merged_at":"t","html_url":"u","merge_commit_sha":"abc"}]'
        self.assertEqual(parse_pr(raw).merge_sha, "abc")  # type: ignore[union-attr]
        raw = '[{"number":3,"state":"open","merged_at":null,"html_url":"u","merge_commit_sha":"zzz"}]'
        self.assertEqual(parse_pr(raw).merge_sha, "")  # type: ignore[union-attr]


class PluginDeployTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.plugins = Path(self.tmp.name) / "plugins"
        self.checkout = self.plugins / "github-installations" / "jhonsanchez.orchestrator-ede327093c3b" / "2074762-0" / "checkout"
        (self.checkout / "bin").mkdir(parents=True)
        (self.checkout / "bin" / "herdr-orch").write_text("#!/bin/sh\n")
        git = ["git", "-C", str(self.checkout), "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run([*git, "init", "-q"], check=True)
        subprocess.run([*git, "add", "."], check=True)
        subprocess.run([*git, "commit", "-qm", "one"], check=True)
        self.sha = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()

    def test_finds_the_installation_and_whether_it_holds_a_sha(self) -> None:
        self.assertEqual(installed_checkout(self.plugins), self.checkout)
        self.assertIsNone(installed_checkout(self.plugins / "nowhere"))
        self.assertTrue(holds(self.checkout, self.sha))
        self.assertFalse(holds(self.checkout, "0" * 40))

    def test_the_older_layout_without_a_version_folder_is_still_found(self) -> None:
        old = self.plugins / "github" / "jhonsanchez.orchestrator-1"
        (old / "bin").mkdir(parents=True)
        (old / "bin" / "herdr-orch").write_text("#!/bin/sh\n")
        os.utime(old / "bin" / "herdr-orch", (9e9, 9e9))
        self.assertEqual(installed_checkout(self.plugins), old)

    def test_runner_must_run_from_the_checkout(self) -> None:
        self.assertFalse(runner_on(self.checkout, os.getpid()))
        child = subprocess.Popen(["sleep", "5"], env={"PYTHONPATH": f"{self.checkout}/.."})
        self.addCleanup(child.kill)
        self.assertTrue(runner_on(self.checkout, child.pid))

    def test_is_deployed_needs_both_the_sha_and_the_runner(self) -> None:
        state = TopicState(Path(self.tmp.name), "waki")
        self.assertFalse(is_deployed(self.sha, self.plugins, state))  # no runner recorded
        child = subprocess.Popen(["sleep", "5"], env={"PYTHONPATH": f"{self.checkout}/.."})
        self.addCleanup(child.kill)
        state.record_runner(child.pid, 1.0, 1)
        self.assertTrue(is_deployed(self.sha, self.plugins, state))
        self.assertFalse(is_deployed("0" * 40, self.plugins, state))
