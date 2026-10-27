import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from herdr_orch.deploy import compare_args, holds, installed_checkout, is_deployed, runner_on
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
        return [c for c in self.herdr.of("prompt") if c[1] == "issue-1" and "deployed" in str(c[2])]

    def told(self) -> list[str]:
        return [str(c[2]) for c in self.herdr.of("prompt") if c[1] == "issue-1"]

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

    def test_a_branch_merged_without_a_merge_sha_is_backfilled_then_gated(self) -> None:
        self.state.set_pr("issue-1", 20, "merged", "passing", 0)  # as recorded before merge_sha existed
        self.flag.touch()
        self.runner.tick(1)
        self.assertEqual(self.prompts(), [])  # GitHub has not answered with a merge commit yet
        self.github.prs["issue-1"] = PullRequest(20, "merged", "passing", merge_sha="abc123def456")
        self.runner.tick(1 + PR_POLL_SECONDS)
        self.runner.tick(1 + PR_POLL_SECONDS + DEPLOY_POLL_SECONDS)
        self.assertEqual(self.state.branch_record("issue-1")["merge_sha"], "abc123def456")  # type: ignore[index]
        self.assertEqual(len(self.prompts()), 1)
        self.assertNotIn("pr", [e.source for e in self.state.events_since(0)])

    def test_deploy_prompt_says_what_to_validate_and_the_topic_instruction(self) -> None:
        topic = Topic(name="waki", dir=self.root / "cfg", deploy_check=self.topic.deploy_check,
                      validate="Run the plugin's tests.")
        runner = Runner(topic, self.state, self.herdr, Quiet(), notify=lambda t, b: None, github=self.github)
        self.github.prs["issue-1"] = PullRequest(20, "merged", "passing", merge_sha="abc123def456")
        self.flag.touch()
        runner.tick(1)
        runner.tick(1 + DEPLOY_POLL_SECONDS)
        (text,) = [t for t in self.told() if "deployed" in t]
        self.assertIn("installed plugin is at abc123de", text)
        self.assertIn("Run the plugin's tests.", text)
        self.assertIn("herdr-orch validated waki --branch issue-1", text)
        self.assertIn("say what you checked", text)

    def test_green_merged_and_released_each_prompt_once_across_restarts(self) -> None:
        self.github.prs["issue-1"] = PullRequest(20, "open", "passing")
        self.runner.tick(1)
        self.assertEqual([t for t in self.told() if "green" in t].__len__(), 1)
        self.github.prs["issue-1"] = PullRequest(20, "merged", "passing", merge_sha="abc123def456")
        self.runner.tick(1 + PR_POLL_SECONDS)
        self.state.mark_validated("issue-1", 2)
        again = Runner(self.topic, self.state, self.herdr, Quiet(), notify=lambda t, b: None, github=self.github)
        again.tick(1 + 2 * PR_POLL_SECONDS)
        again.tick(1 + 3 * PR_POLL_SECONDS)
        kinds = [k for k in ("green", "is merged", "released") for t in self.told() if k in t]
        self.assertEqual(kinds, ["green", "is merged", "released"])
        self.assertIsNone(self.state.branch_record("issue-1"))

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

    def shallow_checkout(self) -> tuple[Path, str]:
        """A depth-1 clone of a three-commit repo, as herdr installs the plugin; returns it and the oldest sha."""
        origin = Path(self.tmp.name) / "origin"
        git = ["git", "-C", str(origin), "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run(["git", "init", "-q", str(origin)], check=True)
        for n in "abc":
            subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", n], check=True)
        old = subprocess.run([*git, "rev-parse", "HEAD~2"], capture_output=True, text=True, check=True).stdout.strip()
        clone = Path(self.tmp.name) / "shallow"
        subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{origin}", str(clone)], check=True)
        return clone, old

    def fake_gh(self, status: str, code: int = 0) -> Path:
        """A gh on PATH that answers the compare endpoint like --jq .status would."""
        bindir = Path(self.tmp.name) / "bin"
        bindir.mkdir(exist_ok=True)
        gh = bindir / "gh"
        gh.write_text(f"#!/bin/sh\ncase \"$*\" in *compare/*...*) echo {status}; exit {code};; esac\nexit 1\n")
        gh.chmod(0o755)
        self.addCleanup(os.environ.__setitem__, "PATH", os.environ["PATH"])
        os.environ["PATH"] = f"{bindir}:{os.environ['PATH']}"
        return gh

    def test_a_shallow_clone_asks_github_about_older_merges(self) -> None:
        clone, old = self.shallow_checkout()
        self.assertEqual(subprocess.run(["git", "-C", str(clone), "rev-parse", "--is-shallow-repository"],
                                        capture_output=True, text=True).stdout.strip(), "true")
        self.assertEqual(subprocess.run(["git", "-C", str(clone), "merge-base", "--is-ancestor", old, "HEAD"],
                                        capture_output=True).returncode, 128)
        head = subprocess.run(["git", "-C", str(clone), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        self.assertTrue(holds(clone, head))  # HEAD itself needs no GitHub
        self.fake_gh("ahead")
        self.assertTrue(holds(clone, old))
        self.fake_gh("identical")
        self.assertTrue(holds(clone, old))

    def test_a_sha_that_is_not_behind_head_or_that_github_cannot_judge_is_not_held(self) -> None:
        clone, old = self.shallow_checkout()
        for status, code in (("behind", 0), ("diverged", 0), ("ahead", 1)):
            self.fake_gh(status, code)
            self.assertFalse(holds(clone, old), status)
        self.assertEqual(compare_args("a", "b")[4], "repos/{owner}/{repo}/compare/a...b")

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
        deadline = time.monotonic() + 5  # Popen returns before the child execs: its environ is the parent's until then
        while not runner_on(self.checkout, child.pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(runner_on(self.checkout, child.pid))

    def test_is_deployed_needs_both_the_sha_and_the_runner(self) -> None:
        state = TopicState(Path(self.tmp.name), "waki")
        self.assertFalse(is_deployed(self.sha, self.plugins, state))  # no runner recorded
        child = subprocess.Popen(["sleep", "5"], env={"PYTHONPATH": f"{self.checkout}/.."})
        self.addCleanup(child.kill)
        state.record_runner(child.pid, 1.0, 1)
        self.assertTrue(is_deployed(self.sha, self.plugins, state))
        self.assertFalse(is_deployed("0" * 40, self.plugins, state))


class AutoReleaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = TopicState(Path(self.tmp.name), "waki")
        self.state.take("w1:p1", "issue-1")
        self.state.track_branch("issue-1", "issue-1", "/r", "w9", now=0)
        self.herdr = FakeHerdr()
        self.herdr.agent_list = [Agent("issue-1", "idle", "w1:p1", "w1"), Agent("orch-waki", "working", "w2:p1", "w2")]
        self.github = FakeGitHub()
        topic = Topic(name="waki", dir=Path(self.tmp.name) / "cfg")  # no deploy_check
        self.runner = Runner(topic, self.state, self.herdr, Quiet(), notify=lambda t, b: None, github=self.github)

    def released(self) -> bool:
        return self.state.branch_record("issue-1") is None

    def test_validated_and_merged_is_released_without_a_deploy_gate(self) -> None:
        self.github.prs["issue-1"] = PullRequest(20, "merged", "passing", merge_sha="abc")
        self.state.mark_validated("issue-1", 0)
        self.runner.tick(1)
        self.assertTrue(self.released())
        self.assertEqual(self.herdr.of("worktree_remove"), [("worktree_remove", "w9")])
        self.assertEqual(self.state.managed(), {})
        self.assertIn("released", [e.source for e in self.state.events_since(0)])

    def test_not_validated_or_github_down_keeps_it(self) -> None:
        self.github.prs["issue-1"] = PullRequest(20, "merged", "passing", merge_sha="abc")
        self.runner.tick(1)
        self.assertFalse(self.released())
        self.state.mark_validated("issue-1", 1)
        self.github.down = True
        self.runner.tick(1 + PR_POLL_SECONDS)
        self.assertFalse(self.released())
