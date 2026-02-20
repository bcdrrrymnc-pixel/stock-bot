"""
決算・ニュース Discord通知Bot
- TDnet（東証適時開示）で決算短信をリアルタイム取得 ← メイン
- EDINET APIで業績修正・薬事承認などを補完
- yfinanceで財務データを取得
- Discordの決算チャンネル・ニュースチャンネルに通知
"""

import os
import json
import time
import requests
import yfinance as yf
from bs4 import BeautifulSoup
from collections import Counter
from datetime import datetime, date, timedelta
from pathlib import Path

# ──────────────────────────────────────────────
# 設定
# ──────────────────────────────────────────────
DISCORD_EARNINGS_WEBHOOK = os.environ["DISCORD_EARNINGS_WEBHOOK"]
DISCORD_NEWS_WEBHOOK     = os.environ["DISCORD_NEWS_WEBHOOK"]
EDINET_API_KEY           = os.environ.get("EDINET_API_KEY", "")

SENT_FILE    = Path("sent_ids.json")
EDINET_BASE  = "https://api.edinet-fsa.go.jp/api/v2"
TDNET_URL    = "https://www.release.tdnet.info/inbs/I_main_00.html"

# EDINETで除外する書類
EDINET_SKIP = [
    "有価証券報告書", "四半期報告書", "半期報告書",
    "臨時報告書", "内部統制報告書", "大量保有報告書",
    "変更報告書", "公開買付", "訂正", "有価証券届出書",
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
    ids = list(sent)[-3000:]
    SENT_FILE.write_text(
        json.dumps({"ids": ids}, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

# ──────────────────────────────────────────────
# TDnet スクレイピング（東証適時開示）
# ──────────────────────────────────────────────
def fetch_tdnet_disclosures() -> list[dict]:
    """
    TDnetの当日開示一覧を取得。
    返り値: [{"id", "company", "ticker", "title", "time", "url"}, ...]
    """
    results = []
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; StockBot/1.0)"}
        r = requests.get(TDNET_URL, headers=headers, timeout=30)
        r.encoding = "utf-8"
        soup = BeautifulSoup(r.text, "html.parser")

        # TDnetのテーブル行をパース
        rows = soup.select("table#main-list-table tr")
        if not rows:
            # テーブルIDが変わった場合の代替
            rows = soup.select("tr.odd, tr.even")

        for row in rows:
            cols = row.find_all("td")
            if len(cols) < 4:
                continue

            time_str = cols[0].get_text(strip=True)
            ticker   = cols[1].get_text(strip=True)
            company  = cols[2].get_text(strip=True)
            title_td = cols[3]
            title    = title_td.get_text(strip=True)

            # リンク取得
            a_tag = title_td.find("a")
            href  = ""
            if a_tag and a_tag.get("href"):
                href = "https://www.release.tdnet.info/inbs/" + a_tag["href"].lstrip("./")

            # IDはURL末尾 or ticker+title のハッシュ
            doc_id = href.split("=")[-1] if "=" in href else f"tdnet_{ticker}_{title[:20]}"

            results.append({
                "id":      doc_id,
                "company": company,
                "ticker":  ticker,
                "title":   title,
                "time":    time_str,
                "url":     href,
                "source":  "tdnet",
            })

        print(f"[TDnet] {len(results)}件取得")
    except Exception as e:
        print(f"[TDnet] 取得エラー: {e}")

    return results

def classify_tdnet(item: dict) -> str | None:
    title = item.get("title", "")

    if any(kw in title for kw in ["決算短信", "四半期決算短信", "中間決算短信"]):
        return "earnings"
    if any(kw in title for kw in ["上方修正", "下方修正", "業績修正", "業績予想の修正"]):
        return "revision"
    if any(kw in title for kw in ["薬事", "FDA", "治験", "新薬", "承認取得", "製造販売承認"]):
        return "pharma"
    return None

# ──────────────────────────────────────────────
# EDINET API（業績修正・薬事承認の補完用）
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

def classify_edinet(doc: dict) -> str | None:
    desc = doc.get("docDescription", "")
    if any(kw in desc for kw in EDINET_SKIP):
        return None
    if any(kw in desc for kw in ["上方修正", "下方修正", "業績修正", "業績予想の修正"]):
        return "revision"
    if any(kw in desc for kw in ["薬事", "FDA", "治験", "新薬", "承認取得", "製造販売承認"]):
        return "pharma"
    return None

# ──────────────────────────────────────────────
# yfinance 財務データ取得
# ──────────────────────────────────────────────
def get_financials(ticker_jp: str) -> dict:
    # tickerが空・数字でない場合はスキップ
    if not ticker_jp or not ticker_jp.isdigit():
        return {}
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

def build_tdnet_earnings_embed(item: dict, fin: dict) -> dict:
    ticker  = item.get("ticker", "").strip()
    company = fin.get("company") or item.get("company", "不明")
    sector  = fin.get("sector") or "不明"
    title   = item.get("title", "")
    doc_url = item.get("url", "https://www.release.tdnet.info")
    t       = item.get("time", "")

    heading = f"📊 {company}"
    if ticker:
        heading += f"（{ticker}）"
    heading += " 決算発表"

    fields = [
        {"name": "💹 売上高",     "value": fmt_yen(fin.get("revenue")),    "inline": True},
        {"name": "📈 純利益",     "value": fmt_yen(fin.get("net_income")), "inline": True},
        {"name": "🏦 有利子負債", "value": fmt_yen(fin.get("total_debt")), "inline": True},
    ]

    return {
        "username": "決算Bot",
        "embeds": [{
            "title":       heading,
            "description": title,
            "url":         doc_url,
            "color":       0x00b4d8,
            "fields":      fields,
            "footer":      {"text": f"セクター: {sector}　|　開示時刻: {t}　|　TDnet"},
            "timestamp":   datetime.utcnow().isoformat() + "Z",
        }]
    }

def build_news_embed_tdnet(item: dict, doc_type: str) -> dict:
    company = item.get("company", "不明")
    ticker  = item.get("ticker", "").strip()
    title   = item.get("title", "")
    doc_url = item.get("url", "https://www.release.tdnet.info")
    t       = item.get("time", "")

    type_map = {
        "revision": ("🔄 業績修正", 0xe63946 if "下方" in title else 0x2dc653),
        "pharma":   ("💊 新薬・薬事承認", 0x9b5de5),
    }
    label, color = type_map.get(doc_type, ("📌 開示情報", 0xadb5bd))

    heading = f"{label}｜{company}"
    if ticker:
        heading += f"（{ticker}）"

    return {
        "username": "ニュースBot",
        "embeds": [{
            "title":       heading,
            "description": title,
            "url":         doc_url,
            "color":       color,
            "footer":      {"text": f"開示時刻: {t}　|　TDnet"},
            "timestamp":   datetime.utcnow().isoformat() + "Z",
        }]
    }

def build_news_embed_edinet(doc: dict, doc_type: str) -> dict:
    company = doc.get("filerName", "不明")
    ticker  = (doc.get("secCode") or "").strip()
    desc    = doc.get("docDescription", "")
    doc_url = f"https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?S1{doc.get('docID','')}"

    type_map = {
        "revision": ("🔄 業績修正", 0xe63946 if "下方" in desc else 0x2dc653),
        "pharma":   ("💊 新薬・薬事承認", 0x9b5de5),
    }
    label, color = type_map.get(doc_type, ("📌 開示情報", 0xadb5bd))

    heading = f"{label}｜{company}"
    if ticker:
        heading += f"（{ticker}）"

    return {
        "username": "ニュースBot",
        "embeds": [{
            "title":       heading,
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
        print("[Discord] Webhook URLが空です。")
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
    sent     = load_sent()
    new_sent = 0
    print(f"[送信済みID] {len(sent)}件をロード")

    # ── TDnet処理（決算短信メイン） ──────────────
    tdnet_items = fetch_tdnet_disclosures()
    tdnet_types = [classify_tdnet(i) for i in tdnet_items]
    tdnet_counts = Counter(t for t in tdnet_types if t)
    print(f"[TDnet分類] {tdnet_counts}")

    for item, itype in zip(tdnet_items, tdnet_types):
        if not itype:
            continue
        doc_id = f"tdnet_{item['id']}"
        if doc_id in sent:
            continue

        ticker = item.get("ticker", "").strip()
        fin    = get_financials(ticker) if ticker else {}

        if itype == "earnings":
            payload = build_tdnet_earnings_embed(item, fin)
            post_discord(DISCORD_EARNINGS_WEBHOOK, payload)
            print(f"[決算送信] {item['company']}（{ticker}）{item['title']}")
        else:
            payload = build_news_embed_tdnet(item, itype)
            post_discord(DISCORD_NEWS_WEBHOOK, payload)
            print(f"[ニュース送信] {itype} / {item['company']}")

        sent.add(doc_id)
        new_sent += 1
        time.sleep(1)

    # ── EDINET処理（業績修正・薬事承認の補完） ──
    edinet_docs = []
    for days_ago in range(0, 3):
        target = (date.today() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        edinet_docs = fetch_edinet_documents(target)
        if edinet_docs:
            break

    edinet_counts = Counter(classify_edinet(d) for d in edinet_docs if classify_edinet(d))
    print(f"[EDINET分類] {edinet_counts}")

    for doc in edinet_docs:
        doc_id = f"edinet_{doc.get('docID','')}"
        if not doc_id or doc_id in sent:
            continue
        dtype = classify_edinet(doc)
        if not dtype:
            continue

        payload = build_news_embed_edinet(doc, dtype)
        post_discord(DISCORD_NEWS_WEBHOOK, payload)
        print(f"[ニュース送信EDINET] {dtype} / {doc.get('filerName')}")

        sent.add(doc_id)
        new_sent += 1
        time.sleep(1)

    save_sent(sent)
    print(f"完了。新規送信: {new_sent}件")

if __name__ == "__main__":
    main()
