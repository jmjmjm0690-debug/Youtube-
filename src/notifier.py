import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx

from src.config_loader import AppConfig
from src.script_generator import GeneratedEpisode
from src.youtube_uploader import UploadResult

logger = logging.getLogger(__name__)


class Notifier:
    """
    投稿履歴 (data/history.json) の更新・永続化および
    Discord / Slack 等への Webhook 通知を行うモジュール。
    """

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def record_publication_history(
        self,
        episode: GeneratedEpisode,
        upload_result: UploadResult,
        actual_duration_sec: float,
    ) -> None:
        """投稿完了したトピック・動画ID・クォータ消費量を data/history.json に記録"""
        history_path = self.config.history_path
        history_path.parent.mkdir(parents=True, exist_ok=True)

        now_utc = datetime.now(timezone.utc)
        today_str = now_utc.strftime("%Y-%m-%d")

        if history_path.exists():
            try:
                data = json.loads(history_path.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        else:
            data = {}

        data.setdefault("schema_version", "1.0")
        data["updated_at"] = now_utc.isoformat()

        daily_quota = data.get("daily_quota", {})
        if daily_quota.get("date") == today_str:
            prev_used = int(daily_quota.get("used_units", 0))
        else:
            prev_used = 0

        data["daily_quota"] = {
            "date": today_str,
            "used_units": prev_used + upload_result.quota_cost_units,
        }

        published_list = data.get("published_videos", [])
        published_list.append(
            {
                "video_id": upload_result.video_id,
                "video_url": upload_result.video_url,
                "topic_keyword": episode.topic_keyword,
                "title": episode.metadata.title,
                "privacy_status": upload_result.privacy_status,
                "duration_sec": round(actual_duration_sec, 1),
                "total_chars": episode.total_chars,
                "insight_ratio": episode.insight_ratio,
                "published_at": now_utc.isoformat(),
            }
        )
        # 直近500件を保持
        data["published_videos"] = published_list[-500:]

        history_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        logger.info("投稿履歴を保存しました: %s", history_path)

    def send_webhook(
        self,
        status: str,
        topic_keyword: str,
        title: str = "",
        video_url: str = "",
        duration_sec: float = 0.0,
        insight_ratio: float = 0.0,
        elapsed_sec: float = 0.0,
        error_message: str = "",
    ) -> None:
        """Discord または Slack の Webhook URL が設定されている場合に完了・エラー通知を送信"""
        webhook_url = self.config.env.webhook_url
        if not webhook_url:
            return

        if status == "SUCCESS":
            summary_text = (
                f"✅ **YouTube トレンド解説動画 自動公開完了**\n"
                f"・**選定トレンド**: `{topic_keyword}`\n"
                f"・**タイトル**: {title}\n"
                f"・**公開URL**: {video_url}\n"
                f"・**動画尺**: {duration_sec / 60.0:.1f}分 ({duration_sec:.0f}秒) / **独自考察比率**: {insight_ratio * 100:.1f}%\n"
                f"・**処理所要時間**: {elapsed_sec:.1f}秒"
            )
        else:
            summary_text = (
                f"⚠️ **YouTube トレンド解説動画 自動生成エラー**\n"
                f"・**対象トレンド**: `{topic_keyword or '選定中'}`\n"
                f"・**経過時間**: {elapsed_sec:.1f}秒\n"
                f"・**エラー内容**: {error_message[:500]}"
            )

        # Discord (content) / Slack (text) 両対応ペイロード
        payload: Dict[str, Any] = (
            {"content": summary_text}
            if "discord" in webhook_url.lower()
            else {"text": summary_text}
        )

        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(webhook_url, json=payload)
                resp.raise_for_status()
            logger.info("Webhook 通知を送信しました (status=%s)", status)
        except Exception as e:
            logger.warning("Webhook 通知の送信に失敗しました: %s", e)
