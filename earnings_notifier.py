"""
決算・ニュース Discord通知Bot
- yanoshin TDnet APIで決算短信・業績修正をリアルタイム取得
- EDINET APIで業績修正・薬事承認を補完
- yfinanceで財務データ取得（単位・NaN修正済み）
"""

import os
import json
import time
import requests
from datetime import datetime, date, timedelta
from pathlib import Path

DISCORD_EARNINGS_WEBHOOK = os.environ["DISCORD_EARNINGS_WEBHOOK"]
DISCORD_NEWS_WEBHOOK     = os.environ["DISCORD_NEWS_WEBHOOK"]
EDINET_API_KEY           = os.environ.get("EDINET_API_KEY", "")

SENT_FILE   = Path("sent_ids.json")
EDINET_BASE = "https://api.edinet-fsa.go.jp/api/v2"

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
# TDnet取得（当日のみ・重複防止）
# ──────────────────────────────────────────────
def fetch_tdnet() -> list[dict]:
    results = []
    headers = {"User-Agent": "Mozilla/5.0 (compatible; StockBot/1.0)"}

    # 当日分のみ取得（yesterdayは重複の原因になるため除外）
    # ただし月曜日・祝日明けは前営業日も取得
    today = date.today()
    urls = ["https://webapi.yanoshin.jp/webapi/tdnet/list/today.json"]

    # 月曜日（weekday=0）は金曜分も取得
    if today.weekday() == 0:
        friday = today - timedelta(days=3)
        urls.append(f"https://webapi.yanoshin.jp/webapi/tdnet/list/{friday.strftime('%Y%m%d')}.json")

    seen_ids = set()  # このfetch内での重複防止

    for url in urls:
        try:
            r = requests.get(url, headers=headers, timeout=30)
            print(f"[TDnet] {url} → {r.status_code}")
            if r.status_code != 200:
                continue

            data  = r.json()
            items = data.get("items") or [] if isinstance(data, dict) else data

            print(f"[TDnet] {len(items)}件取得")

            for item in items:
                d = item.get("Tdnet") or item
                doc_id  = str(d.get("id") or "")
                company = d.get("company_name") or ""
                code    = str(d.get("company_code") or "")
                title   = d.get("title") or ""
                pub_at  = d.get("pubdate") or ""
                url_pdf = d.get("document_url") or ""

                if not doc_id or not title:
                    continue
                if doc_id in seen_ids:
                    continue
                seen_ids.add(doc_id)

                # 個別株のみ（4桁数字＋末尾0の5桁、ETF/REITを除外）
                if not code.isdigit() or len(code) != 5 or code[4] != "0":
                    continue
                if code[:2] in ("10","11","12","13","14","15","16","17","18","19"):
                    continue

                ticker = code[:4]
                results.append({
                    "id":      doc_id,
                    "company": company,
                    "ticker":  ticker,
                    "title":   title,
                    "time":    pub_at,
                    "url":     url_pdf,
                    # XBRL財務データURL（get_financialsで使用）
                    "url_report_type_summary":            d.get("url_report_type_summary"),
                    "url_report_type_fs_consolidated":    d.get("url_report_type_fs_consolidated"),
                    "url_report_type_fs_non_consolidated": d.get("url_report_type_fs_non_consolidated"),
                    "url_xbrl": d.get("url_xbrl"),
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
# EDINET（業績修正・薬事承認補完）
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
    desc = doc.get("docDescription") or ""
    if any(kw in desc for kw in EDINET_SKIP):
        return None
    if any(kw in desc for kw in ["上方修正", "下方修正", "業績修正", "業績予想の修正"]):
        return "revision"
    if any(kw in desc for kw in ["薬事", "FDA", "治験", "新薬", "承認取得", "製造販売承認"]):
        return "pharma"
    return None

# ──────────────────────────────────────────────
# yfinance（単位・NaN修正）
# ──────────────────────────────────────────────
def safe_float(v) -> float | None:
    """NaN・None・無効値をNoneに変換"""
    if v is None:
        return None
    try:
        f = float(v)
        return None if f != f else f  # NaNチェック
    except:
        return None

def to_oku(v) -> float | None:
    """百万円 → 億円に変換"""
    f = safe_float(v)
    return None if f is None else f / 100

def get_financials(ticker_jp: str, tdnet_item: dict = None) -> dict:
    """yanoshin XBRLから財務データを取得"""
    company = (tdnet_item or {}).get("company", "")

    if tdnet_item:
        result = fetch_xbrl_summary(tdnet_item)
        if result:
            result["company"] = result.get("company") or company
            return result

    return {"company": company, "sector": ""}

def fetch_xbrl_summary(item: dict) -> dict | None:
    """yanoshinのXBRL/サマリーURLから財務データを取得"""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; StockBot/1.0)"}

    # yanoshinが提供するXBRL関連URL（連結優先）
    urls = [u for u in [
        item.get("url_report_type_fs_consolidated"),
        item.get("url_report_type_fs_non_consolidated"),
        item.get("url_report_type_summary"),
        item.get("url_xbrl"),
    ] if u]

    print(f"[XBRL] URL候補: {len(urls)}件")

    for url in urls:
        try:
            r = requests.get(url, headers=headers, timeout=20)
            print(f"[XBRL] {url[:60]} → {r.status_code}")
            if r.status_code != 200:
                continue
            data = r.json()
            result = parse_xbrl_response(data)
            if result:
                print(f"[XBRL] 財務データ取得成功")
                return result
        except Exception as e:
            print(f"[XBRL] エラー: {e}")
            continue

    print(f"[XBRL] データ取得できず（全URLがNoneまたは失敗）")
    return None

def parse_xbrl_response(data) -> dict | None:
    """yanoshin XBRLレスポンスをパース（百万円単位を想定）"""
    try:
        if not isinstance(data, dict):
            return None

        # yanoshinのXBRL JSONは {キー: {value: 数値, ...}} の構造
        def find(d, *keys):
            for k in keys:
                for dk in list(d.keys()):
                    if k.lower() in dk.lower():
                        v = d[dk]
                        if isinstance(v, (int, float)):
                            return safe_float(v)
                        if isinstance(v, dict):
                            for vk in ("value", "Amount", "current", "CurrentYearDuration"):
                                if vk in v:
                                    return safe_float(v[vk])
            return None

        rev   = find(data, "NetSales", "Revenue", "Sales")
        op    = find(data, "OperatingIncome", "OperatingProfit")
        pre   = find(data, "OrdinaryIncome", "IncomeBeforeTax", "PretaxIncome")
        net   = find(data, "NetIncome", "ProfitAttributable", "NetProfit")
        debt  = find(data, "InterestBearingDebt", "BorrowingsAndBonds", "Debt")
        opcf  = find(data, "OperatingCF", "CashFromOperating", "NetCashProvidedByOperating")
        invcf = find(data, "InvestingCF", "CashFromInvesting", "NetCashUsedInInvesting")
        fincf = find(data, "FinancingCF", "CashFromFinancing", "NetCashUsedInFinancing")

        # 全部Noneなら失敗
        if all(v is None for v in [rev, op, pre, net]):
            return None

        fcf = None
        if opcf is not None and invcf is not None:
            fcf = opcf + invcf

        # 値が大きすぎる場合は円単位→百万円単位に変換
        def normalize(v):
            if v is None: return None
            if abs(v) > 1e10:  # 1兆円超なら円単位と判断
                return v / 1_000_000
            return v

        return {
            "company":         "",
            "sector":          "",
            "revenue":         to_oku(normalize(rev)),
            "revenue_prev":    None,
            "op_income":       to_oku(normalize(op)),
            "op_income_prev":  None,
            "pretax_income":   to_oku(normalize(pre)),
            "pretax_prev":     None,
            "net_income":      to_oku(normalize(net)),
            "net_income_prev": None,
            "total_debt":      to_oku(normalize(debt)),
            "op_cf":           to_oku(normalize(opcf)),
            "inv_cf":          to_oku(normalize(invcf)),
            "fin_cf":          to_oku(normalize(fincf)),
            "fcf":             to_oku(normalize(fcf)),
        }
    except Exception as e:
        print(f"[XBRL parse] エラー: {e}")
        return None

# ──────────────────────────────────────────────
# フォーマット
# ──────────────────────────────────────────────
def fmt_oku(value) -> str:
    """億円単位の数値を表示"""
    v = safe_float(value)
    if v is None:
        return "N/A"
    if abs(v) >= 10000:
        return f"{v/10000:.1f}兆円"
    if abs(v) >= 1:
        return f"{v:.1f}億円"
    return f"{v*100:.0f}百万円"

def fmt_yoy(cur, prev) -> str:
    c = safe_float(cur)
    p = safe_float(prev)
    if c is None or p is None or p == 0:
        return ""
    pct = (c - p) / abs(p) * 100
    arrow = "🔺" if pct >= 0 else "🔻"
    return f" {arrow}{abs(pct):.1f}%"

def fs(fin, cur_key, prev_key) -> str:
    v = fin.get(cur_key)
    if v is None:
        return "N/A"
    return fmt_oku(v) + fmt_yoy(v, fin.get(prev_key))

def fc(fin, key) -> str:
    v = fin.get(key)
    if v is None:
        return "N/A"
    fv = safe_float(v)
    sign = " 🟢" if fv is not None and fv >= 0 else " 🔴"
    return fmt_oku(v) + sign

def build_earnings_embed(item: dict, fin: dict) -> dict:
    ticker  = item.get("ticker", "").strip()
    company = fin.get("company") or item.get("company", "不明")
    sector  = fin.get("sector") or "不明"
    heading = f"📊 {company}" + (f"（{ticker}）" if ticker else "") + " 決算発表"

    # FCF計算
    fcf_str = fc(fin, "fcf")

    return {
        "username": "決算Bot",
        "embeds": [{
            "title":       heading,
            "description": item.get("title", ""),
            "url":         item.get("url") or "https://www.release.tdnet.info",
            "color":       0x00b4d8,
            "fields": [
                {"name": "💹 売上高",         "value": fs(fin, "revenue",       "revenue_prev"),    "inline": True},
                {"name": "🏭 営業利益",        "value": fs(fin, "op_income",     "op_income_prev"),  "inline": True},
                {"name": "📋 経常利益(税前)",  "value": fs(fin, "pretax_income", "pretax_prev"),     "inline": True},
                {"name": "📈 純利益",          "value": fs(fin, "net_income",    "net_income_prev"), "inline": True},
                {"name": "🏦 有利子負債",      "value": fmt_oku(fin.get("total_debt")),              "inline": True},
                {"name": "\u200b",             "value": "\u200b",                                    "inline": True},
                {"name": "💰 営業CF",          "value": fc(fin, "op_cf"),                            "inline": True},
                {"name": "🔧 投資CF",          "value": fc(fin, "inv_cf"),                           "inline": True},
                {"name": "💳 財務CF",          "value": fc(fin, "fin_cf"),                           "inline": True},
                {"name": "📉 FCF",             "value": fcf_str,                                     "inline": True},
            ],
            "footer":    {"text": f"セクター: {sector} | ※データはXBRL（TDnet）から取得"},
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
    sent     = load_sent()
    new_sent = 0
    print(f"[送信済みID] {len(sent)}件をロード")

    # TDnet（当日分）
    for item in fetch_tdnet():
        itype = classify_tdnet(item)
        if not itype:
            continue
        doc_id = f"tdnet_{item['id']}"
        if doc_id in sent:
            continue
        ticker = item.get("ticker", "").strip()
        if itype == "earnings":
            fin = get_financials(ticker, tdnet_item=item)
            post_discord(DISCORD_EARNINGS_WEBHOOK, build_earnings_embed(item, fin))
            print(f"[決算送信] {item['company']}（{ticker}）")
        else:
            post_discord(DISCORD_NEWS_WEBHOOK, build_news_embed(
                item["company"], ticker, item["title"], item.get("url",""), itype))
            print(f"[ニュース送信] {itype} / {item['company']}")
        sent.add(doc_id)
        new_sent += 1
        time.sleep(1)

    # EDINET補完（当日のみ）
    target = date.today().strftime("%Y-%m-%d")
    for doc in fetch_edinet_documents(target):
        doc_id = f"edinet_{doc.get('docID','')}"
        if doc_id in sent:
            continue
        dtype = classify_edinet(doc)
        if not dtype:
            continue
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
