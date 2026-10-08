"""台股每日收集：把上市＋上櫃的收盤行情、三大法人、本益比、融資融券整理成「每個交易日一個 CSV」，存在 data/ 資料夾。

來源都是官方公開資料，不需要金鑰。各來源的更新時間不同，所以每天晚上排程跑多次，
每次重抓，直到所有來源都確認是當天的資料為止（已確認的部分不會被弄壞）。

用法：
  python collect.py                    收集最近一個交易日（台灣時間 17:00 前算前一天）
  python collect.py --date 2026-10-08  指定日期
  python collect.py --backfill 40      回補最近 40 個交易日的「價量」（沒有法人、本益比、融資券）

只讀環境變數 MAIL_URL、MAIL_TOKEN（寄通知信，選用）與 LAST_RUN（當天最後一次排程設為 1）。
"""
import argparse
import csv
import datetime as dt
import json
import os
import re
import time

import requests

TZ = dt.timezone(dt.timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 stock-collect"}
TWSE_WEB = "https://www.twse.com.tw/rwd/zh"
TPEX_WEB = "https://www.tpex.org.tw/www/zh-tw/afterTrading/otc"
TWSE_OPEN = "https://openapi.twse.com.tw/v1"
TPEX_OPEN = "https://www.tpex.org.tw/openapi/v1"
DATA = "data"
META = "data/_meta"
MARKET_CSV = "data/market.csv"
SLEEP = 1.5          # 對政府網站客氣一點

COLS = ["date", "code", "name", "market", "open", "high", "low", "close", "chg", "volume", "value", "trades",
        "pe", "yield", "pb", "foreign_net", "trust_net", "dealer_net", "inst_net",
        "margin_bal", "margin_chg", "short_bal"]
# 各來源負責哪個市場、哪些欄位
SOURCES = {
    "twse_inst": ("TWSE", ["foreign_net", "trust_net", "dealer_net", "inst_net"]),
    "tpex_inst": ("TPEx", ["foreign_net", "trust_net", "dealer_net", "inst_net"]),
    "twse_fund": ("TWSE", ["pe", "yield", "pb"]),
    "tpex_fund": ("TPEx", ["pe", "yield", "pb"]),
    "twse_margin": ("TWSE", ["margin_bal", "margin_chg", "short_bal"]),
    "tpex_margin": ("TPEx", ["margin_bal", "margin_chg", "short_bal"]),
}
REQUIRED = ["twse_prices", "tpex_prices"]
ALL = REQUIRED + list(SOURCES)


# ---------- 小工具 ----------
def get_json(url, params=None):
    """最多試 3 次；後兩次改用不壓縮傳輸（避開傳輸中斷）。失敗回 None。"""
    for i in range(3):
        headers = dict(UA)
        if i >= 1:
            headers["Accept-Encoding"] = "identity"
        try:
            r = requests.get(url, params=params, headers=headers, timeout=(10, 90))
            if r.status_code == 200:
                return r.json()
            print("  HTTP %s：%s" % (r.status_code, url.split("/")[-1]))
            if r.status_code in (403, 404):
                return None
        except (requests.RequestException, ValueError) as e:
            print("  %s：%s（第%d次）" % (type(e).__name__, url.split("/")[-1], i + 1))
        time.sleep((5, 15, 30)[i])      # 逐次加長等待（遇到 502、逾時或被限流時給對方喘息）
    return None


def num(x):
    """'1,234.5'、'+0.19'、HTML 包著的數字 → float；空白、'--'、'X' → None。"""
    if x is None:
        return None
    s = re.sub(r"<[^>]*>", "", str(x)).replace(",", "").strip()
    if s in ("", "-", "--", "---", "----", "X", "x", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def fmt(v):
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if float(v).is_integer():
        return str(int(v))
    return ("%.4f" % v).rstrip("0").rstrip(".")


def hdr(s):
    return re.sub(r"<br\s*/?>|\s+", "", str(s))


def first_index(names):
    ix = {}
    for i, n in enumerate(names):
        ix.setdefault(n, i)
    return ix


def norm_date(s):
    """'20261008'、'2026/10/08'、'115/10/08'、'1151008' → '2026-10-08'；看不懂回 None。"""
    d = re.sub(r"\D", "", str(s or ""))
    try:
        if len(d) == 8:
            return dt.date(int(d[:4]), int(d[4:6]), int(d[6:])).isoformat()
        if len(d) == 7:
            return dt.date(int(d[:3]) + 1911, int(d[3:5]), int(d[5:])).isoformat()
    except ValueError:
        pass
    return None


def pick_table(obj, need):
    for t in obj.get("tables") or []:
        h = [hdr(f) for f in t.get("fields") or []]
        if all(n in h for n in need) and t.get("data"):
            return h, t["data"]
    return None, None


# ---------- 價量（必要來源，用「指定日期」的網站 JSON，可回補歷史） ----------
def twse_prices(d):
    obj = get_json(TWSE_WEB + "/afterTrading/MI_INDEX", {"date": d.strftime("%Y%m%d"), "type": "ALLBUT0999", "response": "json"})
    if not isinstance(obj, dict) or str(obj.get("stat")).upper() != "OK":
        return None
    nd = norm_date(obj.get("date"))
    if nd is not None and nd != d.isoformat():      # 看得懂日期而且不是這一天 → 還沒公布；看不懂格式就不擋
        return None
    h, rows = pick_table(obj, ["證券代號", "證券名稱", "收盤價", "漲跌(+/-)", "漲跌價差"])
    if not rows:
        return None
    ix = first_index(h)
    out = {}
    for r in rows:
        code = str(r[ix["證券代號"]]).strip()
        if not code:
            continue
        sign = re.sub(r"<[^>]*>", "", str(r[ix["漲跌(+/-)"]])).strip()
        diff = num(r[ix["漲跌價差"]])
        if diff is None:
            chg = None
        elif diff == 0:
            chg = 0.0
        elif sign == "+":
            chg = diff
        elif sign == "-":
            chg = -diff
        else:
            chg = None
        pe = num(r[ix["本益比"]]) if "本益比" in ix else None
        g = lambda n: num(r[ix[n]]) if n in ix else None  # noqa: E731
        if g("收盤價") is None:        # 當天沒有成交：不算上漲、下跌或持平
            chg = None
        out[code] = {"name": str(r[ix["證券名稱"]]).strip(), "open": g("開盤價"), "high": g("最高價"), "low": g("最低價"),
                     "close": g("收盤價"), "chg": chg, "volume": g("成交股數"), "value": g("成交金額"),
                     "trades": g("成交筆數"), "pe": pe if pe else None}
    return out


def tpex_prices(d):
    obj = get_json(TPEX_WEB, {"date": d.strftime("%Y/%m/%d"), "type": "EW", "response": "json"})
    if not isinstance(obj, dict) or str(obj.get("stat")).lower() != "ok":
        return None
    nd = norm_date(obj.get("date"))
    if nd is not None and nd != d.isoformat():
        return None
    h, rows = pick_table(obj, ["代號", "名稱", "收盤", "漲跌"])
    if not rows:
        return None
    ix = first_index(h)
    g = lambda r, n: num(r[ix[n]]) if n in ix else None  # noqa: E731
    out = {}
    for r in rows:
        code = str(r[ix["代號"]]).strip()
        if not code:
            continue
        out[code] = {"name": str(r[ix["名稱"]]).strip(), "open": g(r, "開盤"), "high": g(r, "最高"), "low": g(r, "最低"),
                     "close": g(r, "收盤"), "chg": g(r, "漲跌"), "volume": g(r, "成交股數"),
                     "value": g(r, "成交金額(元)"), "trades": g(r, "成交筆數"), "pe": None}
    return out


# ---------- 三大法人（只收淨買賣超股數） ----------
def twse_inst(d):
    obj = get_json(TWSE_WEB + "/fund/T86", {"date": d.strftime("%Y%m%d"), "selectType": "ALLBUT0999", "response": "json"})
    if not isinstance(obj, dict) or str(obj.get("stat")).upper() != "OK" or norm_date(obj.get("date")) != d.isoformat():
        return None
    f = [hdr(x) for x in obj.get("fields") or []]
    need = ["證券代號", "外陸資買賣超股數(不含外資自營商)", "外資自營商買賣超股數", "投信買賣超股數", "自營商買賣超股數", "三大法人買賣超股數"]
    rows = obj.get("data") or []
    if not rows or any(n not in f for n in need):
        return None
    ix = first_index(f)
    out = {}
    for r in rows:
        code = str(r[ix["證券代號"]]).strip()
        a, b = num(r[ix[need[1]]]), num(r[ix[need[2]]])
        out[code] = {"foreign_net": (a or 0) + (b or 0), "trust_net": num(r[ix["投信買賣超股數"]]),
                     "dealer_net": num(r[ix["自營商買賣超股數"]]), "inst_net": num(r[ix["三大法人買賣超股數"]])}
    return out


def _key(r, name):
    """欄位名稱有空白、大小寫不一的問題，比對時全部去空白轉小寫。"""
    want = re.sub(r"\s+", "", name).lower()
    for k in r:
        if re.sub(r"\s+", "", k).lower() == want:
            return k
    return None


def tpex_inst(d):
    rows = get_json(TPEX_OPEN + "/tpex_3insti_daily_trading")
    if not isinstance(rows, list) or not rows or norm_date(rows[0].get("Date")) != d.isoformat():
        return None
    keys = {n: _key(rows[0], n) for n in ("SecuritiesCompanyCode", "ForeignInvestorsIncludeMainlandAreaInvestors-Difference",
                                          "SecuritiesInvestmentTrustCompanies-Difference", "Dealers-Difference", "TotalDifference")}
    if any(v is None for v in keys.values()):
        print("  櫃買三大法人欄位名稱有變，略過")
        return None
    out = {}
    for r in rows:
        out[str(r[keys["SecuritiesCompanyCode"]]).strip()] = {
            "foreign_net": num(r.get(keys["ForeignInvestorsIncludeMainlandAreaInvestors-Difference"])),
            "trust_net": num(r.get(keys["SecuritiesInvestmentTrustCompanies-Difference"])),
            "dealer_net": num(r.get(keys["Dealers-Difference"])), "inst_net": num(r.get("TotalDifference"))}
    return out


# ---------- 本益比、殖利率、股價淨值比 ----------
def twse_fund(d):
    rows = get_json(TWSE_OPEN + "/exchangeReport/BWIBBU_ALL")
    if not isinstance(rows, list) or not rows or norm_date(rows[0].get("Date")) != d.isoformat():
        return None
    return {str(r.get("Code", "")).strip(): {"pe": num(r.get("PEratio")) or None, "yield": num(r.get("DividendYield")),
                                            "pb": num(r.get("PBratio"))} for r in rows}


def tpex_fund(d):
    rows = get_json(TPEX_OPEN + "/tpex_mainboard_peratio_analysis")
    if not isinstance(rows, list) or not rows or norm_date(rows[0].get("Date")) != d.isoformat():
        return None
    return {str(r.get("SecuritiesCompanyCode", "")).strip(): {"pe": num(r.get("PriceEarningRatio")) or None,
                                                             "yield": num(r.get("YieldRatio")), "pb": num(r.get("PriceBookRatio"))} for r in rows}


# ---------- 融資融券（股數單位：張） ----------
def twse_margin(d):
    """證交所這份資料沒有日期欄位，無法直接確認是不是當天；是否過期由 is_stale() 拿前一天比對。"""
    rows = get_json(TWSE_OPEN + "/exchangeReport/MI_MARGN")
    if not isinstance(rows, list) or not rows:
        return None
    out = {}
    for r in rows:
        code = str(r.get("股票代號", "")).strip()
        if not code:
            continue
        today, prev = num(r.get("融資今日餘額")) or 0.0, num(r.get("融資前日餘額")) or 0.0
        out[code] = {"margin_bal": today, "margin_chg": today - prev, "short_bal": num(r.get("融券今日餘額")) or 0.0}
    return out


def tpex_margin(d):
    rows = get_json(TPEX_OPEN + "/tpex_mainboard_margin_balance")
    if not isinstance(rows, list) or not rows or norm_date(rows[0].get("Date")) != d.isoformat():
        return None
    out = {}
    for r in rows:
        code = str(r.get("SecuritiesCompanyCode", "")).strip()
        today, prev = num(r.get("MarginPurchaseBalance")) or 0.0, num(r.get("MarginPurchaseBalancePreviousDay")) or 0.0
        out[code] = {"margin_bal": today, "margin_chg": today - prev, "short_bal": num(r.get("ShortSaleBalance")) or 0.0}
    return out


def is_stale(data, prev):
    """證交所融資餘額幾乎都和前一個交易日一樣 → 判定還沒更新。前一天沒有融資資料可比就無法判斷，視為可用。"""
    n = same = 0
    for code, rec in data.items():
        p = prev.get(code)
        try:
            pv = float(p["margin_bal"]) if p and p.get("margin_bal", "") != "" else None
        except ValueError:
            pv = None
        if pv is None or pv <= 0:
            continue
        n += 1
        same += abs(pv - rec["margin_bal"]) < 1e-9
    return n >= 200 and same / n >= 0.95


FETCH = {"twse_prices": twse_prices, "tpex_prices": tpex_prices, "twse_inst": twse_inst, "tpex_inst": tpex_inst,
         "twse_fund": twse_fund, "tpex_fund": tpex_fund, "twse_margin": twse_margin, "tpex_margin": tpex_margin}


# ---------- 檔案 ----------
def csv_path(d):
    return "%s/%s.csv" % (DATA, d.isoformat())


def meta_path(d):
    return "%s/%s.json" % (META, d.isoformat())


def load_csv(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        return {r["code"]: r for r in csv.DictReader(f)}


def load_prev(d):
    days = []
    if os.path.isdir(DATA):
        days = sorted(n[:-4] for n in os.listdir(DATA) if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.csv", n) and n[:-4] < d.isoformat())
    return load_csv("%s/%s.csv" % (DATA, days[-1])) if days else {}


def load_meta(d):
    try:
        return json.load(open(meta_path(d), encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_meta(d, meta):
    os.makedirs(META, exist_ok=True)
    json.dump(meta, open(meta_path(d), "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def write_csv(d, rows):
    os.makedirs(DATA, exist_ok=True)
    rows = sorted(rows, key=lambda r: (r["market"], r["code"]))
    with open(csv_path(d), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        w.writerows(rows)


def update_market(d, rows):
    """每個市場每天的上漲、下跌、持平家數（只算 4 位數代號的一般股票）。給大盤廣度指標用。"""
    out = []
    for m in ("TWSE", "TPEx"):
        up = down = flat = 0
        for r in rows:
            if r["market"] != m or not re.fullmatch(r"\d{4}", r["code"]) or r["chg"] == "":
                continue
            c = float(r["chg"])
            up += c > 0
            down += c < 0
            flat += c == 0
        out.append({"date": d.isoformat(), "market": m, "n": up + down + flat, "up": up, "down": down, "flat": flat})
    old = []
    if os.path.exists(MARKET_CSV):
        with open(MARKET_CSV, encoding="utf-8-sig", newline="") as f:
            old = [r for r in csv.DictReader(f) if r["date"] != d.isoformat()]
    allr = sorted(old + out, key=lambda r: (r["date"], r["market"]))
    with open(MARKET_CSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "market", "n", "up", "down", "flat"])
        w.writeheader()
        w.writerows(allr)
    return out


def price_rows(d, res):
    rows = {}
    for market, key in (("TWSE", "twse_prices"), ("TPEx", "tpex_prices")):
        for code, rec in res[key].items():
            r = {c: "" for c in COLS}
            r.update(date=d.isoformat(), code=code, market=market)
            r.update({k: fmt(v) for k, v in rec.items()})
            rows[(market, code)] = r
    return rows


# ---------- 通知 ----------
def send_mail(subject, body):
    url, token = os.environ.get("MAIL_URL"), os.environ.get("MAIL_TOKEN")
    if not url or not token:
        print("未設定 MAIL_URL / MAIL_TOKEN，略過通知")
        return False
    try:
        r = requests.post(url, json={"token": token, "subject": subject, "body": body}, timeout=60)
        ok = r.status_code == 200 and bool(r.json().get("ok"))
        print("通知寄送：成功" if ok else "通知寄送：失敗 %s" % r.status_code)
        return ok
    except Exception as e:  # noqa: BLE001
        print("通知寄送：失敗", str(e)[:100])
        return False


# ---------- 主流程 ----------
def latest_weekday(now):
    d = now.date()
    if now.hour < 17:
        d -= dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def run_day(d, last_run=False):
    meta = load_meta(d)
    if meta and all(meta.get("sources", {}).get(k) == "ok" for k in ALL):
        print("%s 已經收集完成，略過" % d)
        return
    prior, prev = load_csv(csv_path(d)), load_prev(d)
    res = {}
    for name, fn in FETCH.items():
        print("抓取", name)
        res[name] = fn(d)
        time.sleep(SLEEP)
    if res["twse_prices"] is None and res["tpex_prices"] is None:
        print("上市與上櫃都沒有 %s 的行情（休市，或尚未公布）" % d)
        return
    if res["twse_prices"] is None or res["tpex_prices"] is None:
        miss = "上市" if res["twse_prices"] is None else "上櫃"
        print("只有一邊有 %s 的行情（缺%s），這次不存檔" % (d, miss))
        if last_run and not meta.get("warned"):
            send_mail("【台股】%s 資料不完整：缺%s行情" % (d, miss), "今天最後一次排程仍抓不到%s的收盤行情，沒有存檔。\n請稍後到 Actions 手動重跑，或看執行日誌。" % miss)
            meta["warned"] = True
            save_meta(d, meta)
        return
    if res["twse_margin"] is not None and is_stale(res["twse_margin"], prev):
        print("證交所融資融券看起來還是前一天的資料，這次先不採用")
        res["twse_margin"] = None

    rows = price_rows(d, res)
    status = {k: "ok" for k in REQUIRED}
    for name, (market, cols) in SOURCES.items():
        data = res[name]
        if data is not None:
            for code, rec in data.items():
                r = rows.get((market, code))
                if not r:
                    continue
                for k, v in rec.items():
                    if k == "pe" and r["pe"] != "":
                        continue          # 證交所收盤表自帶的本益比優先
                    r[k] = fmt(v)
            status[name] = "ok"
        else:                              # 這次沒抓到：保留先前已確認的值，不讓它退步
            carried = False
            for (m, code), r in rows.items():
                pr = prior.get(code) if m == market else None
                if pr:
                    for c in cols:
                        if r[c] == "" and pr.get(c, "") != "":
                            r[c] = pr[c]
                            carried = True
            status[name] = "ok" if carried and meta.get("sources", {}).get(name) == "ok" else "pending"

    write_csv(d, list(rows.values()))
    ad = update_market(d, list(rows.values()))
    pending = [k for k, v in status.items() if v != "ok"]
    meta.update({"date": d.isoformat(), "sources": status, "rows": len(rows), "updated": dt.datetime.now(TZ).isoformat()})
    n_tw = sum(1 for k in rows if k[0] == "TWSE")
    print("已存檔 %s：上市 %d、上櫃 %d；待補來源：%s" % (csv_path(d), n_tw, len(rows) - n_tw, pending or "無"))
    if not pending and not meta.get("mailed"):
        body = ["資料已收集完成：%s" % d, "上市 %d 檔、上櫃 %d 檔" % (n_tw, len(rows) - n_tw), ""]
        body += ["%s 上漲 %d／下跌 %d／持平 %d（一般股票 %d 檔）" % (a["market"], a["up"], a["down"], a["flat"], a["n"]) for a in ad]
        if send_mail("【台股】%s 資料已更新" % d, "\n".join(body)):
            meta["mailed"] = True
    elif pending and last_run and not meta.get("warned"):
        if send_mail("【台股】%s 資料部分缺漏" % d, "今天最後一次排程後，這些來源仍沒確認到當天資料：\n" + "\n".join(pending)
                     + "\n\n價量已存檔；缺的欄位是空白。可稍後到 Actions 手動重跑 --date %s。" % d):
            meta["warned"] = True
    save_meta(d, meta)


def backfill(start, n):
    """回補最近 n 個交易日的價量。已經有檔的日期不重抓；休市日（兩邊都沒資料）自動略過。"""
    have = tried = 0
    d = start
    for _ in range(n * 3 + 20):
        if have >= n:
            break
        if d.weekday() < 5:
            if os.path.exists(csv_path(d)):
                have += 1
            else:
                tried += 1
                tw = twse_prices(d)
                time.sleep(SLEEP * 2)
                tp = tpex_prices(d)
                time.sleep(SLEEP * 2)
                if tw is not None and tp is not None:
                    rows = price_rows(d, {"twse_prices": tw, "tpex_prices": tp})
                    write_csv(d, list(rows.values()))
                    update_market(d, list(rows.values()))
                    status = {k: "ok" for k in REQUIRED}
                    status.update({k: "na" for k in SOURCES})
                    save_meta(d, {"date": d.isoformat(), "sources": status, "rows": len(rows), "backfilled": True,
                                  "updated": dt.datetime.now(TZ).isoformat()})
                    have += 1
                    print("回補 %s：%d 檔" % (d, len(rows)))
                else:
                    print("略過 %s（休市或沒有資料）" % d)
        d -= dt.timedelta(days=1)
    print("回補完成：目前有 %d 個交易日（本次嘗試 %d 天）" % (have, tried))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="")
    ap.add_argument("--backfill", type=int, default=0)
    args = ap.parse_args()
    target = latest_weekday(dt.datetime.now(TZ))
    if args.backfill > 0:
        backfill(target, args.backfill)
        return
    d = dt.date.fromisoformat(args.date) if args.date else target
    run_day(d, last_run=os.environ.get("LAST_RUN") == "1")


if __name__ == "__main__":
    main()
