"""
決算・ニュース Discord通知Bot
- TDnet RSSフィードで決算短信をリアルタイム取得
- EDINET APIで業績修正・薬事承認を補完
- yfinanceで財務データを取得
"""

import os
import json
import time
import requests
import yfinance as yf
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, date, timedelta
from pathlib import Path

DISCORD_EARNINGS_WEBHOOK = os.environ["DISCORD_EARNINGS_WEBHOOK"]
DISCORD_NEWS_WEBHOOK     = os.environ["DISCORD_NEWS_WEBHOOK"]
EDINET_API_KEY           = os.environ.get("EDINET_API_KEY", "")

SENT_FILE   = Path("sent_ids.json")
EDINET_BASE = "https://api.edinet-fsa.go.jp/api/v2"

# TDnet RSSフィード（東証適時開示 全件）
TDNET_RSS_URLS = [
    "https://www.release.tdnet.info/inbs/RSS_I_main_00.xml",   # 当日全件
    "https://www.release.tdnet.info/inbs/RSS_I_main_01.xml",   # 前日
]

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
    SENT_FILE.write_text(json.dumps({"ids": ids}, ensure_ascii=False, indent=2), encoding="utf-8")

# ──────────────────────────────────────────────
# TDnet RSS取得
# ──────────────────────────────────────────────
def fetch_tdnet_rss() -> list[dict]:
    results = []
    headers = {"User-Agent": "Mozilla/5.0 (compatible; StockBot/1.0)"}

    for rss_url in TDNET_RSS_URLS:
        try:
            r = requests.get(rss_url, headers=headers, timeout=30)
            print(f"[TDnet RSS] {rss_url} → {r.status_code}")

            if r.status_code != 200:
                continue

            # デバッグ：先頭200文字表示
            print(f"[TDnet RSS] 先頭: {r.text[:300]!r}")

            root = ET.fromstring(r.content)
            ns   = {"": root.tag.split("}")[0].strip("{")} if "}" in root.tag else {}

            # RSS 2.0 形式
            items = root.findall(".//item")
            print(f"[TDnet RSS] item数: {len(items)}")

            for item in items:
                def txt(tag):
                    el = item.find(tag)
                    return el.text.strip() if el is not None and el.text else ""

                title   = txt("title")
                link    = txt("link")
                pubdate = txt("pubDate")
                desc    = txt("description")

                # descriptionからticker・会社名を抽出
                # 形式例: "7203 トヨタ自動車"
                company = desc
                ticker  = ""
                parts   = desc.strip().split(" ", 1)
                if len(parts) == 2 and parts[0].isdigit() and len(parts[0]) == 4:
                    ticker  = parts[0]
                    company = parts[1]

                doc_id = link.split("=")[-1] if "=" in link else f"rss_{title[:30]}"

                results.append({
                    "id":      doc_id,
                    "company": company,
                    "ticker":  ticker,
                    "title":   title,
                    "time":    pubdate,
                    "url":     link,
                    "source":  "tdnet",
                })

        except Exception as e:
            print(f"[TDnet RSS] エラー ({rss_url}): {e}")

    print(f"[TDnet RSS] 合計: {len(results)}件")
    if results:
        print(f"[TDnet RSS] サンプル: {results[0]}")
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
# EDINET（業績修正・薬事承認の補完）
# ──────────────────────────────────────────────
def edinet_headers() -> dict:
    return {"Ocp-Apim-Subscription-Key": EDINET_API_KEY} if EDINET_API_KEY else {}

def fetch_edinet_documents(target_date: str) -> list[dict]:
    url = f"{EDINET_BASE}/documents.json"
    params = {"date": target_date, "type": 2 if EDINET_API_KEY else 1}
    try:
        r = requests.get(url, params=params, headers=edinet_headers(), timeout=30)
        r.raise_for_status()
        results = r.json().get("results", [])
        print(f"[EDINET] {target_date} → {len(results)}件")
        return results
    except Exception as e:
        print(f"[EDINET] エラー: {e}")
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
# yfinance
# ──────────────────────────────────────────────
def get_financials(ticker_jp: str) -> dict:
    if not ticker_jp or not ticker_jp.isdigit():
        return {}
    try:
        tk   = yf.Ticker(f"{ticker_jp}.T")
        info = tk.info
        fin  = tk.financials
        revenue = net_income = None
        if not fin.empty:
            rev_key = [k for k in fin.index if "Revenue" in k]
            inc_key = [k for k in fin.index if "Net Income" in k]
            if rev_key: revenue    = fin.loc[rev_key[0]].iloc[0]
            if inc_key: net_income = fin.loc[inc_key[0]].iloc[0]
        return {
            "company":    info.get("longName") or info.get("shortName", ""),
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
    if value is None: return "N/A"
    v = float(value)
    if abs(v) >= 1e12: return f"{v/1e12:.2f}兆円"
    if abs(v) >= 1e8:  return f"{v/1e8:.1f}億円"
    return f"{v/1e4:.0f}万円"

def build_earnings_embed(item: dict, fin: dict) -> dict:
    ticker  = item.get("ticker", "").strip()
    company = fin.get("company") or item.get("company", "不明")
    sector  = fin.get("sector") or "不明"
    heading = f"📊 {company}" + (f"（{ticker}）" if ticker else "") + " 決算発表"
    return {
        "username": "決算Bot",
        "embeds": [{
            "title": heading,
            "description": item.get("title", ""),
            "url": item.get("url", "https://www.release.tdnet.info"),
            "color": 0x00b4d8,
            "fields": [
                {"name": "💹 売上高",     "value": fmt_yen(fin.get("revenue")),    "inline": True},
                {"name": "📈 純利益",     "value": fmt_yen(fin.get("net_income")), "inline": True},
                {"name": "🏦 有利子負債", "value": fmt_yen(fin.get("total_debt")), "inline": True},
            ],
            "footer": {"text": f"セクター: {sector} | TDnet"},
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }]
    }

def build_news_embed(company, ticker, title, url, doc_type, source="TDnet") -> dict:
    type_map = {
        "revision": ("🔄 業績修正", 0xe63946 if "下方" in title else 0x2dc653),
        "pharma":   ("💊 新薬・薬事承認", 0x9b5de5),
    }
    label, color = type_map.get(doc_type, ("📌 開示情報", 0xadb5bd))
    heading = f"{label}｜{company}" + (f"（{ticker}）" if ticker else "")
    return {
        "username": "ニュースBot",
        "embeds": [{"title": heading, "description": title[:200], "url": url,
                    "color": color, "footer": {"text": source},
                    "timestamp": datetime.utcnow().isoformat() + "Z"}]
    }

def post_discord(webhook_url: str, payload: dict):
    if not webhook_url:
        print("[Discord] Webhook URLが空です。")
        return
    r = requests.post(webhook_url, json=payload, timeout=15)
    if r.status_code == 429:
        time.sleep(int(r.headers.get("Retry-After", 5)))
        requests.post(webhook_url, json=payload, timeout=15)
    elif r.status_code not in (200, 204):
        print(f"[Discord] エラー {r.status_code}: {r.text[:200]}")
    else:
        print("[Discord] 送信成功")

# ──────────────────────────────────────────────
# メイン
# ──────────────────────────────────────────────
def main():
    sent = load_sent()
    new_sent = 0
    print(f"[送信済みID] {len(sent)}件をロード")

    # TDnet RSS
    for item in fetch_tdnet_rss():
        itype = classify_tdnet(item)
        if not itype: continue
        doc_id = f"tdnet_{item['id']}"
        if doc_id in sent: continue
        ticker = item.get("ticker", "").strip()
        if itype == "earnings":
            fin = get_financials(ticker) if ticker else {}
            post_discord(DISCORD_EARNINGS_WEBHOOK, build_earnings_embed(item, fin))
            print(f"[決算送信] {item['company']}（{ticker}）")
        else:
            post_discord(DISCORD_NEWS_WEBHOOK, build_news_embed(
                item["company"], ticker, item["title"], item["url"], itype))
            print(f"[ニュース送信] {itype} / {item['company']}")
        sent.add(doc_id)
        new_sent += 1
        time.sleep(1)

    # EDINET補完
    edinet_docs = []
    for days_ago in range(3):
        target = (date.today() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        edinet_docs = fetch_edinet_documents(target)
        if edinet_docs: break
    for doc in edinet_docs:
        doc_id = f"edinet_{doc.get('docID','')}"
        if doc_id in sent: continue
        dtype = classify_edinet(doc)
        if not dtype: continue
        ticker = (doc.get("secCode") or "").strip()
        desc   = doc.get("docDescription", "")
        url    = f"https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?S1{doc.get('docID','')}"
        post_discord(DISCORD_NEWS_WEBHOOK, build_news_embed(
            doc.get("filerName","不明"), ticker, desc, url, dtype, "EDINET"))
        print(f"[ニュース送信EDINET] {dtype} / {doc.get('filerName')}")
        sent.add(doc_id)
        new_sent += 1
        time.sleep(1)

    save_sent(sent)
    print(f"完了。新規送信: {new_sent}件")

if __name__ == "__main__":
    main()
