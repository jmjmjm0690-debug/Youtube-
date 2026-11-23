# 収益化特化型 YouTube 急上昇トレンド解説＆考察動画 完全自動生成・自動公開システム
（VOICEVOX：四国めたん＆ずんだもん 掛け合い解説版）

日本の最新「急上昇トレンド」を自動取得し、YouTube パートナー プログラム（YPP）の収益化審査・広告単価（RPM）最大化に特化した**16:9 横長・8分〜12分の「事実整理（35%以下）＋独自の深掘り構造考察（65%以上）」動画**を、**四国めたん（ノーマル）＋ ずんだもん（ノーマル）**の2人掛け合い形式で完全自動生成し、YouTube Data API v3 で**自動アップロード＆公開（`public`）**まで完結させるシステムです。

---

## 1. システムの主な特徴（YPP 収益化要件への完全準拠）

1. **16:9 通常動画（1920×1080 / 30fps）＆ 8分以上の尺保証**:
   - ミッドロール広告の挿入基準である **8分（480秒）以上**（標準 8.5〜11.5分 / 約4,500〜5,800文字）を5章構成で生成。万一文字数が不足した場合は自動で深掘り考察パートを追加展開します。
2. **「量産型・再利用コンテンツ」判定を回避する独自性と視覚演出**:
   - 台本全体の **60%以上** を「なぜ今起きたかの構造分析」「今後の複数シナリオ予測」「視聴者のメリット・デメリットと対策」で構成。
   - 話している考察・話題の内容に合わせて、2人の後ろの大型モニター画面とスタジオ背景が **8種類のビジュアル図解画面（急上昇注目度メーター、時系列フロー図、数値バーチャート、SNS世論ドーナツ円グラフ、3大要因の構造相関マップ、左右2カラムVS比較ボード、今後1〜3年の未来シナリオ予測曲線、実践対策チェックリスト）** へ動的に切り替わります。
   - **坂本アヒル様の公式PSD立ち絵素材（四国めたん立ち絵素材2.1 ＆ ずんだもん立ち絵素材2.3）** から全5種類の感情タグ（`normal` / `happy` / `surprised` / `serious` / `troubled`）× 本物の口パク開閉（`close` / `open`）および腕ポーズ差分を展開。しゃべっていない側も暗くならず、常に鮮やかで明るい立ち絵のまま掛け合いを行います。
3. **VOICEVOX 利用規約の完全順守（プログラム強制挿入）**:
   - ローカル実行時はプロジェクト組み込みの **VOICEVOX 公式コアエンジン（`voicevox_core` + `0.vvm`）**、GitHub Actions / Docker 実行時は **VOICEVOX Engine コンテナ** を使用し、本物の四国めたん＆ずんだもん音声で合成します。
   - 概要欄に以下のクレジット表記およびチャプター目次タイムスタンプを自動挿入します：
     - `VOICEVOX:四国めたん`（Speaker ID: `2` ノーマル固定）
     - `VOICEVOX:ずんだもん`（Speaker ID: `3` ノーマル固定）
     - `立ち絵:坂本アヒル様`

---

## 2. セットアップ手順

### 2.1 Google Cloud Console での YouTube Data API v3 有効化と OAuth 2.0 取得

1. [Google Cloud Console](https://console.cloud.google.com/) で新規プロジェクトを作成します。
2. **「APIとサービス」 > 「ライブラリ」** から **`YouTube Data API v3`** を検索して「有効にする」をクリックします。
3. **「APIとサービス」 > 「OAuth 同意画面」** を設定します：
   - User Type: **外部（External）**
   - スコープに `.../auth/youtube.upload` を追加します。
   - **重要**: 同意画面の公開ステータスが「テスト」のままだと リフレッシュトークンが7日間で失効するため、長期自動運用する場合は **「アプリを公開（本番環境）」** に切り替えるか、テストユーザーにご自身の Google アカウントを登録してください。
4. **「APIとサービス」 > 「認証情報」 > 「認証情報を作成」 > 「OAuth クライアント ID」** を選択します：
   - アプリケーションの種類: **デスクトップ アプリ**
   - 作成後、JSON をダウンロードしてプロジェクト直下に `client_secret.json` という名前で保存します。
5. ローカル環境で以下のコマンドを実行し、リフレッシュトークンを取得します：
   ```bash
   pip install -r requirements.txt
   python scripts/get_youtube_refresh_token.py
   ```
6. ブラウザが立ち上がるので、動画を投稿したい YouTube チャンネルの Google アカウントでログインして権限を許可します。ターミナルに表示された以下の3つの値を控えます：
   - `YOUTUBE_CLIENT_ID`
   - `YOUTUBE_CLIENT_SECRET`
   - `YOUTUBE_REFRESH_TOKEN`

> [!NOTE]
> **カスタムサムネイル自動設定（`thumbnails.set`）の前提条件**
> YouTube 側の仕様により、API からカスタムサムネイルを設定するには、対象の YouTube チャンネルで **[電話番号による本人確認（中級者向け機能）](https://www.youtube.com/verify)** が完了している必要があります。必ず事前に YouTube Studio の「設定 > チャンネル > 機能の利用資格」から有効化してください。

---

### 2.2 キャラクター立ち絵・BGM素材の配置方法（利用規約・クレジット確認）

本システムは、**素材を1枚も配置していない初期状態でも、`scripts/generate_default_assets.py` により全5表情×口パク開閉の計20枚の立ち絵アバターと著作権フリーBGMを自動生成して即座に動作**します。

公式・有志の立ち絵素材（例：坂本アヒル様、浅井麻様などの商用・YouTube収益化可能な立ち絵）に差し替える場合は、以下のファイル名で PNG 画像（透過背景推奨）を配置してください：

```text
assets/
├── characters/
│   ├── metan/                  # 四国めたん立ち絵
│   │   ├── normal_close.png    # 通常（口閉じ）
│   │   ├── normal_open.png     # 通常（口開き）
│   │   ├── happy_close.png     # 笑顔（口閉じ）
│   │   ├── happy_open.png      # 笑顔（口開き）
│   │   ├── surprised_close.png # 驚き（口閉じ）
│   │   ├── surprised_open.png  # 驚き（口開き）
│   │   ├── serious_close.png   # 真剣（口閉じ）
│   │   ├── serious_open.png    # 真剣（口開き）
│   │   ├── troubled_close.png  # 困り顔（口閉じ）
│   │   └── troubled_open.png   # 困り顔（口開き）
│   └── zundamon/               # ずんだもん立ち絵（同上のファイル名構成）
└── bgm/
    └── default_loop.wav        # 任意の著作権フリーBGM (.wav / .mp3)
```

* ※特定の表情差分がない場合は、`normal_close.png` と `normal_open.png` の2枚だけを配置しても、他の表情が自動的に `normal` にフォールバックして動作します。
* **立ち絵作者様のクレジット追記**: 使用する立ち絵の利用規約で概要欄への作者名表記（例：`立ち絵: 坂本アヒル様` 等）が求められている場合は、`config/settings.yaml` の `monetization_policy.required_credits` リストに追記するだけで全動画の概要欄へ自動挿入されます。

---

### 2.3 環境変数 / GitHub Secrets 一覧

GitHub リポジトリの **Settings > Secrets and variables > Actions** に以下を登録してください（ローカル実行時は `.env.example` を `.env` にコピーして記入します）。

#### GitHub Secrets（機密情報）
| Secret 名 | 必須 | 内容 |
| :--- | :---: | :--- |
| `GEMINI_API_KEY` | **必須** | Google AI Studio で取得した Gemini API キー |
| `YOUTUBE_CLIENT_ID` | **必須** | Google Cloud OAuth 2.0 クライアント ID |
| `YOUTUBE_CLIENT_SECRET` | **必須** | Google Cloud OAuth 2.0 クライアントシークレット |
| `YOUTUBE_REFRESH_TOKEN` | **必須** | `scripts/get_youtube_refresh_token.py` で取得したリフレッシュトークン |
| `WEBHOOK_URL` | 任意 | 完了・エラー通知を送る Discord または Slack の Incoming Webhook URL |

#### GitHub Variables または環境変数（動作設定）
| 変数名 | デフォルト値 | 内容 |
| :--- | :--- | :--- |
| `GEMINI_MODEL` | `gemini-2.5-pro` | 掛け合い台本・深掘り考察生成に使用するメインモデル |
| `GEMINI_FAST_MODEL` | `gemini-2.5-flash` | トレンド候補の一次スコアリング・メタデータ生成用モデル |
| `YOUTUBE_PRIVACY_STATUS` | `public` | `public`（完全自動公開）/ `unlisted`（限定公開）/ `private`（非公開） |
| `YOUTUBE_CATEGORY_ID` | `25` | YouTube カテゴリ ID（`25`: ニュースと政治, `28`: 科学と技術） |

---

## 3. テスト確認から本番（完全自動公開）への移行手順

### Step 1: ローカルまたは Docker Compose でのドライラン（YouTube 投稿なし動画生成テスト）
```bash
# Docker Compose で VOICEVOX Engine と一緒にワンコマンド動画生成テスト
DRY_RUN=true docker compose up --build

# またはローカル Python で直接ドライラン
python -m src.main --dry-run --topic "生成AIエージェントと日本の働き方改革"
```
実行完了後、`output/<日時>/` フォルダ内に以下が出力されます：
- `final_video.mp4`（1920×1080 / 30fps / 8分以上の掛け合い解説動画）
- `thumbnail.png`（1280×720 高CTRサムネイル画像）
- `youtube_description.txt`（チャプター目次・`VOICEVOX:四国めたん` / `VOICEVOX:ずんだもん` 表記入り概要欄）
- `episode_script.json`（全セリフ・スライド構造化JSON）

### Step 2: 限定公開モード（`unlisted`）での YouTube 実投稿テスト
1. GitHub リポジトリの **Actions** タブを開き、**`Auto Generate & Publish YouTube Trend Video`** を選択します。
2. **「Run workflow」** を押し、以下のように指定して手動実行します：
   - `privacy_status`: **`unlisted`**（限定公開）
   - `dry_run`: **`false`**
3. YouTube Studio で限定公開された動画を開き、以下を確認します：
   - 動画尺が8分以上あり、四国めたん＆ずんだもんの掛け合いテンポ（話速 `1.18`、ポーズ `0.24s`）が自然か
   - 字幕の色分け（ピンク／グリーン）、図解スライド切り替え、カスタムサムネイルが正しく反映されているか
   - 概要欄に `VOICEVOX:四国めたん` と `VOICEVOX:ずんだもん` およびチャプター目次が入っているか

### Step 3: 完全自動公開モード（`public`）での本番定時運用
限定公開テストで品質を確認できたら、追加の設定変更は不要です。
`.github/workflows/auto_publish_youtube.yml` のデフォルトは `YOUTUBE_PRIVACY_STATUS=public` に設定されているため、毎日 **日本時間 18:00（UTC 09:00）** に自動起動し、トレンド収集から YouTube での**一般公開（Public）**および `data/history.json` の履歴コミットまで完全無人で完結します。
