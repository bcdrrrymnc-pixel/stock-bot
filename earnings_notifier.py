"""
決算・ニュース Discord通知Bot
- EDINET APIで適時開示・決算情報を取得
- yfinanceで財務データを補完
- Discordの決算チャンネル・ニュースチャンネルに通知
"""

import os
import json
import time
import hashlib
import requests
import yfinance as yf
from datetime import datetime, date, timedelta
from pathlib import Path

# ──────────────────────────────────────────────
# 設定
# ──────────────────────────────────────────────
DISCORD_EARNINGS_WEBHOOK = os.environ["DISCORD_EARNINGS_WEBHOOK"]
DISCORD_NEWS_WEBHOOK     = os.environ["DISCORD_NEWS_WEBHOOK"]
EDINET_API_KEY           = os.environ.get("EDINET_API_KEY", "")  # 任意

SENT_FILE = Path("sent_ids.json")

# セクターコード → 表示名 (東証33業種)
SECTOR_NAMES = {
    "0050": "水産・農林業", "1050": "鉱業", "2050": "建設業",
    "3050": "食料品", "3100": "繊維製品", "3150": "パルプ・紙",
    "3200": "化学", "3250": "医薬品", "3300": "石油・石炭製品",
    "3350": "ゴム製品", "3400": "ガラス・土石製品", "3450": "鉄鋼",
    "3500": "非鉄金属", "3550": "金属製品", "3600": "機械",
    "3650": "電気機器", "3700": "輸送用機器", "3750": "精密機器",
    "3800": "その他製品", "4050": "電気・ガス業", "5050": "陸運業",
    "5100": "海運業", "5150": "空運業", "5200": "倉庫・運輸関連",
    "5250": "情報・通信業", "6050": "卸売業", "6100": "小売業",
    "7050": "銀行業", "7100": "証券・商品先物", "7150": "保険業",
    "7200": "その他金融業", "8050": "不動産業", "9050": "サービス業",
}

# ──────────────────────────────────────────────
# 送信済みID管理
# ──────────────────────────────────────────────
def load_sent() -> set:
    if SENT_FILE.exists():
        data = json.loads(SENT_FILE.read_text(encoding="utf-8"))
        return set(data.get("ids", []))
    return set()

def save_sent(sent: set):
    # 直近2000件だけ保持（肥大化防止）
    ids = list(sent)[-2000:]
    SENT_FILE.write_text(json.dumps({"ids": ids}, ensure_ascii=False, indent=2), encoding="utf-8")

# ──────────────────────────────────────────────
# EDINET API
# ──────────────────────────────────────────────
EDINET_BASE = "https://api.edinet-fsa.go.jp/api/v2"

def edinet_headers() -> dict:
    return {"Ocp-Apim-Subscription-Key": EDINET_API_KEY} if EDINET_API_KEY else {}

def fetch_edinet_documents(target_date: str) -> list[dict]:
    """指定日の書類一覧を取得"""
    url = f"{EDINET_BASE}/documents.json"
    # type=1: メタデータのみ（APIキー不要）
    # type=2: 書類情報あり（APIキー必須） → キーがあれば使う
    doc_type = 2 if EDINET_API_KEY else 1
    params = {"date": target_date, "type": doc_type}
    try:
        r = requests.get(url, params=params, headers=edinet_headers(), timeout=30)
        r.raise_for_status()
        data = r.json()
        results = data.get("results", [])
        print(f"[EDINET] {target_date} → {len(results)}件 (type={doc_type})")

        # type=1の場合はメタのみなので docDescription などが空になる可能性がある
        # → 件数確認用にログ出力して返す
        if results:
            sample = results[0]
            print(f"[EDINET] サンプル: {sample.get('filerName','')} / formCode={sample.get('formCode','')} / docTypeCode={sample.get('docTypeCode','')}")
        return results
    except Exception as e:
        print(f"[EDINET] 書類一覧取得エラー: {e}")
        # レスポンス内容も出力して原因特定しやすくする
        try:
            print(f"[EDINET] レスポンス: {r.text[:300]}")
        except:
            pass
        return []

def classify_doc(doc: dict) -> str | None:
    """書類種別を分類して返す"""
    form = doc.get("formCode", "")
    desc = doc.get("docDescription", "")

    # ── 決算短信（東証規則・内閣府令どちらも） ──
    # docDescription に「決算短信」を含むものすべてを対象にする
    if any(kw in desc for kw in ["決算短信", "四半期決算短信", "中間決算短信"]):
        return "earnings"
    # formCodeベースでも拾う（念のため）
    if form in (
        "030000", "030001",  # 有価証券報告書・半期
        "043000", "043001",  # 四半期報告書
        "044000", "044001",  # 半期報告書
        "020000",            # 臨時報告書（決算含む場合あり）
    ):
        # 有価証券報告書は別扱い
        if form in ("030000", "030001"):
            return "annual_report"
        return "earnings"

    # ── 上方・下方修正 ──
    if any(kw in desc for kw in ["上方修正", "下方修正", "業績修正", "業績予想の修正"]):
        return "revision"

    # ── 適時開示・臨時報告書 ──
    if any(kw in desc for kw in ["適時開示", "臨時報告", "重要事実"]):
        return "timely"

    # ── 新薬・医薬品承認 ──
    if any(kw in desc for kw in ["承認", "薬事", "FDA", "医薬品", "治験"]):
        return "pharma"

    # ── 有価証券報告書（formCode未一致の場合も） ──
    if "有価証券報告書" in desc:
        return "annual_report"

    return None

# ──────────────────────────────────────────────
# yfinance 財務データ取得
# ──────────────────────────────────────────────
def get_financials(ticker_jp: str) -> dict:
    """例: '7203' → '7203.T' でyfinance取得"""
    symbol = f"{ticker_jp}.T"
    try:
        tk = yf.Ticker(symbol)
        info = tk.info
        financials = tk.financials

        revenue = net_income = total_debt = None
        if not financials.empty:
            rev_key = [k for k in financials.index if "Revenue" in k or "売上" in k]
            inc_key = [k for k in financials.index if "Net Income" in k]
            if rev_key:
                revenue = financials.loc[rev_key[0]].iloc[0]
            if inc_key:
                net_income = financials.loc[inc_key[0]].iloc[0]

        total_debt = info.get("totalDebt")
        sector     = info.get("sector", "")
        company    = info.get("longName") or info.get("shortName", symbol)

        return {
            "company": company,
            "sector": sector,
            "revenue": revenue,
            "net_income": net_income,
            "total_debt": total_debt,
            "symbol": symbol,
        }
    except Exception as e:
        print(f"[yfinance] {ticker_jp} 取得エラー: {e}")
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
    """決算チャンネル用Embed"""
    ticker   = doc.get("secCode", "")
    company  = fin.get("company") or doc.get("filerName", "不明")
    sector   = fin.get("sector", "不明")
    period   = doc.get("periodEnd", "")
    doc_url  = f"https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?S1{doc.get('docID','')}"

    color = 0x00b4d8  # 青

    fields = [
        {"name": "💹 売上高",   "value": fmt_yen(fin.get("revenue")),    "inline": True},
        {"name": "📈 純利益",   "value": fmt_yen(fin.get("net_income")), "inline": True},
        {"name": "🏦 有利子負債", "value": fmt_yen(fin.get("total_debt")), "inline": True},
    ]

    return {
        "username": "決算Bot",
        "embeds": [{
            "title": f"📊 {company}（{ticker}）決算発表",
            "url": doc_url,
            "color": color,
            "fields": fields,
            "footer": {"text": f"セクター: {sector}　|　決算期: {period}　|　EDINET"},
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }]
    }

def build_news_embed(doc: dict, doc_type: str) -> dict:
    """ニュースチャンネル用Embed"""
    company = doc.get("filerName", "不明")
    ticker  = doc.get("secCode", "")
    desc    = doc.get("docDescription", "")
    doc_url = f"https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?S1{doc.get('docID','')}"

    type_map = {
        "timely":       ("📢 適時開示",     0xf4a261),
        "annual_report":("📋 有価証券報告書", 0x6c757d),
        "revision":     ("🔄 業績修正",      0xe63946 if "下方" in desc else 0x2dc653),
        "pharma":       ("💊 新薬・薬事",    0x9b5de5),
    }
    label, color = type_map.get(doc_type, ("📌 開示情報", 0xadb5bd))

    return {
        "username": "ニュースBot",
        "embeds": [{
            "title": f"{label}｜{company}（{ticker}）",
            "description": desc[:200] or "詳細はリンク先を確認",
            "url": doc_url,
            "color": color,
            "footer": {"text": "EDINET適時開示"},
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }]
    }

# ──────────────────────────────────────────────
# Discord送信
# ──────────────────────────────────────────────
def post_discord(webhook_url: str, payload: dict):
    r = requests.post(webhook_url, json=payload, timeout=15)
    if r.status_code == 429:
        retry = int(r.headers.get("Retry-After", 5))
        print(f"[Discord] Rate limit。{retry}秒後リトライ")
        time.sleep(retry)
        requests.post(webhook_url, json=payload, timeout=15)
    elif r.status_code not in (200, 204):
        print(f"[Discord] エラー {r.status_code}: {r.text[:200]}")

# ──────────────────────────────────────────────
# メイン処理
# ──────────────────────────────────────────────
def main():
    sent = load_sent()

    # 直近5日分を順番に試す（土日・祝日・データ遅延対策）
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

    # 分類ごとの件数を表示
    classified = [classify_doc(d) for d in docs]
    from collections import Counter
    counts = Counter(c for c in classified if c)
    print(f"[分類] {counts}")

    # 決算として検出した書類をログ出力
    for d, c in zip(docs, classified):
        if c == "earnings":
            print(f"[決算検出] {d.get('filerName','')} | {d.get('docDescription','')} | formCode={d.get('formCode','')} | secCode={d.get('secCode','')}")

    for doc in docs:
        doc_id = doc.get("docID", "")
        if not doc_id or doc_id in sent:
            continue

        doc_type = classify_doc(doc)
        if not doc_type:
            continue

        ticker = (doc.get("secCode") or "").replace(" ", "")

        if doc_type == "earnings" and ticker:
            fin = get_financials(ticker)
            payload = build_earnings_embed(doc, fin)
            post_discord(DISCORD_EARNINGS_WEBHOOK, payload)
            print(f"[決算] {doc.get('filerName')} を送信")
            time.sleep(1)

        else:
            payload = build_news_embed(doc, doc_type)
            post_discord(DISCORD_NEWS_WEBHOOK, payload)
            print(f"[ニュース] {doc_type} / {doc.get('filerName')} を送信")
            time.sleep(1)

        sent.add(doc_id)

    save_sent(sent)
    print("完了")

if __name__ == "__main__":
    main()
