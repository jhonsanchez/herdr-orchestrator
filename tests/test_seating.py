import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from herdr_orch.herdr import Agent, Workspace
from herdr_orch import seating
from herdr_orch.seating import agent_name, bring_back, default_base, ensure_link, ensure_orch_session, orch_herdr, seat, session_name, start, startup_screen
from herdr_orch.state import TopicState
from herdr_orch.topics import Topic
from tests.fakes import FakeHerdr


class AgentNameTest(unittest.TestCase):
    def test_branch_becomes_a_valid_herdr_agent_name(self) -> None:
        self.assertEqual(agent_name("WAKI-1412"), "waki-1412")
        self.assertEqual(agent_name("feat/Add Sync"), "feat-add-sync")
        self.assertEqual(agent_name("123-fix"), "a-123-fix")
        self.assertEqual(len(agent_name("x" * 50)), 32)

    def test_long_branches_sharing_a_prefix_get_distinct_names(self) -> None:
        a = agent_name("dependabot/npm_and_yarn/frontend/vite-5.4.1")
        b = agent_name("dependabot/npm_and_yarn/frontend/vue-3.5.0")
        c = agent_name("dependabot/npm_and_yarn/frontend/vite-5.4.1")
        self.assertNotEqual(a, b)
        self.assertEqual(a, c)
        self.assertTrue(all(len(n) <= 32 and n.startswith("dependabot-npm_and_yarn") for n in (a, b)))

    def test_a_name_a_live_agent_has_gets_a_suffix(self) -> None:
        self.assertEqual(agent_name("feat-a", {"feat-a"})[:7], "feat-a-")


MCP_SCREEN = """ 2 new MCP servers found in this project
  Select any you wish to enable.
  MCP servers may execute code or access system resources. All tool calls require approval.
  ❯ [✔] ms365
    [✔] trilium
       Enable selected
 Space to select · Esc to reject all"""
TRUST_SCREEN = """ Quick safety check: Is this a project you created or one you trust? (Like your own code)
 ❯ No, exit
   Yes, I trust this folder"""
PROMPT = """ ☀  Clear  0% of context
❯ Try "how does <filepath> work?"
  ⏵⏵ auto mode on (shift+tab to cycle)"""


def cloned_repo(root: Path, default: str) -> Path:
    """A clone of a bare repo whose default branch is `default`, so origin/HEAD is set."""
    def run(*args: str) -> None:
        subprocess.run(args, check=True, capture_output=True)

    run("git", "init", "-q", "--bare", "-b", default, str(root / "remote.git"))
    seed = root / "seed"
    run("git", "init", "-q", "-b", default, str(seed))
    run("git", "-C", str(seed), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init")
    run("git", "-C", str(seed), "push", "-q", str(root / "remote.git"), default)
    run("git", "clone", "-q", str(root / "remote.git"), str(root / "clone"))
    return root / "clone"


class DefaultBaseTest(unittest.TestCase):
    def test_reads_the_repos_default_branch_from_origin_head(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(default_base(str(cloned_repo(Path(d), "trunk"))), "origin/trunk")

    def test_a_repo_without_origin_head_leaves_it_to_herdr(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            subprocess.run(["git", "init", "-q", d], check=True)
            self.assertIsNone(default_base(d))
            self.assertIsNone(default_base(str(Path(d) / "missing")))


class StartupScreenTest(unittest.TestCase):
    def test_recognises_claudes_startup_screens(self) -> None:
        self.assertEqual(startup_screen(MCP_SCREEN), "enable this project's MCP servers")
        self.assertEqual(startup_screen(MCP_SCREEN.replace("2 new MCP servers", "1 new MCP server")), "enable this project's MCP servers")
        self.assertEqual(startup_screen(TRUST_SCREEN), "trust this folder")
        self.assertIsNone(startup_screen(PROMPT))


@mock.patch.object(seating, "POLL_SECONDS", 0)
class SeatingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.topic = Topic(
            name="zebra", dir=root / "cfg/topics/zebra", model="sonnet", base="origin/develop",
            env={"CLAUDE_CONFIG_DIR": "~/.claude-zebra"},
        )
        self.state = TopicState(root / "state", "zebra")
        self.herdr = FakeHerdr()
        self.spawned: list[tuple[str, str]] = []
        self.herdr.workspace_list = [Workspace("w3", "zpc", "/home/u/projects/zebra/zpc")]

    def spawn(self, cli: str, topic: Topic, state: TopicState) -> None:
        self.spawned.append((cli, topic.name))
        self.herdr.calls.append(("spawn",))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_seat_creates_worktree_exports_env_starts_registers_and_briefs(self) -> None:
        r = seat(self.topic, self.state, self.herdr, workspace_id="w3", branch="FALCON-12", home="/home/u", cli="/opt/herdr-orch")
        self.assertEqual(self.herdr.of("worktree_create"), [
            ("worktree_create", "w3", "FALCON-12", "FALCON-12", "/home/u/.herdr/worktrees/zpc/FALCON-12", "origin/develop"),
        ])
        self.assertIn(("pane_run", r.pane_id, "export CLAUDE_CONFIG_DIR=/home/u/.claude-zebra"), self.herdr.of("pane_run"))
        self.assertEqual(self.herdr.of("agent_start"), [("agent_start", "falcon-12", r.pane_id, ("--model", "sonnet"))])
        self.assertEqual(self.state.managed(), {r.pane_id: "falcon-12"})
        [prompt] = self.herdr.of("prompt")
        self.assertEqual(prompt[1], "falcon-12")
        self.assertIn(str(self.topic.brief_path), str(prompt[2]))
        self.assertIn(f"run: HERDR_ORCH_CONFIG={self.topic.config_root} HERDR_ORCH_STATE={self.state.root} /opt/herdr-orch "
                      "validated zebra --branch FALCON-12", str(prompt[2]))

    def test_seat_exports_the_agents_name_and_topic_before_starting_it(self) -> None:
        r = seat(self.topic, self.state, self.herdr, workspace_id="w3", branch="FALCON-12", home="/home/u", cli="/opt/herdr-orch")
        runs = [c[2] for c in self.herdr.of("pane_run")]
        self.assertIn("export HERDR_ORCH_AGENT=falcon-12", runs)
        self.assertIn("export HERDR_ORCH_TOPIC=zebra", runs)
        calls = [c[0] for c in self.herdr.calls]
        self.assertLess(max(i for i, c in enumerate(calls) if c == "pane_run"), calls.index("agent_start"))
        self.assertTrue(all(c[1] == r.pane_id for c in self.herdr.of("pane_run")))

    def test_seat_refuses_a_branch_or_workspace_a_live_agent_already_tracks(self) -> None:
        r = seat(self.topic, self.state, self.herdr, workspace_id="w3", branch="FALCON-12", home="/home/u", cli="/opt/herdr-orch")
        self.herdr.agent_list.append(Agent("falcon-12", "idle", r.pane_id, "w9"))
        self.state.track_branch("FALCON-12", "falcon-12", "/home/u/projects/zebra/zpc", "w9", 1.0)
        ws = "w9"
        with self.assertRaisesRegex(ValueError, "FALCON-12 is already tracked"):
            seat(self.topic, self.state, self.herdr, workspace_id="w3", branch="FALCON-12", home="/home/u", cli="/opt/herdr-orch")
        self.herdr.workspace_list.append(Workspace(str(ws), "FALCON-12", "/home/u/projects/zebra/zpc", "/wt"))
        with self.assertRaisesRegex(ValueError, "one branch in one workspace"):
            seat(self.topic, self.state, self.herdr, workspace_id=str(ws), branch="FALCON-13", home="/home/u", cli="/opt/herdr-orch")
        self.assertEqual(len(self.herdr.of("worktree_create")), 1)
        self.assertEqual(self.state.managed(), {r.pane_id: "falcon-12"})

    def test_a_failed_seat_removes_what_it_made_and_is_an_event(self) -> None:
        self.herdr.prompt_error = "falcon-12 never took the prompt"
        said: list[str] = []
        with self.assertRaisesRegex(RuntimeError, "never took the prompt"):
            seat(self.topic, self.state, self.herdr, workspace_id="w3", branch="FALCON-12", home="/home/u",
                 say=said.append, cli="/opt/herdr-orch")
        self.assertEqual(self.herdr.of("worktree_remove"), [("worktree_remove", "wt1")])
        self.assertEqual([w.workspace_id for w in self.herdr.workspace_list], ["w3"])
        self.assertEqual(self.state.managed(), {})
        self.assertIsNone(self.state.branch_record("FALCON-12"))
        [event] = [e for e in self.state.events.read_text().splitlines() if "\tseat_failed\t" in e]
        self.assertIn("seat of FALCON-12 failed", event)
        self.assertIn("removed workspace/worktree wt1", event)
        self.assertIn("removed workspace/worktree wt1", said[-1])

    def test_a_seat_waiting_at_a_dialog_is_kept(self) -> None:
        self.herdr.becomes_ready = False
        self.herdr.ready_after_start = False
        with mock.patch.object(seating, "DIALOG_TIMEOUT_SECONDS", 0.01), self.assertRaises(TimeoutError):
            seat(self.topic, self.state, self.herdr, workspace_id="w3", branch="FALCON-12", home="/home/u",
                 say=lambda m: None, cli="/opt/herdr-orch")
        self.assertEqual(self.herdr.of("worktree_remove"), [])
        self.assertIsNotNone(self.state.branch_record("FALCON-12"))

    def test_seat_names_long_colliding_branches_apart_and_name_overrides(self) -> None:
        first = seat(self.topic, self.state, self.herdr, workspace_id="w3", branch="dependabot/npm_and_yarn/frontend/a",
                     home="/home/u", cli="/x")
        self.herdr.agent_list.append(Agent(first.name, "idle", first.pane_id, "wt1"))
        second = seat(self.topic, self.state, self.herdr, workspace_id="w3", branch="dependabot/npm_and_yarn/frontend/b",
                      home="/home/u", cli="/x")
        self.assertNotEqual(first.name, second.name)
        named = seat(self.topic, self.state, self.herdr, workspace_id="w3", branch="issue-5", home="/home/u", cli="/x", name="mine")
        self.assertEqual(named.name, "mine")
        with self.assertRaisesRegex(ValueError, "not valid"):
            seat(self.topic, self.state, self.herdr, workspace_id="w3", branch="issue-6", home="/home/u", cli="/x", name="Bad Name")
        self.assertEqual(len(self.herdr.of("worktree_create")), 3)

    def write_playbook(self, text: str = "# Playbook\n\n## Yours alone\n\n- Production.\n") -> None:
        self.topic.dir.mkdir(parents=True, exist_ok=True)
        self.topic.playbook_path.write_text(text)

    def test_start_opens_workspace_attention_pane_and_orchestrator_with_no_runner_pane(self) -> None:
        self.write_playbook()
        start(self.topic, self.state, self.herdr, cli="/opt/plug/bin/herdr-orch", spawn=self.spawn)
        [(_, cwd, label)] = self.herdr.of("workspace_create")
        self.assertEqual(label, "orch-zebra")
        self.assertEqual(self.herdr.of("pane_split"), [])
        self.assertEqual(self.herdr.of("pane_run"), [])
        self.assertEqual(self.spawned, [("/opt/plug/bin/herdr-orch", "zebra")])
        [(_, name, pane, args)] = self.herdr.of("agent_start")
        self.assertEqual(name, "orch-zebra")
        self.assertEqual(self.herdr.of("attention_open"), [("attention_open", "w51", pane, "zebra")])
        [prompt] = self.herdr.of("prompt")
        self.assertIn(str(self.topic.playbook_path), str(prompt[2]))
        self.assertIn(f"HERDR_ORCH_STATE={self.state.dir.parent.parent} /opt/plug/bin/herdr-orch heartbeat zebra", str(prompt[2]))
        self.assertIn("/opt/plug/bin/herdr-orch status zebra", str(prompt[2]))

    def test_the_runner_starts_only_after_the_orchestrator_exists(self) -> None:
        self.write_playbook()
        start(self.topic, self.state, self.herdr, cli="/x", spawn=self.spawn)
        kinds = [c[0] for c in self.herdr.calls]
        self.assertLess(kinds.index("agent_start"), kinds.index("spawn"))

    def test_start_refuses_a_playbook_without_yours_alone(self) -> None:
        self.write_playbook("# Playbook\n\nAnything goes.\n")
        with self.assertRaises(ValueError):
            start(self.topic, self.state, self.herdr, cli="/x", spawn=self.spawn)
        self.assertEqual(self.herdr.calls, [])

    def test_start_refuses_a_leftover_workspace_without_its_orchestrator(self) -> None:
        self.write_playbook()
        self.herdr.workspace_list.append(Workspace("w7", "orch-zebra", None))
        with self.assertRaises(RuntimeError):
            start(self.topic, self.state, self.herdr, cli="/x", spawn=self.spawn)
        self.assertEqual(self.herdr.of("workspace_create"), [])

    def test_seat_on_an_unknown_workspace_says_so(self) -> None:
        with self.assertRaisesRegex(ValueError, "w404"):
            seat(self.topic, self.state, self.herdr, workspace_id="w404", branch="x", home="/home/u", cli="/opt/herdr-orch")

    def test_start_waits_for_a_startup_dialog_before_briefing(self) -> None:
        self.write_playbook()
        self.herdr.ready_after_start = False
        said: list[str] = []
        self.assertTrue(start(self.topic, self.state, self.herdr, cli="/x", spawn=self.spawn, say=said.append))
        self.assertEqual(self.herdr.of("wait_ready"), [("wait_ready", "orch-zebra")])
        self.assertIn("orch-zebra", said[0])
        self.assertEqual(len(self.herdr.of("prompt")), 1)

    def test_start_gives_up_without_briefing_when_the_dialog_is_never_answered(self) -> None:
        self.write_playbook()
        self.herdr.ready_after_start = False
        self.herdr.becomes_ready = False
        self.assertFalse(start(self.topic, self.state, self.herdr, cli="/x", spawn=self.spawn, say=lambda m: None))
        self.assertEqual(self.herdr.of("prompt"), [])

    def test_seat_waits_for_a_startup_dialog_too(self) -> None:
        self.herdr.ready_after_start = False
        seat(self.topic, self.state, self.herdr, workspace_id="w3", branch="FALCON-12", home="/home/u", say=lambda m: None, cli="/opt/herdr-orch")
        self.assertEqual(self.herdr.of("wait_ready"), [("wait_ready", "falcon-12")])
        self.assertEqual(len(self.herdr.of("prompt")), 1)

    def test_seat_without_a_topic_base_uses_the_repos_default_branch(self) -> None:
        repo = cloned_repo(Path(self.tmp.name), "main")
        self.herdr.workspace_list = [Workspace("w4", "x", str(repo))]
        topic = Topic(name="personal", dir=self.topic.dir)
        seat(topic, self.state, self.herdr, workspace_id="w4", branch="issue-3", home="/home/u", cli="/opt/herdr-orch")
        [call] = self.herdr.of("worktree_create")
        self.assertEqual(call[-1], "origin/main")

    def test_seat_waits_out_the_mcp_screen_before_briefing(self) -> None:
        self.herdr.screens["w9:p1"] = [MCP_SCREEN, MCP_SCREEN, PROMPT]
        said: list[str] = []
        seat(self.topic, self.state, self.herdr, workspace_id="w3", branch="FALCON-12", home="/home/u", say=said.append, cli="/opt/herdr-orch")
        self.assertEqual(said, ["falcon-12 is waiting at a startup screen (enable this project's MCP servers): answer it in its pane."])
        self.assertEqual(len(self.herdr.of("read_screen")), 3)
        kinds = [c[0] for c in self.herdr.calls]
        self.assertLess(max(i for i, k in enumerate(kinds) if k == "read_screen"), kinds.index("prompt"))

    def test_seat_gives_up_when_the_screen_never_clears(self) -> None:
        self.herdr.screens["w9:p1"] = [MCP_SCREEN]
        with mock.patch.object(seating, "DIALOG_TIMEOUT_SECONDS", 0.01):
            with self.assertRaises(TimeoutError):
                seat(self.topic, self.state, self.herdr, workspace_id="w3", branch="FALCON-12", home="/home/u", say=lambda m: None, cli="/opt/herdr-orch")
        self.assertEqual(self.herdr.of("prompt"), [])

    def test_start_waits_out_the_mcp_screen_too(self) -> None:
        self.write_playbook()
        self.herdr.screens["w9:p1"] = [MCP_SCREEN, PROMPT]
        self.assertTrue(start(self.topic, self.state, self.herdr, cli="/x", spawn=self.spawn, say=lambda m: None))
        self.assertEqual(len(self.herdr.of("prompt")), 1)

    def test_a_clean_start_reads_the_screen_once_and_briefs(self) -> None:
        seat(self.topic, self.state, self.herdr, workspace_id="w3", branch="FALCON-12", home="/home/u", say=lambda m: self.fail(m), cli="/opt/herdr-orch")
        self.assertEqual(len(self.herdr.of("read_screen")), 1)
        self.assertEqual(len(self.herdr.of("prompt")), 1)

    def test_the_orchestrator_runs_on_the_topics_model(self) -> None:
        self.write_playbook()
        start(self.topic, self.state, self.herdr, cli="/x", spawn=self.spawn, say=lambda m: None)
        [(_, name, _, args)] = self.herdr.of("agent_start")
        self.assertEqual((name, args), ("orch-zebra", ("--model", "sonnet")))

    def test_orchestrator_model_overrides_the_topics_model(self) -> None:
        self.write_playbook()
        topic = Topic(name="zebra", dir=self.topic.dir, model="sonnet", orchestrator_model="opus")
        start(topic, self.state, self.herdr, cli="/x", spawn=self.spawn, say=lambda m: None)
        [(_, _, _, args)] = self.herdr.of("agent_start")
        self.assertEqual(args, ("--model", "opus"))

    def test_a_detached_start_seats_the_orchestrator_in_its_own_session_and_points_it_at_the_topic(self) -> None:
        self.write_playbook()
        self.state.record_socket("/zebra.sock")
        orch = FakeHerdr()
        start(self.topic, self.state, self.herdr, cli="/x", spawn=self.spawn, orch=orch)
        self.assertEqual(self.herdr.calls, [("spawn",)])  # nothing added to the Topic's session
        [(_, _, label)] = orch.of("workspace_create")
        self.assertEqual(label, "orch-zebra")
        calls = [c[0] for c in orch.calls]
        self.assertLess(calls.index("pane_run"), calls.index("agent_start"))
        [(_, pane, command)] = orch.of("pane_run")
        self.assertEqual(command, "export HERDR_SOCKET_PATH=/zebra.sock")
        self.assertEqual(orch.of("attention_open"), [])
        self.assertEqual(len(orch.of("prompt")), 1)

    def test_a_detached_start_reports_a_startup_dialog_in_the_inbox_until_it_is_answered(self) -> None:
        self.write_playbook()
        orch = FakeHerdr()
        orch.ready_after_start = False
        start(self.topic, self.state, self.herdr, cli="/x", spawn=self.spawn, say=lambda m: None, orch=orch)
        item, resolved = [i for i in self.state.inbox_lines() if i["id"] == "orch-dialog"]
        self.assertIn("herdr session attach orch", str(item["question"]))
        self.assertEqual((item["status"], resolved["status"]), ("open", "resolved"))  # briefed once it was answered

    def test_a_detached_start_finds_its_orchestrator_in_the_orch_session_only(self) -> None:
        self.write_playbook()
        orch = FakeHerdr()
        orch.agent_list = [Agent("orch-zebra", "idle", "w1:p1", "w1")]
        self.state.mark_briefed()
        self.assertTrue(start(self.topic, self.state, self.herdr, cli="/x", spawn=self.spawn, orch=orch))
        self.assertEqual(orch.of("workspace_create"), [])
        self.assertEqual(self.spawned, [("/x", "zebra")])

    def test_start_says_where_it_seated_the_orchestrator(self) -> None:
        self.write_playbook()
        self.state.record_socket("/home/u/.config/herdr/sessions/personal/herdr.sock")
        said: list[str] = []
        start(self.topic, self.state, self.herdr, cli="/x", spawn=self.spawn, say=said.append)
        self.assertIn("orch-zebra is running in workspace w51 of herdr session personal", said)

    def test_start_says_where_an_orchestrator_already_running_is(self) -> None:
        self.write_playbook()
        self.state.record_socket("/home/u/.config/herdr/herdr.sock")
        self.state.mark_briefed()
        self.herdr.agent_list = [Agent("orch-zebra", "idle", "w5:p1", "w5")]
        said: list[str] = []
        self.assertTrue(start(self.topic, self.state, self.herdr, cli="/x", spawn=self.spawn, say=said.append))
        self.assertEqual(said, ["orch-zebra was already running in workspace w5 of herdr session default"])

    def test_a_detached_start_says_how_to_reach_the_orchestrator(self) -> None:
        self.write_playbook()
        orch = FakeHerdr()
        orch.agent_list = [Agent("orch-zebra", "idle", "w1:p1", "w1")]
        self.state.mark_briefed()
        said: list[str] = []
        start(self.topic, self.state, self.herdr, cli="/x", spawn=self.spawn, say=said.append, orch=orch)
        self.assertEqual(said, ["orch-zebra was already running in workspace w1 of herdr session orch: herdr-orch show zebra"])

    def test_start_resumes_and_briefs_an_orchestrator_seated_but_never_briefed(self) -> None:
        self.write_playbook()
        self.herdr.agent_list = [Agent("orch-zebra", "idle", "w5:p1", "w5")]
        self.assertTrue(start(self.topic, self.state, self.herdr, cli="/x", spawn=self.spawn, say=lambda m: None))
        self.assertEqual(self.herdr.of("agent_start"), [])
        self.assertEqual(self.herdr.of("workspace_create"), [])
        [prompt] = self.herdr.of("prompt")
        self.assertIn(str(self.topic.playbook_path), str(prompt[2]))
        self.assertTrue(self.state.briefed.exists())
        start(self.topic, self.state, self.herdr, cli="/x", spawn=self.spawn, say=lambda m: None)
        self.assertEqual(len(self.herdr.of("prompt")), 1)

    def test_a_fresh_start_marks_briefed_after_the_brief_is_sent(self) -> None:
        self.write_playbook()
        start(self.topic, self.state, self.herdr, cli="/x", spawn=self.spawn)
        self.assertTrue(self.state.briefed.exists())

    def test_start_respawns_the_runner_and_touches_nothing_else_when_the_orchestrator_is_briefed(self) -> None:
        self.state.mark_briefed()
        self.herdr.agent_list = [Agent("orch-zebra", "idle", "w5:p1", "w5")]
        start(self.topic, self.state, self.herdr, cli="/opt/plug/bin/herdr-orch", spawn=self.spawn)
        self.assertEqual(self.herdr.calls, [("spawn",)])

    def test_start_refuses_a_deploy_check_without_a_github_table(self) -> None:
        self.write_playbook()
        topic = Topic(name="zebra", dir=self.topic.dir, deploy_check=["true"])
        with self.assertRaisesRegex(ValueError, r"\[github\]"):
            start(topic, self.state, self.herdr, cli="/x", spawn=self.spawn)
        self.assertEqual(self.herdr.calls, [])

    def test_the_start_prompt_has_the_github_paragraph_only_with_a_github_table(self) -> None:
        self.write_playbook()
        start(self.topic, self.state, self.herdr, cli="/x", spawn=self.spawn)
        (off,) = [str(c[2]) for c in self.herdr.of("prompt")]
        self.assertNotIn("PR event", off)
        self.assertNotIn("gh pr merge", off)
        herdr = FakeHerdr()
        topic = Topic(name="zebra", dir=self.topic.dir, github={})
        start(topic, TopicState(Path(self.tmp.name) / "state2", "zebra"), herdr, cli="/x", spawn=self.spawn)
        (on,) = [str(c[2]) for c in herdr.of("prompt")]
        self.assertIn("PR event", on)
        self.assertNotIn("gh pr merge", on)

    def test_validated_without_github_needs_only_evidence(self) -> None:
        self.state._write_branch({"branch": "issue-1", "agent": "a1"})
        seating.validated(self.state, "issue-1", 5.0, "ran it", pr_gated=False)
        self.assertTrue(any(e.source == "validated" for e in self.state.events_since(0)))
        with self.assertRaises(ValueError):
            seating.validated(self.state, "issue-1", 6.0, " ", pr_gated=False)

    def test_release_without_github_makes_no_pr_check(self) -> None:
        self.state._write_branch({"branch": "issue-1", "agent": "a1", "repo_root": "/r"})
        seating.release(self.state, self.herdr, None, "issue-1")
        self.assertIsNone(self.state.branch_record("issue-1"))


if __name__ == "__main__":
    unittest.main()


class EnsureLinkTest(unittest.TestCase):
    def test_the_brief_names_the_stable_link_and_it_follows_an_install_that_moves(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve() / "home"
            old, new = Path(tmp).resolve() / "plug" / "1-0" / "bin" / "herdr-orch", Path(tmp).resolve() / "plug" / "2-0" / "bin" / "herdr-orch"
            for cli in (old, new):
                cli.parent.mkdir(parents=True)
                cli.write_text("#!/bin/sh\n")
            briefed = ensure_link(str(old), str(home))
            self.assertEqual(briefed, f"{home}/.local/bin/herdr-orch")
            self.assertEqual(Path(briefed).resolve(), old)
            self.assertEqual(ensure_link(str(old), str(home)), briefed)  # already right: untouched
            self.assertEqual(ensure_link(str(new), str(home)), briefed)  # stale: repointed, same briefed path
            self.assertEqual(Path(briefed).resolve(), new)

    def test_the_link_goes_to_the_newest_install_not_the_one_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            old, new = (root / "plugins" / "github-installations" / f"jhonsanchez.orchestrator-x" / v / "checkout" / "bin" / "herdr-orch"
                        for v in ("1-0", "2-0"))
            for n, cli in enumerate((old, new)):
                cli.parent.mkdir(parents=True)
                cli.write_text("#!/bin/sh\n")
                os.utime(cli, (1000 + n, 1000 + n))
            link = ensure_link(str(old), str(root / "home"), root / "plugins")
            self.assertEqual(Path(link).resolve(), new)



class RunnerRespawnTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.topic = Topic(name="waki", dir=root / "cfg/topics/waki")
        self.state = TopicState(root / "state", "waki")
        self.spawned: list[str] = []

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def ensure(self, alive: bool, now: float = 1000.0) -> bool:
        return seating.ensure_runner("/x/herdr-orch", self.topic, self.state, now,
                                     spawn=lambda cli, t, s: self.spawned.append(cli), alive=lambda pid: alive)

    def test_a_dead_pid_is_respawned(self) -> None:
        self.state.record_runner(4242, 990.0, 5)
        self.assertTrue(self.ensure(alive=False))
        self.assertEqual(self.spawned, ["/x/herdr-orch"])

    def test_no_runner_file_is_respawned(self) -> None:
        self.assertTrue(self.ensure(alive=True))

    def test_a_live_fresh_runner_is_left_alone(self) -> None:
        self.state.record_runner(4242, 990.0, 5)
        self.assertFalse(self.ensure(alive=True))
        self.assertEqual(self.spawned, [])

    def test_a_stale_runner_is_respawned_even_when_its_pid_lives(self) -> None:
        self.state.record_runner(4242, 100.0, 5)
        with mock.patch.object(seating, "_stop_wedged") as stop:
            self.assertTrue(self.ensure(alive=True))
        stop.assert_called_once_with(4242, "waki")
        self.assertEqual(len(self.spawned), 1)

    def test_a_runner_briefly_stale_during_a_reexec_is_left_alone(self) -> None:
        self.state.record_runner(4242, 910.0, 5)  # 90s: past stale, inside the grace period
        with mock.patch.object(seating, "_stop_wedged") as stop:
            self.assertFalse(self.ensure(alive=True))
        stop.assert_not_called()
        self.assertEqual(self.spawned, [])

    def test_spawn_gives_the_runner_the_topics_recorded_socket(self) -> None:
        self.state.record_socket("/run/waki.sock")
        with mock.patch("subprocess.Popen") as popen:
            seating.spawn_runner("/x/herdr-orch", self.topic, self.state)
        self.assertEqual(popen.call_args.kwargs["env"]["HERDR_SOCKET_PATH"], "/run/waki.sock")


class SessionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.topic = Topic(name="waki", dir=root / "cfg/topics/waki")
        self.state = TopicState(root / "state", "waki")
        self.sessions: dict[str | None, FakeHerdr] = {"/waki.sock": FakeHerdr(), "/personal.sock": FakeHerdr()}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def pick(self, caller: str | None) -> FakeHerdr:
        return seating.session_herdr(self.topic, self.state, caller, self.sessions.__getitem__)  # type: ignore[return-value]

    def test_a_recorded_socket_wins_over_the_callers(self) -> None:
        self.state.record_socket("/waki.sock")
        self.sessions["/waki.sock"].agent_list = [Agent("orch-waki", "idle", "w1:p1", "w1")]
        self.assertIs(self.pick("/personal.sock"), self.sessions["/waki.sock"])

    def test_a_recorded_session_without_its_orchestrator_refuses(self) -> None:
        self.state.record_socket("/waki.sock")
        with self.assertRaisesRegex(RuntimeError, "/waki.sock"):
            self.pick("/personal.sock")
        self.assertEqual(self.sessions["/personal.sock"].calls, [])

    def test_an_orchestrator_running_in_its_own_session_satisfies_the_guard(self) -> None:
        self.state.record_socket("/waki.sock")
        self.state.record_orch_socket("/orch.sock")
        self.sessions["/orch.sock"] = FakeHerdr()
        self.sessions["/orch.sock"].agent_list = [Agent("orch-waki", "idle", "w1:p1", "w1")]
        self.assertIs(self.pick("/personal.sock"), self.sessions["/waki.sock"])

    def test_the_recorded_session_itself_may_start_a_missing_orchestrator(self) -> None:
        self.state.record_socket("/waki.sock")
        self.assertIs(self.pick("/waki.sock"), self.sessions["/waki.sock"])

    def test_nothing_recorded_uses_the_callers(self) -> None:
        self.assertIs(self.pick("/personal.sock"), self.sessions["/personal.sock"])


class OrchSessionTest(unittest.TestCase):
    def runner(self, running_after: int) -> tuple[list[list[str]], list[dict[str, object]], object]:
        """A fake `herdr`: `session list` shows orch running from its `running_after`-th call on."""
        listed: list[list[str]] = []
        started: list[dict[str, object]] = []

        def run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
            listed.append(cmd)
            up = len(listed) > running_after
            rows = [{"name": "personal", "running": True, "socket_path": "/personal.sock"},
                    {"name": "orch", "running": up, "socket_path": "/orch.sock"}]
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"sessions": rows}), stderr="")

        def popen(cmd: list[str], **kw: object) -> object:
            started.append({"cmd": cmd, **kw})
            return object()

        self.run_, self.popen = run, popen
        return listed, started, run

    def test_a_running_orch_session_is_reused(self) -> None:
        _, started, run = self.runner(running_after=0)
        self.assertEqual(ensure_orch_session(run=run, popen=self.popen, sleep=lambda s: None, binary="herdr"), "/orch.sock")  # type: ignore[arg-type]
        self.assertEqual(started, [])

    def test_a_missing_orch_session_is_started_headless_and_detached_from_the_callers(self) -> None:
        _, started, run = self.runner(running_after=2)
        with mock.patch.dict(os.environ, {"HERDR_SESSION": "personal", "HERDR_SOCKET_PATH": "/personal.sock", "HERDR_PANE_ID": "w1:p1"}):
            sock = ensure_orch_session(run=run, popen=self.popen, sleep=lambda s: None, binary="herdr")  # type: ignore[arg-type]
        self.assertEqual(sock, "/orch.sock")
        [one] = started
        self.assertEqual(one["cmd"], ["herdr", "server"])
        env = one["env"]
        assert isinstance(env, dict)
        self.assertEqual(env["HERDR_SESSION"], "orch")
        self.assertNotIn("HERDR_SOCKET_PATH", env)
        self.assertNotIn("HERDR_PANE_ID", env)
        self.assertTrue(one["start_new_session"])

    def test_a_session_that_never_comes_up_is_an_error(self) -> None:
        _, _, run = self.runner(running_after=10**6)
        with mock.patch.object(seating, "SESSION_WAIT_S", 1.0), self.assertRaisesRegex(RuntimeError, "did not come up"):
            ensure_orch_session(run=run, popen=self.popen, sleep=lambda s: None, binary="herdr")  # type: ignore[arg-type]

    def test_the_orchestrator_is_reached_through_its_recorded_socket_else_the_topics_herdr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = TopicState(Path(tmp), "waki")
            topic_herdr, orch = FakeHerdr(), FakeHerdr()
            made = {"/orch.sock": orch}
            self.assertIs(orch_herdr(state, topic_herdr, made.__getitem__), topic_herdr)  # type: ignore[arg-type]
            state.record_orch_socket("/orch.sock")
            self.assertIs(orch_herdr(state, topic_herdr, made.__getitem__), orch)  # type: ignore[arg-type]


class SessionNameTest(unittest.TestCase):
    def test_a_named_sessions_socket_names_it(self) -> None:
        self.assertEqual(session_name("/home/u/.config/herdr/sessions/waki/herdr.sock"), "waki")

    def test_the_default_sessions_socket_is_default(self) -> None:
        self.assertEqual(session_name("/home/u/.config/herdr/herdr.sock"), "default")

    def test_no_socket_is_no_name(self) -> None:
        self.assertIsNone(session_name(None))


class BringBackTest(unittest.TestCase):
    """An Orchestrator a detached start put in the orch session moves back to its Topic's session."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.topic = Topic(name="personal", dir=root / "cfg/topics/personal")
        self.state = TopicState(root / "state", "personal")
        self.state.record_socket("/personal.sock")
        self.state.record_orch_socket("/orch.sock")
        self.state.mark_briefed()
        self.state.record_runner(4242, 1000.0, 7)
        self.orch = FakeHerdr()
        self.orch.agent_list = [Agent("orch-personal", "idle", "w1:p1", "w1")]
        self.stopped: list[str] = []
        self.killed: list[tuple[int, str]] = []

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def bring_back(self) -> None:
        bring_back(self.topic, self.state, self.orch, stop_session=self.stopped.append,
                   stop_runner=lambda pid, topic: self.killed.append((pid, topic)))

    def test_it_closes_the_orch_workspace_and_forgets_the_orch_session(self) -> None:
        self.bring_back()
        self.assertEqual(self.orch.of("workspace_close"), [("workspace_close", "w1")])
        self.assertIsNone(self.state.orch_socket())
        self.assertEqual(self.state.socket(), "/personal.sock")
        self.assertFalse(self.state.briefed.exists())  # the reseated Orchestrator is briefed afresh

    def test_it_stops_the_runner_that_watches_the_orch_session(self) -> None:
        self.bring_back()
        self.assertEqual(self.killed, [(4242, "personal")])
        self.assertIsNone(self.state.runner_status())  # the runner start spawns next must not defer to it

    def test_it_stops_the_orch_session_once_no_orchestrator_is_left_in_it(self) -> None:
        self.bring_back()
        self.assertEqual(self.stopped, ["orch"])

    def test_it_leaves_the_orch_session_to_another_topics_orchestrator(self) -> None:
        self.orch.agent_list.append(Agent("orch-waki", "idle", "w2:p1", "w2"))
        self.bring_back()
        self.assertEqual(self.stopped, [])

    def test_an_orchestrator_already_gone_from_the_orch_session_only_clears_the_record(self) -> None:
        self.orch.agent_list = []
        self.bring_back()
        self.assertEqual(self.orch.of("workspace_close"), [])
        self.assertIsNone(self.state.orch_socket())
