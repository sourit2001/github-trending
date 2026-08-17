from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from github_trending_feishu.__main__ import (
    TrendingRepo,
    build_deepseek_prompt,
    build_report,
    enrich_repo,
    enrich_repos_with_deepseek,
    prepare_readme_for_summary,
    repo_to_snapshot,
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
    def test_removes_visual_noise_and_truncates(self) -> None:
        readme = "<!-- hidden -->\n![logo](logo.png)\n# Tool\n" + ("feature details\n" * 50)
        result = prepare_readme_for_summary(readme, limit=120)

        self.assertNotIn("hidden", result)
        self.assertNotIn("logo.png", result)
        self.assertIn("# Tool", result)
        self.assertTrue(result.endswith("[README 已截断]"))


class EnrichmentTests(unittest.TestCase):
    def test_fallback_populates_all_reader_facing_fields(self) -> None:
        repo = enrich_repo(sample_repo())

        self.assertTrue(repo.zh_description)
        self.assertTrue(repo.problem)
        self.assertTrue(repo.key_features)
        self.assertTrue(repo.target_users)
        self.assertTrue(repo.use_case)
        self.assertTrue(repo.getting_started)
        self.assertTrue(repo.cautions)

    def test_prompt_contains_readme_and_structured_schema(self) -> None:
        repo = sample_repo(
            readme_excerpt="# Tool\nInstall with pip.",
            topics=["developer-tools"],
            license="MIT",
        )
        prompt = build_deepseek_prompt([repo])

        self.assertIn("Install with pip", prompt)
        self.assertIn("key_features", prompt)
        self.assertIn("getting_started", prompt)
        self.assertIn("MIT", prompt)

    def test_video_generator_fallback_stays_specific(self) -> None:
        repo = enrich_repo(sample_repo(
            description="利用 AI 大模型和自动化工作流，根据主题或关键词一键生成高清短视频。",
            topics=["ai-video-generator", "text-to-speech", "subtitles", "video-automation"],
        ))

        self.assertIn("短视频", repo.zh_description)
        self.assertIn("短视频", repo.problem)
        self.assertTrue(any("文字转语音" in feature for feature in repo.key_features))
        self.assertIn("短视频创作者", repo.target_users)

    @patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-key"})
    @patch("github_trending_feishu.__main__.request_deepseek_summaries")
    def test_deepseek_fields_are_applied(self, request_summaries: object) -> None:
        request_summaries.return_value = {  # type: ignore[attr-defined]
            "example/tool": {
                "zh_description": "一个具体工具。",
                "problem": "解决具体问题。",
                "key_features": ["能力一", "能力二"],
                "target_users": "目标用户。",
                "use_case": "具体场景。",
                "getting_started": "按照 README 安装。",
                "cautions": "仍需评估成熟度。",
            }
        }
        repo = sample_repo()

        enrich_repos_with_deepseek([repo])

        self.assertEqual(repo.problem, "解决具体问题。")
        self.assertEqual(repo.key_features, ["能力一", "能力二"])
        self.assertEqual(repo.getting_started, "按照 README 安装。")

    def test_snapshot_excludes_full_readme_input(self) -> None:
        snapshot = repo_to_snapshot(sample_repo(readme_excerpt="large README"))

        self.assertNotIn("readme_excerpt", snapshot)


class RenderingTests(unittest.TestCase):
    def test_markdown_report_uses_explainer_sections(self) -> None:
        repo = enrich_repo(sample_repo())
        report = build_report(
            [repo],
            "GitHub Trending Daily",
            datetime(2026, 8, 17, tzinfo=timezone.utc),
        )

        self.assertIn("**它是什么：**", report)
        self.assertIn("**解决的问题：**", report)
        self.assertIn("**核心能力：**", report)
        self.assertIn("**如何开始：**", report)
        self.assertNotIn("| Rank |", report)


if __name__ == "__main__":
    unittest.main()
