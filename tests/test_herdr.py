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


if __name__ == "__main__":
    unittest.main()
