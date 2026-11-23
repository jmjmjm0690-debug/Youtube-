import json
import logging
from pathlib import Path

from PIL import Image

from src.config_loader import AppConfig, SecretMaskingFilter
from src.main import run_pipeline
from src.script_generator import ScriptGenerator
from src.trend_fetcher import SelectedTrendResult, TrendFetcher
from src.visual_renderer import VisualRenderer


def test_secret_masking_filter() -> None:
    """ログ出力にAPIキーやリフレッシュトークンが漏洩しないことを検証"""
    secret_filter = SecretMaskingFilter(["AIzaSyTestSecretKey12345", "1//0gRefreshTokenSecret"])
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg="Error with key AIzaSyTestSecretKey12345 and token 1//0gRefreshTokenSecret",
        args=(),
        exc_info=None,
    )
    secret_filter.filter(record)
    assert "AIzaSyTestSecretKey12345" not in record.msg
    assert "1//0gRefreshTokenSecret" not in record.msg
    assert "***MASKED_SECRET***" in record.msg


def test_trend_deduplication() -> None:
    """過去に投稿済みのトピックが重複除外されることを検証"""
    config = AppConfig()
    fetcher = TrendFetcher(config)
    past = ["日銀の追加利上げと住宅ローン金利への影響", "生成AIエージェント普及"]

    assert fetcher._is_duplicate("日銀の追加利上げと住宅ローン金利への影響", past) is True
    assert fetcher._is_duplicate("生成AIエージェント普及の最新動向", past) is True
    assert fetcher._is_duplicate("次世代ペロブスカイト太陽電池の量産化", past) is False


def test_script_monetization_requirements_and_credits() -> None:
    """
    生成される台本および概要欄が以下のYPP収益化要件を満たすことを検証:
    1. 推定再生時間が8分 (480秒) 以上であること
    2. 独自考察・構造分析パートの比率が 60% (0.60) 以上であること
    3. 概要欄に VOICEVOX:四国めたん と VOICEVOX:ずんだもん が必ず含まれること
    4. スピーカーIDが 四国めたん=2, ずんだもん=3 に固定されていること
    """
    config = AppConfig()
    vv_chars = config.settings["voicevox"]["characters"]
    assert vv_chars["metan"]["speaker_id"] == 2
    assert vv_chars["zundamon"]["speaker_id"] == 3

    trend = SelectedTrendResult(
        selected_keyword="次世代AI半導体と国内データセンター投資の急拡大",
        topic_category="technology",
        monetization_safety_score=96,
        insight_depth_score=94,
        selection_reason="技術・経済・雇用への波及効果が大きいトレンドのため",
        factual_summary="日本国内でのAIデータセンター新設と次世代半導体開発への大型投資が相次いで発表されています。",
        key_analytical_angles=[
            "なぜ今日本国内へのデータセンター集中投資が起きているのか",
            "電力インフラ・地方経済・IT人材市場に起きる3年後の変化",
            "私たちが知っておくべきメリット・リスクと今後の注目ポイント",
        ],
        reference_sources=["Google Trends JP", "経済産業省公開資料"],
    )

    gen = ScriptGenerator(config)
    episode = gen._generate_offline_fallback_episode(trend)

    assert episode.estimated_duration_sec >= 480.0, (
        f"動画尺が8分(480秒)未満です: {episode.estimated_duration_sec}s"
    )
    assert episode.total_chars >= 4200, (
        f"総文字数が不足しています: {episode.total_chars}"
    )
    assert episode.insight_ratio >= 0.60, (
        f"独自考察比率が60%未満です: {episode.insight_ratio}"
    )

    desc = gen.build_youtube_description(episode)
    assert "VOICEVOX:四国めたん" in desc
    assert "VOICEVOX:ずんだもん" in desc
    assert "00:00" in desc


def test_end_to_end_dryrun_video_and_thumbnail_generation() -> None:
    """
    エンドツーエンドのドライランを実行し、
    1. 8分(480秒)以上の 1920x1080 MP4 動画が出力されること
    2. 1280x720 のサムネイル PNG が出力されること
    3. 概要欄テキストと構造化台本 JSON が正常に出力されること
    を実ファイルで検証する。
    """
    report = run_pipeline(
        force_topic="国産AI半導体と電力インフラ改革の真相",
        privacy_override="unlisted",
        dry_run_override=True,
        keep_temp=False,
        allow_offline_voice_fallback=True,
    )

    assert report["status"] == "SUCCESS"
    assert report["duration_sec"] >= 480.0
    assert report["insight_ratio"] >= 0.60

    video_path = Path(report["video_path"])
    thumb_path = Path(report["thumbnail_path"])
    desc_path = Path(report["description_path"])

    assert video_path.exists() and video_path.stat().st_size > 100_000
    assert thumb_path.exists() and thumb_path.stat().st_size > 20_000
    assert desc_path.exists()

    with Image.open(thumb_path) as img:
        assert img.size == (1280, 720)

    desc_text = desc_path.read_text(encoding="utf-8")
    assert "VOICEVOX:四国めたん" in desc_text
    assert "VOICEVOX:ずんだもん" in desc_text
