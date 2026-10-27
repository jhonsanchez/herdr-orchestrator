import os
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from herdr_orch.herdr import Agent, Workspace
from herdr_orch import seating
from herdr_orch.seating import agent_name, default_base, ensure_link, seat, start, startup_screen
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
        self.assertEqual(self.herdr.of("pane_run"), [("pane_run", r.pane_id, "export CLAUDE_CONFIG_DIR=/home/u/.claude-zebra")])
        self.assertEqual(self.herdr.of("agent_start"), [("agent_start", "falcon-12", r.pane_id, ("--model", "sonnet"))])
        self.assertEqual(self.state.managed(), {r.pane_id: "falcon-12"})
        [prompt] = self.herdr.of("prompt")
        self.assertEqual(prompt[1], "falcon-12")
        self.assertIn(str(self.topic.brief_path), str(prompt[2]))
        self.assertIn(f"run: HERDR_ORCH_CONFIG={self.topic.config_root} HERDR_ORCH_STATE={self.state.root} /opt/herdr-orch "
                      "validated zebra --branch FALCON-12", str(prompt[2]))

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

