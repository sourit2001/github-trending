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
- 内容：仓库基础信息、README 深度解读、解决的问题、核心能力、适用人群、使用方式和注意事项

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
- `GITHUB_TOKEN`: 可选，用于提高 GitHub README 和仓库元数据 API 的请求额度；GitHub Actions 会自动使用当前工作流令牌
- `TRENDING_LANGUAGE`: GitHub Trending 语言，例如 `python`、`typescript`、`go`
- `TRENDING_SINCE`: `daily`、`weekly` 或 `monthly`
- `TRENDING_LIMIT`: 发送项目数量
- `REPORT_DIR`: Markdown 报告目录，默认 `data/reports`
- `SNAPSHOT_DIR`: JSON 快照目录，默认 `data/snapshots`
- `REPORT_TIMEZONE`: 报告文件名使用的日期时区，默认 `Asia/Shanghai`

如果不配置 `DEEPSEEK_API_KEY`，程序会使用本地规则生成中文内容；如果 DeepSeek 调用失败，也会自动回退到本地规则，避免影响飞书推送。

生成解读时，程序会读取公开仓库的 README、Topics、License 和最近更新时间。每个仓库的解读约 150-250 个汉字，包括：

- 它是什么
- 解决的问题
- 核心能力
- 适合谁
- 典型场景
- 如何开始
- 阅读提示

README 只作为当次分析输入，不会完整写入每日 JSON 快照。README 获取失败时，程序会根据 Trending 页面的一行描述生成简化版内容。

## 同步到 Obsidian

GitHub Actions 运行在 GitHub 的服务器上，不能直接写入你 Mac 本地的 iCloud Drive。当前流程会把报告提交到本仓库：

- Markdown 报告：`data/reports/YYYY-MM-DD.md`
- JSON 快照：`data/snapshots/YYYY-MM-DD.json`

如果 Mac mini 上已有拉取程序，需要让它拉取本仓库的最新提交，再把 `data/reports/YYYY-MM-DD.md` 复制或同步到 Obsidian 的 iCloud vault 目录。

注意当前 workflow 是先生成报告并发送飞书，然后才执行自动提交。看到飞书消息时，GitHub 上的报告提交可能还在后续步骤中，Mac mini 如果立即拉取可能会拉不到当天文件。建议拉取程序延迟 1-2 分钟，或按固定间隔轮询 GitHub 仓库更新。

workflow 默认在 `18:12 UTC` 运行，也就是北京时间第二天 `02:12`。报告文件名默认按 `Asia/Shanghai` 生成，例如北京时间 7 月 28 日运行会写入 `data/reports/2026-07-28.md`。
