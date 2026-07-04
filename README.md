# GitHub Trending Feishu Notifier

每天通过 GitHub Actions 抓取 GitHub Trending，并发送到飞书群机器人 Webhook。

## 使用方式

1. 在飞书群里添加自定义机器人，复制 Webhook URL。
2. 在 GitHub 仓库的 `Settings -> Secrets and variables -> Actions` 中新增 secret：
   - `FEISHU_WEBHOOK_URL`: 飞书机器人 Webhook URL
   - `FEISHU_SECRET`: 可选，如果机器人开启了签名校验，则填写飞书给出的签名密钥
3. 手动运行 `Daily GitHub Trending` workflow，或等待每天自动运行。

默认配置：

- 时间：每天 UTC 01:00
- 范围：GitHub Trending daily
- 数量：前 10 个项目
- 语言：全部语言

## 本地测试

只生成报告和打印飞书消息，不实际发送：

```bash
PYTHONPATH=src python -m github_trending_feishu --dry-run
```

发送到飞书：

```bash
FEISHU_WEBHOOK_URL="https://open.feishu.cn/open-apis/bot/v2/hook/..." \
PYTHONPATH=src python -m github_trending_feishu
```

## 可配置环境变量

- `FEISHU_WEBHOOK_URL`: 飞书机器人 Webhook URL
- `FEISHU_SECRET`: 可选，飞书机器人签名密钥
- `TRENDING_LANGUAGE`: GitHub Trending 语言，例如 `python`、`typescript`、`go`
- `TRENDING_SINCE`: `daily`、`weekly` 或 `monthly`
- `TRENDING_LIMIT`: 发送项目数量
- `REPORT_DIR`: Markdown 报告目录，默认 `data/reports`
- `SNAPSHOT_DIR`: JSON 快照目录，默认 `data/snapshots`
