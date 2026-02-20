"""
決算・ニュース Discord通知Bot
- yanoshin TDnet API（非公式・無料）で決算短信をリアルタイム取得
- EDINET APIで業績修正・薬事承認を補完
- yfinanceで財務データを取得
"""

import os
import json
import time
import requests
import yfinance as yf
from datetime import datetime, date, timedelta
from pathlib import Path

DISCORD_EARNINGS_WEBHOOK = os.environ["DISCORD_EARNINGS_WEBHOOK"]
DISCORD_NEWS_WEBHOOK     = os.environ["DISCORD_NEWS_WEBHOOK"]
EDINET_API_KEY           = os.environ.get("EDINET_API_KEY", "")

SENT_FILE   = Path("sent_ids.json")
EDINET_BASE = "https://api.edinet-fsa.go.jp/api/v2"

# yanoshin TDnet API（無料・非公式）
# today = 当日のみ、recent = 直近
TDNET_API_URL = "https://webapi.yanoshin.jp/webapi/tdnet/list/today.json"

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
# yanoshin TDnet API
# ──────────────────────────────────────────────
def fetch_tdnet() -> list[dict]:
    results = []
    headers = {"User-Agent": "Mozilla/5.0 (compatible; StockBot/1.0)"}

    # 当日と前日の2日分取得
    urls = [
        "https://webapi.yanoshin.jp/webapi/tdnet/list/today.json",
        "https://webapi.yanoshin.jp/webapi/tdnet/list/yesterday.json",
    ]

    for url in urls:
        try:
            r = requests.get(url, headers=headers, timeout=30)
            print(f"[TDnet] {url} → {r.status_code}")
            if r.status_code != 200:
                continue

            data = r.json()
            print(f"[TDnet] レスポンスキー: {list(data.keys()) if isinstance(data, dict) else type(data)}")

            # items or results or list
            items = []
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = data.get("items") or data.get("results") or data.get("list") or []

            print(f"[TDnet] {len(items)}件")
            if items:
                print(f"[TDnet] サンプル: {items[0]}")

            for item in items:
                # yanoshin APIは {"Tdnet": {...}} の入れ子構造
                d = item.get("Tdnet") or item
                doc_id  = str(d.get("id") or "")
                company = d.get("company_name") or ""
                ticker  = str(d.get("company_code") or "").replace("0", "", 1)[:4]
                title   = d.get("title") or ""
                pub_at  = d.get("pubdate") or ""
                url_pdf = d.get("document_url") or ""

                if not doc_id or not title:
                    continue

                results.append({
                    "id": doc_id, "company": company, "ticker": ticker,
                    "title": title, "time": pub_at, "url": url_pdf,
                })

        except Exception as e:
            print(f"[TDnet] エラー ({url}): {e}")

    print(f"[TDnet] 合計: {len(results)}件")
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
# yfinance（当期＋前期比）
# ──────────────────────────────────────────────
def get_financials(ticker_jp: str) -> dict:
    if not ticker_jp or not ticker_jp.isdigit():
        return {}
    try:
        tk   = yf.Ticker(f"{ticker_jp}.T")
        info = tk.info
        fin  = tk.financials  # columns: 当期, 前期, ...（降順）

        def extract(fin, keyword):
            keys = [k for k in fin.index if keyword in k]
            if not keys or fin.empty:
                return None, None
            row = fin.loc[keys[0]]
            cur  = row.iloc[0] if len(row) > 0 else None
            prev = row.iloc[1] if len(row) > 1 else None
            return cur, prev

        rev_cur,  rev_prev  = extract(fin, "Revenue")
        inc_cur,  inc_prev  = extract(fin, "Net Income")

        return {
            "company":       info.get("longName") or info.get("shortName", ""),
            "sector":        info.get("sector", ""),
            "revenue":       rev_cur,
            "revenue_prev":  rev_prev,
            "net_income":    inc_cur,
            "net_income_prev": inc_prev,
            "total_debt":    info.get("totalDebt"),
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

def fmt_yoy(cur, prev) -> str:
    """前期比を計算して矢印付きで返す"""
    if cur is None or prev is None or prev == 0:
        return ""
    pct = (float(cur) - float(prev)) / abs(float(prev)) * 100
    arrow = "🔺" if pct >= 0 else "🔻"
    return f" {arrow}{abs(pct):.1f}%"

def build_earnings_embed(item: dict, fin: dict) -> dict:
    ticker  = item.get("ticker", "").strip()
    company = fin.get("company") or item.get("company", "不明")
    sector  = fin.get("sector") or "不明"
    heading = f"📊 {company}" + (f"（{ticker}）" if ticker else "") + " 決算発表"

    rev = fin.get("revenue")
    inc = fin.get("net_income")
    dbt = fin.get("total_debt")
    rev_yoy = fmt_yoy(rev, fin.get("revenue_prev"))
    inc_yoy = fmt_yoy(inc, fin.get("net_income_prev"))

    rev_str = fmt_yen(rev) + rev_yoy if rev is not None else "N/A"
    inc_str = fmt_yen(inc) + inc_yoy if inc is not None else "N/A"
    dbt_str = fmt_yen(dbt)

    return {
        "username": "決算Bot",
        "embeds": [{
            "title": heading,
            "description": item.get("title", ""),
            "url": item.get("url") or "https://www.release.tdnet.info",
            "color": 0x00b4d8,
            "fields": [
                {"name": "💹 売上高",     "value": rev_str, "inline": True},
                {"name": "📈 純利益",     "value": inc_str, "inline": True},
                {"name": "🏦 有利子負債", "value": dbt_str, "inline": True},
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

    # TDnet（yanoshin API）
    for item in fetch_tdnet():
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
                item["company"], ticker, item["title"], item.get("url",""), itype))
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
