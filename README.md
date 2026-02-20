# 📊 Discord 株式通知Bot

PCの電源が切れていても、GitHub Actionsが**15分ごと**に自動実行して Discordに通知します。

---

## 📁 ファイル構成

```
discord-stock-bot/
├── earnings_notifier.py          # メインスクリプト
├── requirements.txt              # Pythonパッケージ
├── sent_ids.json                 # 送信済みID（自動生成）
└── .github/
    └── workflows/
        └── notifier.yml          # GitHub Actions設定
```

---

## 🚀 セットアップ手順

### Step 1: Discordのwebhookを作成

1. Discordサーバー → チャンネル設定 → **連携サービス** → **ウェブフック**
2. **決算チャンネル**用と**ニュースチャンネル**用の2つ作成
3. それぞれのWebhook URLをコピー

### Step 2: GitHubリポジトリを作成

```bash
# このフォルダをGitHubにpush
git init
git add .
git commit -m "initial commit"
git remote add origin https://github.com/あなた/discord-stock-bot.git
git push -u origin main
```

### Step 3: GitHub Secretsを登録

リポジトリの **Settings → Secrets and variables → Actions → New repository secret** で以下を登録：

| Secret名 | 値 |
|---|---|
| `DISCORD_EARNINGS_WEBHOOK` | 決算チャンネルのWebhook URL |
| `DISCORD_NEWS_WEBHOOK` | ニュースチャンネルのWebhook URL |
| `EDINET_API_KEY` | EDINETのAPIキー（任意・無くても動作） |

### Step 4: EDINETのAPIキー取得（任意・無料）

1. https://api.edinet-fsa.go.jp/ にアクセス
2. 利用申請フォームから申請（無料）
3. キーが届いたら `EDINET_API_KEY` Secretに登録

> ⚠️ APIキー未登録でも動作しますが、1日あたりの取得件数に制限がかかる場合があります

### Step 5: 動作確認

GitHub → Actions → **📊 Stock Discord Notifier** → **Run workflow** で手動実行してテスト

---

## 📬 通知の仕様

### 決算チャンネル（#決算）
- **決算短信**が提出されたタイミングで通知
- 売上高・純利益・有利子負債を表示
- セクター・決算期を表示

### ニュースチャンネル（#ニュース）
| 種別 | トリガー |
|---|---|
| 📢 適時開示 | 臨時報告書が提出された |
| 📋 有価証券報告書 | 年次報告書が提出された |
| 🔄 業績修正 | 上方・下方修正の書類が提出された |
| 💊 新薬・薬事 | 承認・薬事関連の書類が提出された |

---

## ⏰ 実行スケジュール

**平日（月〜金）の 8:00〜18:45 JST** に 15分ごと自動実行。  
PCの電源OFF・スリープ中でも GitHub のクラウド上で動きます。

---

## 🔧 カスタマイズ

### 特定銘柄だけ通知したい場合

`earnings_notifier.py` の `main()` 関数内に以下を追加：

```python
# 監視する証券コード（東証）
WATCH_TICKERS = {"7203", "6758", "9984", "4502"}

# fetch後のループの中に追加
if ticker and ticker not in WATCH_TICKERS:
    continue
```

### 通知時間を変えたい場合

`.github/workflows/notifier.yml` の `cron` を編集。  
**JST = UTC + 9時間** なので注意。

例：毎日 9:00 JST のみ実行
```yaml
- cron: "0 0 * * 1-5"
```

---

## ❓ よくある質問

**Q: sent_ids.json とは？**  
A: 同じ情報を2回送らないための送信済みIDリストです。自動でコミットされます。

**Q: 土日・祝日は動きますか？**  
A: スケジュールは平日のみです。祝日は取得データが0件になるだけで問題ありません。

**Q: GitHub Actionsは無料ですか？**  
A: パブリックリポジトリは完全無料。プライベートでも月2000分まで無料（本Botは1回1〜2分なので約60日分）。
