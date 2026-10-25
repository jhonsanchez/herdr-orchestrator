import json
import tempfile
import unittest
from pathlib import Path

from herdr_orch.hooks import DENY_REASON, handle
from herdr_orch.state import TopicState

ASK = {
    "hook_event_name": "PreToolUse",
    "tool_name": "AskUserQuestion",
    "tool_input": {"questions": [
        {"question": "Which storage backend?", "header": "Storage", "multiSelect": False,
         "options": [{"label": "SQLite (Recommended)", "description": "one file"},
                     {"label": "Postgres", "description": "a server"}]},
        {"question": "Add tags?", "header": "Tags", "multiSelect": False,
         "options": [{"label": "Later (Recommended)", "description": ""}, {"label": "Now", "description": ""}]},
    ]},
}
PERMISSION = {"hook_event_name": "PermissionRequest", "tool_name": "Bash", "tool_input": {"command": "rm -rf build"}}


def stop(message: str, active: bool = False) -> dict[str, object]:
    return {"hook_event_name": "Stop", "last_assistant_message": message, "stop_hook_active": active}


class HooksTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = {"HERDR_ORCH_STATE": str(self.root), "HERDR_PANE_ID": "w1:p2"}
        self.state = TopicState(self.root, "waki")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def items(self) -> list[dict[str, object]]:
        if not self.state.inbox.exists():
            return []
        return [json.loads(line) for line in self.state.inbox.read_text().splitlines()]

    def test_an_unregistered_pane_is_untouched_by_every_hook(self) -> None:
        for payload in (ASK, PERMISSION, stop("Shall I continue?")):
            self.assertIsNone(handle(payload, self.env, now=1.0))
        self.assertIsNone(handle(ASK, {"HERDR_ORCH_STATE": str(self.root)}, now=1.0))  # outside herdr
        self.assertEqual(self.items(), [])

    def test_a_managed_agents_question_becomes_inbox_items_and_a_deny(self) -> None:
        self.state.take("w1:p2", "waki-12")
        out = handle(ASK, self.env, now=1000.0)
        assert out is not None
        decision = out["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertEqual(decision["permissionDecisionReason"], DENY_REASON)
        first, second = self.items()
        self.assertEqual(first["v"], 1)
        self.assertEqual((first["topic"], first["agent"], first["pane"], first["kind"]), ("waki", "waki-12", "w1:p2", "decision"))
        self.assertEqual(first["question"], "Which storage backend?")
        self.assertEqual(first["options"], ["SQLite (Recommended)", "Postgres"])
        self.assertEqual((first["status"], first["answer"], first["guess"]), ("open", None, False))
        self.assertEqual(first["created_at"], 1000.0)
        self.assertEqual(second["question"], "Add tags?")
        self.assertNotEqual(first["id"], second["id"])

    def test_a_permission_prompt_is_recorded_and_left_to_wait(self) -> None:
        self.state.take("w1:p2", "waki-12")
        self.assertIsNone(handle(PERMISSION, self.env, now=5.0))
        [item] = self.items()
        self.assertEqual(item["kind"], "permission")
        self.assertEqual(item["question"], "Allow Bash: rm -rf build")

    def test_a_turn_ending_in_a_question_is_recorded_as_a_guess(self) -> None:
        self.state.take("w1:p2", "waki-12")
        self.assertIsNone(handle(stop("I wrote the plan. Should I start on step 1?"), self.env, now=5.0))
        self.assertIsNone(handle(stop("Done. Tests pass."), self.env, now=6.0))
        self.assertIsNone(handle(stop("Retrying?", active=True), self.env, now=7.0))
        [item] = self.items()
        self.assertEqual((item["kind"], item["guess"]), ("question", True))
        self.assertEqual(item["question"], "I wrote the plan. Should I start on step 1?")

    def test_a_long_closing_message_keeps_only_its_last_paragraph(self) -> None:
        self.state.take("w1:p2", "waki-12")
        handle(stop("Lots of context.\n\nMore context.\n\nWhich option do you prefer?  "), self.env, now=5.0)
        [item] = self.items()
        self.assertEqual(item["question"], "Which option do you prefer?")


if __name__ == "__main__":
    unittest.main()
