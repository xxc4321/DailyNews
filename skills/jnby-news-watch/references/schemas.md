# Structured contracts

## `digest --json`

顶层字段：

- `schema_version`: 当前为 `1`。
- `report_id`, `generated_at`, `window_since`, `window_until`。
- `news_limit`, `review_limit`, `delivery_eligible`。
- `warnings[]`, `health[]`, `cost{}`。
- `news[]`, `customer_voice[]`, `candidates[]`, `feishu_pages[]`。

`news[]` 包含 `cluster_id`, `content_hash`, `title`, `original_url`, `published_at`, `language`, `publisher`, `score`, `evidence_grade`, `summary_zh`, `score_explanation`, `score_contributions{}`, `independent_sources`, `locations[]`, `source_urls[]`, `gate_reason`, `already_sent_today`。

`customer_voice[]` 包含 `cluster_id`, `label`, `score`, `confidence`, `summary_zh`, `action_hint`, `independent_reviews`, `platforms[]`, `representative_urls[]`。

## Focus YAML

```yaml
id: focus-...
label: 巴黎新店
terms: [Paris, 巴黎, 线下开店, 物流, 关税]
valid_from: 2026-08-20T08:00:00+08:00
valid_until: 2026-09-19T08:00:00+08:00
decay_days: 7
strength: 100
status: proposed   # proposed | approved
notes: ""
```

## Source YAML

每个来源至少包含 `id`, `name`, `channel`, `adapter`, `tier`, `approved`。直接抓取来源还需要 `url`；需要凭据的适配器只从环境变量读取凭据。`channel` 为 `news / discovery / review`，`tier` 为 `S0 / S1 / S2 / V`。

外部 Agent 可实现同名 `SourceAdapter.collect(source, queries, time_window) -> SourceResult` 并注入 `Pipeline`，无需修改可信门、评分或渲染层。
