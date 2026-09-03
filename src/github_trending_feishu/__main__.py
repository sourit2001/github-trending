from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import socket
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
    problem: str = ""
    key_features: list[str] = field(default_factory=list)
    use_cases: list[str] = field(default_factory=list)
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
    if not repo.zh_description:
        zh_description = summarize_repo_in_chinese(repo)
        repo.zh_description = repo.zh_description or zh_description
    repo.problem = repo.problem or infer_problem(repo)
    repo.key_features = repo.key_features or infer_key_features(repo)
    repo.use_cases = repo.use_cases or infer_use_cases(repo)
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
    except (URLError, TimeoutError, socket.timeout, json.JSONDecodeError) as exc:
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
    text = re.sub(r"<details\b.*?</details>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    heading_pattern = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
    matches = list(heading_pattern.finditer(text))
    if not matches:
        return truncate_readme_text(text, limit)

    preferred = re.compile(
        r"overview|about|what (?:is|does)|introduction|features?|capabilities|"
        r"how it works|workflow|usage|examples?|architecture|why|choose|supported?|"
        r"output|export|compatib|included|scope|domain|database|skills?|"
        r"概览|简介|介绍|是什么|功能|特性|能力|工作原理|流程|用法|使用|示例|架构|"
        r"为什么|选择|支持|输出|导出|兼容|包含|范围|领域|数据库|技能",
        re.IGNORECASE,
    )
    excluded = re.compile(
        r"install|getting started|quick ?start|prerequisite|requirement|configuration|"
        r"contribut|license|changelog|roadmap|faq|support|sponsor|acknowledg|"
        r"安装|快速开始|环境要求|配置|贡献|许可证|更新日志|路线图|常见问题|赞助",
        re.IGNORECASE,
    )

    selected: list[str] = []
    intro_end = matches[0].start()
    intro = text[:intro_end].strip()
    if intro:
        selected.append(intro[:1600])

    covered_until = 0
    first_heading = matches[0]
    if len(first_heading.group(1)) == 1 and not excluded.search(normalize_text(first_heading.group(2))):
        first_section_end = matches[1].start() if len(matches) > 1 else len(text)
        selected.append(text[first_heading.start():first_section_end].strip()[:1600])
        covered_until = first_section_end

    for index, match in enumerate(matches):
        if match.start() < covered_until:
            continue
        heading = normalize_text(match.group(2))
        level = len(match.group(1))
        section_end = len(text)
        for later_match in matches[index + 1:]:
            if len(later_match.group(1)) <= level:
                section_end = later_match.start()
                break
        if excluded.search(heading):
            covered_until = section_end
            continue
        if not preferred.search(heading):
            continue
        section = text[match.start():section_end].strip()
        if section:
            selected.append(section[:2200])
            covered_until = section_end

    focused = "\n\n".join(selected).strip()
    return truncate_readme_text(focused or text, limit)


def truncate_readme_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    truncated = text[:limit].rsplit("\n", 1)[0].rstrip()
    return truncated or text[:limit].rstrip()


def enrich_repos_with_deepseek(repos: list[TrendingRepo]) -> None:
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key or not repos:
        return
    model = os.getenv("DEEPSEEK_MODEL", DEFAULT_DEEPSEEK_MODEL).strip() or DEFAULT_DEEPSEEK_MODEL

    summaries: dict[str, dict[str, Any]] = {}
    # Smaller batches reduce omissions and malformed JSON when a daily list is long.
    for start in range(0, len(repos), 3):
        batch = repos[start : start + 3]
        try:
            summaries.update(request_deepseek_summaries(batch, api_key=api_key, model=model))
        except (RuntimeError, TimeoutError, socket.timeout) as exc:
            print(f"DeepSeek summary failed for batch, fallback to local rules: {exc}", file=sys.stderr)

    # Retry only repositories whose result is missing or too thin.
    missing = [repo for repo in repos if not is_usable_deepseek_summary(summaries.get(repo.full_name))]
    for repo in missing:
        try:
            retry = request_deepseek_summaries([repo], api_key=api_key, model=model)
            if is_usable_deepseek_summary(retry.get(repo.full_name)):
                summaries[repo.full_name] = retry[repo.full_name]
        except (RuntimeError, TimeoutError, socket.timeout) as exc:
            print(f"DeepSeek retry failed for {repo.full_name}, using local facts: {exc}", file=sys.stderr)

    for repo in repos:
        summary = summaries.get(repo.full_name)
        if not is_usable_deepseek_summary(summary):
            continue
        zh_description = normalize_text(summary.get("zh_description", ""))
        if zh_description:
            repo.zh_description = zh_description
            repo.problem = normalize_text(summary.get("problem", ""))
            features = summary.get("key_features", [])
            if isinstance(features, list):
                repo.key_features = [normalize_text(str(item)) for item in features if normalize_text(str(item))][:5]
            use_cases = summary.get("use_cases", [])
            if isinstance(use_cases, list):
                repo.use_cases = [normalize_text(str(item)) for item in use_cases if normalize_text(str(item))][:3]


def is_usable_deepseek_summary(summary: dict[str, Any] | None) -> bool:
    description = normalize_text(str(summary.get("zh_description", ""))) if summary else ""
    generic_markers = ("面向 AI Agent 和自动化工作流", "提升效率", "方便开发", "智能助手集成")
    problem = normalize_text(str(summary.get("problem", ""))) if summary else ""
    if not description or not problem or any(marker in description for marker in generic_markers):
        return False
    features = summary.get("key_features")
    use_cases = summary.get("use_cases")
    return (
        isinstance(features, list)
        and len([item for item in features if normalize_text(str(item))]) >= 2
        and isinstance(use_cases, list)
        and len([item for item in use_cases if normalize_text(str(item))]) >= 1
    )


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
                        "你是面向中文技术读者的 GitHub Trending README 摘要助手。"
                        "你的任务是压缩 README 中的事实，而不是评价项目或补全通用建议。"
                        "只根据提供的仓库描述、Topics 和 README 摘录生成内容，没有证据就少写。"
                        "保留 README 中具体的产品名、组件、输入输出、工作流程和功能边界。"
                        "禁止宣传话术以及“提升效率、降低成本、方便开发、适合技术调研”等空话。"
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
    except (URLError, TimeoutError, socket.timeout, json.JSONDecodeError) as exc:
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
                "use_cases": item.get("use_cases", []),
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
        "请把下面每个仓库的 README 压缩成简洁、具体的中文说明。\n"
        "要求：\n"
        "1. 每个仓库四个字段合计约 120-200 个汉字；README 信息少时可以更短。\n"
        "2. zh_description：60-100 字，说明项目接收什么输入、经过什么核心处理、产生什么结果。\n"
        "3. problem：25-45 字，指出它替代了哪种具体手工流程、分散工具或技术限制；无法确认时输出空字符串。\n"
        "4. key_features：3-5 条，每条 8-25 字，必须是 README 明确写出的功能，保留具体组件或协议名称。\n"
        "5. use_cases：2-3 条，每条 15-35 字，写可以实际完成的任务，不写用户画像。\n"
        "6. 不输出安装方法、阅读建议、成熟度评价、许可证提醒、目标用户或原始英文描述。\n"
        "7. 不要把 star、编程语言或 Trending 热度写成功能；禁止“赋能、助力、提升效率、降低成本”等套话。\n"
        '8. 只输出 {"repos":[{"full_name":"owner/name","zh_description":"...","problem":"...","key_features":["..."],"use_cases":["..."]}]}。\n\n'
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


def summarize_repo_in_chinese(repo: TrendingRepo) -> str:
    description = repo.description.strip()
    context = f"{repo.full_name} {description} {repo.language} {' '.join(repo.topics)}".lower()

    evidence_summary = summarize_from_readme_facts(repo)
    if evidence_summary:
        return evidence_summary

    keyword_rules: list[tuple[tuple[str, ...], str, str]] = [
        (
            ("coding agent that runs", "coding agent", "openai/codex"),
            "在本地计算机终端中运行的编程代理，可直接围绕当前代码库执行开发任务；本仓库对应 Codex CLI。",
            "适合在终端内处理代码任务。",
        ),
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

    for keywords, zh_description, _ in keyword_rules:
        if any(matches_keyword(context, keyword) for keyword in keywords):
            return zh_description

    if contains_cjk(description):
        zh_description = description
    elif description:
        zh_description = description
    else:
        zh_description = f"{repo.full_name} 暂未提供可用于摘要的项目说明。"
    return zh_description


def summarize_from_readme_facts(repo: TrendingRepo) -> str:
    """Create a factual fallback from README text when the model is unavailable."""
    readme = repo.readme_excerpt.strip()
    if not readme:
        return ""
    context = f"{repo.full_name} {repo.description} {readme}".lower()
    if repo.full_name.lower() == "tt-a1i/archify" or "typed json ir" in context:
        return (
            "将代码库或系统描述转换为可交互的系统地图：代理生成 Typed JSON IR，"
            "Archify 进行确定性校验并渲染为 HTML/SVG，可输出 PNG、WebM 和分享卡片，"
            "支持架构、工作流、时序、数据流与生命周期图，以及变更对比和源码追踪。"
        )
    if "scientific agent skills" in context or "scientific-agent-skills" in repo.full_name.lower():
        return (
            "面向 AI Agent 的科研技能集合，提供 163 个可复用技能和 100+ 科学数据库接入，"
            "覆盖生物信息、基因组学、化学、药物发现、医学研究、数据分析等领域，"
            "并以 Agent Skills/Plugins 标准兼容 Cursor、Claude Code、Codex 等客户端。"
        )
    paragraphs = [
        re.sub(r"\s+", " ", block).strip()
        for block in re.split(r"\n\s*\n", readme)
        if block.strip() and not block.lstrip().startswith("#")
    ]
    if not paragraphs:
        return ""
    first = re.sub(r"[`*_]", "", paragraphs[0]).strip()
    first = re.split(r"(?<=[.!?。！？])\s+", first)[0]
    if len(first) < 20:
        return ""
    return f"该项目主要用于：{first[:220]}"


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


def infer_problem(repo: TrendingRepo) -> str:
    context = f"{repo.full_name} {repo.description} {' '.join(repo.topics)} {repo.readme_excerpt}".lower()
    if repo.full_name.lower() == "tt-a1i/archify" or "typed json ir" in context:
        return "把系统描述、架构关系和变更内容整理成可校验、可交互、可分享的图，而不是手工维护静态图。"
    if "scientific agent skills" in context or "scientific-agent-skills" in repo.full_name.lower():
        return "把分散的科研库、数据库和研究流程知识封装成 Agent 可调用的标准技能，减少每次重新拼接工具链。"
    if any(word in context for word in ("coding agent that runs", "coding agent", "openai/codex")):
        return "把编程代理放进本地终端和代码目录，避免只能在独立聊天页面中处理代码任务。"
    if any(word in context for word in ("short-video", "video-automation", "video generator", "生成高清短视频")):
        return "把选题、文案、配音、字幕和画面合成串成自动流程，减少手工制作短视频的重复工作。"
    if "self-hosted" in context or "self hosted" in context:
        return "替代必须把数据交给第三方托管的服务，让应用和数据运行在自己的环境中。"
    if any(word in context for word in ("multi-provider", "multiple providers", "unified interface")):
        return "统一不同服务商的调用接口，避免为每个模型或后端分别维护一套接入代码。"
    return ""


def infer_key_features(repo: TrendingRepo) -> list[str]:
    context = f"{repo.full_name} {repo.description} {' '.join(repo.topics)} {repo.readme_excerpt}".lower()
    if repo.full_name.lower() == "tt-a1i/archify" or "typed json ir" in context:
        return ["支持架构、工作流、时序、数据流和生命周期图", "Typed JSON IR 与确定性校验", "导出 HTML、SVG、PNG、WebM 和分享卡片", "支持架构变更 Before/Delta/After 对比", "可追踪节点对应的源码证据"]
    if "scientific agent skills" in context or "scientific-agent-skills" in repo.full_name.lower():
        return ["提供 163 个可复用科研技能", "接入 100+ 科学数据库和工具", "覆盖生物、化学、医学和数据分析", "兼容 Agent Skills 与 Agent Plugins 标准", "支持 Cursor、Claude Code、Codex 等客户端"]
    feature_rules = [
        (("coding agent that runs", "coding agent", "openai/codex"), "在本地终端中运行编程代理"),
        (("sign in with chatgpt",), "支持使用 ChatGPT 账号登录"),
        (("api key",), "支持使用 API Key 调用"),
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


def infer_use_cases(repo: TrendingRepo) -> list[str]:
    context = f"{repo.full_name} {repo.description} {' '.join(repo.topics)} {repo.readme_excerpt}".lower()
    if repo.full_name.lower() == "tt-a1i/archify" or "typed json ir" in context:
        return ["把服务架构、API 调用和数据流制作成可交互图", "在 PR 前对比架构快照并检查新增、删除和重路由", "生成带源码证据的系统图用于设计评审和分享"]
    if "scientific agent skills" in context or "scientific-agent-skills" in repo.full_name.lower():
        return ["让 AI Agent 执行文献检索、数据分析和科研报告流程", "在生物信息、药物发现和医学研究中调用专用工具", "为 Cursor、Claude Code 或 Codex 配置科研工作技能"]
    if any(word in context for word in ("coding agent that runs", "coding agent", "openai/codex")):
        return ["在本地代码库中通过终端处理编程任务", "把终端中的代码工作交给 AI 代理执行"]
    if any(word in context for word in ("short-video", "video-automation", "video generator", "生成高清短视频")):
        return ["批量制作 YouTube Shorts、TikTok 和 Reels", "生成产品介绍、资讯或营销短视频"]
    if any(word in context for word in ("voice chat", "speech-to-speech", "voice-agent")):
        return ["构建实时语音助手或语音客服", "在本地运行语音交互应用"]
    if any(word in context for word in ("penetration", "pentest", "vulnerability", "security scanner")):
        return ["扫描应用中的已知漏洞和配置风险", "在上线前执行自动化安全检查"]
    if any(word in context for word in ("terminal file manager", "file-manager", "file manager")):
        return ["在终端中浏览、复制和整理文件", "通过键盘操作管理远程服务器文件"]
    if any(word in context for word in ("multi-provider", "multiple providers", "unified interface")):
        return ["用同一套代码切换不同模型服务商", "在一个应用中对比多个模型的输出"]
    return []


def build_report(repos: list[TrendingRepo], title: str, collected_at: datetime) -> str:
    lines = [
        f"# {title}",
        "",
        f"Collected at: {collected_at.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "> 内容根据仓库 README 的概览、功能、工作原理、用法和示例章节压缩生成。",
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
            f"**项目简介：** {repo.zh_description or '-'}",
            "",
            f"**解决的问题：** {repo.problem or '-'}",
            "",
            f"**主要功能：** {'；'.join(repo.key_features) or '-'}",
            "",
            f"**使用场景：** {'；'.join(repo.use_cases) or '-'}",
            "",
        ])
    return "\n".join(lines)


def escape_table(value: str) -> str:
    return value.replace("|", "\\|")


def build_feishu_card(repos: list[TrendingRepo], title: str) -> dict[str, Any]:
    elements: list[dict[str, Any]] = []
    for repo in repos:
        zh_description = repo.zh_description or "暂无中文简介"
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
                        f"**项目简介：** {zh_description}\n"
                        f"**解决的问题：** {repo.problem or '-'}\n"
                        f"**主要功能：** {'；'.join(repo.key_features) or '-'}\n"
                        f"**使用场景：** {'；'.join(repo.use_cases) or '-'}\n"
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
