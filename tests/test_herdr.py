import subprocess
import unittest
from unittest import mock

from herdr_orch.herdr import CliHerdr
from herdr_orch.state import PLUGIN_ID


class CliHerdrAttentionOpenTest(unittest.TestCase):
    def test_splits_beside_the_target_pane_without_naming_the_workspace(self) -> None:
        done = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch("herdr_orch.herdr.subprocess.run", return_value=done) as run:
            CliHerdr("herdr").attention_open("w1X", "w1X:p1")
        argv = run.call_args.args[0]
        # herdr 0.9.5 rejects --workspace beside --target-pane for a split (invalid_params).
        self.assertNotIn("--workspace", argv)
        self.assertEqual(argv, [
            "herdr", "plugin", "pane", "open", "--plugin", PLUGIN_ID, "--entrypoint", "attention",
            "--placement", "split", "--direction", "right", "--target-pane", "w1X:p1", "--no-focus",
        ])

    def test_a_topic_scopes_the_pane_through_its_env(self) -> None:
        done = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch("herdr_orch.herdr.subprocess.run", return_value=done) as run:
            CliHerdr("herdr").attention_open("w1X", "w1X:p1", "waki")
        self.assertEqual(run.call_args.args[0][-2:], ["--env", "HERDR_ORCH_ATTENTION_TOPIC=waki"])


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


if __name__ == "__main__":
    unittest.main()
