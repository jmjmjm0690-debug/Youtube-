import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from tenacity import retry, stop_after_attempt, wait_exponential

from src.config_loader import AppConfig

logger = logging.getLogger(__name__)

YOUTUBE_UPLOAD_SCOPE = ["https://www.googleapis.com/auth/youtube.upload"]
TOKEN_URI = "https://oauth2.googleapis.com/token"


@dataclass
class UploadResult:
    video_id: str
    video_url: str
    privacy_status: str
    thumbnail_applied: bool
    quota_cost_units: int


class YouTubeUploader:
    """
    YouTube Data API v3 を用いて 1080p MP4 動画のアップロード、
    カスタムサムネイル設定、OAuth 2.0 アクセストークンの自動リフレッシュ、
    および1日あたりのAPIクォータ上限管理を行うモジュール。
    """

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.yt_cfg = config.settings.get("youtube", {})
        self.daily_quota_limit = int(
            self.yt_cfg.get("daily_quota_limit", 10000)
        )
        self.estimated_cost = int(
            self.yt_cfg.get("estimated_upload_cost", 1650)
        )

    def _check_and_reserve_quota(self) -> int:
        """data/history.json を参照し、本日の推定クォータ消費量が上限を超えないか検証する"""
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if not self.config.history_path.exists():
            return 0

        try:
            data = json.loads(
                self.config.history_path.read_text(encoding="utf-8")
            )
            quota_info = data.get("daily_quota", {})
            if quota_info.get("date") == today_str:
                used = int(quota_info.get("used_units", 0))
            else:
                used = 0

            if used + self.estimated_cost > self.daily_quota_limit:
                raise RuntimeError(
                    f"YouTube Data API の日次クォータ上限に達するため投稿を中止します "
                    f"(本日使用済: {used} / 上限: {self.daily_quota_limit} / 必要: {self.estimated_cost})"
                )
            return used
        except RuntimeError:
            raise
        except Exception as e:
            logger.warning("クォータ履歴の確認をスキップしました: %s", e)
            return 0

    def _build_authenticated_service(self):
        """リフレッシュトークンを用いてアクセストークンを自動更新し YouTube API クライアントを生成"""
        env = self.config.env
        if not (
            env.youtube_client_id
            and env.youtube_client_secret
            and env.youtube_refresh_token
        ):
            raise ValueError(
                "YouTube OAuth 2.0 認証情報 (YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET / YOUTUBE_REFRESH_TOKEN) "
                "が設定されていません。.env または GitHub Secrets を確認してください。"
            )

        creds = Credentials(
            token=None,
            refresh_token=env.youtube_refresh_token,
            token_uri=TOKEN_URI,
            client_id=env.youtube_client_id,
            client_secret=env.youtube_client_secret,
            scopes=YOUTUBE_UPLOAD_SCOPE,
        )
        creds.refresh(Request())
        return build("youtube", "v3", credentials=creds, cache_discovery=False)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=3, min=4, max=30))
    def upload_video(
        self,
        video_path: Path,
        thumbnail_path: Path,
        title: str,
        description: str,
        tags: List[str],
        privacy_status: Optional[str] = None,
        category_id: Optional[str] = None,
    ) -> UploadResult:
        """
        動画を YouTube にアップロードし、カスタムサムネイルを設定して公開状態にする。
        """
        self._check_and_reserve_quota()
        service = self._build_authenticated_service()

        effective_privacy = (
            privacy_status or self.config.env.youtube_privacy_status or "public"
        ).lower()
        effective_category = (
            category_id or self.config.env.youtube_category_id or "25"
        )
        made_for_kids = bool(self.yt_cfg.get("made_for_kids", False))

        body = {
            "snippet": {
                "title": title[:100],
                "description": description[:4900],
                "tags": tags[:20],
                "categoryId": effective_category,
                "defaultLanguage": "ja",
                "defaultAudioLanguage": "ja",
            },
            "status": {
                "privacyStatus": effective_privacy,
                "selfDeclaredMadeForKids": made_for_kids,
            },
        }

        media = MediaFileUpload(
            str(video_path),
            mimetype="video/mp4",
            chunksize=8 * 1024 * 1024,
            resumable=True,
        )

        logger.info(
            "YouTube への動画アップロードを開始します (title='%s', privacy='%s')...",
            title,
            effective_privacy,
        )
        insert_req = service.videos().insert(
            part="snippet,status",
            body=body,
            media_body=media,
        )

        response = None
        while response is None:
            status, response = insert_req.next_chunk()
            if status:
                pct = int(status.progress() * 100)
                logger.info("YouTube アップロード進行状況: %d%%", pct)

        video_id = response["id"]
        video_url = f"https://www.youtube.com/watch?v={video_id}"
        logger.info("動画アップロード完了: %s", video_url)

        # カスタムサムネイル設定
        thumb_applied = False
        if thumbnail_path.exists():
            thumb_applied = self._set_thumbnail(
                service, video_id, thumbnail_path
            )

        quota_used = 1650 if thumb_applied else 1600
        return UploadResult(
            video_id=video_id,
            video_url=video_url,
            privacy_status=effective_privacy,
            thumbnail_applied=thumb_applied,
            quota_cost_units=quota_used,
        )

    def _set_thumbnail(
        self, service, video_id: str, thumbnail_path: Path
    ) -> bool:
        for attempt in range(1, 4):
            try:
                media = MediaFileUpload(
                    str(thumbnail_path), mimetype="image/png", resumable=False
                )
                service.thumbnails().set(
                    videoId=video_id, media_body=media
                ).execute()
                logger.info(
                    "カスタムサムネイルの適用が完了しました (video_id=%s)", video_id
                )
                return True
            except HttpError as e:
                logger.warning(
                    "カスタムサムネイル設定リトライ (%d/3): %s", attempt, e
                )
                time.sleep(2 * attempt)
            except Exception as e:
                logger.warning("カスタムサムネイル設定エラー: %s", e)
                break
        return False
