"""
文字化け防止・対話型ローカル動画生成テストランチャー
(scripts/interactive_dryrun.py)
"""

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from src.main import run_pipeline


def main() -> None:
    print("=" * 70)
    print(" [2] トレンド解説動画 ローカル生成テスト（YouTubeアップロードなし）")
    print("=" * 70)
    print()
    topic = input(
        "解説したいトピックを入力してください（空欄のままEnterで急上昇トレンドから自動選定）:\n> "
    ).strip()
    print()
    report = run_pipeline(
        force_topic=topic if topic else None,
        dry_run_override=True,
    )
    print("\n" + "=" * 70)
    print(" 動画・サムネイルの生成が完了しました！")
    print(f" ・動画ファイル  : {report['video_path']}")
    print(f" ・サムネイル画像: {report['thumbnail_path']}")
    print(f" ・概要欄テキスト: {report['description_path']}")
    print("=" * 70)


if __name__ == "__main__":
    main()
