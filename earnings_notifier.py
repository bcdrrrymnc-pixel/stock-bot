"""
決算・ニュース Discord通知Bot
- TDnet（東証適時開示）で決算短信をリアルタイム取得 ← メイン
- EDINET APIで業績修正・薬事承認などを補完
- yfinanceで財務データを取得
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

DISCORD_EARNINGS_WEBHOOK = os.environ["DISCORD_EARNINGS_WEBHOOK"]
DISCORD_NEWS_WEBHOOK     = os.environ["DISCORD_NEWS_WEBHOOK"]
EDINET_API_KEY           = os.environ.get("EDINET_API_KEY", "")

SENT_FILE    = Path("sent_ids.json")
EDINET_BASE  = "https://api.edinet-fsa.go.jp/api/v2"
TDNET_URL    = "https://www.release.tdnet.info/inbs/I_main_00.html"

EDINET_SKIP = [
    "有価証券報告書", "四半期報告書", "半期報告書",
    "臨時報告書", "内部統制報告書", "大量保有報告書",
    "変更報告書", "公開買付", "訂正", "有価証券届出書",
]

def load_sent() -> set:
    if SENT_FILE.exists():
        data = json.loads(SENT_FILE.read_text(encoding="utf-8"))
        return set(data.get("ids", []))
    return set()

def save_sent(sent: set):
    ids = list(sent)[-3000:]
    SENT_FILE.write_text(json.dumps({"ids": ids}, ensure_ascii=False, indent=2), encoding="utf-8")

# ──────────────────────────────────────────────
# TDnet スクレイピング（デバッグ版）
# ──────────────────────────────────────────────
def fetch_tdnet_disclosures() -> list[dict]:
    results = []
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        r = requests.get(TDNET_URL, headers=headers, timeout=30)
        r.encoding = "utf-8"
        soup = BeautifulSoup(r.text, "html.parser")

        # ── デバッグ：HTML構造を確認 ──
        print(f"[TDnet] HTTPステータス: {r.status_code}")
        print(f"[TDnet] ページタイトル: {soup.title}")

        # テーブル一覧
        tables = soup.find_all("table")
        print(f"[TDnet] テーブル数: {len(tables)}")
        for t in tables[:5]:
            print(f"  table id={t.get('id')!r} class={t.get('class')!r}")

        # divのid一覧（構造把握）
        divs = soup.find_all("div", id=True)
        print(f"[TDnet] div id一覧: {[d.get('id') for d in divs[:20]]}")

        # trを全部試す
        all_rows = soup.find_all("tr")
        print(f"[TDnet] tr総数: {len(all_rows)}")
        for row in all_rows[:5]:
            cols = row.find_all("td")
            if cols:
                print(f"  td数={len(cols)} | {[c.get_text(strip=True)[:30] for c in cols[:4]]}")

        # ── パース試行1: id="main-list-table" ──
        tbl = soup.select_one("table#main-list-table")
        if tbl:
            rows = tbl.find_all("tr")
            print(f"[TDnet] main-list-table rows: {len(rows)}")
        else:
            print("[TDnet] main-list-table が見つかりません。全trで試みます。")
            rows = all_rows

        for row in rows:
            cols = row.find_all("td")
            if len(cols) < 4:
                continue
            time_str = cols[0].get_text(strip=True)
            ticker   = cols[1].get_text(strip=True)
            company  = cols[2].get_text(strip=True)
            title_td = cols[3]
            title    = title_td.get_text(strip=True)
            a_tag    = title_td.find("a")
            href     = ""
            if a_tag and a_tag.get("href"):
                base = "https://www.release.tdnet.info/inbs/"
                href = base + a_tag["href"].lstrip("./")
            doc_id = href.split("=")[-1] if "=" in href else f"{ticker}_{title[:20]}"

            results.append({
                "id": doc_id, "company": company, "ticker": ticker,
                "title": title, "time": time_str, "url": href, "source": "tdnet",
            })

        print(f"[TDnet] パース結果: {len(results)}件")
        if results:
            print(f"[TDnet] サンプル: {results[0]}")

    except Exception as e:
        print(f"[TDnet] エラー: {e}")

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

def get_financials(ticker_jp: str) -> dict:
    if not ticker_jp or not ticker_jp.isdigit():
        return {}
    symbol = f"{ticker_jp}.T"
    try:
        tk = yf.Ticker(symbol)
        info = tk.info
        fin  = tk.financials
        revenue = net_income = None
        if not fin.empty:
            rev_key = [k for k in fin.index if "Revenue" in k]
            inc_key = [k for k in fin.index if "Net Income" in k]
            if rev_key: revenue    = fin.loc[rev_key[0]].iloc[0]
            if inc_key: net_income = fin.loc[inc_key[0]].iloc[0]
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
    title   = item.get("title", "")
    doc_url = item.get("url", "https://www.release.tdnet.info")
    heading = f"📊 {company}" + (f"（{ticker}）" if ticker else "") + " 決算発表"
    return {
        "username": "決算Bot",
        "embeds": [{
            "title": heading, "description": title, "url": doc_url, "color": 0x00b4d8,
            "fields": [
                {"name": "💹 売上高",     "value": fmt_yen(fin.get("revenue")),    "inline": True},
                {"name": "📈 純利益",     "value": fmt_yen(fin.get("net_income")), "inline": True},
                {"name": "🏦 有利子負債", "value": fmt_yen(fin.get("total_debt")), "inline": True},
            ],
            "footer": {"text": f"セクター: {sector} | {item.get('time','')} | TDnet"},
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
        retry = int(r.headers.get("Retry-After", 5))
        time.sleep(retry)
        requests.post(webhook_url, json=payload, timeout=15)
    elif r.status_code not in (200, 204):
        print(f"[Discord] エラー {r.status_code}: {r.text[:200]}")
    else:
        print("[Discord] 送信成功")

def main():
    sent = load_sent()
    new_sent = 0
    print(f"[送信済みID] {len(sent)}件をロード")

    # TDnet
    tdnet_items = fetch_tdnet_disclosures()
    for item in tdnet_items:
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
