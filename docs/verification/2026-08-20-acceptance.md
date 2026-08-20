# JNBY News Watch 验收报告

- 验收日期：2026-08-20
- 运行环境：Windows，Asia/Shanghai，Hermes 0.18.0
- 交付仓库：`https://github.com/xxc4321/DailyNews`

## 结论

V1 的可追溯新闻、可解释评分、动态焦点、DeepSeek 语义增强、Hermes 定时和飞书私聊投递已联调。Customer Voice 的数据门已完成，但当前公开 Bluesky 搜索在本机网络返回 403，因此实时客评覆盖仍需要合法的平台 API 或 Joe 授权导出。系统对该失败关闭，未绕过平台限制。

## 自动化验证

| 项目 | 结果 |
|---|---|
| 完整离线测试 | `68 passed in 4.38s` |
| Skill 快速校验 | `Skill is valid!` |
| 安装器幂等预演 | Skill 链接、包装脚本、cron 全部 `noop` |
| 密钥泄漏检查 | 64 个版本候选文件中未匹配 Hermes 现有密钥，未发现 DeepSeek key 格式 |
| 投递失败/重试 | 失败不确认；Hermes 成功后回执对账通过 |

覆盖范围包括：10/5 与 20/10 数量、临时和持久焦点、焦点衰减/回滚、多语言归一、去重聚类、高影响双源门、评论操纵过滤、SSRF/提示注入/超大响应防护、DeepSeek 回退、SQLite 状态和投递幂等。

## 真实 API 与信源检查

- DeepSeek `deepseek-v4-flash` 两次小批量请求均通过严格 JSON 校验。
- 第一次：cache hit 0、miss 176、output 58，峰值估算 USD 0.000154。
- 第二次：cache hit 128、miss 48、output 58，峰值估算 USD 0.000099472。
- 72 小时真实不投递运行：正式新闻 10 条、Customer Voice 0 条、候补 10 条，正式新闻均为 HTTPS 原文链接且证据等级为 B；本次 DeepSeek 估算 USD 0.003163192。
- 8 个信源成功响应，GDELT 因本机 Python TLS 错误被隔离，Bluesky 因 HTTP 403 被隔离，授权客评导入在未配置 URL 时保持禁用。
- 单一来源的关税/高影响内容正确降入候补区；评分低于 20 的泛内容不进正式榜。

DeepSeek 官方峰值为 UTC 01:00–04:00 与 06:00–10:00，即北京时间 09:00–12:00 与 14:00–18:00；其余时段为半价低峰。早报 08:00 因此处于低峰。价格快照保存在 `assets/deepseek-pricing.yaml`。

## 飞书与 Hermes

- 已成功发送一条 `[TEST]` 连通消息和一份小型日报；本地仅保存回执 SHA-256，不保存聊天 ID 或密钥。
- 正式任务 ID：`dff593d9afe9`；名称：`JNBY Daily Intelligence`。
- 计划：`0 8 * * *`；启用；`no-agent`；投递到 Hermes 已配置的飞书 home channel。
- 最后人工触发状态：`ok`；待确认回执：0。
- 下次运行：`2026-08-21T08:00:00+08:00`。
- 现有其他 Hermes cron 未被删除或改写。

## 安装路径

- Skill 链接：`C:\Users\Joe\AppData\Local\hermes\skills\jnby-news-watch`
- Hermes 包装脚本：`C:\Users\Joe\AppData\Local\hermes\scripts\jnby-news-watch.py`
- 运行数据：`C:\Users\Joe\AppData\Local\hermes\runtime\jnby-news-watch`
- 本地验收证据：`.jnby-news-watch/reports/2026-08-20/`（Git 忽略）

## 已知限制与下一步

1. 新闻源已可稳定输出 10 条，但直接源当前偏英文零售/供应链；应继续补充可访问的法语、意大利语时尚媒体 RSS/API。
2. Bluesky 当前不可用，不建议降低安全阈门。推荐优先接入 Joe 有权使用的店铺评论导出，或为具有正式 API 权限的社交平台新增独立适配器。
3. 早报不会为了凑数将未过门的新闻或客评放入正式区；当 Customer Voice 不足 5 条时会明确少发。
