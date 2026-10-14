"""台股掃描：讀 data/ 的每日 CSV，依 scan_config.json 的規則整理成一封通知信。

信分三段：
  一、持股：每檔的收盤、本益比、乖離率、量比、法人買賣超，並標出碰到哪些警示條件
  二、追蹤清單：同樣欄位
  三、全市場候選：符合你設定的全部條件的股票，並列出每一關剩下幾檔，方便調整條件

只做資料整理與依你的規則篩選，不是買賣建議。

用法：
  python scan.py                    掃描最近一個交易日（資料齊了才寄信）
  python scan.py --date 2026-10-13  指定日期
  python scan.py --force            不管資料齊不齊、寄過沒有，都重寄
  python scan.py --dry              只印出信的內容，不寄信、不存檔
  python scan.py --mail-test        只寄一封測試信，確認 MAIL_URL / MAIL_TOKEN 設定正確

資料「齊了」的定義：價量、上櫃本益比、上市與上櫃的法人買賣超都已確認是當天的（NEEDED）。
證交所的殖利率／淨值比與融資融券這封信用不到，不列入。

環境變數：MAIL_URL、MAIL_TOKEN（寄信）、LAST_RUN=1（當天最後一次排程：資料還沒齊也寄一封，信裡會註明）、
FORCE=1（等同 --force）。
"""
import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
import traceback

import collect

CONFIG = "scan_config.json"
SCAN_DIR = "scan"
DEFAULTS = {
    "holdings": [],
    "watchlist": [],
    "indicators": {"ma_days": 20, "vol_short_days": 5, "vol_long_days": 20},
    "alerts": {"pe_max": 35, "below_ma": True, "stop_price": {}},
    "screen": {"pe_min": 0, "pe_max": 30, "min_value": 100000000, "bias_min": -8, "bias_max": 0,
               "vol_ratio_max": 0.7, "sort": "value", "max_results": 20,
               "inst_streak": {"enabled": False, "who": "foreign", "days": 3}},
    "revenue": {"enabled": True, "title": "市場建議股票掃描", "min_value": 100000000, "max_results": 10, "sort": "yoy_abs",
                "patterns": [{"name": "年減且月減", "yoy": "down", "mom": "down"}, {"name": "年增且月增", "yoy": "up", "mom": "up"}]},
    "inst_rank": {"enabled": True, "title": "三大法人連續買超排名", "who": "inst", "min_days": 3, "min_value": 100000000, "top": 10, "lookback": 40},
}
# 掃描信真正用到的來源：價量（含上市本益比）、上櫃本益比、法人買賣超。
# 證交所的殖利率／淨值比（twse_fund）和各市場融資融券這封信沒用到，沒齊不影響信件，也不擋寄信。
NEEDED = ["twse_prices", "tpex_prices", "tpex_fund", "twse_inst", "tpex_inst"]
# 哪個來源沒齊，會影響信裡的什麼
AFFECT = {"twse_inst": "上市法人買賣超", "tpex_inst": "上櫃法人買賣超", "twse_fund": "上市本益比", "tpex_fund": "上櫃本益比（上櫃股票不會進候選名單）",
          "twse_margin": "上市融資融券（本信未使用）", "tpex_margin": "上櫃融資融券（本信未使用）"}
INST_COL = {"foreign": "foreign_net", "trust": "trust_net", "inst": "inst_net"}
INST_NAME = {"foreign": "外資", "trust": "投信", "inst": "三大法人"}


# ---------- 設定 ----------
def merge(base, new):
    out = dict(base)
    for k, v in (new or {}).items():
        if k.startswith("_"):
            continue
        out[k] = merge(base[k], v) if isinstance(base.get(k), dict) and isinstance(v, dict) else v
    return out


def load_config(path=CONFIG):
    missing = False
    try:
        raw = json.load(open(path, encoding="utf-8"))
    except FileNotFoundError:
        print("找不到 %s，使用內建預設值" % path)
        raw, missing = {}, True
    except ValueError as e:
        raise ValueError("%s 格式有誤（是不是多了或少了逗號、引號？）：%s" % (path, e))
    cfg = merge(DEFAULTS, raw)
    cfg["_missing"] = missing
    cfg["holdings"] = [str(c) for c in cfg["holdings"]]
    cfg["watchlist"] = [str(c) for c in cfg["watchlist"]]
    return cfg


# ---------- 歷史與指標 ----------
def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def history(d, n):
    """回傳 [(日期, {代號: 列})]，到 d 為止最近 n 個有檔案的交易日，由舊到新。d 本身必須有檔案。"""
    days = sorted(x[:-4] for x in os.listdir(collect.DATA) if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.csv", x) and x[:-4] <= d.isoformat())
    return [(x, collect.load_csv("%s/%s.csv" % (collect.DATA, x))) for x in days[-n:]]


def mean(v):
    return sum(v) / len(v)


def metrics(code, hist, ind):
    today = hist[-1][1].get(code)
    if not today:
        return None
    close, chg = f(today["close"]), f(today["chg"])
    m = {"code": code, "name": today["name"], "market": today["market"], "close": close, "chg": chg,
         "pct": chg / (close - chg) * 100 if close is not None and chg is not None and close - chg > 0 else None,
         "value": f(today["value"]), "pe": f(today["pe"]),
         "foreign": f(today["foreign_net"]), "trust": f(today["trust_net"]), "inst": f(today["inst_net"]),
         "ma": None, "bias": None, "vol_ratio": None}
    n = ind["ma_days"]
    cl = [f(r.get(code, {}).get("close")) for _, r in hist[-n:]]
    if len(cl) == n and None not in cl and close is not None:
        m["ma"] = mean(cl)
        m["bias"] = (close / m["ma"] - 1) * 100
    s, lg = ind["vol_short_days"], ind["vol_long_days"]
    vs = [f(r.get(code, {}).get("volume")) for _, r in hist[-s:]]
    vl = [f(r.get(code, {}).get("volume")) for _, r in hist[-lg:]]
    if len(vs) == s and len(vl) == lg and None not in vs and None not in vl and mean(vl) > 0:
        m["vol_ratio"] = mean(vs) / mean(vl)
    return m


def is_stock(code):
    return bool(re.fullmatch(r"\d{4}", code))


def flags(m, alerts, ind):
    out = []
    if is_stock(m["code"]) and m["pe"] is not None and m["pe"] > alerts["pe_max"]:
        out.append("本益比 %.1f 超過 %g" % (m["pe"], alerts["pe_max"]))
    if alerts["below_ma"] and m["ma"] is not None and m["close"] < m["ma"]:
        out.append("收盤跌破 %d 日均線" % ind["ma_days"])
    stop = f(alerts["stop_price"].get(m["code"]))
    if stop is not None and m["close"] is not None and m["close"] <= stop:
        out.append("收盤 %.2f 已到停損價 %g" % (m["close"], stop))
    return out


def lots(x):
    return "%+d" % round(x / 1000)


def line(m, tags=(), with_value=False):
    if m["close"] is None:
        head = "%s %s｜今日無成交" % (m["code"], m["name"])
    else:
        head = "%s %s｜收 %s%s" % (m["code"], m["name"], ("%.2f" % m["close"]).rstrip("0").rstrip("."),
                                  "（%+.2f%%）" % m["pct"] if m["pct"] is not None else "")
    parts = [head,
             "PE %.1f" % m["pe"] if m["pe"] is not None else "PE -",
             "乖離 %+.1f%%" % m["bias"] if m["bias"] is not None else "乖離 -（均線資料不足）",
             "量比 %.2f" % m["vol_ratio"] if m["vol_ratio"] is not None else "量比 -"]
    # 第二行：成交值、法人買賣超、標記（拆行是為了避免 Gmail 在行中間斷字）
    second = []
    if with_value and m["value"] is not None:
        second.append("成交 %.1f億" % (m["value"] / 1e8))
    if m["foreign"] is not None or m["trust"] is not None:
        second.append("外資 %s張 投信 %s張" % (lots(m["foreign"]) if m["foreign"] is not None else "-", lots(m["trust"]) if m["trust"] is not None else "-"))
    s = "｜".join(parts)
    tail = "｜".join(second)
    if tags:
        tail += ("　" if tail else "") + "【" + "；".join(tags) + "】"
    return s + ("\n　" + tail if tail else "")


# ---------- 篩選 ----------
def inst_rule(hist, scr):
    """法人連買規則能不能套用。回傳 (能否套用, 說明)。"""
    r = scr["inst_streak"]
    if not r["enabled"]:
        return False, ""
    col, n = INST_COL[r["who"]], int(r["days"])
    last = hist[-n:]
    ok_days = [d for d, rows in last if rows and sum(1 for x in rows.values() if x.get(col, "") != "") >= max(1, len(rows) // 2)]
    if len(last) < n or len(ok_days) < n:
        return False, "%s連續買超 %d 日：法人資料累積天數不夠（目前最近 %d 日中有 %d 日有資料），本次未套用" % (INST_NAME[r["who"]], n, len(last), len(ok_days))
    return True, ""


def screen(hist, cfg):
    scr, ind = cfg["screen"], cfg["indicators"]
    today = hist[-1][1]
    stages = []
    pool = []
    for code in today:
        if not is_stock(code):
            continue
        m = metrics(code, hist, ind)
        if m and m["close"] is not None and (m["value"] or 0) > 0:
            pool.append(m)
    stages.append(("全市場一般股票（有成交）", len(pool)))
    pool = [m for m in pool if m["pe"] is not None and scr["pe_min"] < m["pe"] < scr["pe_max"]]
    stages.append(("本益比 %g ~ %g（空白者排除）" % (scr["pe_min"], scr["pe_max"]), len(pool)))
    pool = [m for m in pool if m["value"] >= scr["min_value"]]
    stages.append(("成交值 ≥ %.1f 億" % (scr["min_value"] / 1e8), len(pool)))
    short = sum(1 for m in pool if m["bias"] is None or m["vol_ratio"] is None)
    pool = [m for m in pool if m["bias"] is not None and scr["bias_min"] <= m["bias"] <= scr["bias_max"]]
    stages.append(("乖離率 %g%% ~ %g%%（歷史不足者排除）" % (scr["bias_min"], scr["bias_max"]), len(pool)))
    pool = [m for m in pool if m["vol_ratio"] is not None and m["vol_ratio"] <= scr["vol_ratio_max"]]
    stages.append(("量比 ≤ %g" % scr["vol_ratio_max"], len(pool)))
    apply_inst, note = inst_rule(hist, scr)
    if apply_inst:
        col, n = INST_COL[scr["inst_streak"]["who"]], int(scr["inst_streak"]["days"])
        pool = [m for m in pool if all(f(rows.get(m["code"], {}).get(col)) is not None and f(rows[m["code"]][col]) > 0 for _, rows in hist[-n:])]
        stages.append(("%s連續 %d 日買超" % (INST_NAME[scr["inst_streak"]["who"]], n), len(pool)))
    keys = {"value": lambda m: -m["value"], "bias": lambda m: m["bias"], "pe": lambda m: m["pe"]}
    pool.sort(key=keys.get(scr["sort"], keys["value"]))
    return pool, stages, note, short


# ---------- 月營收與法人連買 ----------
def load_revenue():
    d = os.path.join(collect.DATA, "revenue")
    files = sorted(x for x in os.listdir(d) if re.fullmatch(r"\d{4}-\d{2}\.csv", x)) if os.path.isdir(d) else []
    if not files:
        return None, {}
    with open(os.path.join(d, files[-1]), encoding="utf-8-sig", newline="") as fh:
        return files[-1][:-4], {r["code"]: r for r in csv.DictReader(fh)}


def direction(a, b):
    return "up" if a > b else "down" if a < b else "flat"


def rev_line(m, yoy, mom):
    close = ("收 %s" % ("%.2f" % m["close"]).rstrip("0").rstrip(".")) + ("（%+.2f%%）" % m["pct"] if m["pct"] is not None else "") if m["close"] is not None else "今日無成交"
    return "%s %s｜年增率 %+.1f%%｜月增率 %+.1f%%｜%s｜成交 %.1f億" % (m["code"], m["name"], yoy, mom, close, (m["value"] or 0) / 1e8)


def revenue_section(hist, cfg):
    rc, ind = cfg["revenue"], cfg["indicators"]
    out = ["", "四、%s" % rc["title"]]
    ym, rows = load_revenue()
    if not ym:
        return out + ["（還沒有月營收資料。看 Actions 日誌裡「月營收」開頭的幾行，會寫原因）"]
    desc = {"up": "增", "down": "減", "flat": "持平"}
    sort_key = {"yoy_abs": lambda x: -abs(x[1]), "mom_abs": lambda x: -abs(x[2]), "value": lambda x: -(x[3]["value"] or 0)}.get(rc["sort"]) or (lambda x: -abs(x[1]))
    valid = {}
    for code, r in rows.items():
        rev, prev, ly = f(r["rev"]), f(r["prev"]), f(r["ly"])
        if is_stock(code) and None not in (rev, prev, ly) and prev > 0 and ly > 0:
            valid[code] = (rev, prev, ly)
    out += ["月營收資料月份 %s，有完整數字的 %d 家（每月 10 日前各公司陸續公布，月初幾天家數會偏少）" % (ym, len(valid)),
            "年增率＝當月比去年同月，月增率＝當月比上月；去年同月、上月為 0 的不列入。"]
    for p in rc["patterns"]:
        hit = []
        for code, (rev, prev, ly) in valid.items():
            if p.get("yoy", "any") in ("any", direction(rev, ly)) and p.get("mom", "any") in ("any", direction(rev, prev)):
                hit.append(code)
        keep = []
        for code in hit:
            m = metrics(code, hist, ind)
            if m and m["close"] is not None and (m["value"] or 0) >= rc["min_value"]:
                rev, prev, ly = valid[code]
                keep.append((code, (rev / ly - 1) * 100, (rev / prev - 1) * 100, m))
        keep.sort(key=sort_key)
        top = keep[:int(rc["max_results"])]
        cond = "今年%s於去年同月、本月%s於上月" % ({"up": "高", "down": "低"}.get(p.get("yoy"), "不限"), {"up": "高", "down": "低"}.get(p.get("mom"), "不限"))
        out += ["", "【%s】（%s）符合 %d 家，其中成交值 ≥ %.1f 億的 %d 家，列出前 %d 家（依%s）" % (
            p["name"], cond, len(hit), rc["min_value"] / 1e8, len(keep), len(top), {"yoy_abs": "年增率絕對值", "mom_abs": "月增率絕對值", "value": "成交值"}.get(rc["sort"], "年增率絕對值"))]
        out += [rev_line(m, y, mo) for _, y, mo, m in top] or ["（沒有）"]
    return out


def inst_rank_section(hist, cfg):
    ir, ind = cfg["inst_rank"], cfg["indicators"]
    col = INST_COL[ir["who"]]
    out = ["", "五、%s（%s淨買超）" % (ir["title"], INST_NAME[ir["who"]])]
    window = hist[-int(ir["lookback"]):]
    days_with = sum(1 for _, rows in window if any(x.get(col, "") != "" for x in rows.values()))
    ranked = []
    for code in hist[-1][1]:
        if not is_stock(code):
            continue
        streak, cum = 0, 0.0
        for _, rows in reversed(window):
            v = f(rows.get(code, {}).get(col))
            if v is None or v <= 0:
                break
            streak += 1
            cum += v
        if streak >= int(ir["min_days"]):
            m = metrics(code, hist, ind)
            if m and m["close"] is not None and (m["value"] or 0) >= ir["min_value"]:
                ranked.append((streak, cum, f(hist[-1][1][code].get(col)), m))
    ranked.sort(key=lambda x: (-x[0], -x[1]))
    top = ranked[:int(ir["top"])]
    out.append("連續買超 ≥ %d 日、成交值 ≥ %.1f 億的共 %d 檔，列出前 %d 檔（先比連買日數，再比累計張數）；目前有法人資料的交易日 %d 天。" % (
        ir["min_days"], ir["min_value"] / 1e8, len(ranked), len(top), days_with))
    for i, (streak, cum, today_v, m) in enumerate(top, 1):
        close = ("收 %s" % ("%.2f" % m["close"]).rstrip("0").rstrip(".")) + ("（%+.2f%%）" % m["pct"] if m["pct"] is not None else "")
        out.append("%d. %s %s｜連買 %d 日｜累計 %+d張｜今日 %+d張｜%s" % (i, m["code"], m["name"], streak, round(cum / 1000), round((today_v or 0) / 1000), close))
    if not top:
        out.append("（%s）" % ("法人資料累積天數還不到 %d 天，排名暫時是空的" % ir["min_days"] if days_with < int(ir["min_days"]) else "沒有符合的股票"))
    out.append("※ 上櫃股票的法人資料只從開始收集那天起累積，連買日數可能比實際短。")
    return out


# ---------- 組信 ----------
def build(d, cfg, pending=()):
    need = max(cfg["indicators"]["ma_days"], cfg["indicators"]["vol_short_days"], cfg["indicators"]["vol_long_days"],
               int(cfg["screen"]["inst_streak"]["days"]), int(cfg["inst_rank"]["lookback"]) if cfg["inst_rank"]["enabled"] else 0)
    hist = history(d, need)
    if not hist or hist[-1][0] != d.isoformat():
        raise RuntimeError("找不到 %s 的資料檔" % d)
    ind, al = cfg["indicators"], cfg["alerts"]
    mine = set(cfg["holdings"]) | set(cfg["watchlist"])
    tag_of = {c: "持股" for c in cfg["holdings"]}
    tag_of.update({c: "追蹤" for c in cfg["watchlist"] if c not in tag_of})

    def block(codes):
        rows, nflag = [], 0
        for c in codes:
            m = metrics(c, hist, ind)
            if m is None:
                rows.append("%s｜查無今日資料（代號有誤、停牌，或尚未上市櫃）" % c)
                continue
            fl = flags(m, al, ind)
            nflag += bool(fl)
            rows.append(line(m, fl))
        return rows, nflag

    h_rows, h_flag = block(cfg["holdings"])
    w_rows, w_flag = block(cfg["watchlist"])
    cands, stages, inst_note, short = screen(hist, cfg)
    top = cands[:int(cfg["screen"]["max_results"])]

    body = ["台股收盤掃描 %s" % d.isoformat()]
    if pending:
        body += ["", "⚠ 資料還沒齊（這是當天最後一次排程）。未確認的來源："]
        body += ["  - %s：%s" % (k, AFFECT.get(k, "")) for k in pending]
        body += ["  相關欄位可能是空白或前一天的值，請自行斟酌。"]
    if cfg.get("_missing"):
        body += ["", "⚠ 找不到 scan_config.json（要放在倉庫根目錄，檔名要完全一樣），這封信用的是內建預設值，持股與追蹤清單是空的。"]
    body += ["", "一、持股（%d 檔，%d 檔有標記）" % (len(cfg["holdings"]), h_flag)] + (h_rows or ["（scan_config.json 裡沒有設定）"])
    body += ["", "二、追蹤清單（%d 檔，%d 檔有標記）" % (len(cfg["watchlist"]), w_flag)] + (w_rows or ["（scan_config.json 裡沒有設定）"])
    body += ["", "三、全市場候選（符合全部條件 %d 檔，列出前 %d 檔）" % (len(cands), len(top))]
    for m in top:
        body.append(line(m, [tag_of[m["code"]]] if m["code"] in tag_of else [], with_value=True))
    if not top:
        body.append("（今天沒有股票同時符合全部條件）")
    body += ["", "各關剩下幾檔："]
    prev = None
    for name, n in stages:
        body.append("  %s：%d 檔%s" % (name, n, "" if prev is None else "（－%d）" % (prev - n)))
        prev = n
    if short:
        body.append("  （通過成交值這關但歷史不足 %d 日、算不出乖離率或量比的有 %d 檔，已排除）" % (max(ind["ma_days"], ind["vol_long_days"]), short))
    if inst_note:
        body.append("  ※ " + inst_note)
    if cfg["revenue"]["enabled"]:
        body += revenue_section(hist, cfg)
    if cfg["inst_rank"]["enabled"]:
        body += inst_rank_section(hist, cfg)
    body += ["", "說明：乖離率與均線用未還原的收盤價，期間遇到除權息會有些微誤差。外資、投信單位為張。",
             "以上只是依你設定的規則整理資料，不是買賣建議。規則在 scan_config.json。"]
    text = "\n".join(body)
    if len(text) > 7500:
        text = text[:7500] + "\n…（信件過長，已截斷）"
    subject = "【台股】%s 收盤掃描：候選 %d 檔／警示 %d 檔%s" % (d.isoformat(), len(cands), h_flag + w_flag, "（資料未齊）" if pending else "")
    return subject, text, top


def save_candidates(d, top):
    os.makedirs(SCAN_DIR, exist_ok=True)
    with open("%s/%s.csv" % (SCAN_DIR, d.isoformat()), "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["code", "name", "market", "close", "pe", "bias", "vol_ratio", "value"])
        for m in top:
            w.writerow([m["code"], m["name"], m["market"], m["close"], m["pe"], round(m["bias"], 2), round(m["vol_ratio"], 3), m["value"]])


def smeta_path(d):
    return "%s/scan-%s.json" % (collect.META, d.isoformat())


def run(d, force=False, last_run=False, dry=False):
    if not os.path.exists(collect.csv_path(d)):
        print("%s 沒有資料檔（休市或尚未收集），略過" % d)
        return
    meta = collect.load_meta(d)
    src = meta.get("sources", {})
    pending = [k for k in NEEDED if src.get(k) != "ok"]
    complete = bool(meta) and not pending
    try:
        sent = json.load(open(smeta_path(d), encoding="utf-8")).get("sent")
    except Exception:  # noqa: BLE001
        sent = None
    if not (force or dry):
        if complete and sent == "complete":
            print("%s 的掃描信已經寄過，略過" % d)
            return
        if not complete and not last_run:
            print("%s 資料還沒齊（%s），等下一輪" % (d, "、".join(pending) or "沒有狀態檔"))
            return
        if not complete and sent:
            print("%s 已經寄過未齊版本，略過" % d)
            return
    cfg = load_config()
    subject, body, top = build(d, cfg, [] if complete else pending)
    if dry:
        print(subject)
        print(body)
        return
    save_candidates(d, top)
    print(subject)
    if collect.send_mail(subject, body):
        os.makedirs(collect.META, exist_ok=True)
        json.dump({"sent": "complete" if complete else "partial", "at": dt.datetime.now(collect.TZ).isoformat()},
                  open(smeta_path(d), "w", encoding="utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--mail-test", action="store_true")
    args = ap.parse_args()
    if args.mail_test:        # 只測寄信設定：寄一封測試信，失敗時日誌會說明可能的原因
        ok = collect.send_mail("【台股】寄信測試", "收到這封信，表示 stock-scan 倉庫的 MAIL_URL、MAIL_TOKEN 設定正常。")
        sys.exit(0 if ok else 1)
    d = dt.date.fromisoformat(args.date) if args.date else collect.latest_weekday(dt.datetime.now(collect.TZ))
    force = args.force or os.environ.get("FORCE") == "1"
    try:
        run(d, force=force, last_run=os.environ.get("LAST_RUN") == "1", dry=args.dry)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        if not args.dry:
            collect.send_mail("【台股】%s 掃描程式出錯" % d, "掃描程式執行失敗，資料收集不受影響。\n錯誤：%s\n請到 Actions 看日誌，或把日誌貼給 Claude。" % str(e)[:300])
        sys.exit(1)


if __name__ == "__main__":
    main()
