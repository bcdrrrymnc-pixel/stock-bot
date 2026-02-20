"""
決算・ニュース Discord通知Bot
- EDINET APIで決算短信・業績修正・重要開示を取得
- yfinanceで財務データを補完
- Discordの決算チャンネル・ニュースチャンネルに通知
"""

import os
import json
import time
import requests
import yfinance as yf
from collections import Counter
from datetime import datetime, date, timedelta
from pathlib import Path

# ──────────────────────────────────────────────
# 設定
# ──────────────────────────────────────────────
DISCORD_EARNINGS_WEBHOOK = os.environ["DISCORD_EARNINGS_WEBHOOK"]
DISCORD_NEWS_WEBHOOK     = os.environ["DISCORD_NEWS_WEBHOOK"]
EDINET_API_KEY           = os.environ.get("EDINET_API_KEY", "")

SENT_FILE   = Path("sent_ids.json")
EDINET_BASE = "https://api.edinet-fsa.go.jp/api/v2"

# 通知しない書類（大量に来るため除外）
SKIP_KEYWORDS = [
    "有価証券報告書", "四半期報告書", "半期報告書",
    "臨時報告書", "内部統制報告書", "大量保有報告書",
    "変更報告書", "公開買付", "訂正",
]

# ──────────────────────────────────────────────
# 送信済みID管理
# ──────────────────────────────────────────────
def load_sent() -> set:
    if SENT_FILE.exists():
        data = json.loads(SENT_FILE.read_text(encoding="utf-8"))
        return set(data.get("ids", []))
    return set()

def save_sent(sent: set):
    ids = list(sent)[-2000:]
    SENT_FILE.write_text(
        json.dumps({"ids": ids}, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

# ──────────────────────────────────────────────
# EDINET API
# ──────────────────────────────────────────────
def edinet_headers() -> dict:
    return {"Ocp-Apim-Subscription-Key": EDINET_API_KEY} if EDINET_API_KEY else {}

def fetch_edinet_documents(target_date: str) -> list[dict]:
    url    = f"{EDINET_BASE}/documents.json"
    dtype  = 2 if EDINET_API_KEY else 1
    params = {"date": target_date, "type": dtype}
    try:
        r = requests.get(url, params=params, headers=edinet_headers(), timeout=30)
        r.raise_for_status()
        results = r.json().get("results", [])
        print(f"[EDINET] {target_date} → {len(results)}件")
        return results
    except Exception as e:
        print(f"[EDINET] 取得エラー: {e}")
        return []

# ──────────────────────────────────────────────
# 書類分類
# ──────────────────────────────────────────────
def classify_doc(doc: dict) -> str | None:
    desc = doc.get("docDescription", "")

    # 除外リスト
    if any(kw in desc for kw in SKIP_KEYWORDS):
        return None

    # 決算短信（最優先）
    if any(kw in desc for kw in ["決算短信", "四半期決算短信", "中間決算短信"]):
        return "earnings"

    # 業績修正
    if any(kw in desc for kw in ["上方修正", "下方修正", "業績修正", "業績予想の修正"]):
        return "revision"

    # 新薬・薬事承認
    if any(kw in desc for kw in ["薬事", "FDA", "治験", "新薬", "承認取得", "製造販売承認"]):
        return "pharma"

    return None

# ──────────────────────────────────────────────
# yfinance 財務データ取得
# ──────────────────────────────────────────────
def get_financials(ticker_jp: str) -> dict:
    symbol = f"{ticker_jp}.T"
    try:
        tk   = yf.Ticker(symbol)
        info = tk.info
        fin  = tk.financials

        revenue = net_income = None
        if not fin.empty:
            rev_key = [k for k in fin.index if "Revenue" in k]
            inc_key = [k for k in fin.index if "Net Income" in k]
            if rev_key:
                revenue = fin.loc[rev_key[0]].iloc[0]
            if inc_key:
                net_income = fin.loc[inc_key[0]].iloc[0]

        return {
            "company":    info.get("longName") or info.get("shortName", symbol),
            "sector":     info.get("sector", ""),
            "revenue":    revenue,
            "net_income": net_income,
            "total_debt": info.get("totalDebt"),
        }
    except Exception as e:
        print(f"[yfinance] {ticker_jp} エラー: {e}")
        return {}

# ──────────────────────────────────────────────
# フォーマット
# ──────────────────────────────────────────────
def fmt_yen(value) -> str:
    if value is None:
        return "N/A"
    v = float(value)
    if abs(v) >= 1e12:
        return f"{v/1e12:.2f}兆円"
    if abs(v) >= 1e8:
        return f"{v/1e8:.1f}億円"
    return f"{v/1e4:.0f}万円"

def build_earnings_embed(doc: dict, fin: dict) -> dict:
    ticker  = (doc.get("secCode") or "").strip()
    company = fin.get("company") or doc.get("filerName", "不明")
    sector  = fin.get("sector") or "不明"
    period  = doc.get("periodEnd", "")
    desc    = doc.get("docDescription", "")
    doc_url = f"https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?S1{doc.get('docID','')}"

    title = f"📊 {company}"
    if ticker:
        title += f"（{ticker}）"
    title += " 決算発表"

    fields = [
        {"name": "💹 売上高",     "value": fmt_yen(fin.get("revenue")),    "inline": True},
        {"name": "📈 純利益",     "value": fmt_yen(fin.get("net_income")), "inline": True},
        {"name": "🏦 有利子負債", "value": fmt_yen(fin.get("total_debt")), "inline": True},
    ]

    return {
        "username": "決算Bot",
        "embeds": [{
            "title":       title,
            "description": desc[:150] if desc else "",
            "url":         doc_url,
            "color":       0x00b4d8,
            "fields":      fields,
            "footer":      {"text": f"セクター: {sector}　|　決算期: {period}　|　EDINET"},
            "timestamp":   datetime.utcnow().isoformat() + "Z",
        }]
    }

def build_news_embed(doc: dict, doc_type: str) -> dict:
    company = doc.get("filerName", "不明")
    ticker  = (doc.get("secCode") or "").strip()
    desc    = doc.get("docDescription", "")
    doc_url = f"https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?S1{doc.get('docID','')}"

    type_map = {
        "revision": ("🔄 業績修正", 0xe63946 if "下方" in desc else 0x2dc653),
        "pharma":   ("💊 新薬・薬事承認", 0x9b5de5),
    }
    label, color = type_map.get(doc_type, ("📌 開示情報", 0xadb5bd))

    title = f"{label}｜{company}"
    if ticker:
        title += f"（{ticker}）"

    return {
        "username": "ニュースBot",
        "embeds": [{
            "title":       title,
            "description": desc[:200] or "詳細はリンク先を確認",
            "url":         doc_url,
            "color":       color,
            "footer":      {"text": "EDINET"},
            "timestamp":   datetime.utcnow().isoformat() + "Z",
        }]
    }

# ──────────────────────────────────────────────
# Discord送信
# ──────────────────────────────────────────────
def post_discord(webhook_url: str, payload: dict):
    if not webhook_url:
        print("[Discord] Webhook URLが空です。Secretsを確認してください。")
        return
    r = requests.post(webhook_url, json=payload, timeout=15)
    if r.status_code == 429:
        retry = int(r.headers.get("Retry-After", 5))
        print(f"[Discord] Rate limit。{retry}秒後リトライ")
        time.sleep(retry)
        requests.post(webhook_url, json=payload, timeout=15)
    elif r.status_code not in (200, 204):
        print(f"[Discord] エラー {r.status_code}: {r.text[:200]}")
    else:
        print(f"[Discord] 送信成功")

# ──────────────────────────────────────────────
# メイン処理
# ──────────────────────────────────────────────
def main():
    sent = load_sent()
    print(f"[送信済みID] {len(sent)}件をロード")

    docs = []
    for days_ago in range(0, 5):
        target = (date.today() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        docs = fetch_edinet_documents(target)
        if docs:
            print(f"[EDINET] {target} のデータを使用 ({len(docs)}件)")
            break

    if not docs:
        print("[EDINET] 直近5日分すべて0件。終了。")
        return

    # デバッグ：実際の書類名を表示
    print("[デバッグ] 書類サンプル（先頭30件）:")
    for d in docs[:30]:
        print(f"  desc={d.get('docDescription','')!r} | form={d.get('formCode','')} | sec={d.get('secCode','')}")

    all_types = [classify_doc(d) for d in docs]
    print(f"[分類結果] {Counter(t for t in all_types if t)}")

    earnings_found = [(d, t) for d, t in zip(docs, all_types) if t == "earnings"]
    print(f"[決算検出] {len(earnings_found)}件")
    for d, _ in earnings_found:
        print(f"  → {d.get('filerName','')} | {d.get('docDescription','')} | secCode={d.get('secCode','')}")

    new_sent = 0
    for doc in docs:
        doc_id = doc.get("docID", "")
        if not doc_id or doc_id in sent:
            continue

        doc_type = classify_doc(doc)
        if not doc_type:
            continue

        ticker = (doc.get("secCode") or "").strip()

        if doc_type == "earnings":
            fin     = get_financials(ticker) if ticker else {}
            payload = build_earnings_embed(doc, fin)
            post_discord(DISCORD_EARNINGS_WEBHOOK, payload)
            print(f"[決算送信] {doc.get('filerName')}")
        else:
            payload = build_news_embed(doc, doc_type)
            post_discord(DISCORD_NEWS_WEBHOOK, payload)
            print(f"[ニュース送信] {doc_type} / {doc.get('filerName')}")

        sent.add(doc_id)
        new_sent += 1
        time.sleep(1)

    save_sent(sent)
    print(f"完了。新規送信: {new_sent}件")

if __name__ == "__main__":
    main()
