import json
import tempfile
import unittest
from pathlib import Path

from herdr_orch.attention import load
from herdr_orch.github import OpenPr, open_pulls_args, parse_open_pulls, pull_args, review_of, reviews_args
from herdr_orch.herdr import Agent, Workspace
from herdr_orch.ledger import Ledger, classify_branch, classify_pr, ledger_lines, status_rows, status_table
from herdr_orch.runner import PR_POLL_SECONDS, Runner
from herdr_orch.state import TopicState
from herdr_orch.topics import Topic
from tests.fakes import FakeHerdr
from tests.test_prs import FakeGitHub

# The shape of GET /repos/{owner}/{repo}/pulls?state=open (trimmed to the fields read).
LISTING = json.dumps([
    {"number": 646, "title": "Pin the lockfile", "state": "open", "draft": False, "html_url": "https://github.com/o/r/pull/646",
     "user": {"login": "waki-agents"}, "head": {"ref": "WAKI-12", "sha": "aaa111"}, "base": {"ref": "develop"}},
    {"number": 651, "title": "Bump left-pad from 1.0 to 1.1", "state": "open", "draft": False,
     "html_url": "https://github.com/o/r/pull/651", "user": {"login": "dependabot[bot]"},
     "head": {"ref": "dependabot/npm_and_yarn/left-pad-1.1", "sha": "bbb222"}, "base": {"ref": "develop"}},
])
# GET /pulls/{n}/reviews: a comment after a change request changes nothing; a re-approval clears the reviewer's request.
REVIEWS = [
    {"user": {"login": "ana"}, "state": "CHANGES_REQUESTED", "submitted_at": "2026-10-01T10:00:00Z"},
    {"user": {"login": "ana"}, "state": "COMMENTED", "submitted_at": "2026-10-01T11:00:00Z"},
    {"user": {"login": "bo"}, "state": "APPROVED", "submitted_at": "2026-10-01T12:00:00Z"},
]


def pr(number: int, head: str, base: str = "develop", state: str = "clean", checks: str = "passing", **kw: object) -> OpenPr:
    return OpenPr(number, f"PR {number}", head, base, f"sha{number}", "someone", "", mergeable_state=state,
                  checks=checks, **kw)  # type: ignore[arg-type]


class RestShapesTest(unittest.TestCase):
    def test_a_listing_parses_every_author_and_leaves_mergeable_state_for_the_detail_read(self) -> None:
        got = parse_open_pulls(LISTING)
        self.assertEqual([(p.number, p.head_ref, p.base_ref, p.head_sha, p.author, p.draft) for p in got], [
            (646, "WAKI-12", "develop", "aaa111", "waki-agents", False),
            (651, "dependabot/npm_and_yarn/left-pad-1.1", "develop", "bbb222", "dependabot[bot]", False)])
        self.assertEqual({p.mergeable_state for p in got}, {""})
        self.assertEqual(parse_open_pulls(""), [])

    def test_the_standing_review_verdict(self) -> None:
        self.assertEqual(review_of(REVIEWS), "changes_requested")
        self.assertEqual(review_of(REVIEWS + [{"user": {"login": "ana"}, "state": "APPROVED",
                                               "submitted_at": "2026-10-02T00:00:00Z"}]), "approved")
        self.assertEqual(review_of(REVIEWS[1:2]), "none")

    def test_only_rest_endpoints_and_one_list_call(self) -> None:
        for args in (open_pulls_args(), pull_args(7), reviews_args(7)):
            self.assertEqual(args[:4], ["gh", "api", "-X", "GET"])
            self.assertNotIn("graphql", " ".join(args))
        self.assertEqual(open_pulls_args()[4], "repos/{owner}/{repo}/pulls")


class ClassifyTest(unittest.TestCase):
    def test_an_open_pr(self) -> None:
        for p, want in [
            (pr(1, "a", state="dirty"), ("waiting_on", "conflict", "agent")),
            (pr(1, "a", state="behind"), ("waiting_on", "conflict", "agent")),
            (pr(1, "a", checks="failing"), ("waiting_on", "ci", "agent")),
            (pr(1, "a", checks="pending"), ("waiting_on", "ci", "runner")),
            (pr(1, "a", state="unknown"), ("waiting_on", "ci", "runner")),
            (pr(1, "a", review="changes_requested"), ("waiting_on", "review", "agent")),
            (pr(1, "a", state="blocked"), ("waiting_on", "review", "human")),
            (pr(1, "a", state="blocked", review="approved"), ("waiting_on", "merge", "orchestrator")),
            (pr(1, "a"), ("waiting_on", "merge", "orchestrator")),
            (pr(1, "a", draft=True), ("working", None, "agent")),
        ]:
            e = classify_pr(p)
            self.assertEqual((e.state, e.waiting_on, e.owner), want, p)

    def test_a_tracked_branch_without_an_open_pr(self) -> None:
        def got(rec: dict[str, object], status: str | None, gate: bool = True) -> tuple[str, str | None, str]:
            e = classify_branch({"branch": "b", "agent": "b", **rec}, None, status, gate)
            return e.state, e.waiting_on, e.owner
        self.assertEqual(got({}, "working"), ("working", None, "agent"))
        self.assertEqual(got({}, None), ("waiting_on", "agent", "orchestrator"))
        self.assertEqual(got({}, "idle"), ("waiting_on", "agent", "orchestrator"))
        self.assertEqual(got({}, "blocked"), ("waiting_on", "human", "human"))
        self.assertEqual(got({"pr_state": "merged"}, "idle"), ("waiting_on", "deploy", "runner"))
        self.assertEqual(got({"pr_state": "merged", "deployed": True}, "idle"), ("waiting_on", "validate", "agent"))
        self.assertEqual(got({"pr_state": "merged"}, "idle", gate=False), ("waiting_on", "validate", "agent"))
        self.assertEqual(got({"pr_state": "merged", "validated_at": 1, "evidence": "ok"}, None), ("working", None, "runner"))
        self.assertEqual(got({"pr_state": "merged", "validated_at": 1, "evidence": ""}, None)[1], "deploy")
        self.assertEqual(got({"pr_state": "closed"}, "idle"), ("waiting_on", "human", "orchestrator"))


class SweepTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = TopicState(Path(self.tmp.name), "waki")
        self.state.take("w1:p1", "WAKI-12")
        self.state.track_branch("WAKI-12", "WAKI-12", "/r", "w9", now=0)
        self.state.set_pr("WAKI-12", 646, "open", "passing", 0)
        self.gh = FakeGitHub()
        self.ledger = Ledger(self.state, self.gh, deploy_gate=False)
        self.gh.open["/r"] = parse_open_pulls(LISTING)
        self.gh.detail = {646: pr(646, "WAKI-12", state="clean"), 651: pr(651, "dependabot/npm_and_yarn/left-pad-1.1", state="dirty")}
        self.agents = {"WAKI-12": "working"}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def ledger_of(self) -> dict[str, object]:
        record = self.state.branch_record("WAKI-12")
        assert record is not None
        led: dict[str, object] = record["ledger"]  # type: ignore[assignment]
        return led

    def test_tracked_and_untracked_prs_are_both_in_the_ledger(self) -> None:
        self.assertEqual(self.ledger.sweep(["/r"], self.agents, 100), [])
        led = self.ledger_of()
        self.assertEqual(led, {"state": "waiting_on", "waiting_on": "merge", "owner": "orchestrator", "mergeable_state": "clean",
                                "since": 100})
        (u,) = self.state.untracked_prs()
        self.assertEqual((u["number"], u["author"], u["waiting_on"], u["owner"], u["since"]),
                         (651, "dependabot[bot]", "conflict", "orchestrator", 100))

    def test_since_holds_while_the_state_holds_and_resets_when_it_changes(self) -> None:
        self.ledger.sweep(["/r"], self.agents, 100)
        self.ledger.sweep(["/r"], self.agents, 160)
        self.assertEqual(self.state.untracked_prs()[0]["since"], 100)
        self.assertEqual(self.ledger_of()["since"], 100)
        self.gh.open["/r"][0] = pr(646, "WAKI-12", checks="failing", state="clean")
        self.gh.detail[646] = pr(646, "WAKI-12", checks="failing")
        self.ledger.sweep(["/r"], self.agents, 220)
        led = self.ledger_of()
        self.assertEqual((led["waiting_on"], led["owner"], led["since"]), ("ci", "agent", 220))

    def test_a_settled_pr_costs_no_detail_read_until_something_moves(self) -> None:
        self.ledger.sweep(["/r"], self.agents, 100)
        self.assertEqual(sorted(self.gh.detail_reads), [646, 651])
        self.gh.detail_reads.clear()
        self.ledger.sweep(["/r"], self.agents, 160)
        self.assertEqual(self.gh.detail_reads, [])
        self.gh.open["/r"][0] = OpenPr(646, "t", "WAKI-12", "develop", "newsha")  # a push moved the head
        self.ledger.sweep(["/r"], self.agents, 220)
        self.assertEqual(self.gh.detail_reads, [646])

    def test_a_merge_rechecks_the_siblings_on_that_base_and_records_who_went_dirty(self) -> None:
        self.gh.detail[651] = pr(651, "dependabot/npm_and_yarn/left-pad-1.1", state="clean")
        self.ledger.sweep(["/r"], self.agents, 100)
        self.gh.detail_reads.clear()
        self.gh.open["/r"] = self.gh.open["/r"][1:]  # #646 merged into develop
        self.gh.detail[651] = pr(651, "dependabot/npm_and_yarn/left-pad-1.1", state="dirty")
        events = self.ledger.sweep(["/r"], self.agents, 160)
        self.assertEqual(self.gh.detail_reads, [651])  # re-read at once, not at the TTL
        self.assertEqual(events, ["PR #651 (dependabot/npm_and_yarn/left-pad-1.1) went dirty after #646 left develop"])
        self.assertEqual(self.state.untracked_prs()[0]["waiting_on"], "conflict")
        self.assertEqual(self.ledger.sweep(["/r"], self.agents, 220), [])  # reported on the transition only

    def test_a_pr_on_another_base_is_not_rechecked(self) -> None:
        self.ledger.sweep(["/r"], self.agents, 100)
        self.gh.detail_reads.clear()
        self.gh.open["/r"] = [self.gh.open["/r"][0], OpenPr(651, "t", "dependabot/x", "stage", "bbb222")]
        self.ledger.sweep(["/r"], self.agents, 130)  # 651 moved to base stage: a new fact, read once
        self.gh.detail_reads.clear()
        self.gh.open["/r"] = self.gh.open["/r"][1:]  # #646 (develop) merges
        self.ledger.sweep(["/r"], self.agents, 160)
        self.assertEqual(self.gh.detail_reads, [])

    def test_github_down_keeps_the_last_ledger(self) -> None:
        self.ledger.sweep(["/r"], self.agents, 100)
        self.gh.down = True
        self.ledger.sweep(["/r"], self.agents, 300)
        self.assertEqual(len(self.state.untracked_prs()), 1)
        self.assertEqual(self.ledger_of()["since"], 100)

    def test_a_closed_pr_leaves_the_untracked_list(self) -> None:
        self.ledger.sweep(["/r"], self.agents, 100)
        self.gh.open["/r"] = self.gh.open["/r"][:1]
        self.ledger.sweep(["/r"], self.agents, 160)
        self.assertEqual(self.state.untracked_prs(), [])

    def test_a_changed_mergeable_state_alone_does_not_restart_time_in_state(self) -> None:
        self.ledger.sweep(["/r"], self.agents, 100)
        self.gh.open["/r"][0] = pr(646, "WAKI-12", state="blocked", review="approved")  # still waiting on merge
        self.gh.detail[646] = pr(646, "WAKI-12", state="blocked", review="approved")
        self.ledger.sweep(["/r"], self.agents, 160)
        led = self.ledger_of()
        self.assertEqual((led["waiting_on"], led["mergeable_state"], led["since"]), ("merge", "blocked", 100))

    def test_status_rows_and_table(self) -> None:
        self.assertEqual(status_rows(self.state, 0), [])  # nothing swept yet
        self.assertIn("not recorded any yet", status_table([]))
        self.ledger.sweep(["/r"], self.agents, 100)
        rows = status_rows(self.state, 100 + 300)
        self.assertEqual(rows, [
            {"branch": "WAKI-12", "pr": 646, "tracked": True, "state": "waiting_on", "waiting_on": "merge",
             "owner": "orchestrator", "age_s": 300, "checks": "passing", "mergeable_state": "clean"},
            {"branch": "dependabot/npm_and_yarn/left-pad-1.1", "pr": 651, "tracked": False, "state": "waiting_on",
             "waiting_on": "conflict", "owner": "orchestrator", "age_s": 300, "checks": "passing",
             "mergeable_state": "dirty"}])
        lines = status_table(rows).splitlines()
        self.assertEqual(lines[0].split(), ["BRANCH", "PR", "STATE", "OWNER", "IN", "STATE", "CHECKS", "MERGEABLE"])
        self.assertEqual(lines[1].split(), ["WAKI-12", "#646", "waiting", "on", "merge", "orchestrator", "5m", "passing", "clean"])
        self.assertIn("(untracked PR)", lines[2])

    def test_the_attention_lines(self) -> None:
        self.ledger.sweep(["/r"], self.agents, 100)
        self.assertEqual(ledger_lines(self.state, 100 + 300), [
            "WAKI-12: waiting on merge (orchestrator) 5m",
            "untracked PR #651 (dependabot/npm_and_yarn/left-pad-1.1): waiting on conflict (orchestrator) 5m"])


class RunnerLedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.topic = Topic(name="waki", dir=root / "cfg", match=["/r"])
        self.state = TopicState(root, "waki")
        self.herdr = FakeHerdr()
        self.herdr.agent_list = [Agent("orch-waki", "working", "w2:p1", "w2")]
        self.herdr.workspace_list = [Workspace("w2", "orch", "/r")]  # a repo of the Topic with no tracked branch
        self.gh = FakeGitHub()
        self.gh.open["/r"] = parse_open_pulls(LISTING)

        class Quiet:
            def poll(self, now: float) -> None: ...
            def lines(self) -> list[tuple[str, str]]:
                return []
        self.runner = Runner(self.topic, self.state, self.herdr, Quiet(), notify=lambda t, b: None, github=self.gh)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_the_sweep_covers_the_topics_repos_even_with_no_tracked_branch_and_polls_slowly(self) -> None:
        self.runner.tick(1000)
        self.assertEqual([u["number"] for u in self.state.untracked_prs()], [646, 651])
        self.gh.open["/r"] = []
        self.runner.tick(1000 + PR_POLL_SECONDS - 1)
        self.assertEqual(len(self.state.untracked_prs()), 2)
        self.runner.tick(1000 + PR_POLL_SECONDS)
        self.assertEqual(self.state.untracked_prs(), [])

    def test_the_attention_pane_shows_the_ledger(self) -> None:
        self.runner.tick(1000)
        self.state.touch_heartbeat(1000)
        view = load(Path(self.tmp.name), [self.topic], self.herdr, 1300)
        self.assertIn("waki: untracked PR #646 (WAKI-12): waiting on ci (runner) 5m", view.ledger)
