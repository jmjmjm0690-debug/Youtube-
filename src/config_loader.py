import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

import yaml
from dotenv import load_dotenv

# プロジェクトルートディレクトリ
ROOT_DIR = Path(__file__).resolve().parent.parent


class SecretMaskingFilter(logging.Filter):
    """ログ・エラー出力からAPIキーやOAuthトークン等の機密情報を自動的にマスクするフィルタ"""

    def __init__(self, secrets: List[str]) -> None:
        super().__init__()
        self.secrets = [s for s in secrets if s and len(s) >= 6]

    def _mask_text(self, text: str) -> str:
        masked = text
        for secret in self.secrets:
            if secret in masked:
                masked = masked.replace(secret, "***MASKED_SECRET***")
        return masked

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self._mask_text(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: self._mask_text(str(v)) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    self._mask_text(str(a)) if isinstance(a, str) else a
                    for a in record.args
                )
        return True


@dataclass
class EnvConfig:
    gemini_api_key: str
    gemini_model: str
    gemini_fast_model: str
    voicevox_base_url: str
    youtube_client_id: str
    youtube_client_secret: str
    youtube_refresh_token: str
    youtube_privacy_status: str
    youtube_category_id: str
    webhook_url: str
    force_topic_keyword: str
    dry_run: bool


class AppConfig:
    """システム全体の設定・プロンプト・環境変数を一元管理するクラス"""

    def __init__(self, root_dir: Path = ROOT_DIR) -> None:
        self.root_dir = root_dir
        load_dotenv(self.root_dir / ".env")

        self.settings_path = self.root_dir / "config" / "settings.yaml"
        self.prompts_path = self.root_dir / "config" / "prompts.yaml"
        self.dict_path = self.root_dir / "config" / "pronunciation_dict.yaml"
        self.history_path = self.root_dir / "data" / "history.json"

        self.settings: Dict[str, Any] = self._load_yaml(self.settings_path)
        self.prompts: Dict[str, Any] = self._load_yaml(self.prompts_path)
        self.pronunciation_dict: Dict[str, Any] = self._load_yaml(self.dict_path)
        self.env: EnvConfig = self._load_env()

        self._setup_logging()

    @staticmethod
    def _load_yaml(path: Path) -> Dict[str, Any]:
        if not path.exists():
            raise FileNotFoundError(f"設定ファイルが見つかりません: {path}")
        with path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    def _load_env(self) -> EnvConfig:
        vv_default = (
            self.settings.get("voicevox", {}).get(
                "default_base_url", "http://127.0.0.1:50021"
            )
        )
        yt_cfg = self.settings.get("youtube", {})
        default_privacy = yt_cfg.get("default_privacy_status", "public")
        default_cat = str(yt_cfg.get("default_category_id", "25"))

        privacy = os.getenv("YOUTUBE_PRIVACY_STATUS", default_privacy).strip().lower()
        if privacy not in ("public", "unlisted", "private"):
            privacy = "public"

        dry_run_str = os.getenv("DRY_RUN", "false").strip().lower()
        dry_run = dry_run_str in ("true", "1", "yes")

        def _clean_env(key: str, default: str = "") -> str:
            val = os.getenv(key, default).strip()
            if val.lower().startswith("your_"):
                return ""
            return val

        # 廃止された旧2.5系や混雑中の3.8系が指定されている場合は高速安定な3.6/3.5系を優先
        raw_main_model = _clean_env("GEMINI_MODEL", "gemini-3.6-flash") or "gemini-3.6-flash"
        if "2.5" in raw_main_model or "3.8" in raw_main_model:
            raw_main_model = "gemini-3.6-flash"

        raw_fast_model = _clean_env("GEMINI_FAST_MODEL", "gemini-3.5-flash") or "gemini-3.5-flash"
        if "2.5" in raw_fast_model or "3.8" in raw_fast_model:
            raw_fast_model = "gemini-3.5-flash"

        return EnvConfig(
            gemini_api_key=_clean_env("GEMINI_API_KEY"),
            gemini_model=raw_main_model,
            gemini_fast_model=raw_fast_model,
            voicevox_base_url=_clean_env("VOICEVOX_BASE_URL", vv_default).rstrip("/"),
            youtube_client_id=_clean_env("YOUTUBE_CLIENT_ID"),
            youtube_client_secret=_clean_env("YOUTUBE_CLIENT_SECRET"),
            youtube_refresh_token=_clean_env("YOUTUBE_REFRESH_TOKEN"),
            youtube_privacy_status=privacy,
            youtube_category_id=_clean_env("YOUTUBE_CATEGORY_ID", default_cat) or default_cat,
            webhook_url=_clean_env("WEBHOOK_URL"),
            force_topic_keyword=_clean_env("FORCE_TOPIC_KEYWORD"),
            dry_run=dry_run,
        )

    def _setup_logging(self) -> None:
        secrets = [
            self.env.gemini_api_key,
            self.env.youtube_client_id,
            self.env.youtube_client_secret,
            self.env.youtube_refresh_token,
            self.env.webhook_url,
        ]
        mask_filter = SecretMaskingFilter(secrets)

        root_logger = logging.getLogger()
        if not root_logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter(
                "[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
            handler.setFormatter(formatter)
            root_logger.addHandler(handler)
            root_logger.setLevel(logging.INFO)

        for handler in root_logger.handlers:
            handler.addFilter(mask_filter)
