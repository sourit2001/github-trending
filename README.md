# GitHub Trending Feishu Notifier

每天通过 GitHub Actions 抓取 GitHub Trending，并发送到飞书群机器人 Webhook。

## 使用方式

1. 在飞书群里添加自定义机器人，复制 Webhook URL。
2. 在 GitHub 仓库的 `Settings -> Secrets and variables -> Actions` 中新增 secret：
   - `FEISHU_WEBHOOK_URL`: 飞书机器人 Webhook URL
   - `FEISHU_SECRET`: 可选，如果机器人开启了签名校验，则填写飞书给出的签名密钥
   - `DEEPSEEK_API_KEY`: 可选，DeepSeek API Key；配置后用于生成更准确的中文简介和适用场景
3. 手动运行 `Daily GitHub Trending` workflow，或等待每天自动运行。

默认配置：

- 时间：每天 UTC 01:00
- 范围：GitHub Trending daily
- 数量：前 10 个项目
- 语言：全部语言
- 内容：仓库基础信息、中文简介、适用场景、原始描述

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
- `DEEPSEEK_API_KEY`: 可选，配置后使用 DeepSeek 官方 API 生成更准确的中文简介和适用场景
- `DEEPSEEK_MODEL`: 可选，DeepSeek 模型，默认 `deepseek-v4-flash`
- `DEEPSEEK_TIMEOUT`: 可选，DeepSeek 请求超时时间，默认 45 秒
- `TRENDING_LANGUAGE`: GitHub Trending 语言，例如 `python`、`typescript`、`go`
- `TRENDING_SINCE`: `daily`、`weekly` 或 `monthly`
- `TRENDING_LIMIT`: 发送项目数量
- `REPORT_DIR`: Markdown 报告目录，默认 `data/reports`
- `SNAPSHOT_DIR`: JSON 快照目录，默认 `data/snapshots`

如果不配置 `DEEPSEEK_API_KEY`，程序会使用本地规则生成中文内容；如果 DeepSeek 调用失败，也会自动回退到本地规则，避免影响飞书推送。
