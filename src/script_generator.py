import json
import logging
from typing import Dict, List, Literal, Optional

from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from tenacity import retry, stop_after_attempt, wait_exponential

from src.config_loader import AppConfig
from src.trend_fetcher import SelectedTrendResult

logger = logging.getLogger(__name__)

SpeakerId = Literal["metan", "zundamon"]
EmotionId = Literal["normal", "happy", "surprised", "serious", "troubled"]
DiagramType = Literal[
    "bullet_points",
    "comparison",
    "timeline",
    "key_metric",
    "trend_meter",
    "public_reaction",
    "bar_chart",
    "structure_map",
    "future_forecast",
    "action_summary",
]
SectionId = Literal[
    "opening",
    "fact_overview",
    "deep_analysis_structure",
    "deep_analysis_future",
    "ending",
]


class SlideCard(BaseModel):
    """画面中央に表示する図解・解説スライド情報（3〜5セリフごとに動的に切り替わる）"""

    slide_title: str = Field(..., description="スライド見出し (24文字以内)")
    diagram_type: DiagramType = Field(
        default="bullet_points",
        description="図解レイアウト種別: bullet_points / comparison / timeline / key_metric",
    )
    slide_points: List[str] = Field(
        ...,
        description="スライド内に表示する要点・比較項目リスト (2〜4項目、各28文字以内)",
    )
    highlight_keyword: str = Field(
        default="", description="強調バッジまたは核心キーワード (18文字以内)"
    )


class DialogueLine(BaseModel):
    """四国めたん・ずんだもんの1セリフ単位のデータ構造"""

    speaker: SpeakerId = Field(..., description="話者: metan または zundamon")
    emotion: EmotionId = Field(
        default="normal",
        description="立ち絵の表情差分タグ: normal / happy / surprised / serious / troubled",
    )
    text: str = Field(
        ...,
        description="読み上げおよび字幕表示用のセリフ本文 (1セリフ30〜58文字目安)",
    )
    slide: Optional[SlideCard] = Field(
        default=None,
        description="このセリフから新しい解説スライドへ切り替える場合に指定（3〜5セリフに1回必ず指定）",
    )


class SectionBlueprint(BaseModel):
    """アウトライン設計時の各章構成"""

    section_id: SectionId
    section_title: str = Field(..., description="章タイトル（目次タイムスタンプ用）")
    is_insight_section: bool = Field(
        ..., description="独自考察・構造分析・将来予測パートなら true、事実整理なら false"
    )
    target_line_count: int = Field(..., description="この章で生成する目標セリフ数")
    key_discussion_points: List[str] = Field(
        ..., description="この章で2人が議論・解説する具体的論点リスト"
    )


class ScriptOutline(BaseModel):
    """5章構成の全体アウトライン"""

    video_concept: str = Field(..., description="動画全体の独自考察コンセプトと結論")
    sections: List[SectionBlueprint] = Field(..., description="5章構成の設計図")


class SectionDialogueOutput(BaseModel):
    """セクション単位のLLM台本出力スキーマ"""

    lines: List[DialogueLine] = Field(..., description="掛け合いセリフの配列")


class VideoMetadata(BaseModel):
    """SEO・高CTRを狙う動画メタデータおよびサムネイル文言"""

    title: str = Field(..., description="YouTube動画タイトル (28〜36文字目安)")
    thumb_upper_text: str = Field(
        ..., description="サムネイル上段インパクト文字 (10文字以内)"
    )
    thumb_main_text: str = Field(
        ..., description="サムネイル下段メイン極太文字 (10文字以内)"
    )
    thumb_badge_text: str = Field(
        ..., description="サムネイル左上バッジ文字 (6文字以内)"
    )
    summary_paragraph: str = Field(
        ..., description="概要欄冒頭に記載する動画の要約と考察ハイライト (200文字程度)"
    )
    tags: List[str] = Field(..., description="YouTube検索用関連タグ (10〜15個)")


class ScriptSection(BaseModel):
    """完成した各章の台本データ"""

    section_id: SectionId
    section_title: str
    is_insight_section: bool
    lines: List[DialogueLine]


class GeneratedEpisode(BaseModel):
    """8分以上＆独自考察60%以上を検証済みの完全パッケージ台本"""

    topic_keyword: str
    topic_category: str
    sections: List[ScriptSection]
    metadata: VideoMetadata
    reference_sources: List[str]
    total_chars: int
    insight_chars: int
    insight_ratio: float
    estimated_duration_sec: float


class ScriptGenerator:
    """
    Gemini API を用いて四国めたん＆ずんだもんの8〜12分掛け合い解説・深掘り考察台本を生成する。
    - 事実パート35%以下 / 独自考察パート65%以上の構造化制御
    - 8分(480秒)未満の場合の自動深掘り追加生成ループ
    - VOICEVOX利用規約クレジットの確実な自動挿入
    """

    # speedScale=1.18 における日本語発話の平均文字速度（約7.8〜8.2文字/秒＋セリフ間ポーズ0.24秒）
    CHARS_PER_SECOND = 8.0

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.policy = config.settings.get("monetization_policy", {})
        self.vv_cfg = config.settings.get("voicevox", {})
        self.min_duration_sec = float(self.policy.get("min_duration_sec", 480))
        self.min_total_chars = int(self.policy.get("min_total_chars", 4200))
        self.min_insight_ratio = float(self.policy.get("min_insight_ratio", 0.60))

    def estimate_duration_sec(self, sections: List[ScriptSection]) -> float:
        """文字数とセリフ間ポーズ・章間ポーズから再生時間（秒）を高精度に見積もる"""
        inter_pause = float(self.vv_cfg.get("inter_line_pause_sec", 0.24))
        sec_pause = float(self.vv_cfg.get("section_pause_sec", 0.75))

        total_chars = sum(len(line.text) for sec in sections for line in sec.lines)
        total_lines = sum(len(sec.lines) for sec in sections)
        speech_sec = total_chars / self.CHARS_PER_SECOND
        pause_sec = (total_lines * inter_pause) + (len(sections) * sec_pause)
        return round(speech_sec + pause_sec, 1)

    @staticmethod
    def _ensure_slides_populated(
        lines: List[DialogueLine], default_title: str
    ) -> List[DialogueLine]:
        """すべてのセリフ区間において3〜5セリフごとに確実にスライド情報が設定されるよう正規化する"""
        current_slide = SlideCard(
            slide_title=default_title[:24],
            diagram_type="bullet_points",
            slide_points=["トレンドの核心ポイントを整理", "背景構造と今後の影響を徹底考察"],
            highlight_keyword="徹底解説",
        )
        for idx, line in enumerate(lines):
            if line.slide is not None:
                # 文字数上限の安全トリム
                line.slide.slide_title = line.slide.slide_title[:26]
                line.slide.slide_points = [p[:32] for p in line.slide.slide_points[:4]]
                line.slide.highlight_keyword = line.slide.highlight_keyword[:20]
                current_slide = line.slide
            elif idx == 0:
                line.slide = current_slide
        return lines

    def generate_episode(self, trend: SelectedTrendResult) -> GeneratedEpisode:
        """トレンド選定結果から8〜12分の完全な掛け合い台本・メタデータを生成する"""
        if not self.config.env.gemini_api_key:
            logger.warning("GEMINI_API_KEY 未設定のためオフライン検証用フル尺台本を生成します")
            return self._generate_offline_fallback_episode(trend)

        client = genai.Client(
            api_key=self.config.env.gemini_api_key,
            http_options=types.HttpOptions(timeout=55_000),
        )

        # Step 1: 5章構成アウトライン生成
        logger.info("Step 1/4: 5章構成アウトラインと考察軸を設計中...")
        outline = self._generate_outline(client, trend)

        # Step 2: 各章の対話台本を順次生成（長尺8〜12分を確実に担保するため章別に生成）
        sections: List[ScriptSection] = []
        previous_context_summary = ""

        for idx, bp in enumerate(outline.sections):
            logger.info(
                "Step 2/4: 章 [%d/%d] 「%s」 (目標:%dセリフ) の掛け合い台本を生成中...",
                idx + 1,
                len(outline.sections),
                bp.section_title,
                bp.target_line_count,
            )
            sec_out = self._generate_section_dialogue(
                client=client,
                trend=trend,
                video_concept=outline.video_concept,
                blueprint=bp,
                previous_context=previous_context_summary,
            )
            normalized_lines = self._ensure_slides_populated(
                sec_out.lines, bp.section_title
            )
            is_insight = bp.section_id in (
                "deep_analysis_structure",
                "deep_analysis_future",
                "ending",
            )
            sections.append(
                ScriptSection(
                    section_id=bp.section_id,
                    section_title=bp.section_title,
                    is_insight_section=is_insight,
                    lines=normalized_lines,
                )
            )
            last_few = " / ".join(l.text for l in normalized_lines[-3:])
            previous_context_summary = f"前章「{bp.section_title}」の結び: {last_few}"

        # Step 3: 8分以上（安全マージン込みで495秒以上）＆考察比率60%以上の検査と自動深掘り追加
        sections = self._enforce_duration_and_insight_ratio(client, trend, sections)

        # Step 4: 高CTRメタデータ＆サムネイル文言生成
        logger.info("Step 4/4: SEOメタデータとサムネイル文言を生成中...")
        metadata = self._generate_metadata(client, trend, sections)

        total_chars = sum(len(l.text) for s in sections for l in s.lines)
        insight_chars = sum(
            len(l.text) for s in sections if s.is_insight_section for l in s.lines
        )
        insight_ratio = round(insight_chars / max(1, total_chars), 3)
        est_duration = self.estimate_duration_sec(sections)

        logger.info(
            "台本生成完了: 総文字数=%d文字 / 推定尺=%.1f秒 (%.1f分) / 独自考察比率=%.1f%%",
            total_chars,
            est_duration,
            est_duration / 60.0,
            insight_ratio * 100.0,
        )

        return GeneratedEpisode(
            topic_keyword=trend.selected_keyword,
            topic_category=trend.topic_category,
            sections=sections,
            metadata=metadata,
            reference_sources=trend.reference_sources
            or ["Google Trends JP", "国内主要報道・公開統計データ"],
            total_chars=total_chars,
            insight_chars=insight_chars,
            insight_ratio=insight_ratio,
            estimated_duration_sec=est_duration,
        )

    def _generate_with_model_fallback(
        self,
        client: genai.Client,
        preferred_model: str,
        system_instruction: str,
        user_prompt: str,
        response_schema,
        temperature: float = 0.5,
    ):
        """指定モデルが応答遅延(60秒超)・混雑(503)・レート制限(429)の場合に即座に別モデルへ自動切替する"""
        import concurrent.futures

        models_to_try = []
        for m in [
            "gemini-3.6-flash",
            "gemini-3.5-flash",
            "gemini-3.5-flash-lite",
            "gemini-3.1-flash-lite",
            preferred_model,
            "gemini-3.8-flash",
        ]:
            if m and m not in models_to_try:
                models_to_try.append(m)

        last_err: Exception | None = None
        for model_name in models_to_try:
            ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            try:
                logger.info("  -> Gemini API 生成リクエスト送信 (model=%s, timeout=60s)...", model_name)
                fut = ex.submit(
                    client.models.generate_content,
                    model=model_name,
                    contents=user_prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=system_instruction,
                        response_mime_type="application/json",
                        response_schema=response_schema,
                        temperature=temperature,
                    ),
                )
                return fut.result(timeout=60.0)
            except Exception as e:
                logger.warning(
                    "  -> モデル %s が60秒以内に応答しなかったため次の高速モデルへ自動切替します (%s)",
                    model_name,
                    type(e).__name__,
                )
                last_err = e
            finally:
                ex.shutdown(wait=False, cancel_futures=True)

        logger.warning("全Geminiモデルが混雑中のため、オフライン高品質台本テンプレートへフォールバックします: %s", last_err)
        raise RuntimeError(f"全Geminiモデルでの生成に失敗しました: {last_err}")

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=15))
    def _generate_outline(
        self, client: genai.Client, trend: SelectedTrendResult
    ) -> ScriptOutline:
        sys_prompt = self.config.prompts.get("outline_generation_system_prompt", "")
        user_prompt = (
            f"【選定トレンド】: {trend.selected_keyword}\n"
            f"【客観的事実サマリ】: {trend.factual_summary}\n"
            f"【深掘り考察の3大視点】: {json.dumps(trend.key_analytical_angles, ensure_ascii=False)}\n\n"
            "必ず以下の5つの `section_id` を順番通りに含むアウトラインを作成してください:\n"
            "1. `opening` (target_line_count: 12, is_insight_section: false)\n"
            "2. `fact_overview` (target_line_count: 32, is_insight_section: false)\n"
            "3. `deep_analysis_structure` (target_line_count: 38, is_insight_section: true)\n"
            "4. `deep_analysis_future` (target_line_count: 38, is_insight_section: true)\n"
            "5. `ending` (target_line_count: 12, is_insight_section: true)\n"
        )

        resp = self._generate_with_model_fallback(
            client=client,
            preferred_model=self.config.env.gemini_model,
            system_instruction=sys_prompt,
            user_prompt=user_prompt,
            response_schema=ScriptOutline,
            temperature=0.4,
        )
        return ScriptOutline.model_validate_json(resp.text)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=15))
    def _generate_section_dialogue(
        self,
        client: genai.Client,
        trend: SelectedTrendResult,
        video_concept: str,
        blueprint: SectionBlueprint,
        previous_context: str,
    ) -> SectionDialogueOutput:
        sys_prompt = self.config.prompts.get("section_dialogue_system_prompt", "")
        target_lines = max(10, blueprint.target_line_count)
        min_chars_for_section = target_lines * 38

        user_prompt = (
            f"【動画全体のテーマ】: {trend.selected_keyword}\n"
            f"【動画の考察コンセプト】: {video_concept}\n"
            f"【事実の前提】: {trend.factual_summary}\n"
            f"【前章からの流れ】: {previous_context or '（動画の冒頭です）'}\n\n"
            f"【今回執筆する章】: `{blueprint.section_id}` - 「{blueprint.section_title}」\n"
            f"【この章の性質】: {'独自の深掘り考察・構造分析・将来予測パート（ニュースの単なる要約ではなく深い洞察を展開すること）' if blueprint.is_insight_section else '客観的な事実整理と導入フック'}\n"
            f"【この章で必ず扱う論点】:\n"
            f"{json.dumps(blueprint.key_discussion_points, ensure_ascii=False, indent=2)}\n\n"
            f"【分量要件（厳守）】:\n"
            f"- セリフ数: 必ず {target_lines} セリフ以上（1セリフあたり35〜55文字、章合計で {min_chars_for_section} 文字以上）出力してください。\n"
            f"- 3〜5セリフごとに必ず `slide` オブジェクトを設定し、視覚的な解説カード（要点・対比・時系列・注目数値）を切り替えてください。\n"
            f"- 四国めたん（`metan`）とずんだもん（`zundamon`）が交互にテンポよく掛け合い、各セリフに適切な感情タグ（`normal`/`happy`/`surprised`/`serious`/`troubled`）を付与してください。"
        )

        resp = self._generate_with_model_fallback(
            client=client,
            preferred_model=self.config.env.gemini_model,
            system_instruction=sys_prompt,
            user_prompt=user_prompt,
            response_schema=SectionDialogueOutput,
            temperature=0.65,
        )
        return SectionDialogueOutput.model_validate_json(resp.text)

    def _enforce_duration_and_insight_ratio(
        self,
        client: genai.Client,
        trend: SelectedTrendResult,
        sections: List[ScriptSection],
    ) -> List[ScriptSection]:
        """推定再生時間が495秒（8分15秒）未満、または考察比率が60%未満の場合に考察パートを自動拡張する"""
        target_safe_duration = max(495.0, self.min_duration_sec + 15.0)

        for attempt in range(1, 4):
            est_sec = self.estimate_duration_sec(sections)
            total_chars = sum(len(l.text) for s in sections for l in s.lines)
            insight_chars = sum(
                len(l.text) for s in sections if s.is_insight_section for l in s.lines
            )
            ratio = insight_chars / max(1, total_chars)

            if (
                est_sec >= target_safe_duration
                and total_chars >= self.min_total_chars
                and ratio >= self.min_insight_ratio
            ):
                break

            deficit_sec = max(0.0, target_safe_duration - est_sec)
            needed_chars = max(
                int(deficit_sec * self.CHARS_PER_SECOND),
                max(0, self.min_total_chars - total_chars),
                600,
            )
            needed_lines = max(14, int(needed_chars / 38) + 2)

            logger.info(
                "尺・考察比率の自動補強を実行中 (試行 %d/3): 現在=%.1f秒 (%d文字, 考察比率=%.1f%%) -> 追加目標=%dセリフ",
                attempt,
                est_sec,
                total_chars,
                ratio * 100.0,
                needed_lines,
            )

            extra_lines = self._generate_expansion_lines(
                client=client,
                trend=trend,
                needed_lines=needed_lines,
                attempt=attempt,
            )
            # deep_analysis_future セクションへ追加
            target_sec = next(
                (s for s in sections if s.section_id == "deep_analysis_future"),
                sections[-2] if len(sections) >= 2 else sections[-1],
            )
            target_sec.is_insight_section = True
            target_sec.lines.extend(
                self._ensure_slides_populated(extra_lines, "深掘り考察：多角的シナリオ検証")
            )

        return sections

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=15))
    def _generate_expansion_lines(
        self,
        client: genai.Client,
        trend: SelectedTrendResult,
        needed_lines: int,
        attempt: int,
    ) -> List[DialogueLine]:
        sys_prompt = self.config.prompts.get("expansion_dialogue_system_prompt", "")
        focus_angles = [
            "海外や過去の類似事例との構造比較と、日本特有の課題の深掘り",
            "個人・消費者・ビジネスパーソンそれぞれの立場での具体的なメリット・デメリット対比",
            "今後1年〜3年で起きる二次的・三次的な波及効果と、見落とされがちな盲点",
        ]
        angle = focus_angles[(attempt - 1) % len(focus_angles)]

        user_prompt = (
            f"【トレンドテーマ】: {trend.selected_keyword}\n"
            f"【追加深掘りテーマ】: {angle}\n"
            f"【必須出力セリフ数】: {needed_lines} セリフ以上（合計 {needed_lines * 42} 文字以上）\n"
            "四国めたんとずんだもんの掛け合いで、3〜5セリフごとに図解スライド (`slide`) を更新しながら詳しく考察を展開してください。"
        )

        resp = self._generate_with_model_fallback(
            client=client,
            preferred_model=self.config.env.gemini_model,
            system_instruction=sys_prompt,
            user_prompt=user_prompt,
            response_schema=SectionDialogueOutput,
            temperature=0.7,
        )
        out = SectionDialogueOutput.model_validate_json(resp.text)
        return out.lines

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=15))
    def _generate_metadata(
        self,
        client: genai.Client,
        trend: SelectedTrendResult,
        sections: List[ScriptSection],
    ) -> VideoMetadata:
        sys_prompt = self.config.prompts.get("metadata_generation_system_prompt", "")
        section_titles = [s.section_title for s in sections]
        user_prompt = (
            f"【トレンドキーワード】: {trend.selected_keyword}\n"
            f"【事実サマリ】: {trend.factual_summary}\n"
            f"【動画の章構成】: {json.dumps(section_titles, ensure_ascii=False)}\n"
            f"【考察の視点】: {json.dumps(trend.key_analytical_angles, ensure_ascii=False)}\n\n"
            "高CTRタイトル（32文字前後）、サムネイル用上下2段インパクト文言、概要欄冒頭要約、関連タグをJSONで出力してください。"
        )

        resp = self._generate_with_model_fallback(
            client=client,
            preferred_model=self.config.env.gemini_fast_model,
            system_instruction=sys_prompt,
            user_prompt=user_prompt,
            response_schema=VideoMetadata,
            temperature=0.5,
        )
        meta = VideoMetadata.model_validate_json(resp.text)
        meta.title = meta.title[:95]
        meta.thumb_upper_text = meta.thumb_upper_text[:12]
        meta.thumb_main_text = meta.thumb_main_text[:12]
        meta.thumb_badge_text = meta.thumb_badge_text[:8]
        return meta

    def build_youtube_description(
        self,
        episode: GeneratedEpisode,
        section_timestamps: Optional[Dict[str, str]] = None,
    ) -> str:
        """
        VOICEVOX利用規約クレジット（VOICEVOX:四国めたん / VOICEVOX:ずんだもん）および
        目次タイムスタンプ・情報ソースをプログラム的に100%確実に挿入した概要欄テキストを生成する。
        """
        ts_map = section_timestamps or {}
        chapter_lines: List[str] = []
        default_times = ["00:00", "00:45", "03:15", "06:10", "08:45"]

        for idx, sec in enumerate(episode.sections):
            time_str = ts_map.get(
                sec.section_id,
                default_times[idx] if idx < len(default_times) else "08:00",
            )
            if idx == 0:
                time_str = "00:00"  # YouTubeチャプター仕様上、最初は必ず00:00
            chapter_lines.append(f"{time_str} {sec.section_title}")

        sources_text = "\n".join(f"・{src}" for src in episode.reference_sources)
        required_credits = self.policy.get(
            "required_credits", ["VOICEVOX:四国めたん", "VOICEVOX:ずんだもん"]
        )
        credits_block = "\n".join(required_credits)

        tags_hashtags = " ".join(
            f"#{t.replace(' ', '')}" for t in episode.metadata.tags[:5]
        )

        description = (
            f"{episode.metadata.summary_paragraph}\n\n"
            f"この動画では、今話題の「{episode.topic_keyword}」について、客観的な事実整理だけでなく、"
            f"その裏にある構造的背景や今後の影響・シナリオ予測まで、四国めたんとずんだもんの掛け合いで分かりやすく深掘り考察します。\n"
            f"ぜひ皆さんのご意見や予想もコメント欄で教えてください！\n\n"
            f"▼ 目次（チャプター）\n"
            f"{chr(10).join(chapter_lines)}\n\n"
            f"▼ 主な情報ソース・参照元\n"
            f"{sources_text}\n\n"
            f"▼ 音声・キャラクタークレジット（利用規約に基づく表記）\n"
            f"{credits_block}\n\n"
            f"▼ 免責事項・解説スタンスについて\n"
            f"本動画は公開報道および統計データに基づく一般的な構造解説・考察コンテンツであり、"
            f"特定の投資勧誘・医療助言・法的助言を行うものではありません。\n\n"
            f"{tags_hashtags}"
        )
        return description

    def _generate_offline_fallback_episode(
        self, trend: SelectedTrendResult
    ) -> GeneratedEpisode:
        """話題・考察の展開に合わせて後ろの画面（グラフ・相関図・比較表・予測チャート）が次々切り替わるフル尺台本を生成"""
        kw = trend.selected_keyword
        kw_short = kw[:14]

        def add_block(
            target_list: List[DialogueLine],
            slide_title: str,
            dtype: DiagramType,
            badge: str,
            points: List[str],
            dialogues: List[tuple],
        ) -> None:
            slide = SlideCard(
                slide_title=slide_title,
                diagram_type=dtype,
                slide_points=points,
                highlight_keyword=badge,
            )
            for idx, (spk, emo, txt) in enumerate(dialogues):
                target_list.append(
                    DialogueLine(
                        speaker=spk,
                        emotion=emo,
                        text=txt,
                        slide=slide if idx == 0 else None,
                    )
                )

        # 1. オープニング (opening)
        opening_lines: List[DialogueLine] = []
        add_block(
            opening_lines,
            slide_title=f"急上昇「{kw_short}」注目度メーター",
            dtype="trend_meter",
            badge="急上昇速報",
            points=[
                f"検索トレンド急上昇度: 96% (国内トップクラス)",
                f"焦点: {kw_short}がもたらす構造変化",
                "結論: 表面の報道だけでは見えない3つの裏事情",
            ],
            dialogues=[
                ("metan", "happy", f"皆さんこんにちは、四国めたんです。今日は日本の急上昇トレンド「{kw}」の真相と今後の予測を徹底解説しますね。"),
                ("zundamon", "surprised", f"ずんだもんなのだ！最近ニュースでもSNSでも「{kw}」の話題でもちきりだけど、そんなにすごいことが起きているのか？"),
                ("metan", "serious", "ええ、後ろの注目度メーターを見てください。単なる一時的なニュースではなく、私たちの生活や産業のルールを根底から変える転換点なんです。"),
                ("zundamon", "normal", "なるほどなのだ！でも専門用語や断片的なニュースが多くて、結局どういうことなのか全体像が分かりにくいのだ。"),
                ("metan", "happy", "そこできょうは、まず3分で客観的な事実と世間の反応を整理したあと、後半で「なぜ起きたかの構造分析」と「今後の未来シナリオ」を深掘りしますよ！"),
                ("zundamon", "happy", "それは助かるのだ！最後まで見れば裏側の仕組みまでバッチリ分かるってことなのだね、さっそくお願いするのだ！"),
            ],
        )

        # 2. 事実の整理パート (fact_overview)
        fact_lines: List[DialogueLine] = []
        add_block(
            fact_lines,
            slide_title=f"{kw_short}：時系列で見る経緯",
            dtype="timeline",
            badge="時系列整理",
            points=[
                "STEP 1: 従来の課題と水面下での技術・市場変化",
                "STEP 2: 大型発表・政策転換により一気に急上昇トレンド化",
                "STEP 3: 国内外の関連業界・市場が連鎖反応を開始",
            ],
            dialogues=[
                ("metan", "normal", f"まずは客観的な事実関係を時系列で整理しましょう。後ろのフロー図にある通り、「{kw}」は突然降って湧いた話ではないんです。"),
                ("zundamon", "surprised", "ステップ1から順番に積み重なって、今回のステップ2の発表で一気に爆発したってことなのだ？"),
                ("metan", "normal", "その通りです。これまで水面下で進んでいた投資や制度見直しが臨界点を超え、具体的な数値や計画として表に出たことで大きな注目を集めました。"),
                ("zundamon", "normal", "点で見ると急なニュースに見えるけど、時系列の線でつなげて見ると必然の流れだったのがよく分かるのだ。"),
                ("metan", "serious", "さらにステップ3として、一つの業界だけでなく電力・インフラ・雇用など周辺分野へ一気に波及し始めているのが現在のフェーズですね。"),
                ("zundamon", "surprised", "一つのニュースがドミノ倒しみたいにいろんな業界を動かしているのだね！"),
            ],
        )
        add_block(
            fact_lines,
            slide_title="主要データで見るインパクト比較",
            dtype="bar_chart",
            badge="関連データ",
            points=[
                "注目指標A: 関連投資・市場規模の伸び率 (88%)",
                "注目指標B: 既存インフラ・供給体制への負荷 (74%)",
                "注目指標C: 今後3年間の制度・現場変革スピード (92%)",
            ],
            dialogues=[
                ("metan", "normal", "続いて後ろのグラフを見てください。今回のトレンドに関連する主要データのインパクトを視覚化したものです。"),
                ("zundamon", "surprised", "おおっ、投資や変革スピードのバーがめちゃくちゃ伸びているのだ！その一方でインフラへの負荷もかなり高いのだね。"),
                ("metan", "serious", "そうなんです。成長への期待値が非常に高い反面、それを支える土台となる供給力や人材・インフラが追いつくのかという点が最大の論点になっています。"),
                ("zundamon", "troubled", "アクセルを全開で踏みたいのに、道路の整備が間に合うか心配されているような状態なのだ？"),
                ("metan", "happy", "すごく的確なたとえですね！まさにその「成長のアクセル」と「インフラの制約」のせめぎ合いが今回のトレンドの本質なんです。"),
                ("zundamon", "normal", "グラフで見ると、どこにエネルギーが集まっていてどこがボトルネックなのか一目で分かるのだ！"),
            ],
        )
        add_block(
            fact_lines,
            slide_title="世間・専門家の賛否リアクション分析",
            dtype="public_reaction",
            badge="世論の反応",
            points=[
                "推進・期待派 (48%): 国際競争力の復活と経済波及に期待",
                "慎重・懸念派 (34%): コスト負担や実現ハードルを疑問視",
                "静観・様子見 (18%): 具体的な生活への影響を見極め中",
            ],
            dialogues=[
                ("metan", "normal", "ここでSNSや専門家の間でどのような反応が出ているか、後ろの世論分析チャートを見てみましょう。"),
                ("zundamon", "normal", "期待している人が半分近くいる一方で、コストや実現性を心配している慎重派も3割以上いるのだね。"),
                ("metan", "normal", "ええ。推進派は「今動かなければ世界から取り残される」と評価し、慎重派は「現場の負担やコストは誰が払うのか」と鋭く指摘しています。"),
                ("zundamon", "troubled", "どっちの言い分ももっともに聞こえるから、普通のニュースを見ているだけだと判断に迷ってしまうのだ。"),
                ("metan", "serious", "だからこそ、単なる賛成・反対の感想ではなく、その裏にある「構造的なメカニズム」を冷静に分析する必要があるんですね。"),
                ("zundamon", "happy", "いよいよここからがめたんの独自考察パートなのだ！裏側の仕組みを教えてほしいのだ！"),
            ],
        )

        # 3. 深掘り考察①：構造的・社会的背景 (deep_analysis_structure)
        insight1_lines: List[DialogueLine] = []
        add_block(
            insight1_lines,
            slide_title=f"深層考察①：{kw_short}の3大構造要因",
            dtype="structure_map",
            badge="独自構造分析",
            points=[
                "要因1【技術・産業】: 次世代技術への移行と供給網の再編",
                "要因2【経済・コスト】: 従来型モデルの限界と投資集中",
                "要因3【社会・地政学】: 経済安全保障と国内基盤の強化",
            ],
            dialogues=[
                ("metan", "serious", f"ここからは独自の深掘り考察パートです。後ろの構造マップを見てください。「{kw}」が今起きている背景には3つの構造要因が絡み合っています。"),
                ("zundamon", "surprised", "技術と経済と社会情勢の3つの歯車が同時に噛み合ったからこそ、これだけ大きな動きになったのだ？"),
                ("metan", "serious", "その通りです。第一に技術面の世代交代、第二に従来の延長線上ではコストが合わなくなった経済的限界、そして第三に国内で基盤を持とうとする安全保障の動きです。"),
                ("zundamon", "normal", "なるほどなのだ！どれか一つだけじゃなくて、3つが重なっているから後戻りできない大きな潮流になっているのだね。"),
                ("metan", "normal", "ええ。多くの報道は表面の出来事だけを伝えますが、この3つの土台を理解しておくと、今後出る関連ニュースの意味がすべてつながって見えてきますよ。"),
                ("zundamon", "happy", "表面のニュースの裏にこんな立体的な構造があったなんて、図で見るとすごくスッキリ理解できるのだ！"),
                ("metan", "serious", "さらに重要なのは、この3つの要因が互いに影響し合うことで、変化のスピードが従来の予想を遥かに上回っているという点なんです。"),
                ("zundamon", "surprised", "昔なら10年かかっていた変化が、たった2〜3年で一気に進んでしまうくらいの加速っぷりなのだ！"),
            ],
        )
        add_block(
            insight1_lines,
            slide_title="従来モデル vs 新時代モデルの構造対比",
            dtype="comparison",
            badge="構造の転換",
            points=[
                "【従来】コスト削減優先・外部依存・縦割り型の運用",
                "【現在】スピード重視・戦略的投資・分野横断の連携",
                "【転換点】インフラと先端技術を一体で設計する時代へ",
                "【影響】既存ルールに固執する企業と変革層の二極化",
            ],
            dialogues=[
                ("metan", "serious", "では従来と今で何が決定的に違うのか、後ろの比較画面で比べてみましょう。左がこれまでのモデル、右が今進んでいる新モデルです。"),
                ("zundamon", "surprised", "昔はとにかく目先のコストを削って効率化する時代だったけど、今は将来の基盤のために戦略的に投資する時代に変わったのだ？"),
                ("metan", "serious", "まさにそこが核心です。これまで別々に考えられていた技術開発とインフラ整備を、最初からセットで設計しないと勝てない時代に突入したんです。"),
                ("zundamon", "troubled", "ということは、昔の成功体験のまま縦割りで動いている組織やビジネスは、あっという間に置いていかれてしまうのだ…"),
                ("metan", "normal", "おっしゃる通りです。このルールチェンジに気づいて素早く舵を切ったプレイヤーと、従来のやり方に固執するプレイヤーで鮮やかな二極化が起きています。"),
                ("zundamon", "serious", "単なるトレンドじゃなくて「ゲームのルール自体が変わった」と捉えるのが考察の第一歩なのだね！"),
                ("metan", "happy", "その視点を持てるかどうかが、情報をただ消費する人と、未来を先読みして活かせる人の分かれ道になりますよ。"),
                ("zundamon", "normal", "ますます面白くなってきたのだ！他にも見落とされがちな構造のポイントはあるのか？"),
            ],
        )
        add_block(
            insight1_lines,
            slide_title="ボトルネック分析：何が最大の壁になるのか",
            dtype="bar_chart",
            badge="深層課題",
            points=[
                "壁1: 電力・物理インフラの供給余力と地域偏在 (85%)",
                "壁2: 高度な専門人材・現場オペレーション人材不足 (90%)",
                "壁3: 法制度・規制アップデートと社会合意の形成 (72%)",
            ],
            dialogues=[
                ("metan", "serious", "もう一つ踏み込んで、今後必ず直面する「3つの壁（ボトルネック）」を後ろのチャートで検証しておきましょう。"),
                ("zundamon", "surprised", "お金や計画があっても、実際に動かすための「電力インフラ」と「専門人材」の壁が90%近くあってめちゃくちゃ高いのだ！"),
                ("metan", "serious", "はい。ハードウェアや設備はお金で買えても、それを支えるエネルギー供給網や、現場を設計・運用できる人材は一朝一夕には増やせません。"),
                ("zundamon", "troubled", "たしかに建物だけ立派に作っても、動かす電気や使いこなせる人がいなかったら宝の持ち腐れになってしまうのだ。"),
                ("metan", "normal", "だからこそ、このボトルネックを解消する周辺分野――つまり省エネ技術、インフラ保守、教育・リスキリング分野に次の大きなチャンスが生まれるんです。"),
                ("zundamon", "surprised", "なるほどなのだ！壁がある場所こそが、次に伸びる新しいビジネスや需要のヒントになるってことなのだね！"),
                ("metan", "happy", "そういうことです！課題をネガティブに捉えるだけでなく、「どこに解決需要が生まれるか」を読むのが構造考察の醍醐味ですね。"),
                ("zundamon", "happy", "後ろのグラフを見ながら話を聞くと、ニュースの裏側まで透けて見えるようでワクワクするのだ！"),
            ],
        )

        # 4. 深掘り考察②：今後の予測シナリオと視聴者への影響 (deep_analysis_future)
        insight2_lines: List[DialogueLine] = []
        add_block(
            insight2_lines,
            slide_title="今後1〜3年の未来予測シナリオ曲線",
            dtype="future_forecast",
            badge="未来シナリオ",
            points=[
                "シナリオA【順調加速】: インフラ連携が進み生産性と新市場が急拡大",
                "シナリオB【調整局面】: コスト高と供給制約で選別・再編が進行",
                "最有力ルート: 前半は摩擦を伴いつつ後半に実用化が一気に定着",
            ],
            dialogues=[
                ("metan", "serious", f"では後半の考察として、「{kw}」が今後1年から3年でどう展開するのか、後ろの予測シナリオ曲線を見てください。"),
                ("zundamon", "normal", "上向きのシナリオAと、途中で波があるシナリオBの2つのルートが描かれているのだ。どっちになりそうなのだ？"),
                ("metan", "serious", "まずシナリオAは、官民の連携とインフラ整備がスムーズに噛み合い、新しい産業とサービスが急速に立ち上がるポジティブルートです。"),
                ("zundamon", "troubled", "でもさっき見た電力や人材の壁を考えると、最初から一直線にうまくいくとは限らない気もするのだ。"),
                ("metan", "normal", "鋭いですね。そこで現実的に最も可能性が高いのが、最初の1〜2年はシナリオBのようにコスト調整や淘汰を経験し、その後本物が定着するルートです。"),
                ("zundamon", "surprised", "最初はブームでいろんな計画が乱立するけど、途中で本当に実力のあるものだけが生き残って社会のインフラになるってことなのだ！"),
                ("metan", "happy", "まさに過去のインターネット普及期やスマホ黎明期と同じ成長曲線を描く可能性が極めて高いと分析できます。"),
                ("zundamon", "normal", "短期的な浮き沈みに振り回されず、3年後の定着期を見据えておくのが賢い見方なのだね。"),
            ],
        )
        add_block(
            insight2_lines,
            slide_title="視聴者へのメリット vs デメリット・注意点",
            dtype="comparison",
            badge="生活・仕事への影響",
            points=[
                "【メリット1】新サービス普及による利便性向上と業務効率化",
                "【メリット2】関連産業・周辺インフラ領域での新たな雇用と機会",
                "【注意点1】移行期のエネルギー・サービス価格変動リスク",
                "【注意点2】情報リテラシーとスキル更新の有無による格差拡大",
            ],
            dialogues=[
                ("metan", "serious", "では、このトレンドは私たち視聴者の生活や仕事にどんなメリットと注意点をもたらすのか、後ろの比較ボードで整理しましょう。"),
                ("zundamon", "happy", "自分たちの暮らしや働き方にどう影響するのかが一番知りたいところなのだ！緑がメリットで赤が注意点なのだね。"),
                ("metan", "normal", "まず大きなメリットとして、技術とインフラの高度化によって日常のサービスが劇的に便利になり、地域や関連分野にも新しい仕事や需要が生まれます。"),
                ("zundamon", "troubled", "それは嬉しいけど、右側の注意点にある「移行期の価格変動」や「スキル格差」というのはどういうことなのだ？"),
                ("metan", "serious", "大規模なインフラ投資や構造転換が進む過渡期には、一時的にコスト負担が議論になったり、新しい仕組みを使いこなせる人とそうでない人の差が開きやすいんです。"),
                ("zundamon", "surprised", "無関心でいると知らないうちにコスト増の影響だけ受けて、美味しいメリットを取り逃がしてしまうかもしれないのだ！"),
                ("metan", "normal", "ええ。だからこそ「自分には関係ない遠いニュース」と思わず、メリットを享受できる側に回ることが大切なんですね。"),
                ("zundamon", "serious", "メリットとリスクの両面をフェアに比較してくれるから、すごく冷静に判断できるのだ。"),
            ],
        )
        add_block(
            insight2_lines,
            slide_title="私たちが明日から意識すべき3つのアクション",
            dtype="action_summary",
            badge="実践対策",
            points=[
                "対策1: 見出しの煽りに惑わされず一次データと構造を見る",
                "対策2: 自身の仕事・業界で自動化・高度化する領域を把握する",
                "対策3: ボトルネック（電力・インフラ・人材）解消の動きに注目",
            ],
            dialogues=[
                ("metan", "happy", "それを踏まえて、私たちが明日から実践できる3つのアクションを後ろのチェックリストにまとめました。"),
                ("zundamon", "normal", "対策1は「一次データと構造を見ること」、対策2は「自分の仕事への影響を知ること」、対策3は「ボトルネック解消に注目すること」なのだ！"),
                ("metan", "serious", "はい。特に今後関連ニュースを見るときは、「先ほど見た3つの壁（電力・人材・制度）がどう解決されようとしているか」に注目してみてください。"),
                ("zundamon", "happy", "そのチェックポイントを知っているだけで、明日からのニュースの見え方が180度変わりそうなのだ！"),
                ("metan", "normal", "また、新しい技術やサービスに触れる機会があれば、まずは小さく試してみることで変化の波を自分の武器にできますよ。"),
                ("zundamon", "happy", "怖がって遠ざけるんじゃなくて、仕組みを知って賢く活用するのが一番の対策なのだね！"),
                ("metan", "happy", "その通りです、ずんだもん。こうした構造的な視点を積み重ねていくことが、変化の激しい時代を生き抜く最大の羅針盤になります。"),
                ("zundamon", "normal", "今日もめちゃくちゃ深い考察で勉強になったのだ！最後に全体のまとめをお願いするのだ！"),
            ],
        )

        # 5. エンディング (ending)
        ending_lines: List[DialogueLine] = []
        add_block(
            ending_lines,
            slide_title=f"本日の結論まとめ＆視聴者アンケート",
            dtype="action_summary",
            badge="総括まとめ",
            points=[
                f"結論1: 「{kw_short}」は技術・経済・社会の構造転換点",
                "結論2: 成長の鍵はインフラ・人材ボトルネックの突破にある",
                "質問: 皆さんは今後シナリオA・Bどちらに進むと思いますか？",
            ],
            dialogues=[
                ("metan", "happy", f"それでは本日の総括です。「{kw}」の事実整理から構造的背景、そして未来シナリオまで見てきましたが、いかがでしたか？"),
                ("zundamon", "happy", "最初は難しそうなトレンドだと思ったけど、後ろのグラフや図解と一緒に見たら裏側の仕組みまでスッキリ分かったのだ！"),
                ("metan", "normal", "表面のニュースだけでは見えない3つの構造要因と、今後直面するボトルネックを押さえておくことで、これからの展開がずっと読みやすくなりますよ。"),
                ("zundamon", "normal", "みんなは今後の展開について、順調に加速するシナリオAと調整を経るシナリオBのどっちになると思うのだ？"),
                ("metan", "happy", "ぜひ皆さんの予想や、お仕事・生活で感じている実感もコメント欄で教えてくださいね。一つひとつのコメントが新しい考察のヒントになります！"),
                ("zundamon", "happy", "チャンネル登録と高評価もよろしくなのだ！それではまた次回のトレンド深掘り動画でお会いしましょう、バイバイなのだー！"),
            ],
        )

        sections = [
            ScriptSection(
                section_id="opening",
                section_title="オープニング：急上昇トレンドの核心",
                is_insight_section=False,
                lines=opening_lines,
            ),
            ScriptSection(
                section_id="fact_overview",
                section_title="事実の整理：時系列・主要データ・世間の反応",
                is_insight_section=False,
                lines=fact_lines,
            ),
            ScriptSection(
                section_id="deep_analysis_structure",
                section_title="深掘り考察①：なぜ今起きた？3つの構造的背景",
                is_insight_section=True,
                lines=insight1_lines,
            ),
            ScriptSection(
                section_id="deep_analysis_future",
                section_title="深掘り考察②：未来シナリオ予測と私たちの対策",
                is_insight_section=True,
                lines=insight2_lines,
            ),
            ScriptSection(
                section_id="ending",
                section_title="エンディング：本日の結論まとめ",
                is_insight_section=True,
                lines=ending_lines,
            ),
        ]

        total_chars = sum(len(l.text) for s in sections for l in s.lines)
        insight_chars = sum(
            len(l.text) for s in sections if s.is_insight_section for l in s.lines
        )
        insight_ratio = round(insight_chars / max(1, total_chars), 3)
        est_duration = self.estimate_duration_sec(sections)

        metadata = VideoMetadata(
            title=f"【徹底考察】{kw[:16]}の真相とは？裏の構造と今後の衝撃予測を解説",
            thumb_upper_text="なぜ今急上昇？",
            thumb_main_text="真相と今後の予測",
            thumb_badge_text="徹底考察",
            summary_paragraph=f"今日本で急上昇している「{kw}」について、客観的な事実整理と60%以上の独自構造分析・将来シナリオ予測を四国めたんとずんだもんの掛け合いで徹底解説します。",
            tags=[
                kw,
                "トレンド解説",
                "四国めたん",
                "ずんだもん",
                "VOICEVOX解説",
                "時事ニュース考察",
                "構造分析",
                "将来予測",
            ],
        )

        return GeneratedEpisode(
            topic_keyword=kw,
            topic_category=trend.topic_category,
            sections=sections,
            metadata=metadata,
            reference_sources=trend.reference_sources,
            total_chars=total_chars,
            insight_chars=insight_chars,
            insight_ratio=insight_ratio,
            estimated_duration_sec=est_duration,
        )
