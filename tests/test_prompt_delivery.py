import unittest

from herdr_orch.herdr import delivery

SNIPPET = "You are the Orchestrator for"
PASTED = """ ▐▛███▛█   Claude Code v2.1.310
────────────────────────────────────────
❯ You are the Orchestrator for topic personal. Read the playbook at /home/u/…
────────────────────────────────────────
  ⏵⏵ auto mode on (shift+tab to cycle)"""
ANSWERED = """❯ You are the Orchestrator for topic personal. Read the playbook at /home/u/…
● I'll read the playbook first.
● Read(playbook.md)
────────────────────────────────────────
❯
────────────────────────────────────────"""
MISSING = """ ▐▛███▛█   Claude Code v2.1.310
────────────────────────────────────────
❯ Try "fix typecheck errors"
────────────────────────────────────────"""


class DeliveryTest(unittest.TestCase):
    def test_text_left_in_the_input_box_is_pasted_not_sent(self) -> None:
        self.assertEqual(delivery(PASTED, SNIPPET), "pasted")

    def test_text_followed_by_the_agents_reply_was_answered(self) -> None:
        self.assertEqual(delivery(ANSWERED, SNIPPET), "answered")

    def test_text_nowhere_on_screen_is_missing(self) -> None:
        self.assertEqual(delivery(MISSING, SNIPPET), "missing")


if __name__ == "__main__":
    unittest.main()
