from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from .cost import estimate_cost
from .dedupe import NewsCluster, cluster_news, cluster_reviews
from .deepseek import DeepSeekClient, EnrichmentResult, Usage
from .focus import FocusRecord
from .gates import GateDecision, GatePolicy, evaluate_news_gate, evaluate_review_gate
from .models import RunRequest, RuntimeConfig
from .render import (
    DigestNews,
    DigestResult,
    DigestVoice,
    render_feishu_pages,
    write_report_bundle,
)
from .score import ScoreResult, score_news, score_review_theme
from .sources import Query, SourceAdapter, TimeWindow, collect_all
from .state import StateStore


TARGET = "feishu-private"


def _focuses(payload: dict) -> list[FocusRecord]:
    return [
        FocusRecord.from_dict(item)
        for item in (payload or {}).get("focuses", [])
        if item.get("status") == "approved"
    ]


def _queries(profile: dict, focuses: list[FocusRecord]) -> list[Query]:
    base_queries = {
        "zh": (
            '"江南布衣" OR JNBY OR 速写 服装 零售',
            '欧洲 服装 零售 门店 物流',
            '服装 供应链 关税 库存',
        ),
        "en": (
            'JNBY OR "Jiangnan Buyi" OR "CROQUIS fashion" retail',
            'fashion retail store opening France Italy Europe',
            'apparel supply chain logistics tariff inventory Europe',
        ),
        "fr": (
            'JNBY OR "Jiangnan Buyi" mode magasin',
            'mode commerce ouverture magasin France Italie',
            'habillement logistique fournisseur droits de douane',
        ),
        "it": (
            'JNBY OR "Jiangnan Buyi" moda negozio',
            'moda retail apertura negozio Francia Italia',
            'abbigliamento logistica fornitori dazi doganali',
        ),
    }
    focus_terms = list(dict.fromkeys(term for focus in focuses for term in focus.terms))[:3]
    suffix = " " + " ".join(f'"{term}"' for term in focus_terms) if focus_terms else ""
    return [
        Query(text + suffix, language)
        for language in profile.get("languages", ["en"])
        for text in base_queries.get(language, base_queries["en"])
    ]


def _cluster_hash(cluster: NewsCluster) -> str:
    primary = cluster.primary
    payload = "\x1f".join(
        [
            primary.title,
            primary.summary,
            str(primary.metadata.get("body", "")),
            *sorted(f"{item.source_id}:{item.original_url}" for item in cluster.items),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _locations(cluster: NewsCluster) -> tuple[str, ...]:
    known = {
        "Paris",
        "France",
        "Italy",
        "China",
        "Germany",
        "Indonesia",
        "Lithuania",
        "Georgia",
        "Australia",
        "Malaysia",
    }
    values = {
        str(entity)
        for item in cluster.items
        for entity in item.metadata.get("entities", ())
        if str(entity) in known
    }
    return tuple(sorted(values))


class Pipeline:
    def __init__(
        self,
        config: RuntimeConfig,
        state: StateStore,
        adapters: dict[str, SourceAdapter],
        *,
        enricher=None,
        verifier=None,
        report_root: Path | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.config = config
        self.state = state
        self.adapters = adapters
        self.enricher = enricher or DeepSeekClient()
        self.verifier = verifier
        self.report_root = Path(report_root or config.home / "reports")
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _window(self, request: RunRequest, now: datetime) -> TimeWindow:
        until = request.until or now
        since = request.since
        if since is None:
            last = self.state.last_successful_delivery(TARGET)
            backfill = until - timedelta(hours=int(self.config.profile.get("backfill_hours", 72)))
            since = max(last, backfill) if last else backfill
        return TimeWindow(since, until)

    def _make_news(
        self,
        cluster: NewsCluster,
        decision: GateDecision,
        score: ScoreResult,
        summary: str,
        *,
        now: datetime,
    ) -> DigestNews:
        primary = cluster.primary
        content_hash = _cluster_hash(cluster)
        sent_at = self.state.cluster_delivery_time(TARGET, cluster.id, content_hash)
        local_tz = ZoneInfo(self.config.profile.get("timezone", "Asia/Shanghai"))
        already_today = bool(
            sent_at
            and sent_at.astimezone(local_tz).date() == now.astimezone(local_tz).date()
        )
        return DigestNews(
            cluster_id=cluster.id,
            content_hash=content_hash,
            title=primary.title,
            original_url=primary.original_url,
            published_at=primary.published_at,
            language=primary.language,
            publisher=primary.source_name,
            score=score.total,
            evidence_grade=decision.evidence_grade,
            summary_zh=summary or primary.summary or primary.title,
            score_explanation=score.explanation,
            score_contributions=score.contributions,
            independent_sources=cluster.trusted_independent_source_count,
            locations=_locations(cluster),
            source_urls=tuple(item.original_url for item in cluster.items),
            gate_reason="；".join(decision.reasons),
            already_sent_today=already_today,
        )

    def _enrich(self, records: list[dict], mode: str, now: datetime) -> tuple[dict, Usage, list[str]]:
        if not records:
            return {}, Usage(), []
        output: dict[str, dict] = {}
        usage = Usage()
        warnings: list[str] = []
        for offset in range(0, len(records), 50):
            result: EnrichmentResult = self.enricher.enrich(
                records[offset : offset + 50], mode, now=now
            )
            usage = usage + result.usage
            output.update({item["id"]: item for item in result.items})
            if result.used_fallback:
                if result.deferred_until:
                    warnings.append(
                        f"DeepSeek 预算模式已延后至 {result.deferred_until.isoformat()}，当前使用确定性摘要"
                    )
                else:
                    warnings.append("DeepSeek 语义增强不可用，当前使用确定性摘要")
        return output, usage, warnings

    def run(self, request: RunRequest) -> DigestResult:
        request.validate()
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError("pipeline clock must return a timezone-aware timestamp")
        config_version = hashlib.sha256(
            repr((self.config.profile, self.config.sources, self.config.focus)).encode()
        ).hexdigest()[:16]
        run_id = self.state.begin_run(request, config_version)
        try:
            window = self._window(request, now)
            focuses = _focuses(self.config.focus)
            if request.focus_terms:
                focuses.append(
                    FocusRecord(
                        id="ephemeral-"
                        + hashlib.sha256("|".join(request.focus_terms).encode()).hexdigest()[:12],
                        label="本次临时焦点",
                        terms=request.focus_terms,
                        valid_from=window.since,
                        valid_until=window.until + timedelta(seconds=1),
                        decay_days=0,
                        strength=100,
                        status="approved",
                    )
                )
            batch = collect_all(
                sources=self.config.sources.get("sources", []),
                adapters=self.adapters,
                queries=_queries(self.config.profile, focuses),
                time_window=window,
            )
            health: list[dict] = []
            for result in batch.results:
                if result.health:
                    payload = asdict(result.health)
                else:
                    payload = {
                        "source_id": result.source_id,
                        "status": "ok" if result.ok else "failed",
                        "latency_ms": 0,
                        "attempts": 1,
                        "error_code": "",
                        "message": result.error,
                    }
                health.append(payload)
                self.state.record_source_health(result.source_id, payload, now)

            warnings: list[str] = []
            source_responded = any(item["status"] in {"ok", "empty"} for item in health)
            if health and not source_responded:
                warnings.append("全部信源均失败或未配置，本次不把缓存内容标记为新消息")

            formal_scored: list[tuple[NewsCluster, GateDecision, ScoreResult]] = []
            candidate_scored: list[tuple[NewsCluster, GateDecision, ScoreResult]] = []
            candidate_items = list(batch.candidates)
            if self.verifier is not None:
                candidate_items = self.verifier.verify_batch(candidate_items, window)
            minimum_news_score = int(self.config.profile.get("minimum_news_score", 20))
            for cluster in cluster_news(candidate_items):
                decision = evaluate_news_gate(cluster, GatePolicy())
                score = score_news(cluster.primary, self.config.profile, focuses, at=now)
                if decision.eligible and score.total < minimum_news_score:
                    decision = GateDecision(
                        False,
                        "candidate",
                        decision.evidence_grade,
                        decision.reasons
                        + (f"相关度低于正式区阈值 {minimum_news_score}",),
                    )
                record = (cluster, decision, score)
                if decision.eligible:
                    if (
                        request.mode == "scheduled"
                        and self.state.cluster_delivery_time(TARGET, cluster.id, _cluster_hash(cluster))
                    ):
                        continue
                    formal_scored.append(record)
                else:
                    candidate_scored.append(record)
                for item in cluster.items:
                    self.state.upsert_item(
                        channel="news",
                        item_id=item.id,
                        payload={
                            "title": item.title,
                            "url": item.original_url,
                            "publisher": item.source_name,
                            "published_at": item.published_at.isoformat(),
                            "summary": item.summary[:1000],
                        },
                        cluster_id=cluster.id,
                        seen_at=now,
                    )
                self.state.upsert_cluster(
                    cluster_id=cluster.id,
                    channel="news",
                    payload={
                        "item_ids": [item.id for item in cluster.items],
                        "merge_reasons": list(cluster.merge_reasons),
                        "content_hash": _cluster_hash(cluster),
                    },
                    updated_at=now,
                )

            formal_scored.sort(
                key=lambda value: (
                    value[2].total,
                    {"A": 2, "B": 1, "C": 0}.get(value[1].evidence_grade, 0),
                    value[0].primary.published_at,
                ),
                reverse=True,
            )
            candidate_scored.sort(key=lambda value: value[2].total, reverse=True)
            selected_news = formal_scored[: request.news_limit]
            if len(selected_news) < request.news_limit:
                warnings.append(
                    f"可信新闻不足 {request.news_limit} 条，正式区仅输出 {len(selected_news)} 条，候补区单列"
                )

            eligible_reviews = [review for review in batch.reviews if evaluate_review_gate(review).eligible]
            review_scored = [
                (theme, score_review_theme(theme, self.config.profile))
                for theme in cluster_reviews(eligible_reviews)
            ]
            review_scored.sort(key=lambda value: value[1].total, reverse=True)
            selected_reviews = review_scored[: request.review_limit]
            for theme, _ in review_scored:
                for item in theme.items:
                    self.state.upsert_item(
                        channel="review",
                        item_id=item.id,
                        payload={
                            "platform": item.platform,
                            "url": item.original_url,
                            "published_at": item.published_at.isoformat(),
                            "author_key": item.author_key,
                            "text": item.text[:1000],
                        },
                        cluster_id=theme.id,
                        seen_at=now,
                    )
                self.state.upsert_cluster(
                    cluster_id=theme.id,
                    channel="review",
                    payload={
                        "item_ids": [item.id for item in theme.items],
                        "confidence": theme.confidence,
                        "theme_terms": list(theme.theme_terms),
                    },
                    updated_at=now,
                )

            enrichment_batch: list[dict] = []
            for cluster, _, score in selected_news:
                enrichment_batch.append(
                    {
                        "id": f"news:{cluster.id}",
                        "title": cluster.primary.title,
                        "excerpt": cluster.primary.summary or str(cluster.primary.metadata.get("body", ""))[:1500],
                        "deterministic_score": score.total,
                    }
                )
            for theme, score in selected_reviews:
                enrichment_batch.append(
                    {
                        "id": f"review:{theme.id}",
                        "title": " / ".join(theme.theme_terms[:4]) or "Customer Voice",
                        "excerpt": "\n".join(item.text[:400] for item in theme.items[:5]),
                        "deterministic_score": score.total,
                    }
                )
            enrichment, usage, enrichment_warnings = self._enrich(
                enrichment_batch, request.cost_mode, now
            )
            warnings.extend(enrichment_warnings)

            news = tuple(
                self._make_news(
                    cluster,
                    decision,
                    score,
                    enrichment.get(f"news:{cluster.id}", {}).get(
                        "summary_zh", cluster.primary.summary
                    ),
                    now=now,
                )
                for cluster, decision, score in selected_news
            )
            candidates = tuple(
                self._make_news(
                    cluster,
                    decision,
                    score,
                    cluster.primary.summary,
                    now=now,
                )
                for cluster, decision, score in candidate_scored[: max(10, request.news_limit)]
            )
            voices = tuple(
                DigestVoice(
                    cluster_id=theme.id,
                    label=" / ".join(theme.theme_terms[:4]) or "Customer Voice",
                    score=score.total,
                    confidence=theme.confidence,
                    summary_zh=enrichment.get(f"review:{theme.id}", {}).get(
                        "summary_zh", theme.items[0].text
                    ),
                    action_hint="结合门店培训、商品讲解或服务流程核查该信号，不将个案直接外推为趋势。",
                    independent_reviews=theme.independent_author_count,
                    platforms=tuple(sorted({item.platform for item in theme.items})),
                    representative_urls=tuple(item.original_url for item in theme.items[:3]),
                )
                for theme, score in selected_reviews
            )

            try:
                cost_result = estimate_cost(
                    self.config.pricing,
                    model=getattr(self.enricher, "model", "deepseek-v4-flash"),
                    cache_hit_tokens=usage.cache_hit_tokens,
                    cache_miss_tokens=usage.cache_miss_tokens,
                    output_tokens=usage.completion_tokens,
                    at=now,
                )
                cost = asdict(cost_result)
            except ValueError as exc:
                cost = {"error": str(exc)}

            report_seed = "\x1f".join(
                [
                    window.since.isoformat(),
                    window.until.isoformat(),
                    config_version,
                    *[item.content_hash for item in news],
                ]
            )
            report_id = hashlib.sha256(report_seed.encode()).hexdigest()[:24]
            local_now = now.astimezone(
                ZoneInfo(self.config.profile.get("timezone", "Asia/Shanghai"))
            )
            report_dir = self.report_root / local_now.date().isoformat() / report_id
            pages = render_feishu_pages(
                generated_at=now,
                window_since=window.since,
                window_until=window.until,
                news_limit=request.news_limit,
                review_limit=request.review_limit,
                news=news,
                voices=voices,
                candidates=candidates,
                warnings=tuple(dict.fromkeys(warnings)),
                health=tuple(health),
                cost=cost,
            )
            result = DigestResult(
                report_id=report_id,
                generated_at=now,
                window_since=window.since,
                window_until=window.until,
                news_limit=request.news_limit,
                review_limit=request.review_limit,
                news=news,
                customer_voice=voices,
                candidates=candidates,
                feishu_pages=pages,
                report_dir=report_dir,
                delivery_eligible=source_responded,
                warnings=tuple(dict.fromkeys(warnings)),
                health=tuple(health),
                cost=cost,
            )
            write_report_bundle(result)
            self.state.finish_run(
                run_id,
                "success",
                {
                    "report_id": report_id,
                    "news": len(news),
                    "reviews": len(voices),
                    "candidates": len(candidates),
                    "delivery_eligible": result.delivery_eligible,
                },
            )
            return result
        except Exception as exc:
            self.state.finish_run(run_id, "failed", {"error_type": type(exc).__name__})
            raise

    def reserve_delivery(self, result: DigestResult, target: str = TARGET) -> str | None:
        key = hashlib.sha256(f"{target}:{result.report_id}".encode()).hexdigest()
        return key if self.state.record_delivery(result.report_id, target, key) else None

    def confirm_delivery(
        self,
        result: DigestResult,
        idempotency_key: str,
        *,
        delivered_at: datetime | None = None,
        target: str = TARGET,
    ) -> None:
        timestamp = delivered_at or self.clock()
        self.state.mark_delivery_success(idempotency_key, timestamp)
        for item in result.news:
            self.state.mark_cluster_delivered(
                target, item.cluster_id, item.content_hash, timestamp
            )
