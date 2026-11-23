import json
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import feedparser
import httpx
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from tenacity import retry, stop_after_attempt, wait_exponential

from src.config_loader import AppConfig

logger = logging.getLogger(__name__)


class TrendCandidate(BaseModel):
    """収集された急上昇トレンド候補"""

    keyword: str = Field(..., description="トレンドキーワードまたはトピック名")
    source_name: str = Field(..., description="取得元フィード名")
    category: str = Field(default="general", description="カテゴリ")
    approx_traffic: str = Field(default="", description="推定検索ボリューム等")
    related_headlines: List[str] = Field(
        default_factory=list, description="関連ニュース見出し・背景サマリ"
    )
    source_urls: List[str] = Field(
        default_factory=list, description="参照元URLリスト"
    )


class SelectedTrendResult(BaseModel):
    """LLMによる収益化安全性＆考察適性スコアリング選定結果"""

    selected_keyword: str = Field(..., description="選定された最適トレンドキーワード")
    topic_category: str = Field(
        ...,
        description="テーマ分類: technology / business / society / culture / default のいずれか",
    )
    monetization_safety_score: int = Field(
        ..., description="YPP収益化安全性スコア (0〜100)"
    )
    insight_depth_score: int = Field(
        ..., description="独自考察・深掘り適性スコア (0〜100)"
    )
    selection_reason: str = Field(..., description="選定理由および考察の切り口")
    factual_summary: str = Field(..., description="報道・トレンドの客観的事実の要約")
    key_analytical_angles: List[str] = Field(
        ..., description="深掘り考察パートで扱うべき3つの構造分析・将来予測の視点"
    )
    reference_sources: List[str] = Field(
        default_factory=list, description="概要欄に記載する主な情報ソース名"
    )


class TrendFetcher:
    """日本の急上昇トレンドをマルチソースRSSから自動収集し、過去履歴と照合して最適テーマを選定する"""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.trends_cfg = config.settings.get("trends", {})

    def load_past_topics(self) -> List[str]:
        """data/history.json から過去指定日数以内に投稿済みのトピック一覧を取得"""
        history_path: Path = self.config.history_path
        if not history_path.exists():
            return []

        try:
            data = json.loads(history_path.read_text(encoding="utf-8"))
            published = data.get("published_videos", [])
            lookback_days = int(self.trends_cfg.get("history_lookback_days", 60))
            cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)

            topics: List[str] = []
            for item in published:
                kw = str(item.get("topic_keyword", "")).strip()
                title = str(item.get("title", "")).strip()
                pub_at_str = item.get("published_at")
                if pub_at_str:
                    try:
                        pub_dt = datetime.fromisoformat(
                            pub_at_str.replace("Z", "+00:00")
                        )
                        if pub_dt < cutoff:
                            continue
                    except ValueError:
                        pass
                if kw:
                    topics.append(kw)
                if title:
                    topics.append(title)
            return topics
        except Exception as e:
            logger.warning("投稿履歴の読み込みに失敗しました（新規扱い）: %s", e)
            return []

    @staticmethod
    def _normalize_text(text: str) -> str:
        return re.sub(r"[\s　・、。！？!?\-\(\)（）【】「」『』]", "", text.lower())

    def _is_duplicate(self, keyword: str, past_topics: List[str]) -> bool:
        norm_kw = self._normalize_text(keyword)
        if not norm_kw:
            return True
        for past in past_topics:
            norm_past = self._normalize_text(past)
            if not norm_past:
                continue
            if norm_kw == norm_past or (
                len(norm_kw) >= 4 and (norm_kw in norm_past or norm_past in norm_kw)
            ):
                return True
        return False

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=6))
    def _fetch_rss_xml(self, url: str) -> str:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        }
        with httpx.Client(timeout=15.0, follow_redirects=True, headers=headers) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.text

    def _parse_google_trends_rss(self, xml_text: str, source_name: str) -> List[TrendCandidate]:
        """Google Trends 急上昇RSSの独自名前空間タグ (ht:approx_traffic, ht:news_item) を解析"""
        candidates: List[TrendCandidate] = []
        try:
            root = ET.fromstring(xml_text)
            ns = {"ht": "https://trends.google.co.jp/trending/rss"}
            for item in root.findall(".//item"):
                title_el = item.find("title")
                if title_el is None or not title_el.text:
                    continue
                keyword = title_el.text.strip()

                traffic_el = item.find("ht:approx_traffic", ns)
                traffic = traffic_el.text.strip() if (traffic_el is not None and traffic_el.text) else ""

                headlines: List[str] = []
                urls: List[str] = []
                for news_item in item.findall("ht:news_item", ns):
                    nt = news_item.find("ht:news_item_title", ns)
                    nu = news_item.find("ht:news_item_url", ns)
                    nsrc = news_item.find("ht:news_item_source", ns)
                    if nt is not None and nt.text:
                        clean_title = re.sub(r"<[^>]+>", "", nt.text).strip()
                        src_label = f" ({nsrc.text.strip()})" if (nsrc is not None and nsrc.text) else ""
                        headlines.append(f"{clean_title}{src_label}")
                    if nu is not None and nu.text:
                        urls.append(nu.text.strip())

                candidates.append(
                    TrendCandidate(
                        keyword=keyword,
                        source_name=source_name,
                        category="trending",
                        approx_traffic=traffic,
                        related_headlines=headlines[:4],
                        source_urls=urls[:3],
                    )
                )
        except Exception as e:
            logger.warning("Google Trends XML解析フォールバック (%s): %s", source_name, e)
        return candidates

    def fetch_candidates(self, past_topics: List[str]) -> List[TrendCandidate]:
        """設定された複数RSSフィードから急上昇ワード・注目ニュースを収集し重複を除外"""
        feeds = self.trends_cfg.get("rss_feeds", [])
        raw_candidates: List[TrendCandidate] = []

        for feed_info in feeds:
            name = feed_info.get("name", "RSS Feed")
            url = feed_info.get("url", "")
            category = feed_info.get("category", "general")
            if not url:
                continue

            try:
                xml_text = self._fetch_rss_xml(url)
                if "trends.google" in url:
                    parsed_gt = self._parse_google_trends_rss(xml_text, name)
                    if parsed_gt:
                        raw_candidates.extend(parsed_gt)
                        continue

                feed = feedparser.parse(xml_text)
                for entry in feed.entries[:12]:
                    title = re.sub(r"<[^>]+>", "", entry.get("title", "")).strip()
                    summary = re.sub(r"<[^>]+>", "", entry.get("summary", "")).strip()
                    link = entry.get("link", "")
                    if not title:
                        continue
                    headlines = [title]
                    if summary and summary != title:
                        headlines.append(summary[:160])

                    raw_candidates.append(
                        TrendCandidate(
                            keyword=title,
                            source_name=name,
                            category=category,
                            related_headlines=headlines,
                            source_urls=[link] if link else [],
                        )
                    )
            except Exception as e:
                logger.warning("フィード取得スキップ [%s]: %s", name, e)

        # 重複排除と過去投稿トピックとの照合
        unique_candidates: List[TrendCandidate] = []
        seen_keywords: List[str] = []

        for cand in raw_candidates:
            if self._is_duplicate(cand.keyword, past_topics):
                logger.info("過去投稿済みのため除外: %s", cand.keyword)
                continue
            if self._is_duplicate(cand.keyword, seen_keywords):
                continue
            seen_keywords.append(cand.keyword)
            unique_candidates.append(cand)

        max_count = int(self.trends_cfg.get("max_candidates_to_score", 20))
        logger.info("収集された新規トレンド候補数: %d件", len(unique_candidates))
        return unique_candidates[:max_count]

    def select_best_trend(
        self, force_keyword: Optional[str] = None
    ) -> SelectedTrendResult:
        """
        急上昇トレンド候補を取得し、Gemini APIでYPP収益化安全性＆独自考察適性を評価して最適トピックを選定する。
        手動トピック指定 (force_keyword) がある場合も、そのテーマの背景分析軸を構造化して返す。
        """
        past_topics = self.load_past_topics()
        effective_force = (
            force_keyword or self.config.env.force_topic_keyword
        ).strip()

        if effective_force:
            logger.info("手動指定トピックを使用します: %s", effective_force)
            candidates = [
                TrendCandidate(
                    keyword=effective_force,
                    source_name="Manual Override (workflow_dispatch)",
                    category="general",
                    related_headlines=[f"注目トピック「{effective_force}」の最新動向と構造的背景"],
                )
            ]
        else:
            candidates = self.fetch_candidates(past_topics)
            if not candidates:
                logger.warning("外部RSSから新規候補が取得できなかったためフォールバックテーマを使用します")
                candidates = [
                    TrendCandidate(
                        keyword="生成AIエージェント普及による日本の働き方と産業構造の大転換",
                        source_name="Fallback Trend",
                        category="technology",
                        related_headlines=[
                            "企業での自律型AIエージェント導入が急加速、ホワイトカラー業務の再定義へ",
                            "生産性向上の一方で求められるスキル格差とガバナンス課題が浮き彫りに",
                        ],
                    )
                ]

        if not self.config.env.gemini_api_key:
            logger.warning("GEMINI_API_KEY 未設定のためヒューリスティック選定を使用します")
            top = candidates[0]
            return SelectedTrendResult(
                selected_keyword=top.keyword,
                topic_category="technology" if top.category == "technology" else "business",
                monetization_safety_score=95,
                insight_depth_score=92,
                selection_reason="社会的関心が高く、背景構造と将来予測を多角的に深掘りできるため",
                factual_summary=" / ".join(top.related_headlines) or f"{top.keyword}が日本国内で大きな注目を集めています。",
                key_analytical_angles=[
                    "なぜ今この現象・トレンドが急浮上したのか（構造的・歴史的背景）",
                    "業界・経済・私たちの生活に今後起きる3つのシナリオ予測",
                    "視聴者が知っておくべきメリット・デメリットと具体的な備え",
                ],
                reference_sources=[top.source_name, "Google Trends JP / 国内主要報道公開データ"],
            )

        return self._score_with_gemini(candidates, past_topics)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=15))
    def _score_with_gemini(
        self, candidates: List[TrendCandidate], past_topics: List[str]
    ) -> SelectedTrendResult:
        client = genai.Client(
            api_key=self.config.env.gemini_api_key,
            http_options=types.HttpOptions(timeout=45_000),
        )
        system_prompt = self.config.prompts.get("trend_selection_system_prompt", "")

        candidates_payload = [
            {
                "index": idx + 1,
                "keyword": c.keyword,
                "source": c.source_name,
                "category": c.category,
                "traffic": c.approx_traffic,
                "headlines": c.related_headlines,
            }
            for idx, c in enumerate(candidates)
        ]

        user_prompt = (
            f"【過去に投稿済みのトピック履歴（重複禁止）】\n"
            f"{json.dumps(past_topics[-30:], ensure_ascii=False)}\n\n"
            f"【本日の急上昇トレンド・ニュース候補リスト】\n"
            f"{json.dumps(candidates_payload, ensure_ascii=False, indent=2)}\n\n"
            "上記候補の中から最も収益化安全性が高く、8〜12分の掛け合い解説＆深掘り考察動画に最適な1件を選定し、"
            "指定のJSONスキーマで出力してください。"
        )

        import concurrent.futures

        models_to_try = []
        for m in [
            "gemini-3.6-flash",
            "gemini-3.5-flash",
            "gemini-3.5-flash-lite",
            "gemini-3.1-flash-lite",
            self.config.env.gemini_fast_model,
            self.config.env.gemini_model,
        ]:
            if m and m not in models_to_try:
                models_to_try.append(m)

        last_err: Exception | None = None
        response = None
        for model_name in models_to_try:
            ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            try:
                logger.info("トレンド選定LLM呼び出し中 (model=%s)...", model_name)
                fut = ex.submit(
                    client.models.generate_content,
                    model=model_name,
                    contents=user_prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        response_mime_type="application/json",
                        response_schema=SelectedTrendResult,
                        temperature=0.3,
                    ),
                )
                response = fut.result(timeout=45.0)
                break
            except Exception as e:
                logger.warning("モデル %s タイムアウトまたはスキップ・別モデルへ切替: %s", model_name, str(e).splitlines()[0][:120])
                last_err = e
            finally:
                ex.shutdown(wait=False, cancel_futures=True)
        if response is None:
            raise RuntimeError(f"利用可能なすべてのGeminiモデルで呼び出しに失敗しました: {last_err}")

        result = SelectedTrendResult.model_validate_json(response.text)
        if result.topic_category not in (
            "technology",
            "business",
            "society",
            "culture",
            "default",
        ):
            result.topic_category = "default"

        logger.info(
            "トレンド選定完了: 「%s」 (安全性:%d / 考察深さ:%d)",
            result.selected_keyword,
            result.monetization_safety_score,
            result.insight_depth_score,
        )
        return result
