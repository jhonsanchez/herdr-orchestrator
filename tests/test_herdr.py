import subprocess
import unittest
from unittest import mock

from herdr_orch.herdr import AgentBlocked, AgentBusy, CliHerdr
from herdr_orch.state import PLUGIN_ID


class CliHerdrAttentionOpenTest(unittest.TestCase):
    def test_splits_beside_the_target_pane_without_naming_the_workspace(self) -> None:
        done = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch("herdr_orch.herdr.subprocess.run", return_value=done) as run:
            CliHerdr("herdr").attention_open("w1X", "w1X:p1")
        argv = run.call_args_list[0].args[0]
        # herdr 0.9.5 rejects --workspace beside --target-pane for a split (invalid_params).
        self.assertNotIn("--workspace", argv)
        self.assertEqual(argv, [
            "herdr", "plugin", "pane", "open", "--plugin", PLUGIN_ID, "--entrypoint", "attention",
            "--placement", "split", "--direction", "right", "--target-pane", "w1X:p1", "--no-focus",
        ])

    def test_shrinks_the_new_pane_to_a_narrow_column(self) -> None:
        opened = '{"result":{"plugin_pane":{"pane":{"pane_id":"w1X:p3"}}}}'
        done = subprocess.CompletedProcess([], 0, stdout=opened, stderr="")
        with mock.patch("herdr_orch.herdr.subprocess.run", return_value=done) as run:
            CliHerdr("herdr").attention_open("w1X", "w1X:p1")
        self.assertEqual(run.call_args.args[0], [
            "herdr", "pane", "resize", "--pane", "w1X:p3", "--direction", "right", "--amount", "0.150",
        ])

    def test_the_width_is_overridable_from_the_environment(self) -> None:
        opened = '{"result":{"plugin_pane":{"pane":{"pane_id":"w1X:p3"}}}}'
        done = subprocess.CompletedProcess([], 0, stdout=opened, stderr="")
        with mock.patch("herdr_orch.herdr.subprocess.run", return_value=done) as run, \
                mock.patch.dict("os.environ", {"HERDR_ORCH_ATTENTION_WIDTH": "0.25"}):
            CliHerdr("herdr").attention_open("w1X", "w1X:p1")
        self.assertEqual(run.call_args.args[0][-1], "0.250")

    def test_a_topic_scopes_the_pane_through_its_env(self) -> None:
        done = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch("herdr_orch.herdr.subprocess.run", return_value=done) as run:
            CliHerdr("herdr").attention_open("w1X", "w1X:p1", "waki")
        self.assertEqual(run.call_args_list[0].args[0][-2:], ["--env", "HERDR_ORCH_ATTENTION_TOPIC=waki"])


GONE = '{"error":{"code":"workspace_not_found","message":"workspace w23 not found"}}'


class CliHerdrWorktreeRemoveTest(unittest.TestCase):
    def test_a_workspace_herdr_no_longer_has_reports_false(self) -> None:
        err = subprocess.CalledProcessError(1, ["herdr"], output=GONE, stderr="")
        with mock.patch("herdr_orch.herdr.subprocess.run", side_effect=err):
            self.assertFalse(CliHerdr("herdr").worktree_remove("w23"))

    def test_the_error_json_on_stderr_counts_too(self) -> None:
        err = subprocess.CalledProcessError(1, ["herdr"], output="", stderr=GONE)
        with mock.patch("herdr_orch.herdr.subprocess.run", side_effect=err):
            self.assertFalse(CliHerdr("herdr").worktree_remove("w23"))

    def test_any_other_failure_still_raises(self) -> None:
        err = subprocess.CalledProcessError(1, ["herdr"], output='{"error":{"code":"internal"}}', stderr="")
        with mock.patch("herdr_orch.herdr.subprocess.run", side_effect=err):
            with self.assertRaises(subprocess.CalledProcessError):
                CliHerdr("herdr").worktree_remove("w23")

    def test_a_removed_workspace_reports_true(self) -> None:
        done = subprocess.CompletedProcess([], 0, stdout="{}", stderr="")
        with mock.patch("herdr_orch.herdr.subprocess.run", return_value=done):
            self.assertTrue(CliHerdr("herdr").worktree_remove("w23"))


BLOCKED = '{"error":{"code":"agent_blocked","message":"agent issue-1 is blocked"}}'


class CliHerdrPromptBlockedTest(unittest.TestCase):
    def setUp(self) -> None:
        screen = mock.patch.object(CliHerdr, "_styled_screen_of", return_value="❯\n")  # an empty input box
        screen.start()
        self.addCleanup(screen.stop)

    def test_text_in_the_input_box_is_never_typed_over(self) -> None:
        with mock.patch.object(CliHerdr, "_styled_screen_of", return_value="❯ regarding waki session,\n"), \
                mock.patch.object(CliHerdr, "_status_of", return_value="idle"):
            with mock.patch("herdr_orch.herdr.subprocess.run") as run:
                with self.assertRaises(AgentBusy):
                    CliHerdr("herdr").prompt("orch-personal", "hello")
        run.assert_not_called()

    def test_a_dialogs_selected_option_is_not_the_persons_text(self) -> None:
        dialog = "Do you trust this folder?\n ❯ No, exit\n   Yes, I trust this folder\n"
        err = subprocess.CalledProcessError(1, ["herdr"], output=BLOCKED, stderr="")
        with mock.patch.object(CliHerdr, "_styled_screen_of", return_value=dialog), \
                mock.patch.object(CliHerdr, "_status_of", return_value="blocked"), \
                mock.patch("herdr_orch.herdr.subprocess.run", side_effect=err):
            with self.assertRaises(AgentBlocked) as caught:
                CliHerdr("herdr").prompt("issue-1", "hello")
        self.assertNotIsInstance(caught.exception, AgentBusy)

    def test_herdr_refusing_a_blocked_agent_raises_agent_blocked(self) -> None:
        err = subprocess.CalledProcessError(1, ["herdr"], output=BLOCKED, stderr="")
        with mock.patch("herdr_orch.herdr.subprocess.run", side_effect=err):
            with self.assertRaises(AgentBlocked):
                CliHerdr("herdr").prompt("issue-1", "hello")

    def test_any_other_prompt_failure_still_raises_as_is(self) -> None:
        err = subprocess.CalledProcessError(1, ["herdr"], output='{"error":{"code":"internal"}}', stderr="")
        with mock.patch("herdr_orch.herdr.subprocess.run", side_effect=err):
            with self.assertRaises(subprocess.CalledProcessError):
                CliHerdr("herdr").prompt("issue-1", "hello")


if __name__ == "__main__":
    unittest.main()
