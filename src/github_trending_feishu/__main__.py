from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from time import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


GITHUB_TRENDING_URL = "https://github.com/trending"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; github-trending-feishu/0.1; "
    "+https://github.com/)"
)


@dataclass
class TrendingRepo:
    rank: int
    owner: str
    name: str
    url: str
    description: str
    language: str
    stars: str
    forks: str
    stars_today: str
    zh_description: str = ""
    use_case: str = ""

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


class TrendingParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.repos: list[TrendingRepo] = []
        self._in_article = False
        self._article_depth = 0
        self._current: dict[str, str] = {}
        self._current_link_href = ""
        self._capture: str | None = None
        self._buffer: list[str] = []
        self._repo_links_seen = 0
        self._social_links_seen = 0
        self._last_data = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {name: value or "" for name, value in attrs}

        if tag == "article" and "Box-row" in attr.get("class", ""):
            self._in_article = True
            self._article_depth = 1
            self._current = {}
            self._repo_links_seen = 0
            self._social_links_seen = 0
            return

        if not self._in_article:
            return

        self._article_depth += 1

        if tag == "a":
            href = attr.get("href", "")
            self._current_link_href = href
            if self._repo_links_seen == 0 and href.count("/") >= 2:
                self._repo_links_seen += 1
                self._capture = "repo"
                self._buffer = []
            elif href.endswith("/stargazers") or href.endswith("/forks"):
                self._capture = "social"
                self._buffer = []
        elif tag == "p" and "col-9" in attr.get("class", ""):
            self._capture = "description"
            self._buffer = []
        elif tag == "span" and attr.get("itemprop") == "programmingLanguage":
            self._capture = "language"
            self._buffer = []

    def handle_endtag(self, tag: str) -> None:
        if not self._in_article:
            return

        if self._capture and (
            (self._capture in {"repo", "social"} and tag == "a")
            or (self._capture == "description" and tag == "p")
            or (self._capture == "language" and tag == "span")
        ):
            text = normalize_text("".join(self._buffer))
            if self._capture == "repo":
                owner, name = parse_repo_name(text)
                self._current["owner"] = owner
                self._current["name"] = name
                self._current["url"] = "https://github.com" + self._current_link_href
            elif self._capture == "description":
                self._current["description"] = text
            elif self._capture == "language":
                self._current["language"] = text
            elif self._capture == "social":
                if self._current_link_href.endswith("/stargazers"):
                    self._current["stars"] = text
                elif self._current_link_href.endswith("/forks"):
                    self._current["forks"] = text
            self._capture = None
            self._buffer = []

        if tag == "article":
            self._finish_article()
            self._in_article = False
            self._article_depth = 0
            return

        self._article_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._in_article:
            return

        self._last_data = data
        if self._capture:
            self._buffer.append(data)
            return

        text = normalize_text(data)
        if "stars today" in text:
            self._current["stars_today"] = text

    def _finish_article(self) -> None:
        if not self._current.get("owner") or not self._current.get("name"):
            return

        self.repos.append(
            TrendingRepo(
                rank=len(self.repos) + 1,
                owner=self._current["owner"],
                name=self._current["name"],
                url=self._current.get("url", ""),
                description=self._current.get("description", ""),
                language=self._current.get("language", "Unknown"),
                stars=self._current.get("stars", ""),
                forks=self._current.get("forks", ""),
                stars_today=self._current.get("stars_today", ""),
            )
        )


def normalize_text(value: str) -> str:
    return " ".join(value.replace("\n", " ").split()).strip()


def parse_repo_name(value: str) -> tuple[str, str]:
    clean = value.replace(" / ", "/").replace(" ", "")
    if "/" not in clean:
        return "", clean
    owner, name = clean.split("/", 1)
    return owner.strip(), name.strip()


def fetch_trending(language: str = "", since: str = "daily") -> list[TrendingRepo]:
    path = f"{GITHUB_TRENDING_URL}/{language.strip()}" if language.strip() else GITHUB_TRENDING_URL
    url = f"{path}?{urlencode({'since': since})}"
    request = Request(url, headers={"User-Agent": DEFAULT_USER_AGENT})

    try:
        with urlopen(request, timeout=30) as response:
            html = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        raise RuntimeError(f"GitHub Trending request failed: HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(f"GitHub Trending request failed: {exc.reason}") from exc

    parser = TrendingParser()
    parser.feed(html)
    return enrich_repos(parser.repos)


def enrich_repos(repos: list[TrendingRepo]) -> list[TrendingRepo]:
    return [enrich_repo(repo) for repo in repos]


def enrich_repo(repo: TrendingRepo) -> TrendingRepo:
    zh_description, use_case = summarize_repo_in_chinese(repo)
    repo.zh_description = zh_description
    repo.use_case = use_case
    return repo


def summarize_repo_in_chinese(repo: TrendingRepo) -> tuple[str, str]:
    description = repo.description.strip()
    context = f"{repo.full_name} {description} {repo.language}".lower()

    keyword_rules: list[tuple[tuple[str, ...], str, str]] = [
        (
            ("ai", "agent", "coding agent", "codex", "llm", "mcp"),
            "面向 AI Agent 和自动化工作流的项目，用来把模型能力接入开发、工具调用或界面操作。",
            "适合用于研发提效、自动化代码审查、智能助手集成、内部工具 Agent 化等场景。",
        ),
        (
            ("penetration", "vulnerabil", "security", "scan", "attack", "pentest"),
            "开源安全工具，聚焦漏洞发现、渗透测试或应用安全检测。",
            "适合用于上线前安全自查、红队演练、漏洞验证和安全团队日常扫描。",
        ),
        (
            ("machine learning", "ml", "deep learning", "model", "training", "dataset"),
            "机器学习相关项目，覆盖模型、训练、数据处理或学习资料。",
            "适合用于 AI 研发、模型实验、课程学习、技术调研和团队知识沉淀。",
        ),
        (
            ("devtools", "debug", "browser", "chrome"),
            "开发者工具项目，帮助开发、调试或观测 Web 应用与浏览器运行状态。",
            "适合用于前端调试、自动化测试、性能分析和开发工具链集成。",
        ),
        (
            ("self-hosted", "self hosted", "server", "manager", "management", "dashboard"),
            "可自部署的管理类项目，用于集中管理资源、内容或服务。",
            "适合用于个人私有化部署、团队内部平台、数据资产管理和替代商业 SaaS。",
        ),
        (
            ("photo", "video", "media", "music", "player", "rom", "game"),
            "媒体或娱乐内容管理项目，用来整理、播放或管理个人数字内容。",
            "适合用于家庭媒体库、影音资料归档、游戏资源整理和私有娱乐中心。",
        ),
        (
            ("documentation", "book", "specification", "course", "tutorial", "guide"),
            "知识文档或规范类项目，提供系统化资料、标准说明或实践指南。",
            "适合用于技术学习、团队培训、方案选型参考和工程规范建设。",
        ),
        (
            ("ui", "component", "frontend", "react", "vue", "css", "tailwind"),
            "前端界面或组件相关项目，帮助构建交互界面、组件库或设计系统。",
            "适合用于 Web 产品开发、设计系统建设、原型验证和前端工程提效。",
        ),
        (
            ("api", "framework", "sdk", "library", "toolkit"),
            "开发框架或工具库项目，为应用开发提供 API、SDK 或基础能力封装。",
            "适合用于新项目搭建、现有系统能力扩展、二次开发和工程基础设施建设。",
        ),
    ]

    for keywords, zh_description, use_case in keyword_rules:
        if any(keyword in context for keyword in keywords):
            return zh_description, use_case

    language_label = repo.language or "未知语言"
    if contains_cjk(description):
        zh_description = description
    elif description:
        zh_description = f"一个以 {language_label} 为主要技术栈的开源项目，核心能力是：{description}"
    else:
        zh_description = f"一个以 {language_label} 为主要技术栈的 GitHub Trending 开源项目。"

    use_case = build_default_use_case(language_label)
    return zh_description, use_case


def contains_cjk(value: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in value)


def build_default_use_case(language: str) -> str:
    language_use_cases = {
        "Python": "适合用于脚本自动化、数据处理、AI 原型验证或后端服务开发。",
        "TypeScript": "适合用于 Web 应用、Node.js 服务、前端工程化和类型安全的工具开发。",
        "JavaScript": "适合用于 Web 交互、浏览器扩展、Node.js 工具和快速原型开发。",
        "Go": "适合用于云原生服务、CLI 工具、高并发后端和基础设施组件。",
        "Rust": "适合用于高性能系统、命令行工具、底层组件和安全敏感场景。",
        "C#": "适合用于 .NET 企业应用、桌面工具、游戏开发和后端服务。",
        "Java": "适合用于企业后端、Android 应用、大型服务和中间件开发。",
    }
    return language_use_cases.get(
        language,
        "适合用于技术调研、原型验证、学习参考，或按项目定位集成到现有系统中。",
    )


def build_report(repos: list[TrendingRepo], title: str, collected_at: datetime) -> str:
    lines = [
        f"# {title}",
        "",
        f"Collected at: {collected_at.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "| Rank | Repo | Language | Stars | Forks | Today | Description |",
        "|---:|---|---|---:|---:|---:|---|",
    ]
    for repo in repos:
        description_parts = [
            f"**中文简介：** {repo.zh_description or '-'}",
            f"**适用场景：** {repo.use_case or '-'}",
        ]
        if repo.description:
            description_parts.append(f"**原始描述：** {repo.description}")
        lines.append(
            "| "
            f"{repo.rank} | "
            f"[{repo.full_name}]({repo.url}) | "
            f"{repo.language or 'Unknown'} | "
            f"{repo.stars or '-'} | "
            f"{repo.forks or '-'} | "
            f"{repo.stars_today or '-'} | "
            f"{escape_table('<br>'.join(description_parts))} |"
        )
    lines.append("")
    return "\n".join(lines)


def escape_table(value: str) -> str:
    return value.replace("|", "\\|")


def build_feishu_card(repos: list[TrendingRepo], title: str) -> dict[str, Any]:
    elements: list[dict[str, Any]] = []
    for repo in repos:
        description = repo.description or "No description"
        zh_description = repo.zh_description or "暂无中文简介"
        use_case = repo.use_case or "暂无适用场景"
        metadata = " · ".join(
            item
            for item in [
                repo.language or "Unknown",
                f"Stars {repo.stars}" if repo.stars else "",
                repo.stars_today,
            ]
            if item
        )
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"**{repo.rank}. [{repo.full_name}]({repo.url})**\n"
                        f"**中文简介：** {zh_description}\n"
                        f"**适用场景：** {use_case}\n"
                        f"**原始描述：** {description}\n"
                        f"{metadata}"
                    ),
                },
            }
        )
        elements.append({"tag": "hr"})

    if elements and elements[-1].get("tag") == "hr":
        elements.pop()

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "blue",
            },
            "elements": elements,
        },
    }


def sign_feishu_payload(payload: dict[str, Any], secret: str) -> dict[str, Any]:
    timestamp = str(int(time()))
    string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
    sign = base64.b64encode(hmac.new(string_to_sign, b"", hashlib.sha256).digest()).decode("utf-8")
    return {**payload, "timestamp": timestamp, "sign": sign}


def send_feishu_webhook(webhook_url: str, payload: dict[str, Any], secret: str = "") -> None:
    if secret:
        payload = sign_feishu_payload(payload, secret)

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        webhook_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urlopen(request, timeout=30) as response:
            response_body = response.read().decode("utf-8", errors="replace")
            if response.status >= 300:
                raise RuntimeError(f"Feishu webhook failed: HTTP {response.status} {response_body}")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Feishu webhook failed: HTTP {exc.code} {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Feishu webhook failed: {exc.reason}") from exc


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send GitHub Trending repos to Feishu.")
    parser.add_argument("--language", default=os.getenv("TRENDING_LANGUAGE", ""))
    parser.add_argument("--since", default=os.getenv("TRENDING_SINCE", "daily"))
    parser.add_argument("--limit", type=int, default=int(os.getenv("TRENDING_LIMIT", "10")))
    parser.add_argument("--webhook-url", default=os.getenv("FEISHU_WEBHOOK_URL", ""))
    parser.add_argument("--feishu-secret", default=os.getenv("FEISHU_SECRET", ""))
    parser.add_argument("--report-dir", default=os.getenv("REPORT_DIR", "data/reports"))
    parser.add_argument("--snapshot-dir", default=os.getenv("SNAPSHOT_DIR", "data/snapshots"))
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    collected_at = datetime.now(timezone.utc)
    language_label = args.language.strip() or "All Languages"
    title = f"GitHub Trending Daily - {language_label}"

    repos = fetch_trending(language=args.language, since=args.since)[: args.limit]
    if not repos:
        raise RuntimeError("No repositories parsed from GitHub Trending.")

    date_key = collected_at.strftime("%Y-%m-%d")
    snapshot_path = Path(args.snapshot_dir) / f"{date_key}.json"
    report_path = Path(args.report_dir) / f"{date_key}.md"

    write_json(
        snapshot_path,
        {
            "collected_at": collected_at.isoformat(),
            "language": args.language,
            "since": args.since,
            "repos": [asdict(repo) for repo in repos],
        },
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(build_report(repos, title, collected_at), encoding="utf-8")

    payload = build_feishu_card(repos, title)
    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print(f"Wrote {snapshot_path}")
        print(f"Wrote {report_path}")
        return 0

    if not args.webhook_url:
        raise RuntimeError("FEISHU_WEBHOOK_URL is required unless --dry-run is used.")

    send_feishu_webhook(args.webhook_url, payload, args.feishu_secret)
    print(f"Sent {len(repos)} repositories to Feishu.")
    print(f"Wrote {snapshot_path}")
    print(f"Wrote {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
