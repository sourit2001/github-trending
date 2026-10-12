from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from github_trending_feishu.__main__ import (
    TrendingRepo,
    build_claude_prompt,
    build_report,
    enrich_repo,
    enrich_repos_with_claude,
    prepare_readme_for_summary,
    repo_to_snapshot,
    request_claude_summaries,
)


def sample_repo(**overrides: object) -> TrendingRepo:
    values: dict[str, object] = {
        "rank": 1,
        "owner": "example",
        "name": "tool",
        "url": "https://github.com/example/tool",
        "description": "A self-hosted developer tool.",
        "language": "Python",
        "stars": "1,234",
        "forks": "100",
        "stars_today": "50 stars today",
    }
    values.update(overrides)
    return TrendingRepo(**values)  # type: ignore[arg-type]


class ReadmePreparationTests(unittest.TestCase):
    def test_keeps_content_sections_and_skips_setup_noise(self) -> None:
        readme = """<!-- hidden -->
![logo](logo.png)
# Tool
A tool that turns text into a searchable index.

## Installation
Run pip install tool and configure an API key.

## Features
- Parses Markdown and PDF files
- Builds a local search index

## How it works
Documents are parsed, chunked, indexed, and queried locally.

## License
MIT
"""
        result = prepare_readme_for_summary(readme, limit=500)

        self.assertNotIn("hidden", result)
        self.assertNotIn("logo.png", result)
        self.assertNotIn("pip install", result)
        self.assertNotIn("## License", result)
        self.assertIn("turns text into a searchable index", result)
        self.assertIn("## Features", result)
        self.assertIn("parsed, chunked, indexed", result)

    def test_keeps_nonstandard_product_sections(self) -> None:
        readme = """# Archify
Turn a codebase into an interactive system map.
## Why Archify
- Typed JSON IR
## Choose the right diagram
- Architecture and sequence
## Installation
Run the installer.
"""
        result = prepare_readme_for_summary(readme, limit=1200)
        self.assertIn("Why Archify", result)
        self.assertIn("Choose the right diagram", result)
        self.assertNotIn("Run the installer", result)


class EnrichmentTests(unittest.TestCase):
    def test_fallback_avoids_filling_unsupported_sections(self) -> None:
        repo = enrich_repo(sample_repo())

        self.assertTrue(repo.zh_description)
        self.assertTrue(repo.problem)
        self.assertTrue(repo.key_features)
        self.assertEqual(repo.use_cases, [])

    def test_prompt_contains_readme_and_structured_schema(self) -> None:
        repo = sample_repo(
            readme_excerpt="# Tool\nTurns documents into a local search index.",
            topics=["developer-tools"],
            license="MIT",
        )
        prompt = build_claude_prompt([repo])

        self.assertIn("local search index", prompt)
        self.assertIn("key_features", prompt)
        self.assertIn("use_cases", prompt)
        self.assertNotIn("getting_started", prompt)
        self.assertNotIn("target_users", prompt)
        self.assertIn("MIT", prompt)

    def test_video_generator_fallback_stays_specific(self) -> None:
        repo = enrich_repo(sample_repo(
            description="利用 AI 大模型和自动化工作流，根据主题或关键词一键生成高清短视频。",
            topics=["ai-video-generator", "text-to-speech", "subtitles", "video-automation"],
        ))

        self.assertIn("短视频", repo.zh_description)
        self.assertIn("短视频", repo.problem)
        self.assertTrue(any("文字转语音" in feature for feature in repo.key_features))
        self.assertTrue(any("YouTube Shorts" in use_case for use_case in repo.use_cases))

    def test_coding_agent_fallback_uses_repository_facts(self) -> None:
        repo = enrich_repo(sample_repo(
            owner="openai",
            name="codex",
            description="Lightweight coding agent that runs in your terminal",
            language="Rust",
        ))

        self.assertIn("本地", repo.zh_description)
        self.assertIn("终端", repo.problem)
        self.assertTrue(any("终端" in feature for feature in repo.key_features))
        self.assertTrue(any("代码库" in use_case for use_case in repo.use_cases))

    def test_readme_fallback_is_specific_for_archify(self) -> None:
        repo = enrich_repo(sample_repo(
            owner="tt-a1i",
            name="archify",
            description="A system map renderer",
            readme_excerpt="# Archify\nTyped JSON IR renders architecture and workflow diagrams.",
        ))
        self.assertIn("Typed JSON IR", repo.zh_description)
        self.assertGreaterEqual(len(repo.key_features), 2)
        self.assertTrue(repo.use_cases)

    def test_readme_fallback_is_specific_for_scientific_skills(self) -> None:
        repo = enrich_repo(sample_repo(
            owner="K-Dense-AI",
            name="scientific-agent-skills",
            description="Scientific Agent Skills",
            readme_excerpt="# Scientific Agent Skills\n163 ready-to-use scientific skills and 100+ databases.",
        ))
        self.assertIn("163", repo.zh_description)
        self.assertTrue(any("数据库" in feature for feature in repo.key_features))
        self.assertTrue(repo.use_cases)

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
    @patch("github_trending_feishu.__main__.request_claude_summaries")
    def test_claude_fields_are_applied(self, request_summaries: object) -> None:
        request_summaries.return_value = {  # type: ignore[attr-defined]
            "example/tool": {
                "zh_description": "一个具体工具。",
                "problem": "解决具体问题。",
                "key_features": ["能力一", "能力二"],
                "use_cases": ["场景一", "场景二"],
            }
        }
        repo = sample_repo()

        enrich_repos_with_claude([repo])

        self.assertEqual(repo.problem, "解决具体问题。")
        self.assertEqual(repo.key_features, ["能力一", "能力二"])
        self.assertEqual(repo.use_cases, ["场景一", "场景二"])

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
    @patch("github_trending_feishu.__main__.request_claude_summaries", side_effect=TimeoutError("read timed out"))
    def test_claude_timeout_falls_back_without_raising(self, request_summaries: object) -> None:
        repo = sample_repo()

        enrich_repos_with_claude([repo])
        enriched = enrich_repo(repo)

        self.assertIs(enriched, repo)
        self.assertTrue(repo.zh_description)
        self.assertTrue(repo.key_features)

    def test_snapshot_excludes_full_readme_input(self) -> None:
        snapshot = repo_to_snapshot(sample_repo(readme_excerpt="large README"))

        self.assertNotIn("readme_excerpt", snapshot)


class ClaudeApiTests(unittest.TestCase):
    @patch.dict("os.environ", {"ANTHROPIC_TIMEOUT": "45"})
    @patch("github_trending_feishu.__main__.urlopen")
    def test_messages_request_and_text_block_parsing(self, urlopen_mock: MagicMock) -> None:
        summary = {"full_name": "example/tool", "zh_description": "具体工具",
                   "problem": "具体问题", "key_features": ["能力一", "能力二"],
                   "use_cases": ["场景一"]}
        payload = {"stop_reason": "end_turn", "content": [
            {"type": "thinking", "thinking": "ignored"},
            {"type": "text", "text": json.dumps({"repos": [summary]})}]}
        urlopen_mock.return_value.__enter__.return_value.read.return_value = json.dumps(payload).encode()
        result = request_claude_summaries([sample_repo()], api_key="test-key", model="claude-haiku-5-5")
        request = urlopen_mock.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(request.full_url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(request.get_header("X-api-key"), "test-key")
        self.assertEqual(request.get_header("Anthropic-version"), "2023-06-01")
        self.assertEqual(body["model"], "claude-haiku-5-5")
        self.assertTrue(body["system"])
        self.assertEqual(body["messages"][0]["role"], "user")
        self.assertNotIn("temperature", body)
        self.assertNotIn("response_format", body)
        self.assertEqual(urlopen_mock.call_args.kwargs["timeout"], 45)
        self.assertEqual(result["example/tool"]["key_features"], ["能力一", "能力二"])

    @patch("github_trending_feishu.__main__.urlopen")
    def test_unusable_responses_raise_for_fallback(self, urlopen_mock: MagicMock) -> None:
        for payload in (
            {"stop_reason": "max_tokens", "content": [{"type": "text", "text": "{}"}]},
            {"stop_reason": "refusal", "content": []},
            {"stop_reason": "end_turn", "content": []},
            {"stop_reason": "end_turn", "content": [{"type": "text", "text": "invalid"}]},
            {"stop_reason": "end_turn", "content": [{"type": "text", "text": "null"}]},
        ):
            with self.subTest(payload=payload):
                urlopen_mock.return_value.__enter__.return_value.read.return_value = json.dumps(payload).encode()
                with self.assertRaises(RuntimeError):
                    request_claude_summaries([sample_repo()], api_key="test-key", model="claude-haiku-5-5")

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": ""})
    @patch("github_trending_feishu.__main__.request_claude_summaries")
    def test_no_key_skips_api(self, request_mock: MagicMock) -> None:
        enrich_repos_with_claude([sample_repo()])
        request_mock.assert_not_called()


class RenderingTests(unittest.TestCase):
    def test_markdown_report_uses_explainer_sections(self) -> None:
        repo = enrich_repo(sample_repo())
        report = build_report(
            [repo],
            "GitHub Trending Daily",
            datetime(2026, 8, 17, tzinfo=timezone.utc),
        )

        self.assertIn("**项目简介：**", report)
        self.assertIn("**解决的问题：**", report)
        self.assertIn("**主要功能：**", report)
        self.assertIn("**使用场景：**", report)
        self.assertNotIn("**适合谁：**", report)
        self.assertNotIn("**如何开始：**", report)
        self.assertNotIn("**阅读提示：**", report)
        self.assertNotIn("| Rank |", report)


if __name__ == "__main__":
    unittest.main()
