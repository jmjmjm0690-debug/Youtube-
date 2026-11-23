import json
import logging
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Any, Dict

import httpx

from src.config_loader import AppConfig
from src.script_generator import GeneratedEpisode
from src.youtube_uploader import UploadResult

logger = logging.getLogger(__name__)


class Notifier:
    """
    投稿履歴 (data/history.json) の更新・永続化、
    投稿完了・エラー時のメール通知 (Gmail SMTP等)、
    および Discord / Slack 等への Webhook 通知を行うモジュール。
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
        data["published_videos"] = published_list[-500:]

        history_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        logger.info("投稿履歴を保存しました: %s", history_path)

    def send_email(
        self,
        status: str,
        topic_keyword: str,
        title: str = "",
        video_url: str = "",
        duration_sec: float = 0.0,
        insight_ratio: float = 0.0,
        elapsed_sec: float = 0.0,
        error_message: str = "",
    ) -> bool:
        """投稿完了またはエラー時にメールで通知を送信する"""
        env = self.config.env
        to_addr = env.notify_email_to or env.smtp_user
        smtp_user = env.smtp_user or env.notify_email_to
        smtp_pass = env.smtp_password.replace(" ", "")

        if not (to_addr and smtp_user and smtp_pass):
            return False

        msg = EmailMessage()
        msg["From"] = smtp_user
        msg["To"] = to_addr

        if status == "SUCCESS":
            msg["Subject"] = f"【YouTube自動投稿完了】{title or topic_keyword}"
            body = (
                f"YouTube 急上昇トレンド解説＆考察動画の自動生成・公開が完了しました！\n\n"
                f"■ 公開動画URL:\n{video_url}\n\n"
                f"■ 動画タイトル:\n{title}\n\n"
                f"■ 選定トレンド:\n{topic_keyword}\n\n"
                f"■ 動画スペック:\n"
                f"・再生時間: {duration_sec / 60.0:.1f}分 ({duration_sec:.0f}秒)\n"
                f"・独自考察比率: {insight_ratio * 100:.1f}%\n"
                f"・公開設定: {env.youtube_privacy_status}\n"
                f"・処理所要時間: {elapsed_sec:.1f}秒\n"
            )
        else:
            msg["Subject"] = f"【YouTube自動投稿エラー】{topic_keyword or 'トレンド動画生成'}"
            body = (
                f"YouTube トレンド解説動画の自動生成中にエラーが発生しました。\n\n"
                f"■ 対象トレンド: {topic_keyword or '選定中'}\n"
                f"■ 経過時間: {elapsed_sec:.1f}秒\n"
                f"■ エラー詳細:\n{error_message[:1000]}\n"
            )

        msg.set_content(body)

        try:
            if env.smtp_port == 465:
                with smtplib.SMTP_SSL(env.smtp_host, env.smtp_port, timeout=20.0) as server:
                    server.login(smtp_user, smtp_pass)
                    server.send_message(msg)
            else:
                with smtplib.SMTP(env.smtp_host, env.smtp_port, timeout=20.0) as server:
                    server.starttls()
                    server.login(smtp_user, smtp_pass)
                    server.send_message(msg)
            logger.info("完了通知メールを送信しました (宛先=%s, status=%s)", to_addr, status)
            return True
        except Exception as e:
            logger.warning("メール通知の送信に失敗しました: %s", e)
            return False

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
        """メール通知および Webhook (Discord / Slack) 通知を一括実行する"""
        self.send_email(
            status=status,
            topic_keyword=topic_keyword,
            title=title,
            video_url=video_url,
            duration_sec=duration_sec,
            insight_ratio=insight_ratio,
            elapsed_sec=elapsed_sec,
            error_message=error_message,
        )

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
