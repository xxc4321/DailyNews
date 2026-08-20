# JNBY News Watch V2 Ranking and Regions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace broad keyword scoring with auditable industry relevance and return trustworthy news preferentially from JNBY overseas-layout markets plus Europe, France/Paris, Japan, and Southeast Asia.

**Architecture:** Add a focused `regions.py` boundary for market aliases, extraction, and bounded query lanes; keep evidence gating separate from relevance scoring; apply publisher/region constraints only after evidence, industry, and minimum-score gates. Split news and Customer Voice collection windows and expose review-source diagnostics without relaxing access controls.

**Tech Stack:** Python 3.11+, dataclasses, PyYAML, feedparser, SQLite, pytest, Hermes 0.18.0, DeepSeek API.

## Global Constraints

- Do not change Hermes cron ID `dff593d9afe9`, schedule `0 8 * * *`, Feishu target, or any unrelated cron.
- Do not print, copy, or commit API keys, Feishu secrets, chat IDs, runtime databases, generated reports, or author salts.
- Search/discovery evidence remains S2 until a trusted S0/S1 original is opened and verified.
- Region preference never overrides evidence, industry relevance, sponsored-content, or minimum-score gates.
- New S1 domains require Joe's explicit source approval before entering `trusted_domains` or a formal direct-source adapter.
- Customer Voice does not bypass login, CAPTCHA, robots, private accounts, or platform access control.
- When qualified content is insufficient, output fewer than the requested 10 news or 5 review themes and state the gap.

---

### Task 1: Market model and bounded regional query lanes

**Files:**
- Create: `skills/jnby-news-watch/scripts/jnby_news_watch/regions.py`
- Modify: `skills/jnby-news-watch/scripts/jnby_news_watch/pipeline.py`
- Modify: `skills/jnby-news-watch/scripts/jnby_news_watch/sources.py`
- Modify: `skills/jnby-news-watch/assets/default-profile.yaml`
- Test: `tests/test_regions.py`

**Interfaces:**
- Produces: `MarketMatch(countries, cities, groups, priority_labels)`.
- Produces: `extract_market_match(item: RawCandidate, profile: dict) -> MarketMatch`.
- Produces: `build_regional_queries(profile: dict, focuses: list[FocusRecord]) -> list[Query]` with a hard `max_discovery_queries` cap.
- Consumes: `RawCandidate`, `Query`, `FocusRecord`, and profile `markets`/`regional_search` mappings.

- [ ] **Step 1: Write failing market-normalization tests**

```python
def test_extracts_japan_and_southeast_asia_groups():
    japan = candidate("Tokyo fashion retailer opens a store in Japan")
    sea = candidate("Apparel logistics expansion in Malaysia and Singapore")
    assert extract_market_match(japan, PROFILE).groups == ("Japan",)
    assert extract_market_match(sea, PROFILE).groups == ("Southeast Asia",)

def test_paris_maps_to_france_and_europe():
    result = extract_market_match(candidate("Paris fashion flagship opening"), PROFILE)
    assert result.cities == ("Paris",)
    assert result.countries == ("France",)
    assert result.groups == ("Europe",)
```

- [ ] **Step 2: Run tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_regions.py -q`

Expected: collection fails because `jnby_news_watch.regions` does not exist.

- [ ] **Step 3: Implement immutable market extraction**

```python
@dataclass(frozen=True)
class MarketMatch:
    countries: tuple[str, ...] = ()
    cities: tuple[str, ...] = ()
    groups: tuple[str, ...] = ()
    priority_labels: tuple[str, ...] = ()

def extract_market_match(item: RawCandidate, profile: dict) -> MarketMatch:
    fields = candidate_fields(item)
    aliases = profile["markets"]["aliases"]
    # Match normalized phrases with word boundaries, infer Paris -> France -> Europe,
    # and return stable config order with no invented location.
```

- [ ] **Step 4: Add failing bounded-query tests**

```python
def test_queries_cover_each_priority_lane_without_truncating_late_focus():
    focus = approved_focus(("Europe", "France", "Paris", "Japan", "Southeast Asia"))
    queries = build_regional_queries(PROFILE, [focus])
    joined = "\n".join(query.text for query in queries)
    assert "Paris" in joined
    assert "Japan" in joined or "日本" in joined
    assert "Southeast Asia" in joined or "Malaysia" in joined
    assert len(queries) <= PROFILE["regional_search"]["max_discovery_queries"]
```

- [ ] **Step 5: Implement query lanes and Japanese Google locale**

```python
def build_regional_queries(profile: dict, focuses: list[FocusRecord]) -> list[Query]:
    lanes = profile["regional_search"]["lanes"]
    queries = [Query(lane["query"], lane["language"]) for lane in lanes]
    return dedupe_queries(append_focus_terms(queries, focuses))[
        : int(profile["regional_search"]["max_discovery_queries"])
    ]

# GoogleNewsRssAdapter locale addition:
"ja": ("ja", "JP", "JP:ja"),
```

- [ ] **Step 6: Replace `pipeline._queries` and verify GREEN**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_regions.py tests/test_sources.py -q`

Expected: all selected tests pass; generated queries include Europe/Paris/Japan/Southeast Asia and never exceed 12.

- [ ] **Step 7: Commit**

```powershell
git add tests/test_regions.py skills/jnby-news-watch/scripts/jnby_news_watch/regions.py skills/jnby-news-watch/scripts/jnby_news_watch/pipeline.py skills/jnby-news-watch/scripts/jnby_news_watch/sources.py skills/jnby-news-watch/assets/default-profile.yaml
git commit -m "feat: add bounded regional discovery lanes"
```

### Task 2: Exact relevance scoring and industry hard gate

**Files:**
- Modify: `skills/jnby-news-watch/scripts/jnby_news_watch/score.py`
- Modify: `skills/jnby-news-watch/scripts/jnby_news_watch/pipeline.py`
- Modify: `skills/jnby-news-watch/assets/default-profile.yaml`
- Modify: `skills/jnby-news-watch/references/scoring.md`
- Test: `tests/test_scoring_focus.py`
- Test: `tests/test_pipeline_render.py`

**Interfaces:**
- Produces: `DomainDecision(eligible: bool, reasons: tuple[str, ...])`.
- Produces: `evaluate_domain_fit(item, profile, focuses, at) -> DomainDecision`.
- Updates: `score_news(...) -> ScoreResult` while preserving the existing public signature.
- Consumes: `extract_market_match` from Task 1.

- [ ] **Step 1: Write failing exact-match regression tests**

```python
def test_common_less_does_not_match_less_brand():
    item = candidate("Retailers expect less demand this quarter")
    result = score_news(item, PROFILE, [], at=NOW)
    assert result.contributions["brand_industry"] == 0

def test_generic_design_data_and_competitor_do_not_earn_brand_points():
    item = candidate("Game studio design uses customer data to beat a competitor")
    result = score_news(item, PROFILE, [], at=NOW)
    assert result.contributions["brand_industry"] == 0
```

- [ ] **Step 2: Run tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_scoring_focus.py -q`

Expected: assertions fail because current substring matching awards broad category points.

- [ ] **Step 3: Implement field-aware phrase matching and new score caps**

```python
FIELD_MULTIPLIERS = {"title": 1.0, "entities": 1.0, "summary": 0.6, "body": 0.25}
SCORE_CAPS = {
    "brand_industry": 35.0,
    "role_operations": 15.0,
    "supply_chain": 10.0,
    "market": 15.0,
    "focus": 15.0,
    "freshness_impact": 10.0,
}

def phrase_present(text: str, phrase: str, *, case_sensitive: bool = False) -> bool:
    # English uses Unicode token boundaries; Chinese/multi-word phrases use normalized phrase matching.
```

- [ ] **Step 4: Write failing industry-gate tests**

```python
@pytest.mark.parametrize("title", [
    "Mattel Game Studios expands virtual play",
    "Home Depot DIY demand falls",
    "Aerospace supplier removes production bottleneck",
])
def test_generic_cross_industry_news_fails_domain_gate(title):
    assert evaluate_domain_fit(candidate(title), PROFILE, [], NOW).eligible is False

def test_fashion_store_and_apparel_logistics_pass_domain_gate():
    assert evaluate_domain_fit(candidate("Nike fashion store opens in Paris"), PROFILE, [], NOW).eligible
    assert evaluate_domain_fit(candidate("Apparel inventory and logistics change in Malaysia"), PROFILE, [], NOW).eligible
```

- [ ] **Step 5: Implement the hard gate and pipeline candidate reason**

```python
decision = evaluate_domain_fit(cluster.primary, self.config.profile, focuses, now)
if gate.eligible and not decision.eligible:
    gate = GateDecision(False, "candidate", gate.evidence_grade, gate.reasons + decision.reasons)
```

- [ ] **Step 6: Set `minimum_news_score: 35` and verify GREEN**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_scoring_focus.py tests/test_pipeline_render.py -q`

Expected: exact-match, hard-gate, focus, and pipeline tests pass; Mattel/Home Depot DIY fixtures remain candidates.

- [ ] **Step 7: Commit**

```powershell
git add tests/test_scoring_focus.py tests/test_pipeline_render.py skills/jnby-news-watch/scripts/jnby_news_watch/score.py skills/jnby-news-watch/scripts/jnby_news_watch/pipeline.py skills/jnby-news-watch/assets/default-profile.yaml skills/jnby-news-watch/references/scoring.md
git commit -m "feat: enforce exact industry relevance scoring"
```

### Task 3: Sponsored-content downgrade and diverse regional selection

**Files:**
- Create: `skills/jnby-news-watch/scripts/jnby_news_watch/ranking.py`
- Modify: `skills/jnby-news-watch/scripts/jnby_news_watch/gates.py`
- Modify: `skills/jnby-news-watch/scripts/jnby_news_watch/sources.py`
- Modify: `skills/jnby-news-watch/scripts/jnby_news_watch/pipeline.py`
- Test: `tests/test_ranking.py`
- Test: `tests/test_normalize_dedupe_gates.py`
- Test: `tests/test_sources.py`

**Interfaces:**
- Produces: `SelectionResult[T](selected: tuple[T, ...], deferred: tuple[tuple[T, str], ...])`.
- Produces: `select_ranked(records, limit, publisher_key, priority_key, max_per_publisher, priority_share)`.
- Adds `metadata["sponsored"]: bool` to RSS candidates.

- [ ] **Step 1: Write failing sponsored-content tests**

```python
def test_sponsored_url_is_candidate_even_from_s1():
    cluster = trusted_cluster(url="https://retail.example/spons/vendor-story")
    decision = evaluate_news_gate(cluster, GatePolicy(allow_sponsored_formal=False))
    assert decision.eligible is False
    assert "Sponsored" in " ".join(decision.reasons)
```

- [ ] **Step 2: Verify RED and implement sponsored detection**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_normalize_dedupe_gates.py tests/test_sources.py -q`

Expected before implementation: sponsored S1 content is formal.

Implementation:

```python
def _is_sponsored_url(url: str) -> bool:
    return any(marker in urlsplit(url).path.casefold() for marker in ("/spons/", "/sponsored/"))
```

- [ ] **Step 3: Write failing publisher-cap and region-share tests**

```python
def test_selection_caps_publishers_and_reaches_region_share_when_available():
    records = ranked_fixture(priority=8, other=5, dominant_publisher=8)
    result = select_ranked(records, limit=10, publisher_key=publisher, priority_key=is_priority,
                           max_per_publisher=3, priority_share=0.6)
    assert max(Counter(publisher(x) for x in result.selected).values()) <= 3
    assert sum(is_priority(x) for x in result.selected) >= 6
```

- [ ] **Step 4: Implement stable selection and pipeline integration**

```python
target_priority = min(len(priority_records), math.ceil(limit * priority_share))
# Select score-ordered priority records first subject to publisher cap,
# then fill with all remaining qualified records subject to the same cap.
```

- [ ] **Step 5: Verify GREEN**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_ranking.py tests/test_normalize_dedupe_gates.py tests/test_sources.py tests/test_pipeline_render.py -q`

Expected: all selected tests pass; deferred items carry sponsored or publisher-diversity reasons.

- [ ] **Step 6: Commit**

```powershell
git add tests/test_ranking.py tests/test_normalize_dedupe_gates.py tests/test_sources.py tests/test_pipeline_render.py skills/jnby-news-watch/scripts/jnby_news_watch/ranking.py skills/jnby-news-watch/scripts/jnby_news_watch/gates.py skills/jnby-news-watch/scripts/jnby_news_watch/sources.py skills/jnby-news-watch/scripts/jnby_news_watch/pipeline.py
git commit -m "feat: diversify regional ranking and demote sponsored news"
```

### Task 4: Separate Customer Voice window and expose channel diagnostics

**Files:**
- Modify: `skills/jnby-news-watch/scripts/jnby_news_watch/pipeline.py`
- Modify: `skills/jnby-news-watch/scripts/jnby_news_watch/render.py`
- Modify: `skills/jnby-news-watch/assets/default-profile.yaml`
- Modify: `skills/jnby-news-watch/references/schemas.md`
- Test: `tests/test_pipeline_render.py`
- Test: `tests/test_acceptance_offline.py`

**Interfaces:**
- Adds `DigestResult.review_diagnostics: dict` with window, raw, rejected, eligible, themes, and source-status counts.
- Adds additive JSON field `review_diagnostics`; existing fields remain unchanged.
- Uses `review_window_days: 30` while news remains at the request/72-hour window.

- [ ] **Step 1: Write failing dual-window integration test**

```python
def test_news_and_review_sources_receive_different_windows(tmp_path):
    recorder = WindowRecordingAdapter()
    pipeline = make_pipeline(tmp_path, adapter=recorder)
    pipeline.run(RunRequest(mode="manual", since=NOW - timedelta(hours=72), until=NOW))
    assert recorder.windows["news"].since == NOW - timedelta(hours=72)
    assert recorder.windows["review"].since == NOW - timedelta(days=30)
```

- [ ] **Step 2: Run test and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_pipeline_render.py::test_news_and_review_sources_receive_different_windows -q`

Expected: both source types currently receive the same window.

- [ ] **Step 3: Split collection batches and combine health**

```python
news_sources = [s for s in sources if s.get("channel") != "review"]
review_sources = [s for s in sources if s.get("channel") == "review"]
review_window = TimeWindow(window.until - timedelta(days=review_window_days), window.until)
news_batch = collect_all(news_sources, adapters, queries, window)
review_batch = collect_all(review_sources, adapters, queries, review_window)
```

- [ ] **Step 4: Write failing zero/rejected/theme diagnostic tests**

```python
def test_zero_raw_reviews_reports_source_failure():
    result = run_with_review_result(raw=(), health="failed")
    assert result.review_diagnostics["raw_reviews"] == 0
    assert any("Customer Voice" in warning for warning in result.warnings)

def test_rejected_reviews_are_distinguished_from_zero_collection():
    result = run_with_spam_reviews(3)
    assert result.review_diagnostics["raw_reviews"] == 3
    assert result.review_diagnostics["rejected_reviews"] == 3
```

- [ ] **Step 5: Implement diagnostics and rendering**

```python
review_diagnostics = {
    "window_since": review_window.since.isoformat(),
    "window_until": review_window.until.isoformat(),
    "raw_reviews": len(review_batch.reviews),
    "rejected_reviews": rejected_count,
    "eligible_reviews": len(eligible_reviews),
    "themes": len(review_scored),
    "sources": review_source_status_counts,
}
```

- [ ] **Step 6: Verify GREEN and backward-compatible JSON**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_pipeline_render.py tests/test_acceptance_offline.py -q`

Expected: all selected tests pass; overview explains zero Customer Voice and JSON retains existing keys.

- [ ] **Step 7: Commit**

```powershell
git add tests/test_pipeline_render.py tests/test_acceptance_offline.py skills/jnby-news-watch/scripts/jnby_news_watch/pipeline.py skills/jnby-news-watch/scripts/jnby_news_watch/render.py skills/jnby-news-watch/assets/default-profile.yaml skills/jnby-news-watch/references/schemas.md
git commit -m "feat: add customer voice rolling window diagnostics"
```

### Task 5: Regional source research and approval gate

**Files:**
- Create: `docs/research/2026-08-20-regional-source-candidates.md`
- Modify after approval only: `skills/jnby-news-watch/assets/default-sources.yaml`
- Test after approval only: `tests/test_sources.py`

**Interfaces:**
- Produces a source-candidate table with publisher, region, languages, official feed/API URL, ownership/editorial transparency, access result, proposed tier, and reason.
- Does not promote any candidate to S1 without Joe's explicit approval.

- [ ] **Step 1: Research official/primary feeds for each region**

Use official publisher pages and feeds only. Cover at least:

- Europe/France fashion retail;
- Japan fashion retail;
- Southeast Asia retail/fashion;
- an alternative for each failed or blocked candidate.

- [ ] **Step 2: Live-check feed accessibility without bypasses**

For each candidate record HTTP status, content type, most recent item date, direct-link behavior, language, and whether full original pages are publicly reachable. Do not use browser impersonation, login, CAPTCHA solving, mirrors, or scraped credential services.

- [ ] **Step 3: Write the candidate report and request Joe approval**

Expected report columns:

```markdown
| Publisher | Region | Language | Official feed/API | Access | Freshness | Ownership/editorial notes | Proposed tier | Recommendation |
```

- [ ] **Step 4: After approval, add only selected sources and tests**

```python
def test_approved_regional_sources_have_direct_https_urls_and_known_domains():
    for source in regional_sources(CONFIG):
        assert source["approved"] is True
        assert source["tier"] in {"S0", "S1"}
        assert source["url"].startswith("https://")
```

- [ ] **Step 5: Commit the research separately from approved configuration**

```powershell
git add docs/research/2026-08-20-regional-source-candidates.md
git commit -m "docs: evaluate regional fashion news sources"
```

### Task 6: Runtime migration, complete verification, and real regional comparison

**Files:**
- Modify: `README.md`
- Modify: `skills/jnby-news-watch/references/scoring.md`
- Modify: `skills/jnby-news-watch/references/source-policy.md`
- Modify: `docs/verification/2026-08-20-acceptance.md`
- Runtime-only modify with `apply_patch`: `C:\Users\Joe\AppData\Local\hermes\runtime\jnby-news-watch\config\profile.yaml`

**Interfaces:**
- Preserves Hermes cron and Feishu delivery configuration.
- Produces before/after evidence from baseline report `038bfe1f8864cab7ca436b92` and a new dry-run report.

- [ ] **Step 1: Add new profile keys to Hermes runtime without overwriting user state**

```yaml
minimum_news_score: 35
max_items_per_publisher: 3
review_window_days: 30
allow_sponsored_formal: false
max_discovery_queries: 12
priority_region_share: 0.6
```

- [ ] **Step 2: Run the complete offline suite**

Run: `.\.venv\Scripts\python.exe -m pytest -q`

Expected: zero failures.

- [ ] **Step 3: Validate Skill structure and compile Python**

Run:

```powershell
.\.venv\Scripts\python.exe C:\Users\Joe\.codex\skills\.system\skill-creator\scripts\quick_validate.py skills\jnby-news-watch
.\.venv\Scripts\python.exe -m compileall -q skills\jnby-news-watch\scripts scripts
```

Expected: `Skill is valid!` and compile exit code 0.

- [ ] **Step 4: Run real regional dry-run without Feishu delivery**

Run the CLI with `--news 10 --reviews 5 --since 72h` and temporary focus terms `Europe`, `France`, `Paris`, `Japan`, `Southeast Asia`, using credentials only from the existing Hermes environment. Do not print the key and do not invoke `hermes send`.

- [ ] **Step 5: Audit the real report requirement by requirement**

Verify and record:

- every formal item has a direct original HTTPS link and A/B evidence;
- Mattel gaming, Home Depot DIY, Lowe's DIY, and sponsored EDI are absent from formal news;
- no publisher exceeds three formal items;
- each formal item has explicit country/city/region metadata when regionally classified;
- priority/overseas-layout share reaches 60% when at least six qualified regional candidates exist, otherwise the report explicitly shows the shortage;
- Customer Voice diagnostics explain raw/rejected/eligible/theme counts and Bluesky/authorized-source status;
- actual DeepSeek usage and estimated cost are recorded;
- no Feishu message was sent.

- [ ] **Step 6: Run installer dry-run and cron read-only checks**

Run:

```powershell
.\scripts\install_hermes.ps1 -WhatIf
C:\Users\Joe\AppData\Local\hermes\hermes-agent\venv\Scripts\hermes.exe cron list
```

Expected: JNBY install actions are `noop`; exactly one `JNBY Daily Intelligence` job remains at `0 8 * * *`; other jobs are unchanged.

- [ ] **Step 7: Update verification docs, run secret scan, and commit**

Run the repository secret-presence comparison without printing secret values, then:

```powershell
git add README.md skills/jnby-news-watch/references/scoring.md skills/jnby-news-watch/references/source-policy.md docs/verification/2026-08-20-acceptance.md
git commit -m "docs: record v2 regional ranking verification"
git push origin main
```

- [ ] **Step 8: Propose knowledge-base deposition**

Propose updates to the existing `01_PROJECTS/jnby_news_watch/04_决策记录.md`, `05_开发日志.md`, and `10_下一步行动.md`; wait for Joe's confirmation before writing.
