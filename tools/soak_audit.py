#!/usr/bin/env python3
"""SN 抓拍系统 soak 压测稳定性审计（只读，SSH 采集，一次性退出）。

盯两类故障：
  1) 数据丢失——SE9 识别落盘但未入 webserver DB；
  2) 摄像头断联导致服务失败——进程重启、推流死、内存涨。

本机运行，SSH 到两端采数，不在生产机装任何东西、不留后台进程、不轮询。
soak 期间要周期采样时，由 harness 的 CronCreate 定时调用本脚本 `sample`，
进程生命周期交给 harness。

凭据走环境变量，不落盘：
  SE9_HOST / SE9_PASS   (默认 linaro@10.80.40.68 / linaro)
  WEB_HOST / WEB_PASS   (默认 martin-xuan@10.80.40.57 / 123456)

子命令：
  baseline  采两端起点快照 → notes/baseline-<ts>.json
  sample    轻量采一行(RSS/NRestarts/ffmpeg存活) 追加 samples-<run>.csv  (CronCreate 周期调)
  audit     对比 baseline↔当前：数据丢失对账 + 健康报告 + RSS 曲线摘要

用法示例：
  SE9_PASS=linaro WEB_PASS=123456 python3 -I tools/soak_audit.py baseline
  python3 -I tools/soak_audit.py sample  --baseline notes/.../baseline-XXXX.json
  python3 -I tools/soak_audit.py audit   --baseline notes/.../baseline-XXXX.json [--placed 500]
"""
import argparse
import datetime as dt
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

# ---- 固定路径（两端部署约定，来自仓库 README/service）----
SE9_RESULTS_DIR = "/data/soph_SN/sn_results"
SE9_LOG = "/data/soph_SN/logs/sn_monitor.service.log"
SE9_UNIT = "sn-monitor-big"              # 当前档位；small 档时改这里
WEB_DB = "/media/cvitek/xiaohao.liu/product_test/data/results.db"

# soak 期间识别进程频繁断联的中文日志关键字（英文 reconnect/wedge 在本工程计数恒为 0）
LOG_KEYWORDS = ["重连", "断", "退出", "重拉", "timeout", "TEARDOWN", "release", "ERROR", "Traceback"]

NOTES_DIR = Path(__file__).resolve().parent.parent / ".claude" / "notes" / "sn-stress-stability-monitor"


def _conn(host_env, pass_env, host_default):
    host = os.environ.get(host_env, host_default)
    passwd = os.environ.get(pass_env, "")
    return host, passwd


def ssh_run(host, passwd, remote_cmd, timeout=30):
    """SSH 执行一条远端命令，返回 stdout 文本。只读用途。凭据经 SSHPASS 环境变量传入 sshpass，不出现在命令行。"""
    opt = ["-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10"]
    argv = ["sshpass", "-e", "ssh", *opt, host, remote_cmd]
    env = dict(os.environ, SSHPASS=passwd)
    r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
    if r.returncode != 0:
        # 把 stderr 并进来便于诊断，但不抛异常中断整轮采集
        return r.stdout + ("\n[ssh-stderr] " + r.stderr if r.stderr.strip() else "")
    return r.stdout


# ---------------- SE9 侧采集 ----------------

def collect_se9(host, passwd):
    """采 SE9：文件计数 + 服务重启数 + 主进程 RSS + 整机内存 + 日志关键字计数 + 落盘明细。"""
    # 一个远端脚本一次性产出，减少 SSH 往返；明细用 python 产 json 行，计数用 shell。
    # ★ 远端 shell 的 echo 不解释 \t，统一用 printf 输出真 TAB 分隔，否则 _parse_tsv 全空。
    kw_cmd = "; ".join(
        f'printf "KW\\t{k}\\t%%s\\n" "$(grep -c -- {shlex.quote(k)} %s 2>/dev/null)"' % SE9_LOG
        for k in LOG_KEYWORDS
    )
    remote = r"""
cd %(dir)s 2>/dev/null || { echo "NODIR"; exit 0; }
printf "NOW\t%%s\n" "$(date -u +%%Y-%%m-%%dT%%H:%%M:%%S)"
printf "JSON_TOTAL\t%%s\n" "$(ls sn_*.json 2>/dev/null | wc -l)"
printf "JSON_BACK\t%%s\n" "$(ls sn_*_back.json 2>/dev/null | wc -l)"
printf "UPLOADED\t%%s\n" "$(ls *.uploaded 2>/dev/null | wc -l)"
printf "JPG\t%%s\n" "$(ls sn_*.jpg 2>/dev/null | wc -l)"
NR=$(echo %(pass)s | sudo -S systemctl show %(unit)s -p NRestarts 2>/dev/null | cut -d= -f2)
printf "NRESTARTS\t%%s\n" "$NR"
ACTENTER=$(echo %(pass)s | sudo -S systemctl show %(unit)s -p ActiveEnterTimestamp 2>/dev/null | cut -d= -f2-)
printf "ACTIVE_SINCE\t%%s\n" "$ACTENTER"
RSS=$(ps -eo rss,cmd | grep "sn_monitor.py" | grep -v grep | awk '{print $1; exit}')
printf "RSS_KB\t%%s\n" "${RSS:-0}"
printf "FFMPEG_FRONT\t%%s\n" "$(ps -eo cmd | grep -c '[c]am_front')"
printf "FFMPEG_BACK\t%%s\n" "$(ps -eo cmd | grep -c '[c]am_back')"
printf "UPLOADER\t%%s\n" "$(ps -eo cmd | grep -c '[s]n_uploader.py')"
printf "MEM_MB\t%%s\n" "$(free -m | awk '/Mem:/{print $2","$3","$7}')"
%(kw)s
# 落盘明细：每条 json 的 sn/ts/side/是否有.uploaded/.uploaded里的id，供对账
python3 - <<'PYEOF'
import glob,json,os
base="%(dir)s"
for jp in sorted(glob.glob(os.path.join(base,"sn_*.json"))):
    try:
        m=json.load(open(jp,encoding="utf-8"))
    except Exception:
        continue
    stem=jp[:-5]
    side=m.get("side") or ("back" if stem.endswith("_back") else "front")
    up=stem+".uploaded"
    up_id=None; has_up=os.path.exists(up)
    if has_up:
        try: up_id=json.load(open(up)).get("id")
        except Exception: pass
    print("ROW\t"+json.dumps({"sn":m.get("sn",""),"ts":m.get("ts",""),
        "side":side,"has_uploaded":has_up,"up_id":up_id,
        "file":os.path.basename(jp)},ensure_ascii=False))
PYEOF
""" % {"dir": SE9_RESULTS_DIR, "pass": shlex.quote(passwd), "unit": SE9_UNIT, "kw": kw_cmd}
    out = ssh_run(host, passwd, remote, timeout=60)
    return _parse_tsv(out)


def _parse_tsv(out):
    d = {"kw": {}, "rows": []}
    for line in out.splitlines():
        if "\t" not in line:
            continue
        parts = line.split("\t")
        tag = parts[0]
        if tag == "KW" and len(parts) >= 3:
            d["kw"][parts[1]] = _int(parts[2])
        elif tag == "ROW" and len(parts) >= 2:
            try:
                d["rows"].append(json.loads(parts[1]))
            except Exception:
                pass
        elif len(parts) >= 2:
            d[tag.lower()] = parts[1]
    return d


def _int(s):
    try:
        return int(str(s).strip())
    except Exception:
        return 0


# ---------------- webserver 侧采集 ----------------

def collect_web(host, passwd):
    """采 webserver：sn_captures 行数、最大 id、全部 (sn,captured_at,side) 用于对账。"""
    remote = r"""python3 - <<'PYEOF'
import sqlite3, json
try:
    c=sqlite3.connect("%(db)s")
    n=c.execute("select count(*) from sn_captures").fetchone()[0]
    mx=c.execute("select coalesce(max(id),0) from sn_captures").fetchone()[0]
    print("DB_COUNT\t%%d"%%n); print("DB_MAXID\t%%d"%%mx)
    for r in c.execute("select id,sn,captured_at,side from sn_captures"):
        print("DBROW\t"+json.dumps({"id":r[0],"sn":r[1],"captured_at":r[2],"side":r[3]},ensure_ascii=False))
except Exception as e:
    print("DBERR\t%%s"%%e)
PYEOF
""" % {"db": WEB_DB}
    out = ssh_run(host, passwd, remote, timeout=60)
    d = {"db_count": 0, "db_maxid": 0, "dbrows": [], "dberr": None}
    for line in out.splitlines():
        if "\t" not in line:
            continue
        tag, _, val = line.partition("\t")
        if tag == "DB_COUNT":
            d["db_count"] = _int(val)
        elif tag == "DB_MAXID":
            d["db_maxid"] = _int(val)
        elif tag == "DBROW":
            try:
                d["dbrows"].append(json.loads(val))
            except Exception:
                pass
        elif tag == "DBERR":
            d["dberr"] = val
    return d


# ---------------- 对账键归一 ----------------

def _norm_ts(ts):
    """SE9 的 'T' 分隔 与 DB 的空格分隔 统一成到秒的字符串，容忍微秒差异。"""
    if not ts:
        return ""
    s = str(ts).replace("T", " ").strip()
    return s.split(".")[0]  # 丢微秒，秒级对齐（同一事件两端同秒）


def _key(sn, ts, side):
    return "%s|%s|%s" % (sn, _norm_ts(ts), (side or "front"))


# ---------------- 子命令 ----------------

def cmd_baseline(args):
    se9_h, se9_p = _conn("SE9_HOST", "SE9_PASS", "linaro@10.80.40.68")
    web_h, web_p = _conn("WEB_HOST", "WEB_PASS", "martin-xuan@10.80.40.57")
    se9 = collect_se9(se9_h, se9_p)
    web = collect_web(web_h, web_p)
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    snap = {"captured_local": ts, "se9": se9, "web": web}
    path = NOTES_DIR / ("baseline-%s.json" % ts)
    path.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    print("[baseline] 已存 %s" % path)
    print("  SE9: json=%s(back=%s) uploaded=%s jpg=%s NRestarts=%s RSS=%sKB mem_mb=%s" % (
        se9.get("json_total"), se9.get("json_back"), se9.get("uploaded"),
        se9.get("jpg"), se9.get("nrestarts"), se9.get("rss_kb"), se9.get("mem_mb")))
    print("  WEB: db_count=%s maxid=%s%s" % (
        web.get("db_count"), web.get("db_maxid"),
        ("  DBERR=" + web["dberr"]) if web.get("dberr") else ""))
    print("  后续 sample/audit 用: --baseline %s" % path)
    return path


def cmd_sample(args):
    """轻量采样一行，追加 samples-<run>.csv（供 CronCreate 周期调用，画 RSS/重启曲线）。"""
    se9_h, se9_p = _conn("SE9_HOST", "SE9_PASS", "linaro@10.80.40.68")
    se9 = collect_se9(se9_h, se9_p)
    run = Path(args.baseline).stem.replace("baseline-", "") if args.baseline else "adhoc"
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    csv = NOTES_DIR / ("samples-%s.csv" % run)
    new = not csv.exists()
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with csv.open("a", encoding="utf-8") as f:
        if new:
            f.write("local_ts,rss_kb,nrestarts,ffmpeg_front,ffmpeg_back,uploader,db_dummy\n")
        f.write("%s,%s,%s,%s,%s,%s,\n" % (
            now, se9.get("rss_kb", 0), se9.get("nrestarts", 0),
            se9.get("ffmpeg_front", 0), se9.get("ffmpeg_back", 0), se9.get("uploader", 0)))
    print("[sample] %s  RSS=%sKB NRestarts=%s ffmpeg(front/back)=%s/%s → %s" % (
        now, se9.get("rss_kb"), se9.get("nrestarts"),
        se9.get("ffmpeg_front"), se9.get("ffmpeg_back"), csv))


def cmd_audit(args):
    base = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    se9_h, se9_p = _conn("SE9_HOST", "SE9_PASS", "linaro@10.80.40.68")
    web_h, web_p = _conn("WEB_HOST", "WEB_PASS", "martin-xuan@10.80.40.57")
    cur_se9 = collect_se9(se9_h, se9_p)
    cur_web = collect_web(web_h, web_p)

    b_se9, b_web = base["se9"], base["web"]
    print("=" * 64)
    print("SOAK 审计报告  baseline=%s  now=%s" % (base["captured_local"],
          dt.datetime.now().strftime("%Y%m%d_%H%M%S")))
    print("=" * 64)

    # ---- 1. 服务健康 / 摄像头断联 ----
    nr_b, nr_n = _int(b_se9.get("nrestarts")), _int(cur_se9.get("nrestarts"))
    print("\n[1] 服务健康 / 摄像头断联")
    print("  NRestarts: %d → %d  (窗口内新增 %d)%s" % (
        nr_b, nr_n, nr_n - nr_b,
        "  ⚠ 识别进程在压测期间重启!" if nr_n > nr_b else "  ✅ 未重启"))
    print("  ffmpeg front/back 现状: %s/%s%s" % (
        cur_se9.get("ffmpeg_front"), cur_se9.get("ffmpeg_back"),
        "" if (_int(cur_se9.get("ffmpeg_front")) and _int(cur_se9.get("ffmpeg_back")))
        else "  ⚠ 有推流路已死"))
    print("  uploader 现状: %s%s" % (cur_se9.get("uploader"),
          "" if _int(cur_se9.get("uploader")) else "  ⚠ uploader 已死"))
    print("  日志关键字窗口内增量:")
    for k in LOG_KEYWORDS:
        db_, dn_ = _int(b_se9["kw"].get(k)), _int(cur_se9["kw"].get(k))
        if dn_ - db_ != 0:
            print("    %-10s %d → %d  (+%d)" % (k, db_, dn_, dn_ - db_))

    # ---- 2. 内存 ----
    rss_b, rss_n = _int(b_se9.get("rss_kb")), _int(cur_se9.get("rss_kb"))
    print("\n[2] 内存 (P-MEM)")
    print("  主识别进程 RSS: %.1fMB → %.1fMB  (Δ %+.1fMB)%s" % (
        rss_b / 1024, rss_n / 1024, (rss_n - rss_b) / 1024,
        "  ⚠ 内存显著增长，疑似泄漏" if (rss_n - rss_b) > 200 * 1024 else ""))
    _print_rss_curve(args.baseline)

    # ---- 3. 数据丢失对账 ----
    print("\n[3] 数据丢失对账 (窗口内新增)")
    b_keys = {_key(r["sn"], r["ts"], r.get("side")) for r in b_se9.get("rows", [])}
    cur_rows = cur_se9.get("rows", [])
    new_rows = [r for r in cur_rows if _key(r["sn"], r["ts"], r.get("side")) not in b_keys]
    db_keys = {_key(r["sn"], r["captured_at"], r.get("side")) for r in cur_web.get("dbrows", [])}

    recognized = len(new_rows)
    uploaded_marked = sum(1 for r in new_rows if r.get("has_uploaded"))
    # 真丢失：SE9 有识别(json)，但按 sn+ts+side 在 DB 查不到
    lost = [r for r in new_rows if _key(r["sn"], r["ts"], r.get("side")) not in db_keys]
    # 标记异常：有 .uploaded(应已入库) 但 DB 查不到 —— 更可疑
    lost_despite_mark = [r for r in lost if r.get("has_uploaded")]

    print("  SE9 窗口内新增识别落盘(json): %d  (其中已写 .uploaded: %d)" % (recognized, uploaded_marked))
    print("  webserver DB 行数: %d → %d  (窗口内新增 %d)" % (
        _int(b_web.get("db_count")), _int(cur_web.get("db_count")),
        _int(cur_web.get("db_count")) - _int(b_web.get("db_count"))))
    print("  → 落盘但未入 DB(真丢失): %d%s" % (
        len(lost), "  ✅ 无丢失" if not lost else "  ⚠ 有丢失"))
    if lost_despite_mark:
        print("  → 其中 %d 条已写 .uploaded 标记却 DB 查无 —— 高度可疑(上传声称成功但未落库):" % len(lost_despite_mark))
    for r in lost[:30]:
        flag = " [有.uploaded id=%s]" % r.get("up_id") if r.get("has_uploaded") else ""
        print("      丢失: sn=%s ts=%s side=%s%s" % (r["sn"], r["ts"], r.get("side"), flag))
    if len(lost) > 30:
        print("      ...还有 %d 条，完整见 notes" % (len(lost) - 30))

    # ---- 4. 漏抓（需人工投放计数）----
    if args.placed is not None:
        print("\n[4] 漏抓 (人工投放计数)")
        # 投放数通常按"块板"算，一块板=一次正面识别；这里以正面 json 计
        front_recognized = sum(1 for r in new_rows if (r.get("side") or "front") == "front")
        miss = args.placed - front_recognized
        print("  投放板数(人工输入): %d" % args.placed)
        print("  窗口内正面识别落盘: %d" % front_recognized)
        print("  → 漏抓: %d  (漏抓率 %.1f%%)" % (miss, 100.0 * miss / args.placed if args.placed else 0))
    else:
        print("\n[4] 漏抓: 未提供 --placed(投放板数)，跳过。系统无法自动知投放数。")

    # 存完整报告
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    rep = NOTES_DIR / ("audit-%s.json" % dt.datetime.now().strftime("%Y%m%d_%H%M%S"))
    rep.write_text(json.dumps({
        "baseline": args.baseline, "nrestarts": [nr_b, nr_n],
        "rss_kb": [rss_b, rss_n], "recognized_new": recognized,
        "uploaded_marked": uploaded_marked, "db_new": _int(cur_web.get("db_count")) - _int(b_web.get("db_count")),
        "lost": lost, "lost_despite_mark": lost_despite_mark,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n完整报告: %s" % rep)


def _print_rss_curve(baseline_path):
    run = Path(baseline_path).stem.replace("baseline-", "")
    csv = NOTES_DIR / ("samples-%s.csv" % run)
    if not csv.exists():
        print("  (无周期采样数据；soak 期间用 CronCreate 调 `sample` 才有 RSS 曲线)")
        return
    lines = [l for l in csv.read_text(encoding="utf-8").splitlines()[1:] if l.strip()]
    if not lines:
        return
    print("  RSS 曲线 (%d 个采样点):" % len(lines))
    vals = []
    for l in lines:
        c = l.split(",")
        if len(c) >= 2:
            vals.append((c[0], _int(c[1])))
    if not vals:
        return
    lo = min(v for _, v in vals)
    hi = max(v for _, v in vals) or 1
    for ts, v in vals[-12:]:  # 只打印末 12 点，避免刷屏
        bar = "█" * int(40 * (v - lo) / (hi - lo + 1))
        print("    %s %6.0fMB %s" % (ts[-8:], v / 1024, bar))
    slope = (vals[-1][1] - vals[0][1]) / 1024
    print("    趋势: 首%.0fMB → 末%.0fMB  (Δ%+.0fMB)%s" % (
        vals[0][1] / 1024, vals[-1][1] / 1024, slope,
        "  ⚠ 持续上涨疑泄漏" if slope > 200 else ""))


def main():
    ap = argparse.ArgumentParser(description="SN soak 稳定性审计（只读）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("baseline", help="采两端起点快照")
    sp = sub.add_parser("sample", help="轻量采样一行(CronCreate 周期调)")
    sp.add_argument("--baseline", help="baseline json 路径(决定 samples 文件名)")
    sa = sub.add_parser("audit", help="对比 baseline↔当前，出报告")
    sa.add_argument("--baseline", required=True, help="baseline json 路径")
    sa.add_argument("--placed", type=int, default=None, help="人工投放板数(算漏抓)")
    args = ap.parse_args()

    if args.cmd == "baseline":
        cmd_baseline(args)
    elif args.cmd == "sample":
        cmd_sample(args)
    elif args.cmd == "audit":
        cmd_audit(args)


if __name__ == "__main__":
    main()
