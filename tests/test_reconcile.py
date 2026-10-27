import json
import tempfile
import unittest
from pathlib import Path

from herdr_orch.herdr import Agent, Workspace
from herdr_orch.reconcile import Reconciled, reconcile
from herdr_orch.state import TopicState
from herdr_orch.topics import Topic
from tests.fakes import FakeHerdr

REPO = "/home/u/projects/personal/x"


class ReconcileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.topic = Topic(name="personal", dir=root / "cfg", match=["/home/u/projects/personal/*"])
        self.state = TopicState(root, "personal")
        self.herdr = FakeHerdr()
        self.branches = {"/wt/issue-22": "fix-main-followups", "/wt/issue-26": "issue-26"}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_it(self) -> Reconciled:
        return reconcile(self.topic, self.state, self.herdr, 100.0, branch_of=self.branches.get, home="/home/u")

    def seat(self, ws: str, name: str, path: str) -> None:
        self.herdr.workspace_list.append(Workspace(ws, name, REPO, path))
        self.herdr.agent_list.append(Agent(name, "idle", f"{ws}:p1", ws))
        self.state.take(f"{ws}:p1", name)

    def test_a_worktree_without_a_record_gets_one_from_its_checked_out_branch(self) -> None:
        self.seat("w2", "issue-22", "/wt/issue-22")
        result = self.run_it()
        self.assertEqual(result.adopted, ["fix-main-followups"])
        record = self.state.branch_record("fix-main-followups")
        assert record is not None
        self.assertEqual((record["agent"], record["repo_root"], record["workspace_id"]), ("issue-22", REPO, "w2"))

    def test_a_second_branch_in_a_tracked_workspace_is_flagged_not_adopted(self) -> None:
        # #57: the issue-31 agent worked issue-45 in issue-31's worktree.
        self.seat("w23", "issue-31", "/wt/issue-31")
        self.state.track_branch("issue-31", "issue-31", REPO, "w23", 5.0)
        self.branches["/wt/issue-31"] = "issue-45"
        for _ in range(2):  # a repeat pass raises nothing new
            result = self.run_it()
            self.assertEqual((result.adopted, result.switched), ([], ["issue-45"]))
        self.assertIsNone(self.state.branch_record("issue-45"))
        [item] = [i for i in self.state.inbox_lines() if i["status"] == "open"]
        self.assertEqual((item["agent"], item["pane"], item["to"]), ("issue-31", "w23:p1", "orchestrator"))
        self.assertIn("issue-45", str(item["question"]))
        self.assertIn("seat issue-45 separately", str(item["question"]))

    def test_the_switch_flag_is_one_item_per_workspace_and_branch_named_by_label_and_resolved_when_moot(self) -> None:
        # #65: five runner starts left five items reading "None switched to issue-35".
        self.herdr.workspace_list.append(Workspace("w9", "fix-error", REPO, "/wt/issue-35"))
        self.herdr.agent_list.append(Agent(None, "idle", "w9:p1", "w9"))
        self.state.track_branch("fix-error", "other", REPO, "w9", 5.0)
        self.branches["/wt/issue-35"] = "issue-35"
        for _ in range(3):
            self.run_it()
        [item] = [i for i in self.state.inbox_lines() if i["status"] == "open"]
        self.assertEqual(item["id"], "switch-w9-issue-35")
        self.assertIn("fix-error switched to issue-35", str(item["question"]))
        self.herdr.workspace_list.clear()  # workspace released
        self.run_it()
        self.assertEqual([i for i in self.state.inbox_lines() if i["status"] == "open"], [])

    def test_an_existing_record_is_left_alone_and_a_second_pass_changes_nothing(self) -> None:
        self.seat("w3", "issue-26", "/wt/issue-26")
        self.state.track_branch("issue-26", "issue-26", REPO, "w3", 5.0)
        self.state.set_pr("issue-26", 27, "open", "passing", 6.0)
        self.assertEqual(self.run_it().adopted, [])
        record = self.state.branch_record("issue-26")
        assert record is not None
        self.assertEqual(record["pr"], 27)

    def test_the_main_checkout_unmanaged_agents_and_other_topics_are_not_adopted(self) -> None:
        self.herdr.workspace_list.append(Workspace("w1", "x", REPO, REPO))
        self.herdr.workspace_list.append(Workspace("w4", "other", "/home/u/projects/zebra/z", "/wt/issue-22"))
        self.herdr.agent_list.append(Agent("issue-22", "idle", "w4:p1", "w4"))
        self.state.take("w4:p1", "issue-22")
        self.herdr.workspace_list.append(Workspace("w5", "free", REPO, "/wt/issue-26"))
        self.herdr.agent_list.append(Agent("free", "idle", "w5:p1", "w5"))
        self.assertEqual(self.run_it().adopted, [])

    def test_registry_entries_of_gone_agents_are_dropped(self) -> None:
        self.seat("w2", "issue-22", "/wt/issue-22")
        self.state.take("w1P:p1", "issue-10")
        result = self.run_it()
        self.assertEqual(result.dropped, ["issue-10"])
        self.assertEqual(self.state.managed(), {"w2:p1": "issue-22"})

    def test_the_inbox_keeps_each_ids_latest_line_in_first_seen_order(self) -> None:
        self.state.inbox.parent.mkdir(parents=True)
        lines = [{"id": "a", "status": "open"}, {"id": "b", "status": "open"}, {"id": "a", "status": "resolved"}]
        self.state.inbox.write_text("".join(json.dumps(i) + "\n" for i in lines) + "torn{\n")
        self.assertEqual(self.run_it().compacted, 2)
        self.assertEqual([(i["id"], i["status"]) for i in self.state.inbox_lines()], [("a", "resolved"), ("b", "open")])
        self.assertEqual(self.run_it().compacted, 0)
