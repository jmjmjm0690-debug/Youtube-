"""
YouTube Data API v3 & Gemini API 対話型セットアップウィザード
(scripts/get_youtube_refresh_token.py)

特徴:
1. .env の初期プレースホルダー (your_...) を自動判別し、誤ってダミーIDで認証が開くのを防止します。
2. プロジェクトフォルダまたは「ダウンロード (Downloads)」フォルダ内の `client_secret*.json` を自動検出します。
3. まだ Google Cloud の OAuth クライアントIDを作成していない場合は、分かりやすい手順を表示し、
   その場で Client ID / Client Secret（および Gemini API キー）を入力して `.env` を自動更新できます。
4. ブラウザ認証完了後、取得した `YOUTUBE_REFRESH_TOKEN` を `.env` に自動保存します。
"""

import argparse
import os
import re
import webbrowser
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT_DIR / ".env"


def _is_placeholder(val: str) -> bool:
    v = val.strip().lower()
    return (
        not v
        or v.startswith("your_")
        or "your_google_oauth" in v
        or "your_gemini_api_key" in v
    )


def _update_env_file(updates: dict) -> None:
    """指定されたキーと値を .env ファイルに自動保存する"""
    if not ENV_PATH.exists():
        example = ROOT_DIR / ".env.example"
        if example.exists():
            ENV_PATH.write_text(
                example.read_text(encoding="utf-8"), encoding="utf-8"
            )
        else:
            ENV_PATH.write_text("", encoding="utf-8")

    content = ENV_PATH.read_text(encoding="utf-8")
    for key, val in updates.items():
        if not val:
            continue
        pattern = rf"^{re.escape(key)}=.*$"
        replacement = f"{key}={val}"
        if re.search(pattern, content, flags=re.MULTILINE):
            content = re.sub(
                pattern, replacement, content, flags=re.MULTILINE
            )
        else:
            content += f"\n{replacement}\n"

    ENV_PATH.write_text(content, encoding="utf-8")


def _find_client_secret_json() -> Optional[Path]:
    """プロジェクト直下、またはユーザーの Downloads フォルダから client_secret*.json を探す"""
    direct = ROOT_DIR / "client_secret.json"
    if direct.exists():
        return direct

    local_matches = sorted(
        ROOT_DIR.glob("client_secret*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if local_matches:
        return local_matches[0]

    downloads_dir = Path.home() / "Downloads"
    if downloads_dir.exists():
        dl_matches = sorted(
            downloads_dir.glob("client_secret*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if dl_matches:
            return dl_matches[0]

    return None


def main() -> None:
    load_dotenv(ENV_PATH, override=True)
    parser = argparse.ArgumentParser(
        description="YouTube Data API v3 アップロード用 OAuth 2.0 リフレッシュトークン取得ツール"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="ローカル認証コールバック用ポート番号 (デフォルト: 0 = 空きポートを自動割当)",
    )
    args = parser.parse_args()

    print("=" * 72)
    print(" YouTube 自動投稿 & Gemini API 初期セットアップウィザード")
    print("=" * 72)

    # 1. Gemini API キーの確認
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    if _is_placeholder(gemini_key):
        print("\n【ステップ 1/2】Gemini API キーの設定（任意：後で設定する場合は空欄でEnter）")
        print("  取得先: https://aistudio.google.com/app/apikey")
        entered_gemini = input("  GEMINI_API_KEY を貼り付けてください > ").strip()
        if entered_gemini:
            _update_env_file({"GEMINI_API_KEY": entered_gemini})
            print("  -> .env に GEMINI_API_KEY を保存しました！")

    # 2. YouTube OAuth 2.0 クライアント情報の確認
    print("\n【ステップ 2/2】YouTube Data API v3 (OAuth 2.0) の認証セットアップ")
    secrets_json_path = _find_client_secret_json()
    client_id = os.getenv("YOUTUBE_CLIENT_ID", "").strip()
    client_secret = os.getenv("YOUTUBE_CLIENT_SECRET", "").strip()

    if secrets_json_path:
        print(f"  -> OAuth クライアントJSONを検出しました: {secrets_json_path}")
        flow = InstalledAppFlow.from_client_secrets_file(
            str(secrets_json_path), SCOPES
        )
    elif not _is_placeholder(client_id) and not _is_placeholder(client_secret):
        print("  -> .env に設定済みの YOUTUBE_CLIENT_ID を使用します。")
        client_config = {
            "installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        }
        flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    else:
        print(
            "\n  まだ Google Cloud の OAuth クライアントID（または client_secret.json）が設定されていません。"
        )
        print("  --------------------------------------------------------------------")
        print("  【Google Cloud Console での取得手順（約2分）】")
        print("  1. https://console.cloud.google.com/apis/library/youtube.googleapis.com")
        print("     を開き、「YouTube Data API v3」を【有効にする】を押します。")
        print("  2. https://console.cloud.google.com/apis/credentials")
        print("     を開き、「＋認証情報を作成」＞「OAuth クライアント ID」を選びます。")
        print("     （※初回のみ「同意画面を構成」が出る場合は【外部】を選び、テストユーザーに自分のGmailを追加してください）")
        print("  3. アプリケーションの種類で【デスクトップ アプリ】を選んで作成します。")
        print("  4. 表示された「クライアント ID」と「クライアント シークレット」を下に貼り付けるか、")
        print(f"     「JSONをダウンロード」して {ROOT_DIR} に置いてください。")
        print("  --------------------------------------------------------------------")

        open_choice = input(
            "\n  今すぐブラウザで Google Cloud Console を開きますか？ (y/n) [y]: "
        ).strip().lower()
        if open_choice in ("", "y", "yes"):
            webbrowser.open(
                "https://console.cloud.google.com/apis/credentials"
            )

        print("\n  JSONファイルをダウンロードした場合は、そのまま Enter を押してください（自動検索します）。")
        print("  直接入力する場合は、以下に「クライアント ID」を貼り付けてください:")
        input_id = input("  YOUTUBE_CLIENT_ID > ").strip()

        if not input_id:
            secrets_json_path = _find_client_secret_json()
            if not secrets_json_path:
                print(
                    "\n  [!] client_secret.json が見つかりませんでした。作成後に再度このバッチを実行してください。"
                )
                return
            print(f"  -> JSONファイルを検出しました: {secrets_json_path}")
            flow = InstalledAppFlow.from_client_secrets_file(
                str(secrets_json_path), SCOPES
            )
        else:
            input_secret = input("  YOUTUBE_CLIENT_SECRET > ").strip()
            if not input_secret:
                print("\n  [!] クライアントシークレットが入力されませんでした。中断します。")
                return
            _update_env_file(
                {
                    "YOUTUBE_CLIENT_ID": input_id,
                    "YOUTUBE_CLIENT_SECRET": input_secret,
                }
            )
            print("  -> YOUTUBE_CLIENT_ID と YOUTUBE_CLIENT_SECRET を .env に保存しました！")
            client_config = {
                "installed": {
                    "client_id": input_id,
                    "client_secret": input_secret,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["http://localhost"],
                }
            }
            flow = InstalledAppFlow.from_client_config(client_config, SCOPES)

    print("\n  ブラウザを開いて YouTube チャンネルのアカウント認証を開始します...")
    creds = flow.run_local_server(
        port=args.port,
        access_type="offline",
        prompt="consent",
    )

    _update_env_file(
        {
            "YOUTUBE_CLIENT_ID": creds.client_id,
            "YOUTUBE_CLIENT_SECRET": creds.client_secret,
            "YOUTUBE_REFRESH_TOKEN": creds.refresh_token,
        }
    )

    print("\n" + "=" * 72)
    print(" OAuth 2.0 認証に成功し、.env ファイルへ自動保存しました！")
    print("=" * 72)
    print(f"YOUTUBE_CLIENT_ID={creds.client_id}")
    print(f"YOUTUBE_CLIENT_SECRET={creds.client_secret}")
    print(f"YOUTUBE_REFRESH_TOKEN={creds.refresh_token}")
    print("=" * 72)
    print(" -> これで「3_YouTube自動生成＆公開実行.bat」からYouTubeへ自動投稿できます！")


if __name__ == "__main__":
    main()
