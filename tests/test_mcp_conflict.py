import importlib.util
import json
from pathlib import Path
import unittest

path = Path(__file__).resolve().parents[1] / "plugins/hirey-hi/skills/hi-onboard/scripts/check_mcp_conflict.py"
spec = importlib.util.spec_from_file_location("conflict", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
PLUGIN = {"mcpServers": {"hi": {"url": "https://mcp.hirey.ai/mcp",
          "http_headers": {"x-hirey-plugin-host": "codex", "x-hirey-plugin-version": "0.2.13"}}}}


class ConflictTests(unittest.TestCase):
    def test_codex_manifest_prompt_budget_and_agentic_media_entry(self):
        manifest_path = Path(__file__).resolve().parents[1] / "plugins/hirey-hi/.codex-plugin/plugin.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        prompts = manifest["interface"]["defaultPrompt"]
        self.assertLessEqual(len(prompts), 3)
        self.assertTrue(all(len(prompt) <= 128 for prompt in prompts))
        self.assertTrue(any("Agentic Media" in prompt for prompt in prompts))

    def test_agentic_media_upgrade_requests_minimum_workflow_scope_union(self):
        root = Path(__file__).resolve().parents[1] / "plugins/hirey-hi/skills"
        media_skill = (root / "agentic-media/SKILL.md").read_text(encoding="utf-8")
        onboard_skill = (root / "hi-onboard/SKILL.md").read_text(encoding="utf-8")
        for scope in (
            "hirey.f01.identity.me",
            "hirey.f10.agentic_media.work.create",
            "hirey.f10.agentic_media.upload.describe",
            "hirey.f10.agentic_media.upload.complete",
            "hirey.f10.agentic_media.work.status",
        ):
            self.assertIn(scope, media_skill)
        self.assertIn("not only the\nsingle scope", media_skill)
        self.assertIn("exact union required by the active Skill/workflow", onboard_skill)
        self.assertIn("do not request the full catalog", onboard_skill)

    def test_plugin_only(self):
        self.assertEqual(module.inspect({}, PLUGIN)["status"], "plugin_only")

    def test_exact_legacy_url_only(self):
        result = module.inspect({"mcp_servers": {"hi": {"url": "https://mcp.hirey.ai/mcp"}}}, PLUGIN)
        self.assertEqual(result["status"], "legacy_url_only_override")
        self.assertFalse(result["logout_required"])

    def test_customizations_are_not_removed_or_disclosed(self):
        for extra in [{"http_headers": {"Authorization": "private-secret"}},
                      {"enabled": False}, {"tool_timeout_sec": 80},
                      {"disabled_tools": ["workspace_workflows"]}]:
            manual = {"url": "https://mcp.hirey.ai/mcp", **extra}
            self.assertEqual(module.inspect({"mcp_servers": {"hi": manual}}, PLUGIN),
                             {"status": "review_required"})

    def test_custom_endpoint_is_preserved(self):
        self.assertEqual(module.inspect({"mcp_servers": {"hi": {"url": "https://custom.example"}}}, PLUGIN),
                         {"status": "review_required"})

    def test_incomplete_plugin_cannot_replace_manual(self):
        config = {"mcp_servers": {"hi": {"url": "https://mcp.hirey.ai/mcp"}}}
        self.assertEqual(module.inspect(config, {})["status"], "review_required")

    def test_unrelated_servers_ignored(self):
        self.assertEqual(module.inspect({"mcp_servers": {"other": {"token": "private-secret"}}}, PLUGIN),
                         {"status": "plugin_only"})


if __name__ == "__main__":
    unittest.main()
