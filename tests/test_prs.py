import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from herdr_orch import seating
from herdr_orch.attention import load
from herdr_orch.github import PullRequest, checks_of, parse_pr
from herdr_orch.herdr import Agent, Workspace
from herdr_orch.runner import PR_POLL_SECONDS, Runner
from herdr_orch.seating import release, seat, validated
from herdr_orch.state import TopicState
from herdr_orch.topics import Topic
from herdr_orch.watches_core import open_obligations
from tests.fakes import FakeHerdr
from tests.test_seating import cloned_repo

MIN = 60.0


class FakeGitHub:
    def __init__(self) -> None:
        self.prs: dict[str, PullRequest | None] = {}
        self.down = False
        self.asked: list[str] = []

    def pr_for_branch(self, repo_root: str, branch: str) -> PullRequest | None:
        self.asked.append(branch)
        if self.down:
            raise RuntimeError("rate limited")
        return self.prs.get(branch)


def branch(agent: str = "issue-1", pr_state: str | None = "open", checks: str | None = "passing",
           pr: int | None = 20, since: float = 100) -> dict[str, Any]:
    return {"branch": agent, "agent": agent, "repo_root": "/r", "workspace_id": "w9", "pr": pr,
            "pr_state": pr_state, "checks": checks, "pr_since": since}


class ChecksTest(unittest.TestCase):
    def test_one_verdict_from_the_rollup(self) -> None:
        self.assertEqual(checks_of([]), "none")
        self.assertEqual(checks_of([{"conclusion": "SUCCESS"}, {"conclusion": "SKIPPED"}]), "passing")
        self.assertEqual(checks_of([{"conclusion": "SUCCESS"}, {"conclusion": "FAILURE"}]), "failing")
        self.assertEqual(checks_of([{"conclusion": "SUCCESS"}, {"conclusion": "", "status": "IN_PROGRESS"}]), "pending")
        self.assertEqual(checks_of([{"state": "ERROR"}]), "failing")  # a commit status, not a check run

    def test_the_open_pr_wins_else_the_newest(self) -> None:
        raw = ('[{"number":3,"state":"MERGED","url":"u3","statusCheckRollup":[]},'
               '{"number":5,"state":"CLOSED","url":"u5","statusCheckRollup":[]}]')
        self.assertEqual(parse_pr(raw), PullRequest(5, "closed", "none", "u5"))
        raw = raw.replace('"number":3,"state":"MERGED"', '"number":3,"state":"OPEN"')
        self.assertEqual(parse_pr(raw), PullRequest(3, "open", "none", "u3"))
        self.assertIsNone(parse_pr("[]"))


class PrObligationsTest(unittest.TestCase):
    def owed(self, b: dict[str, Any], statuses: dict[str, str]) -> list[str]:
        return [o.reason for o in open_obligations([], [], statuses, {"issue-1"}, [b])]

    def test_an_open_pr_with_no_live_agent_is_owed_a_follow_up(self) -> None:
        self.assertEqual(self.owed(branch(), {}), ["PR #20 is open with no agent on it"])

    def test_failing_checks_and_no_checks_are_owed_unless_the_agent_is_working(self) -> None:
        self.assertEqual(self.owed(branch(checks="failing"), {"issue-1": "idle"}), ["PR #20 has failing checks"])
        self.assertEqual(self.owed(branch(checks="none"), {"issue-1": "done"}), ["PR #20 has no checks"])
        self.assertEqual(self.owed(branch(checks="failing"), {"issue-1": "working"}), [])

    def test_a_healthy_pr_or_a_closed_one_or_no_pr_owes_nothing(self) -> None:
        self.assertEqual(self.owed(branch(), {"issue-1": "done"}), [])
        self.assertEqual(self.owed(branch(pr_state="merged"), {}), [])
        self.assertEqual(self.owed(branch(pr_state=None, pr=None, checks=None), {}), [])

    def test_the_pr_replaces_the_generic_done_obligation(self) -> None:
        from herdr_orch.state import Event
        events = [Event(10, "agents", "issue-1 gone")]
        got = open_obligations(events, [], {}, {"issue-1"}, [branch()])
        self.assertEqual([(o.reason, o.pr) for o in got], [("PR #20 is open with no agent on it", True)])

    def test_an_inbox_item_for_the_agent_after_it_opened_closes_it(self) -> None:
        item = {"agent": "issue-1", "created_at": 150}
        self.assertEqual(open_obligations([], [item], {}, {"issue-1"}, [branch(since=100)]), [])
        self.assertEqual(len(open_obligations([], [item], {}, {"issue-1"}, [branch(since=200)])), 1)


class RunnerPrTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.topic = Topic(name="waki", dir=root / "cfg", heartbeat_window_min=15)
        self.state = TopicState(root, "waki")
        self.state.take("w1:p1", "issue-1")
        self.state.track_branch("issue-1", "issue-1", "/r", "w9", now=0)
        self.herdr = FakeHerdr()
        self.herdr.agent_list = [Agent("issue-1", "working", "w1:p1", "w1"), Agent("orch-waki", "working", "w2:p1", "w2")]
        self.github = FakeGitHub()
        self.notes: list[tuple[str, str]] = []

        class Quiet:
            def poll(self, now: float) -> None: ...
            def lines(self) -> list[tuple[str, str]]:
                return []

        self.runner = Runner(self.topic, self.state, self.herdr, Quiet(), notify=lambda t, b: self.notes.append((t, b)),
                             github=self.github)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def events(self) -> list[str]:
        return [e.text for e in self.state.events_since(0) if e.source == "pr"]

    def test_a_pr_appearing_changing_checks_and_merging_each_become_an_event(self) -> None:
        self.github.prs["issue-1"] = PullRequest(20, "open", "pending")
        self.runner.tick(1)
        self.github.prs["issue-1"] = PullRequest(20, "open", "failing")
        self.runner.tick(1 + PR_POLL_SECONDS)
        self.github.prs["issue-1"] = PullRequest(20, "open", "passing")
        self.runner.tick(1 + 2 * PR_POLL_SECONDS)
        self.github.prs["issue-1"] = PullRequest(20, "merged", "passing")
        self.runner.tick(1 + 3 * PR_POLL_SECONDS)
        self.assertEqual(self.events(), [
            "PR #20 (issue-1) open, checks pending",
            "PR #20 (issue-1) checks failing; send the failure back to its agent",
            "PR #20 (issue-1) checks passing; green: merge it if the playbook lets you, else put it in the Inbox",
            "PR #20 (issue-1) merged; its workspace can be cleaned up",
        ])

    def test_an_unchanged_pr_says_nothing_and_a_merged_one_is_not_asked_about_again(self) -> None:
        self.github.prs["issue-1"] = PullRequest(20, "merged", "passing")
        self.runner.tick(1)
        self.runner.tick(1 + PR_POLL_SECONDS)
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.github.asked, ["issue-1"])

    def test_github_is_asked_no_more_often_than_the_poll_interval(self) -> None:
        self.runner.tick(1)
        self.runner.tick(2)
        self.assertEqual(self.github.asked, ["issue-1"])

    def test_github_being_down_changes_nothing(self) -> None:
        self.github.prs["issue-1"] = PullRequest(20, "open", "passing")
        self.runner.tick(1)
        self.github.down = True
        self.runner.tick(1 + PR_POLL_SECONDS)
        self.assertEqual(self.state.branch_record("issue-1")["pr_state"], "open")  # type: ignore[index]
        self.assertEqual(len(self.events()), 1)

    def test_an_orphaned_pr_shows_in_the_attention_pane_and_chases_the_user_once(self) -> None:
        self.github.prs["issue-1"] = PullRequest(20, "open", "passing")
        self.runner.tick(0)
        self.herdr.agent_list = [Agent("orch-waki", "working", "w2:p1", "w2")]  # the agent and worktree went away
        self.runner.tick(5 * MIN)
        self.assertEqual(self.notes, [])
        self.runner.tick(20 * MIN)
        self.runner.tick(21 * MIN)
        chase = [n for n in self.notes if "follow-up" in n[0]]
        self.assertEqual(len(chase), 1)
        self.assertIn("PR #20 is open with no agent on it", chase[0][1])
        view = load(Path(self.tmp.name), [self.topic], self.herdr, 20 * MIN)
        [row] = [r for r in view.rows() if r.kind == "followup"]
        self.assertIn("PR #20 is open with no agent on it", row.text)

    def test_failing_and_no_check_prs_are_owed_while_their_agent_idles(self) -> None:
        self.herdr.agent_list[0] = Agent("issue-1", "idle", "w1:p1", "w1")
        for checks, text in (("failing", "failing checks"), ("none", "no checks")):
            self.github.prs["issue-1"] = PullRequest(20, "open", checks)  # type: ignore[arg-type]
            self.runner.tick(0 if checks == "failing" else 10 * PR_POLL_SECONDS)
            view = load(Path(self.tmp.name), [self.topic], self.herdr, 0)
            self.assertIn(text, [r for r in view.rows() if r.kind == "followup"][0].text)


@mock.patch.object(seating, "POLL_SECONDS", 0)
class ExistingBranchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.clone = cloned_repo(self.root, "main")
        self.topic = Topic(name="p", dir=self.root / "cfg", model=None)
        self.state = TopicState(self.root / "state", "p")
        self.herdr = FakeHerdr()
        self.herdr.workspace_list = [Workspace("w3", "repo", str(self.clone))]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def push(self, name: str) -> None:
        git = ["git", "-C", str(self.clone), "-c", "user.email=t@t", "-c", "user.name=t"]
        subprocess.run([*git, "checkout", "-q", "-b", name], check=True)
        subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "work"], check=True)
        subprocess.run([*git, "push", "-q", "origin", name], check=True)
        subprocess.run([*git, "checkout", "-q", "main"], check=True)
        subprocess.run([*git, "branch", "-q", "-D", name], check=True)
        subprocess.run([*git, "update-ref", "-d", f"refs/remotes/origin/{name}"], check=True)  # as if never fetched

    def test_seat_existing_bases_the_worktree_on_the_pushed_branch_and_says_to_pick_up_the_pr(self) -> None:
        self.push("issue-10")
        seat(self.topic, self.state, self.herdr, "w3", "issue-10", home="/h", existing=True)
        [call] = self.herdr.of("worktree_create")
        self.assertEqual(call[2], "issue-10")
        self.assertEqual(call[5], "origin/issue-10")
        self.assertIn("gh pr view", str(self.herdr.of("prompt")[0][2]))

    def test_seat_existing_refuses_a_branch_origin_does_not_have(self) -> None:
        with self.assertRaisesRegex(ValueError, "origin has no branch nope"):
            seat(self.topic, self.state, self.herdr, "w3", "nope", home="/h", existing=True)
        self.assertEqual(self.herdr.of("worktree_create"), [])

    def test_seating_records_the_branch_and_a_reseat_keeps_the_known_pr(self) -> None:
        self.push("issue-10")
        self.herdr.agent_list = []
        seat(self.topic, self.state, self.herdr, "w3", "issue-10", home="/h", now=lambda: 5.0)
        record = self.state.branch_record("issue-10")
        self.assertEqual((record["agent"], record["repo_root"]), ("issue-10", str(self.clone)))  # type: ignore[index]
        self.state.set_pr("issue-10", 20, "open", "passing", now=9)
        seat(self.topic, self.state, self.herdr, "w3", "issue-10", home="/h", existing=True)
        self.assertEqual(self.state.branch_record("issue-10")["pr"], 20)  # type: ignore[index]


class ReleaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = TopicState(Path(self.tmp.name), "p")
        self.state.track_branch("issue-1", "issue-1", "/r", "w9", now=0)
        self.herdr, self.github = FakeHerdr(), FakeGitHub()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_an_open_pr_keeps_its_workspace_and_worktree(self) -> None:
        self.github.prs["issue-1"] = PullRequest(20, "open", "passing")
        with self.assertRaisesRegex(RuntimeError, "still open"):
            release(self.state, self.herdr, self.github, "issue-1")
        self.assertEqual(self.herdr.of("worktree_remove"), [])
        self.assertIsNotNone(self.state.branch_record("issue-1"))

    def test_github_not_answering_keeps_it_too(self) -> None:
        self.github.down = True
        with self.assertRaises(RuntimeError):
            release(self.state, self.herdr, self.github, "issue-1")
        self.assertEqual(self.herdr.of("worktree_remove"), [])

    def test_a_merged_or_closed_pr_releases_the_workspace_and_stops_tracking(self) -> None:
        self.github.prs["issue-1"] = PullRequest(20, "closed", "none")
        release(self.state, self.herdr, self.github, "issue-1")
        self.assertEqual(self.herdr.of("worktree_remove"), [("worktree_remove", "w9")])
        self.assertIsNone(self.state.branch_record("issue-1"))

    def test_release_gives_back_the_agents_registry_entry(self) -> None:
        self.state.take("p1", "issue-1")
        self.state.take("p2", "issue-2")
        self.github.prs["issue-1"] = PullRequest(20, "closed", "none")
        release(self.state, self.herdr, self.github, "issue-1")
        self.assertEqual(self.state.managed(), {"p2": "issue-2"})

    def test_validated_marks_the_branch_and_tells_the_orchestrator(self) -> None:
        validated(self.state, "issue-1", 500.0)
        self.assertEqual(self.state.branch_record("issue-1")["validated_at"], 500.0)  # type: ignore[index]
        self.assertEqual([e.source for e in self.state.events_since(0)], ["validated"])

    def test_validated_on_an_untracked_branch_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "not tracked"):
            validated(self.state, "nope", 1.0)


if __name__ == "__main__":
    unittest.main()
