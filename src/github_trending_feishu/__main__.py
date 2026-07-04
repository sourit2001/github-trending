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
        lines.append(
            "| "
            f"{repo.rank} | "
            f"[{repo.full_name}]({repo.url}) | "
            f"{repo.language or 'Unknown'} | "
            f"{repo.stars or '-'} | "
            f"{repo.forks or '-'} | "
            f"{repo.stars_today or '-'} | "
            f"{escape_table(repo.description) or '-'} |"
        )
    lines.append("")
    return "\n".join(lines)


def escape_table(value: str) -> str:
    return value.replace("|", "\\|")


def build_feishu_card(repos: list[TrendingRepo], title: str) -> dict[str, Any]:
    elements: list[dict[str, Any]] = []
    for repo in repos:
        description = repo.description or "No description"
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
                        f"{description}\n"
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
