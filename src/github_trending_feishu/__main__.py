from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from time import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


GITHUB_TRENDING_URL = "https://github.com/trending"
GITHUB_API_URL = "https://api.github.com"
DEEPSEEK_CHAT_COMPLETIONS_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"
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
    problem: str = ""
    key_features: list[str] = field(default_factory=list)
    target_users: str = ""
    getting_started: str = ""
    cautions: str = ""
    topics: list[str] = field(default_factory=list)
    license: str = ""
    pushed_at: str = ""
    readme_excerpt: str = ""

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
    return parser.repos


def enrich_repos(repos: list[TrendingRepo]) -> list[TrendingRepo]:
    enrich_repos_with_github(repos)
    enrich_repos_with_deepseek(repos)
    return [enrich_repo(repo) for repo in repos]


def enrich_repo(repo: TrendingRepo) -> TrendingRepo:
    if not repo.zh_description or not repo.use_case:
        zh_description, use_case = summarize_repo_in_chinese(repo)
        repo.zh_description = repo.zh_description or zh_description
        repo.use_case = repo.use_case or use_case
    repo.problem = repo.problem or infer_problem(repo)
    repo.key_features = repo.key_features or infer_key_features(repo)
    if not repo.key_features:
        repo.key_features = [f"以 {repo.language or '开源技术'} 实现核心能力", "提供可复用的开源代码和文档"]
    repo.target_users = repo.target_users or infer_target_users(repo)
    repo.getting_started = repo.getting_started or "建议先阅读项目 README，确认运行环境、安装步骤和示例，再进行本地验证。"
    repo.cautions = repo.cautions or build_fallback_cautions(repo)
    return repo


def enrich_repos_with_github(repos: list[TrendingRepo]) -> None:
    if not repos:
        return
    with ThreadPoolExecutor(max_workers=min(5, len(repos))) as executor:
        list(executor.map(enrich_repo_with_github, repos))


def enrich_repo_with_github(repo: TrendingRepo) -> None:
    try:
        metadata = request_github_json(f"/repos/{quote(repo.owner)}/{quote(repo.name)}")
        repo.topics = [str(topic) for topic in metadata.get("topics", []) if topic]
        license_info = metadata.get("license") or {}
        repo.license = str(license_info.get("spdx_id") or license_info.get("name") or "")
        repo.pushed_at = str(metadata.get("pushed_at") or "")
    except RuntimeError as exc:
        print(f"GitHub metadata unavailable for {repo.full_name}: {exc}", file=sys.stderr)

    try:
        readme = request_github_readme(repo)
        repo.readme_excerpt = prepare_readme_for_summary(readme)
    except RuntimeError as exc:
        print(f"GitHub README unavailable for {repo.full_name}: {exc}", file=sys.stderr)


def github_request_headers(accept: str) -> dict[str, str]:
    headers = {
        "Accept": accept,
        "User-Agent": DEFAULT_USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def request_github_json(path: str) -> dict[str, Any]:
    request = Request(
        f"{GITHUB_API_URL}{path}",
        headers=github_request_headers("application/vnd.github+json"),
    )
    try:
        with urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}") from exc
    except (URLError, json.JSONDecodeError) as exc:
        raise RuntimeError(str(exc)) from exc
    if not isinstance(payload, dict):
        raise RuntimeError("unexpected response shape")
    return payload


def request_github_readme(repo: TrendingRepo) -> str:
    path = f"/repos/{quote(repo.owner)}/{quote(repo.name)}/readme"
    request = Request(
        f"{GITHUB_API_URL}{path}",
        headers=github_request_headers("application/vnd.github.raw+json"),
    )
    try:
        with urlopen(request, timeout=20) as response:
            return response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(str(exc.reason)) from exc


def prepare_readme_for_summary(readme: str, limit: int = 6000) -> str:
    text = re.sub(r"<!--.*?-->", " ", readme, flags=re.DOTALL)
    text = re.sub(r"!\[[^]]*]\([^)]*\)", " ", text)
    text = re.sub(r"<img\b[^>]*>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<picture\b.*?</picture>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"\[!\[[^]]*]\([^)]*\)]\([^)]*\)", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit("\n", 1)[0].rstrip() + "\n[README 已截断]"


def enrich_repos_with_deepseek(repos: list[TrendingRepo]) -> None:
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key or not repos:
        return
    model = os.getenv("DEEPSEEK_MODEL", DEFAULT_DEEPSEEK_MODEL).strip() or DEFAULT_DEEPSEEK_MODEL

    try:
        summaries = request_deepseek_summaries(
            repos,
            api_key=api_key,
            model=model,
        )
    except RuntimeError as exc:
        print(f"DeepSeek summary failed, fallback to local rules: {exc}", file=sys.stderr)
        return

    for repo in repos:
        summary = summaries.get(repo.full_name)
        if not summary:
            continue
        zh_description = normalize_text(summary.get("zh_description", ""))
        use_case = normalize_text(summary.get("use_case", ""))
        if zh_description and use_case:
            repo.zh_description = zh_description
            repo.use_case = use_case
            repo.problem = normalize_text(summary.get("problem", ""))
            features = summary.get("key_features", [])
            if isinstance(features, list):
                repo.key_features = [normalize_text(str(item)) for item in features if normalize_text(str(item))][:4]
            repo.target_users = normalize_text(summary.get("target_users", ""))
            repo.getting_started = normalize_text(summary.get("getting_started", ""))
            repo.cautions = normalize_text(summary.get("cautions", ""))


def request_deepseek_summaries(
    repos: list[TrendingRepo],
    *,
    api_key: str,
    model: str,
) -> dict[str, dict[str, str]]:
    body = json.dumps(
        {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是面向中文技术读者的 GitHub Trending 分析助手。"
                        "只根据提供的仓库元数据和 README 摘录生成内容；没有证据的能力不要推测。"
                        "要用非项目作者也能理解的中文，解释它是什么、为何需要、能做什么、适合谁以及如何开始。"
                        "避免宣传话术和泛泛的“研发提效、技术调研”，不把 Trending 热度等同于成熟度。"
                        "输出必须是合法 JSON，不要 Markdown，不要额外解释。"
                    ),
                },
                {
                    "role": "user",
                    "content": build_deepseek_prompt(repos),
                },
            ],
            "temperature": 0.2,
            "max_tokens": 6000,
            "response_format": {"type": "json_object"},
        },
        ensure_ascii=False,
    ).encode("utf-8")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    request = Request(
        DEEPSEEK_CHAT_COMPLETIONS_URL,
        data=body,
        headers=headers,
        method="POST",
    )
    timeout = int(os.getenv("DEEPSEEK_TIMEOUT", "45"))

    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} {detail}") from exc
    except (URLError, json.JSONDecodeError) as exc:
        raise RuntimeError(str(exc)) from exc

    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"Unexpected response: {payload}") from exc

    try:
        parsed = json.loads(extract_json_object(content))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON content: {content}") from exc

    items = parsed.get("repos", parsed if isinstance(parsed, list) else [])
    if not isinstance(items, list):
        raise RuntimeError(f"Unexpected summary shape: {parsed}")

    summaries: dict[str, dict[str, str]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        full_name = str(item.get("full_name", "")).strip()
        if full_name:
            summaries[full_name] = {
                "zh_description": str(item.get("zh_description", "")).strip(),
                "problem": str(item.get("problem", "")).strip(),
                "key_features": item.get("key_features", []),
                "target_users": str(item.get("target_users", "")).strip(),
                "use_case": str(item.get("use_case", "")).strip(),
                "getting_started": str(item.get("getting_started", "")).strip(),
                "cautions": str(item.get("cautions", "")).strip(),
            }
    return summaries


def build_deepseek_prompt(repos: list[TrendingRepo]) -> str:
    repo_payload = [
        {
            "full_name": repo.full_name,
            "language": repo.language or "Unknown",
            "description": repo.description,
            "topics": repo.topics,
            "license": repo.license or "Unknown",
            "last_push": repo.pushed_at or "Unknown",
            "readme_excerpt": repo.readme_excerpt,
        }
        for repo in repos
    ]
    return (
        "请为下面每个 GitHub 仓库生成面向普通技术读者的结构化解读。\n"
        "要求：\n"
        "1. 每个仓库全部中文字段合计约 150-250 个汉字，信息不足时宁可写得短，不要编造。\n"
        "2. zh_description：25-45 字，一句话解释项目是什么；problem：25-50 字，解释它解决的具体问题。\n"
        "3. key_features：2-4 条，每条 8-25 字，只写 README 明确支持的能力。\n"
        "4. target_users：20-40 字，说明最适合的用户；use_case：30-60 字，给出具体使用场景。\n"
        "5. getting_started：20-45 字，概括安装、部署或接入方式；cautions：15-45 字，写依赖、成熟度、平台或许可证提醒。\n"
        "6. 不要因为出现 AI、agent、chat 就套通用模板；不要把 star 数量当成功能或质量证据。\n"
        '7. 只输出 {"repos":[{"full_name":"owner/name","zh_description":"...","problem":"...","key_features":["..."],"target_users":"...","use_case":"...","getting_started":"...","cautions":"..."}]}。\n\n'
        f"仓库列表：{json.dumps(repo_payload, ensure_ascii=False)}"
    )


def extract_json_object(content: str) -> str:
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)

    first_brace = content.find("{")
    last_brace = content.rfind("}")
    if first_brace != -1 and last_brace != -1 and first_brace < last_brace:
        return content[first_brace : last_brace + 1]
    return content


def summarize_repo_in_chinese(repo: TrendingRepo) -> tuple[str, str]:
    description = repo.description.strip()
    context = f"{repo.full_name} {description} {repo.language}".lower()

    keyword_rules: list[tuple[tuple[str, ...], str, str]] = [
        (
            ("ai-video-generator", "video automation", "video generation", "generate hd short videos", "一键生成高清短视频"),
            "AI 短视频自动生成工具，可根据主题或关键词完成文案、配音、字幕和视频合成流程。",
            "适合批量制作 YouTube Shorts、TikTok、Reels 等短视频，或验证自动化内容生产流程。",
        ),
        (
            ("grok companion", "waifu", "neuro-sama", "voice chat", "minecraft", "factorio"),
            build_companion_summary(repo),
            "适合用于私有化部署 AI 虚拟角色、实时语音陪伴、游戏内互动助手，或验证跨端 AI companion 产品原型。",
        ),
        (
            ("voice chat", "realtime voice", "speech", "tts", "stt", "audio chat"),
            "实时语音交互项目，重点提供语音聊天、语音输入输出或音频驱动的人机互动能力。",
            "适合用于语音助手、陪伴式应用、客服原型、游戏语音交互和多模态 AI 产品验证。",
        ),
        (
            ("minecraft", "factorio", "game playing", "game automation", "bot player"),
            "面向游戏场景的自动化或智能交互项目，可让程序、机器人或 AI 参与游戏操作。",
            "适合用于游戏 Bot、自动化测试、AI 玩家实验、直播互动和游戏内助手。",
        ),
        (
            ("self-hosted", "self hosted", "you-owned", "selfhost", "local-first"),
            "可自托管的应用项目，强调用户自己掌控数据、部署环境和服务运行方式。",
            "适合用于个人私有化部署、团队内部服务、数据自主可控和替代商业 SaaS。",
        ),
        (
            ("photo", "video", "media", "music", "player", "rom", "game library"),
            "媒体或娱乐内容管理项目，用来整理、播放或管理个人数字内容。",
            "适合用于家庭媒体库、影音资料归档、游戏资源整理和私有娱乐中心。",
        ),
        (
            ("coding agent", "code agent", "codex", "claude code", "dev agent", "mcp"),
            "面向 AI Agent 和自动化工作流的项目，用来把模型能力接入开发、工具调用或界面操作。",
            "适合用于研发提效、自动化代码审查、智能助手集成、内部工具 Agent 化等场景。",
        ),
        (
            ("agent", "llm", "large language model", "generative ai", "chatbot"),
            "AI 应用或智能体相关项目，用来构建对话、自动执行任务或接入大语言模型能力。",
            "适合用于智能助手、业务流程自动化、AI 原型验证和模型能力集成。",
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
            ("server", "manager", "management", "dashboard", "admin"),
            "管理类工具或平台项目，用于集中管理资源、内容、服务或系统状态。",
            "适合用于团队内部平台、运营后台、数据资产管理和工程管理工具。",
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
        if any(matches_keyword(context, keyword) for keyword in keywords):
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


def matches_keyword(context: str, keyword: str) -> bool:
    if len(keyword) <= 3 and keyword.isascii() and keyword.isalnum():
        return re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", context) is not None
    return keyword in context


def build_companion_summary(repo: TrendingRepo) -> str:
    features = extract_feature_labels(repo.description)
    feature_text = "，".join(features)
    if feature_text:
        return f"自托管的虚拟陪伴/AI companion 项目，重点能力包括{feature_text}。"
    return "自托管的虚拟陪伴/AI companion 项目，用来构建可交互的数字角色和多端陪伴应用。"


def extract_feature_labels(description: str) -> list[str]:
    lower_description = description.lower()
    feature_rules = [
        (("self-hosted", "self hosted", "you-owned"), "用户自托管和数据自主管理"),
        (("grok companion", "companion"), "虚拟陪伴角色"),
        (("realtime voice chat", "voice chat"), "实时语音聊天"),
        (("minecraft",), "Minecraft 联动"),
        (("factorio",), "Factorio 联动"),
        (("web / macos / windows", "web/macos/windows"), "Web/macOS/Windows 多端支持"),
    ]

    features: list[str] = []
    for keywords, label in feature_rules:
        if any(keyword in lower_description for keyword in keywords):
            features.append(label)
    return features


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


def infer_target_users(repo: TrendingRepo) -> str:
    context = f"{repo.description} {' '.join(repo.topics or [])}".lower()
    if any(word in context for word in ("short-video", "video-automation", "content-creation", "video generator")):
        return "适合短视频创作者、内容运营人员，以及需要批量生产视频素材的团队。"
    if any(word in context for word in ("design", "ui", "frontend", "react", "vue")):
        return "适合前端开发者、产品设计师，以及需要搭建界面或设计系统的团队。"
    if any(word in context for word in ("agent", "llm", "model", "machine learning")):
        return "适合 AI 应用开发者、算法工程师，以及正在验证智能化产品的团队。"
    if any(word in context for word in ("server", "devops", "kubernetes", "deploy", "ci/cd")):
        return "适合后端、DevOps 和平台工程团队，以及需要自建服务的技术人员。"
    return f"适合希望评估或集成这类能力的 {repo.language or '软件'} 开发者和技术团队。"


def infer_problem(repo: TrendingRepo) -> str:
    context = f"{repo.description} {' '.join(repo.topics or [])}".lower()
    if any(word in context for word in ("short-video", "video-automation", "video generator", "生成高清短视频")):
        return "把选题、文案、配音、字幕和画面合成串成自动流程，减少手工制作短视频的重复工作。"
    if any(word in context for word in ("agent", "llm", "generative-ai")):
        return "降低大模型能力接入具体应用和自动化流程时的开发与整合成本。"
    if "self-hosted" in context or "self hosted" in context:
        return "让用户能够在自己的环境中运行服务，并自行掌控数据和部署方式。"
    return f"围绕“{repo.zh_description.rstrip('。')}”提供可复用实现，减少从零开发和整合的工作量。"


def infer_key_features(repo: TrendingRepo) -> list[str]:
    context = f"{repo.description} {' '.join(repo.topics or [])}".lower()
    feature_rules = [
        (("ai-video-generator", "video generation", "生成高清短视频"), "根据主题或关键词生成短视频"),
        (("video-workflow", "video-automation", "workflow-automation"), "自动串联视频制作工作流"),
        (("text-to-speech", "tts"), "支持文字转语音配音"),
        (("subtitles", "subtitle"), "支持字幕生成与合成"),
        (("self-hosted", "self hosted", "local-first"), "支持自行部署和掌控数据"),
        (("api", "sdk"), "提供 API 或 SDK 接入能力"),
    ]
    features = [
        label
        for keywords, label in feature_rules
        if any(matches_keyword(context, keyword) for keyword in keywords)
    ]
    if features:
        return features[:4]
    return extract_feature_labels(repo.description)[:3]


def build_fallback_cautions(repo: TrendingRepo) -> str:
    details: list[str] = []
    if repo.license:
        details.append(f"许可证为 {repo.license}")
    else:
        details.append("使用前需确认许可证")
    details.append("Trending 代表近期关注度，不代表已达到生产成熟度")
    return "；".join(details) + "。"


def build_report(repos: list[TrendingRepo], title: str, collected_at: datetime) -> str:
    lines = [
        f"# {title}",
        "",
        f"Collected at: {collected_at.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "> 解读基于仓库公开描述、README 与元数据自动生成；Trending 热度不等同于项目成熟度。",
        "",
    ]
    for repo in repos:
        metadata = " · ".join(
            item for item in (
                repo.language or "Unknown",
                f"⭐ {repo.stars}" if repo.stars else "",
                repo.stars_today,
                f"License: {repo.license}" if repo.license else "",
            ) if item
        )
        lines.extend([
            f"## {repo.rank}. [{repo.full_name}]({repo.url})",
            "",
            metadata,
            "",
            f"**它是什么：** {repo.zh_description or '-'}",
            "",
            f"**解决的问题：** {repo.problem or '-'}",
            "",
            f"**核心能力：** {'；'.join(repo.key_features or []) or '-'}",
            "",
            f"**适合谁：** {repo.target_users or '-'}",
            "",
            f"**典型场景：** {repo.use_case or '-'}",
            "",
            f"**如何开始：** {repo.getting_started or '-'}",
            "",
            f"**阅读提示：** {repo.cautions or '-'}",
            "",
        ])
        if repo.description:
            lines.extend([f"<details><summary>GitHub 原始描述</summary>", "", repo.description, "", "</details>", ""])
    return "\n".join(lines)


def escape_table(value: str) -> str:
    return value.replace("|", "\\|")


def build_feishu_card(repos: list[TrendingRepo], title: str) -> dict[str, Any]:
    elements: list[dict[str, Any]] = []
    for repo in repos:
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
                        f"**它是什么：** {zh_description}\n"
                        f"**解决的问题：** {repo.problem or '暂无说明'}\n"
                        f"**核心能力：** {'；'.join(repo.key_features or []) or '暂无说明'}\n"
                        f"**适合谁：** {repo.target_users or '暂无说明'}\n"
                        f"**典型场景：** {use_case}\n"
                        f"**如何开始：** {repo.getting_started or '请查看 README'}\n"
                        f"**阅读提示：** {repo.cautions or '请自行评估项目成熟度'}\n"
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


def repo_to_snapshot(repo: TrendingRepo) -> dict[str, Any]:
    data = asdict(repo)
    data.pop("readme_excerpt", None)
    return data


def parse_report_timezone(value: str) -> ZoneInfo | timezone:
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError:
        print(f"Unknown REPORT_TIMEZONE {value!r}, fallback to UTC.", file=sys.stderr)
        return timezone.utc


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send GitHub Trending repos to Feishu.")
    parser.add_argument("--language", default=os.getenv("TRENDING_LANGUAGE", ""))
    parser.add_argument("--since", default=os.getenv("TRENDING_SINCE", "daily"))
    parser.add_argument("--limit", type=int, default=int(os.getenv("TRENDING_LIMIT", "10")))
    parser.add_argument("--webhook-url", default=os.getenv("FEISHU_WEBHOOK_URL", ""))
    parser.add_argument("--feishu-secret", default=os.getenv("FEISHU_SECRET", ""))
    parser.add_argument("--report-dir", default=os.getenv("REPORT_DIR", "data/reports"))
    parser.add_argument("--snapshot-dir", default=os.getenv("SNAPSHOT_DIR", "data/snapshots"))
    parser.add_argument("--report-timezone", default=os.getenv("REPORT_TIMEZONE", "Asia/Shanghai"))
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
    repos = enrich_repos(repos)

    report_timezone = parse_report_timezone(args.report_timezone)
    date_key = collected_at.astimezone(report_timezone).strftime("%Y-%m-%d")
    snapshot_path = Path(args.snapshot_dir) / f"{date_key}.json"
    report_path = Path(args.report_dir) / f"{date_key}.md"

    write_json(
        snapshot_path,
        {
            "collected_at": collected_at.isoformat(),
            "language": args.language,
            "since": args.since,
            "repos": [repo_to_snapshot(repo) for repo in repos],
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
