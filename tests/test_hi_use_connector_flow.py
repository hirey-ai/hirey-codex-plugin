import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class HiUseConnectorFlowTest(unittest.TestCase):
    def test_skill_uses_target_specific_executable_route(self):
        skill = (ROOT / "plugins/hirey-hi/skills/hi-use/SKILL.md").read_text()
        for required in (
            "people.find_private",
            "contact.policy",
            "reach.connector_candidates",
            "reach.route.plan",
            "reach.route.get",
            "reach.route.decide",
            "never infer a Connector from `relationship.list`",
            "do not fabricate a Connector",
            "source_scope: reachable_private",
            "do not say they are in the user's own private network",
            "never print the raw `[]`",
            "ask for another spelling",
        ):
            self.assertIn(required, skill)

    def test_manifest_and_status_calls_share_version(self):
        manifest = (ROOT / "plugins/hirey-hi/.codex-plugin/plugin.json").read_text()
        self.assertIn('"version": "0.2.13"', manifest)
        for path in (
            "plugins/hirey-hi/skills/hi-use/SKILL.md",
            "plugins/hirey-hi/skills/hi-onboard/SKILL.md",
            "plugins/hirey-hi/skills/agentic-media/SKILL.md",
            "plugins/hirey-hi/README.md",
        ):
            self.assertIn("0.2.13", (ROOT / path).read_text())


if __name__ == "__main__":
    unittest.main()
