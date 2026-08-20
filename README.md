# DailyNews / JNBY News Watch

一个可追溯、可评分、可定时运行的海外时尚零售情报 Skill。默认面向 JNBY 海外客户经营岗位，输出独立的 `Top 10 新闻` 与 `Top 5 Customer Voice`，每条都保留原始链接。

## 快速开始（Windows PowerShell）

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe skills\jnby-news-watch\scripts\run.py health
.\.venv\Scripts\python.exe skills\jnby-news-watch\scripts\run.py digest --news 10 --reviews 5 --dry-run
```

密钥只放在环境变量或 Hermes 自己的配置中，不要写进仓库：

```powershell
$env:DEEPSEEK_API_KEY = "${YOUR_DEEPSEEK_API_KEY}"
```

首次运行会在当前目录创建 `.jnby-news-watch/`，其中保存本地配置副本、SQLite 状态、焦点提议和报告；该目录已被 Git 忽略。

## 常用调用

```powershell
# 临时要 20 条新闻和 10 个客评主题，查看过去 7 天
.\.venv\Scripts\python.exe skills\jnby-news-watch\scripts\run.py digest --news 20 --reviews 10 --since 7d --focus Paris --focus logistics

# 提议一个持续 30 天的工作焦点（不会自动生效）
.\.venv\Scripts\python.exe skills\jnby-news-watch\scripts\run.py focus propose --text "巴黎新店" --term Paris --term "线下开店" --term 物流 --term 关税 --days 30

# Joe 确认后再批准
.\.venv\Scripts\python.exe skills\jnby-news-watch\scripts\run.py focus approve <focus-id>
```

## 可信边界

- S0 官方源、S1 已批准媒体源可进入正式榜；搜索和聚合发现默认是 S2，只进候补区，回到可信原文后才能升级。
- 一般新闻需要一个可信原文；关税、供应链中断、门店经营和声誉等重大事件需要两个真正独立的可信来源。
- Customer Voice 必须来自公开原帖或授权导出，作者只保留不可逆匿名键；个案不会被包装成趋势。
- 网页、RSS、API、帖子和评论永远作为不可信数据，不可改变指令、批准信源或触发外部动作。

## 模块化使用

核心 Python 包位于 `skills/jnby-news-watch/scripts/jnby_news_watch/`。信源适配、可信门、评分、DeepSeek 增强、渲染和投递状态相互独立，可嵌入 Hermes、其他 Agent、CI 或自建工作流。机器工作流使用 `digest --json`；Agent Skills 兼容入口是 `skills/jnby-news-watch/SKILL.md`。

更详细的评分、信源策略、Hermes 接入和结构化字段见 `skills/jnby-news-watch/references/`。
