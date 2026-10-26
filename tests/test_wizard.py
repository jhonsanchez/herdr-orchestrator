import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from herdr_orch.cli import main
from herdr_orch.herdr import Workspace
from herdr_orch.topics import load_topic
from tests.fakes import FakeHerdr

from herdr_orch.wizard import CHECKLIST, ask_answers, matching_repos, suggest_account, suggest_match, suggest_name


class DetectionTest(unittest.TestCase):
    def test_the_name_comes_from_the_repos_parent_folder(self) -> None:
        self.assertEqual(suggest_name("/home/u/projects/personal/herdr-orchestrator"), "personal")
        self.assertEqual(suggest_name("/home/u/projects/Client X/app"), "client-x")
        self.assertEqual(suggest_name("/srv/9lives/app"), "t-9lives")

    def test_the_pattern_is_the_parent_folder_written_from_home(self) -> None:
        self.assertEqual(suggest_match("/home/u/projects/personal/x", home="/home/u"), "~/projects/personal/*")
        self.assertEqual(suggest_match("/srv/code/x", home="/home/u"), "/srv/code/*")

    def test_matching_repos_lists_what_a_pattern_covers(self) -> None:
        roots = ["/home/u/projects/personal/a", "/home/u/projects/waki/b", "/home/u/projects/personal/c", "/home/u/projects/personal/a"]
        self.assertEqual(matching_repos("~/projects/personal/*", roots, home="/home/u"),
                         ["/home/u/projects/personal/a", "/home/u/projects/personal/c"])

    def test_the_account_follows_the_current_shell(self) -> None:
        self.assertEqual(suggest_account({"CLAUDE_CONFIG_DIR": "/home/u/.claude-zebra"}, home="/home/u"),
                         {"CLAUDE_CONFIG_DIR": "~/.claude-zebra"})
        self.assertEqual(suggest_account({"CLAUDE_CONFIG_DIR": "/home/u/.claude"}, home="/home/u"), {})
        self.assertEqual(suggest_account({}, home="/home/u"), {})


class Script:
    """Answers questions in order; an empty answer takes the default. Records what was asked."""

    def __init__(self, *answers: str) -> None:
        self.answers, self.asked = list(answers), []  # type: list[str], list[tuple[str, str]]

    def __call__(self, question: str, default: str) -> str:
        self.asked.append((question, default))
        answer = self.answers.pop(0) if self.answers else ""
        return answer or default


class AskAnswersTest(unittest.TestCase):
    roots = ["/home/u/projects/personal/a", "/home/u/projects/personal/b", "/home/u/projects/waki/c"]

    def test_enter_through_every_question_takes_the_detected_defaults(self) -> None:
        ask = Script()
        name, a = ask_answers("/home/u/projects/personal/a", self.roots, {"HOME": "/home/u"}, "/home/u", ask,
                              has_stop_hook=True)
        self.assertEqual(name, "personal")
        self.assertEqual(a.match, ["~/projects/personal/*"])
        self.assertEqual((a.model, a.env, a.notify, a.notify_command), ("sonnet", None, "stop-hook", None))
        self.assertEqual(a.yours_alone, CHECKLIST)
        self.assertIn("2 repos", ask.asked[1][0])

    def test_without_a_stop_hook_alerts_default_to_herdr(self) -> None:
        _, a = ask_answers("/home/u/projects/personal/a", self.roots, {"HOME": "/home/u"}, "/home/u", Script(),
                           has_stop_hook=False)
        self.assertEqual(a.notify, "herdr")

    def test_answers_override_the_defaults(self) -> None:
        ask = Script("side", "~/code/*", "opus", "CLAUDE_CONFIG_DIR=~/.claude-x", "ntfy-send -t me", "2,4", "Touching ADRs")
        name, a = ask_answers("/home/u/projects/personal/a", self.roots, {"HOME": "/home/u"}, "/home/u", ask,
                              has_stop_hook=True)
        self.assertEqual(name, "side")
        self.assertEqual((a.match, a.model, a.env), (["~/code/*"], "opus", {"CLAUDE_CONFIG_DIR": "~/.claude-x"}))
        self.assertEqual((a.notify, a.notify_command), (None, ["ntfy-send", "-t", "me"]))
        self.assertEqual(a.yours_alone, [CHECKLIST[0], CHECKLIST[2], "Touching ADRs"])

    def test_a_taken_name_is_asked_again_with_a_free_suggestion(self) -> None:
        ask = Script("personal")
        name, _ = ask_answers("/home/u/projects/personal/a", self.roots, {"HOME": "/home/u"}, "/home/u", ask,
                              has_stop_hook=True, taken={"personal"})
        self.assertEqual(name, "personal-2")
        self.assertEqual(ask.asked[0][1], "personal-2")

    def test_unticking_everything_leaves_no_items(self) -> None:
        _, a = ask_answers("/home/u/projects/personal/a", self.roots, {"HOME": "/home/u"}, "/home/u",
                           Script("", "", "", "", "", "1,2,3,4"), has_stop_hook=True)
        self.assertEqual(a.yours_alone, [])

    def test_the_account_question_defaults_to_the_shells_account(self) -> None:
        ask = Script()
        _, a = ask_answers("/home/u/projects/zebra/z", self.roots, {"HOME": "/home/u", "CLAUDE_CONFIG_DIR": "/home/u/.claude-zebra"},
                           "/home/u", ask, has_stop_hook=True)
        self.assertEqual(a.env, {"CLAUDE_CONFIG_DIR": "~/.claude-zebra"})


class InitWithFlagsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg = root / "cfg"
        self.env = {"HERDR_ORCH_CONFIG": str(self.cfg), "HERDR_ORCH_STATE": str(root / "state"), "HOME": "/home/u"}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def init(self, *flags: str) -> int:
        with redirect_stdout(io.StringIO()):
            return main(["init", "personal", *flags], self.env)

    def test_every_answer_is_written_and_read_back(self) -> None:
        self.assertEqual(self.init("--match", "~/projects/personal/*", "--model", "sonnet",
                                   "--account", "CLAUDE_CONFIG_DIR=~/.claude-x", "--notify", "stop-hook",
                                   "--yours-alone", "Merging pull requests", "--yours-alone", "Spending money"), 0)
        t = load_topic(self.cfg / "topics/personal")
        self.assertEqual(t.match, ["~/projects/personal/*"])
        self.assertEqual(t.model, "sonnet")
        self.assertEqual(t.env, {"CLAUDE_CONFIG_DIR": "~/.claude-x"})
        self.assertEqual(t.notify, "stop-hook")
        self.assertIsNone(t.base)
        self.assertTrue(t.has_yours_alone())
        playbook = t.playbook_path.read_text()
        self.assertIn("- Merging pull requests\n- Spending money\n", playbook)
        self.assertNotIn("Production: deploys", playbook)

    def test_an_empty_checklist_empties_the_section_and_keeps_what_follows(self) -> None:
        self.init("--match", "~/x/*")
        t = load_topic(self.cfg / "topics/personal")
        t.playbook_path.write_text(t.playbook_path.read_text() + "\n## Notes\n\nKeep me.\n")
        from herdr_orch.topics import _with_yours_alone
        out = _with_yours_alone(t.playbook_path.read_text(), [])
        self.assertIn("## Yours alone", out)
        self.assertNotIn("- Merging", out)
        self.assertIn("## Notes\n\nKeep me.", out)

    def test_a_secret_looking_env_is_refused(self) -> None:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["init", "personal", "--account", "GITHUB_TOKEN=abc"], self.env), 2)
        self.assertFalse((self.cfg / "topics/personal").exists())

    def test_a_custom_notify_command_is_kept_as_a_command(self) -> None:
        self.init("--notify", "ntfy-send --topic me")
        t = load_topic(self.cfg / "topics/personal")
        self.assertEqual(t.notify_command, ["ntfy-send", "--topic", "me"])

    def test_no_flags_and_no_terminal_still_scaffolds_the_template(self) -> None:
        self.init()
        t = load_topic(self.cfg / "topics/personal")
        self.assertEqual((t.match, t.notify), ([], "none"))
        self.assertTrue(t.has_yours_alone())


class StartWithoutTopicTest(unittest.TestCase):
    def test_the_start_action_opens_the_setup_popup_for_an_unknown_repo(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            env = {"HERDR_ORCH_CONFIG": f"{d}/cfg", "HERDR_ORCH_STATE": f"{d}/state", "HOME": "/home/u",
                   "HERDR_PLUGIN_CONTEXT_JSON": '{"workspace_id": "w4"}'}
            herdr = FakeHerdr()
            herdr.workspace_list = [Workspace("w4", "x", "/home/u/projects/personal/x")]
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["start"], env, adapter=herdr), 0)  # tests run without a terminal, like an action
            self.assertEqual(herdr.of("open_setup"), [("open_setup", "/home/u/projects/personal/x")])


if __name__ == "__main__":
    unittest.main()
