import tempfile
import unittest
from pathlib import Path

from herdr_orch.topics import config_root, init_topic, load_topics, topic_for


class TopicsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_topic(self, name: str, toml: str) -> None:
        d = self.root / "topics" / name
        d.mkdir(parents=True)
        (d / "topic.toml").write_text(toml)

    def test_config_root_resolution(self) -> None:
        self.assertEqual(config_root({"HERDR_ORCH_CONFIG": "/a", "HERDR_PLUGIN_CONFIG_DIR": "/b"}), Path("/a"))
        self.assertEqual(config_root({"HERDR_PLUGIN_CONFIG_DIR": "/b"}), Path("/b"))
        self.assertEqual(
            config_root({"HOME": "/h"}),
            Path("/h/.config/herdr/plugins/config/jhonsanchez.orchestrator"),
        )

    def test_empty_topic_toml_runs_on_defaults(self) -> None:
        self.write_topic("waki", "")
        [t] = load_topics(self.root)
        self.assertEqual(t.name, "waki")
        self.assertEqual(t.match, [])
        self.assertEqual(t.env, {})
        self.assertIsNone(t.model)
        self.assertIsNone(t.base)
        self.assertEqual(t.notify_command, [])
        self.assertEqual(t.heartbeat_window_min, 15)
        self.assertEqual(t.brief_path, self.root / "topics/waki/brief.md")
        self.assertEqual(t.watches_dir, self.root / "topics/waki/watches")

    def test_agent_table_sets_kind_and_args_and_defaults_to_claude(self) -> None:
        self.write_topic("a", "")
        self.write_topic("b", '[agent]\nkind = "pi"\nargs = ["--fast"]\n')
        a, b = load_topics(self.root)
        self.assertEqual((a.agent_kind, a.agent_args), ("claude", []))
        self.assertEqual((b.agent_kind, b.agent_args), ("pi", ["--fast"]))

    def test_model_with_a_kind_that_takes_no_model_is_refused_at_load(self) -> None:
        self.write_topic("a", 'model = "sonnet"\n[agent]\nkind = "pi"\n')
        with self.assertRaisesRegex(ValueError, "pi.*takes no --model"):
            load_topics(self.root)

    def test_model_is_accepted_for_codex(self) -> None:
        self.write_topic("a", 'model = "gpt-5"\n[agent]\nkind = "codex"\n')
        [t] = load_topics(self.root)
        self.assertEqual((t.agent_kind, t.model), ("codex", "gpt-5"))

    def test_unknown_agent_key_is_refused(self) -> None:
        self.write_topic("a", '[agent]\nknd = "pi"\n')
        with self.assertRaisesRegex(ValueError, "unknown \\[agent\\] key"):
            load_topics(self.root)

    def test_overrides_are_read(self) -> None:
        self.write_topic(
            "zebra",
            'match = ["~/projects/zebra/*"]\nmodel = "sonnet"\nbase = "origin/develop"\n'
            'notify_command = ["ntfy-send", "-t"]\nheartbeat_window_min = 5\n'
            '[env]\nCLAUDE_CONFIG_DIR = "~/.claude-zebra"\n',
        )
        [t] = load_topics(self.root)
        self.assertEqual(t.match, ["~/projects/zebra/*"])
        self.assertEqual(t.model, "sonnet")
        self.assertEqual(t.base, "origin/develop")
        self.assertEqual(t.notify_command, ["ntfy-send", "-t"])
        self.assertEqual(t.heartbeat_window_min, 5)
        self.assertEqual(t.env, {"CLAUDE_CONFIG_DIR": "~/.claude-zebra"})

    def test_membership_by_repo_root_pattern(self) -> None:
        self.write_topic("waki", 'match = ["~/projects/waki/*"]')
        self.write_topic("zebra", 'match = ["/srv/zebra/*"]')
        topics = load_topics(self.root)
        t = topic_for("/home/u/projects/waki/waki-platform", topics, home="/home/u")
        self.assertEqual(t.name if t else None, "waki")
        t = topic_for("/srv/zebra/zpc", topics, home="/home/u")
        self.assertEqual(t.name if t else None, "zebra")
        self.assertIsNone(topic_for("/home/u/projects/personal/x", topics, home="/home/u"))

    def test_init_scaffolds_a_topic_with_yours_alone_section(self) -> None:
        path = init_topic(self.root, "waki")
        self.assertTrue((path / "topic.toml").is_file())
        self.assertIn("## Yours alone", (path / "playbook.md").read_text())
        self.assertTrue((path / "brief.md").is_file())
        self.assertTrue((path / "watches").is_dir())
        [t] = load_topics(self.root)
        self.assertEqual(t.name, "waki")

    def test_init_refuses_an_existing_topic_and_bad_names(self) -> None:
        init_topic(self.root, "waki")
        with self.assertRaises(FileExistsError):
            init_topic(self.root, "waki")
        with self.assertRaises(ValueError):
            init_topic(self.root, "Bad Name")


if __name__ == "__main__":
    unittest.main()
