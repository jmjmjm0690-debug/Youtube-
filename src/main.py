import argparse
import json
import logging
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.config_loader import AppConfig
from src.notifier import Notifier
from src.script_generator import ScriptGenerator
from src.trend_fetcher import TrendFetcher
from src.video_composer import VideoComposer
from src.visual_renderer import VisualRenderer
from src.voicevox_client import VoicevoxClient
from src.youtube_uploader import UploadResult, YouTubeUploader

logger = logging.getLogger(__name__)


def run_pipeline(
    force_topic: Optional[str] = None,
    privacy_override: Optional[str] = None,
    dry_run_override: Optional[bool] = None,
    keep_temp: bool = False,
    allow_offline_voice_fallback: bool = False,
) -> dict:
    """
    急上昇トレンド収集 → 8分超・考察60%以上掛け合い台本生成 → VOICEVOX音声合成 →
    16:9 1080p動画＆サムネイル生成 → YouTube Data API 自動公開 → 履歴更新＆通知
    までを一気通貫で実行するメインパイプライン。
    """
    start_time = time.time()
    config = AppConfig()
    if dry_run_override is not None:
        config.env.dry_run = dry_run_override
    if privacy_override:
        config.env.youtube_privacy_status = privacy_override.strip().lower()

    notifier = Notifier(config)
    timestamp_tag = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = config.root_dir / "output" / timestamp_tag
    work_dir = config.root_dir / "tmp" / f"work_{timestamp_tag}"
    output_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    selected_keyword = ""
    try:
        logger.info("============================================================")
        logger.info("YouTube 急上昇トレンド解説＆考察動画 自動生成パイプライン開始")
        logger.info(
            "モード: %s / 公開設定: %s / モデル: %s",
            "DRY_RUN (投稿スキップ)" if config.env.dry_run else "PRODUCTION (YouTube自動投稿)",
            config.env.youtube_privacy_status,
            config.env.gemini_model,
        )
        logger.info("============================================================")

        # 1. 急上昇トレンド収集・選定
        trend_fetcher = TrendFetcher(config)
        trend_result = trend_fetcher.select_best_trend(force_keyword=force_topic)
        selected_keyword = trend_result.selected_keyword

        # 2. 四国めたん＆ずんだもん掛け合い台本・メタデータ生成 (8分以上 & 考察60%以上)
        script_gen = ScriptGenerator(config)
        episode = script_gen.generate_episode(trend_result)

        script_json_path = output_dir / "episode_script.json"
        script_json_path.write_text(
            episode.model_dump_json(indent=2), encoding="utf-8"
        )
        logger.info("構造化台本JSONを出力しました: %s", script_json_path)

        # 3. VOICEVOX 音声合成 (四国めたん ID:2 / ずんだもん ID:3)
        vv_client = VoicevoxClient(config)
        timeline = vv_client.synthesize_episode(
            episode=episode,
            work_dir=work_dir,
            allow_offline_fallback=allow_offline_voice_fallback or config.env.dry_run,
        )

        # 実測尺に基づき YouTube 概要欄（目次チャプター＋VOICEVOX必須クレジット）を確定生成
        description_text = script_gen.build_youtube_description(
            episode=episode,
            section_timestamps=timeline.section_timestamps,
        )
        desc_path = output_dir / "youtube_description.txt"
        desc_path.write_text(description_text, encoding="utf-8")

        # 4. サムネイル (1280x720) ＆ 本編動画 (1920x1080 30fps MP4) 生成
        renderer = VisualRenderer(config)
        thumbnail_path = output_dir / "thumbnail.png"
        renderer.render_thumbnail(episode, thumbnail_path)

        composer = VideoComposer(config, renderer)
        video_mp4_path = output_dir / "final_video.mp4"
        composer.compose_video(
            episode=episode,
            timeline=timeline,
            work_dir=work_dir,
            output_mp4_path=video_mp4_path,
        )

        # 5. YouTube Data API v3 自動アップロード＆公開
        if config.env.dry_run:
            logger.info("DRY_RUN=true のため YouTube アップロードをスキップしました。")
            upload_result = UploadResult(
                video_id="DRY_RUN_VIDEO_ID",
                video_url="https://www.youtube.com/watch?v=DRY_RUN_VIDEO_ID",
                privacy_status=config.env.youtube_privacy_status,
                thumbnail_applied=True,
                quota_cost_units=0,
            )
        else:
            uploader = YouTubeUploader(config)
            upload_result = uploader.upload_video(
                video_path=video_mp4_path,
                thumbnail_path=thumbnail_path,
                title=episode.metadata.title,
                description=description_text,
                tags=episode.metadata.tags,
                privacy_status=config.env.youtube_privacy_status,
            )
            notifier.record_publication_history(
                episode=episode,
                upload_result=upload_result,
                actual_duration_sec=timeline.total_duration_sec,
            )

        elapsed = time.time() - start_time
        notifier.send_webhook(
            status="SUCCESS",
            topic_keyword=episode.topic_keyword,
            title=episode.metadata.title,
            video_url=upload_result.video_url,
            duration_sec=timeline.total_duration_sec,
            insight_ratio=episode.insight_ratio,
            elapsed_sec=elapsed,
        )

        summary_report = {
            "status": "SUCCESS",
            "topic_keyword": episode.topic_keyword,
            "title": episode.metadata.title,
            "video_id": upload_result.video_id,
            "video_url": upload_result.video_url,
            "privacy_status": upload_result.privacy_status,
            "duration_sec": round(timeline.total_duration_sec, 1),
            "duration_min": round(timeline.total_duration_sec / 60.0, 2),
            "total_chars": episode.total_chars,
            "insight_ratio": episode.insight_ratio,
            "video_path": str(video_mp4_path),
            "thumbnail_path": str(thumbnail_path),
            "description_path": str(desc_path),
            "elapsed_sec": round(elapsed, 1),
        }
        (output_dir / "run_report.json").write_text(
            json.dumps(summary_report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("全工程が正常に完了しました！ レポート: %s", summary_report)
        return summary_report

    except Exception as e:
        elapsed = time.time() - start_time
        logger.exception("パイプライン実行中にエラーが発生しました: %s", e)
        notifier.send_webhook(
            status="ERROR",
            topic_keyword=selected_keyword,
            elapsed_sec=elapsed,
            error_message=str(e),
        )
        raise
    finally:
        if not keep_temp and work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="収益化特化型 YouTube 急上昇トレンド解説＆考察動画 完全自動生成・自動公開システム"
    )
    parser.add_argument(
        "--topic",
        type=str,
        default=None,
        help="解説するトレンドトピックを手動指定（未指定時は急上昇RSSから自動選定）",
    )
    parser.add_argument(
        "--privacy",
        type=str,
        choices=["public", "unlisted", "private"],
        default=None,
        help="YouTube公開設定の上書き (public / unlisted / private)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="動画・サムネイル生成まで実行し、YouTubeへのアップロードをスキップする",
    )
    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="作業用の一時音声・フレーム画像ディレクトリを削除せず保持する",
    )
    args = parser.parse_args()

    try:
        run_pipeline(
            force_topic=args.topic,
            privacy_override=args.privacy,
            dry_run_override=True if args.dry_run else None,
            keep_temp=args.keep_temp,
        )
    except Exception:
        sys.exit(1)


if __name__ == "__main__":
    main()
