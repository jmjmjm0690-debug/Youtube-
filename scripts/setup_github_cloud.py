"""
GitHub Actions クラウド完全自動運用（PC電源オフ対応）セットアップウィザード
(scripts/setup_github_cloud.py)

このスクリプトを実行すると:
1. ローカルの Git リポジトリ初期化・コミットを自動で行います（.env 等の機密ファイルは除外）。
2. ブラウザで GitHub の新規リポジトリ作成画面を開きます。
3. 作成した GitHub リポジトリの URL を貼り付けるだけで、全コードと坂本アヒル様立ち絵アセットを自動プッシュします。
4. GitHub Actions の Secrets 登録画面を自動で開き、.env から読み取った4つの登録値を画面に一覧表示します。
"""

import os
import re
import subprocess
import webbrowser
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT_DIR / ".env"


def _run_git(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(ROOT_DIR),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=check,
    )


def ensure_git_initialized_and_committed() -> None:
    if not (ROOT_DIR / ".git").exists():
        _run_git(["init", "-b", "main"])

    # ローカルの git user.name / user.email が未設定の場合のデフォルト設定
    name_res = _run_git(["config", "user.name"], check=False)
    if not name_res.stdout.strip():
        _run_git(["config", "user.name", "YouTube Trend Auto Studio"])
    email_res = _run_git(["config", "user.email"], check=False)
    if not email_res.stdout.strip():
        _run_git(["config", "user.email", "auto-studio@users.noreply.github.com"])

    _run_git(["branch", "-M", "main"], check=False)
    _run_git(["add", "."])
    status = _run_git(["status", "--porcelain"], check=False)
    if status.stdout.strip():
        _run_git(
            [
                "commit",
                "-m",
                "Initial commit: YouTube Trend Auto Studio (VOICEVOX Metan & Zundamon)",
            ],
            check=False,
        )


def main() -> None:
    load_dotenv(ENV_PATH, override=True)
    print("=" * 74)
    print(" [5] GitHub Actions クラウド完全自動運用セットアップ（PC電源オフで毎日自動投稿）")
    print("=" * 74)

    print("\n[ステップ 1/3] ローカル Git リポジトリの準備中...")
    ensure_git_initialized_and_committed()
    print("  -> コード・坂本アヒル様立ち絵20枚・BGM・GitHub Actions設定のコミットが完了しました！")
    print("     （※ .env や client_secret.json などの秘密情報は安全に除外されています）")

    print("\n[ステップ 2/3] GitHub 空リポジトリの作成とコードのアップロード")
    print("  1. GitHub で新しい空のリポジトリを作成します（Private / 非公開リポジトリでOKです）。")
    print("     ・Repository name 例: youtube-trend-auto-studio")
    print("     ・Add a README file 等はすべて【チェックなし（空のまま）】で「Create repository」を押します。")

    open_gh = (
        input("\n  今すぐブラウザで GitHub 新規リポジトリ作成ページ (https://github.com/new) を開きますか？ (y/n) [y]: ")
        .strip()
        .lower()
    )
    if open_gh in ("", "y", "yes"):
        webbrowser.open("https://github.com/new")

    print("\n  作成した GitHub リポジトリの URL（例: https://github.com/ユーザー名/youtube-trend-auto-studio.git）")
    repo_url = input("  を貼り付けて Enter を押してください:\n  > ").strip()
    if not repo_url:
        print("\n  [!] URL が入力されませんでした。またいつでもこのバッチから再開できます。")
        return

    if not repo_url.endswith(".git") and "github.com/" in repo_url:
        repo_url = repo_url.rstrip("/") + ".git"

    _run_git(["remote", "remove", "origin"], check=False)
    _run_git(["remote", "add", "origin", repo_url])

    print(f"\n  GitHub ({repo_url}) へコードをプッシュしています...")
    print("  （※GitHub のログイン画面が出た場合はブラウザで承認してください）")
    push_proc = subprocess.run(
        ["git", "push", "-u", "origin", "main"],
        cwd=str(ROOT_DIR),
    )
    if push_proc.returncode != 0:
        print("\n  [!] プッシュに失敗しました。URLまたはGitHubのログイン状態を確認して再実行してください。")
        return

    print("\n  -> GitHub への全コードのアップロードが成功しました！")

    # リポジトリの Secrets 設定URLと Actions URL を算出
    base_web_url = re.sub(r"\.git$", "", repo_url)
    secrets_url = f"{base_web_url}/settings/secrets/actions"
    actions_url = f"{base_web_url}/actions"

    print("\n" + "=" * 74)
    print(" [ステップ 3/3] GitHub Secrets への認証情報の登録（最後の1ステップ！）")
    print("=" * 74)
    print(f"  以下の URL（自動でブラウザを開きます）の「New repository secret」ボタンから、")
    print(f"  下記の 4 つの Name と Secret をコピー＆ペーストして登録してください：\n")
    print(f"  登録先ページ: {secrets_url}\n")
    print("-" * 74)
    for key in [
        "GEMINI_API_KEY",
        "YOUTUBE_CLIENT_ID",
        "YOUTUBE_CLIENT_SECRET",
        "YOUTUBE_REFRESH_TOKEN",
        "NOTIFY_EMAIL_TO",
        "SMTP_USER",
        "SMTP_PASSWORD",
    ]:
        val = os.getenv(key, "").strip()
        if not val or val.lower().startswith("your_"):
            continue
        print(f"  【Name】  : {key}")
        print(f"  【Secret】: {val}")
        print("-" * 74)

    webbrowser.open(secrets_url)

    print("\n  ★ 上記を登録したら、以下の Actions タブを開いて「Run workflow」を押すと")
    print("    クラウド上で今すぐテスト実行でき、以降は【PCの電源が切れていても1日6回（4時間おき）】に自動投稿されます！")
    print(f"  Actions ページ: {actions_url}")
    print("=" * 74)


if __name__ == "__main__":
    main()
