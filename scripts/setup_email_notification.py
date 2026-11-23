"""
投稿完了メール通知（Gmail SMTP）対話型セットアップ＆テスト送信ウィザード
(scripts/setup_email_notification.py)
"""

import os
import sys
import webbrowser
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from scripts.get_youtube_refresh_token import _update_env_file
from src.config_loader import AppConfig
from src.notifier import Notifier

ENV_PATH = ROOT_DIR / ".env"


def main() -> None:
    load_dotenv(ENV_PATH, override=True)
    print("=" * 72)
    print(" [6] YouTube 自動投稿完了メール通知 セットアップウィザード")
    print("=" * 72)

    default_email = (
        os.getenv("NOTIFY_EMAIL_TO", "").strip() or "jmjmjm0690@gmail.com"
    )
    print(f"\n1. 通知を受け取るメールアドレスを入力してください (Enterで {default_email}):")
    to_email = input(f"   宛先メールアドレス [{default_email}] > ").strip() or default_email

    default_user = os.getenv("SMTP_USER", "").strip() or to_email
    print(f"\n2. 送信元として使う Gmail アドレスを入力してください (Enterで {default_user}):")
    smtp_user = input(f"   Gmail アドレス [{default_user}] > ").strip() or default_user

    print("\n3. Gmail の「アプリ パスワード（16文字）」の設定")
    print("   ------------------------------------------------------------------")
    print("   ※通常のGoogleログインパスワードではなく、Googleアカウントの")
    print("     「アプリ パスワード（16文字の英字）」を使用します。")
    print("   取得URL: https://myaccount.google.com/apppasswords")
    print("   （アプリ名に「YouTube自動投稿」などと入力して「作成」を押すと16文字が表示されます）")
    print("   ------------------------------------------------------------------")

    open_ap = (
        input("\n   今すぐブラウザで Google アプリパスワード作成画面を開きますか？ (y/n) [y]: ")
        .strip()
        .lower()
    )
    if open_ap in ("", "y", "yes"):
        webbrowser.open("https://myaccount.google.com/apppasswords")

    smtp_pass = (
        input("\n   表示された16文字の「アプリ パスワード」を貼り付けてください:\n   > ")
        .strip()
        .replace(" ", "")
    )
    if not smtp_pass:
        print("\n   [!] アプリパスワードが入力されませんでした。中断します。")
        return

    _update_env_file(
        {
            "NOTIFY_EMAIL_TO": to_email,
            "SMTP_HOST": "smtp.gmail.com",
            "SMTP_PORT": "465",
            "SMTP_USER": smtp_user,
            "SMTP_PASSWORD": smtp_pass,
        }
    )
    print("\n   -> .env にメール通知設定を保存しました！")

    print(f"\n   {to_email} 宛てにテスト通知メールを送信しています...")
    config = AppConfig()
    notifier = Notifier(config)
    ok = notifier.send_email(
        status="SUCCESS",
        topic_keyword="メール通知テスト（1日6回自動投稿システム）",
        title="【テスト通知】YouTube自動投稿完了メールの設定が完了しました！",
        video_url="https://www.youtube.com/",
        duration_sec=600.0,
        insight_ratio=0.68,
        elapsed_sec=1.0,
    )
    if ok:
        print("\n" + "=" * 72)
        print(f" テストメールの送信に成功しました！ ({to_email} の受信トレイを確認してください)")
        print(" これ以降、動画の自動投稿が完了するたびにメールが届きます！")
        print("=" * 72)
    else:
        print("\n   [!] テストメールの送信に失敗しました。アプリパスワード(16文字)と2段階認証設定を確認してください。")


if __name__ == "__main__":
    main()
