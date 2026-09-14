#!/usr/bin/env python3
"""profile_probe.py —— 裁剪/切块参数扫描器(新模组快速定档工具)。

对一批「图片 + 期望SN」,笛卡尔积扫 crop_w × crop_h × crop_cy × tile_grid × tile_up × roi_path
各组合,报每组的 耗时 / SN是否命中(带score) / 候选数;末尾给「命中且最快」的推荐组合,
可直接抄进 sn_monitor 的 PROFILES 定新档。

为什么要它:每上一款新模组/新板型,SN 铭牌位置与占比都变,靠手改 PROFILES 盲试很慢。
把 v18 各向异性裁剪(crop_center)+ 切块(split_grid)的参数空间一次性扫出来,人看表选组合即可。

只读:仅调 OCR 推理,不落库、不上传、不碰任何在跑的服务。需在板子上跑(要 sail 加载 OCR bmodel)。

用法见 README「4.7 参数扫描工具」。
"""
import sys, os, time, glob, argparse, itertools, re

# 板子部署路径(与各 spike 脚本一致);import sn_monitor 前必须先进 PP-OCR 目录, 否则 bmodel 相对路径找不到
BOARD_DIR = "/data/soph_SN"
PP_OCR_DIR = "/data/soph_SN/sophon-demo/sample/PP-OCR/python"


def parse_jobs(a):
    """产出 [(img_path, want_sn), ...]。三种来源优先级: --jobs > --jobs-file > 默认扫 results-dir。
    默认来源从命中帧文件名 sn_<SN>_<ts>.jpg 反解期望 SN, 免得每次手敲。"""
    jobs = []
    if a.jobs:
        for item in re.split(r"[,\s]+", a.jobs.strip()):
            if not item:
                continue
            path, _, sn = item.rpartition(":")   # rpartition 兼容路径里含 ':' 的极端情况
            jobs.append((path, sn.upper()))
        return jobs
    if a.jobs_file:
        for line in open(a.jobs_file, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = re.split(r"[,:\s]+", line)
            if len(parts) >= 2:
                jobs.append((parts[0], parts[1].upper()))
        return jobs
    # 默认: results-dir 下 sn_*.jpg, SN = 文件名中间段
    for jpg in sorted(glob.glob(os.path.join(a.results_dir, "sn_*.jpg"))):
        m = re.match(r"sn_(.+)_\d{8}_\d{6}", os.path.basename(jpg))
        if m:
            jobs.append((jpg, m.group(1).upper()))
    return jobs


def parse_grid(a):
    """把各 CLI 列表解析成扫描维度。返回 (crop_w[], crop_h[], crop_cy[], tiles[], ups[], rois[])。"""
    fl = lambda s: [float(x) for x in re.split(r"[,\s]+", s.strip()) if x]
    tiles = []
    for tok in re.split(r"[,\s]+", a.tile.strip()):
        if not tok:
            continue
        gx, _, gy = tok.lower().partition("x")
        tiles.append((int(gx), int(gy)))
    rois = []
    for tok in re.split(r"[,\s]+", a.roi.strip().lower()):
        if tok in ("on", "1", "true", "yes"):
            rois.append(True)
        elif tok in ("off", "0", "false", "no"):
            rois.append(False)
    return fl(a.crop_w), fl(a.crop_h), fl(a.crop_cy), tiles, fl(a.up), rois


def hit(texts, want):
    """命中判定: 期望 SN 作为子串出现在任一候选(去空格、大写)里, 命中返回其 score。"""
    for t, sc in texts:
        if want and want in t.upper().replace(" ", ""):
            return round(float(sc), 3)
    return None


def combo_label(cw, ch, cy, tg, up, roi):
    return f"cw{cw:.2f} ch{ch:.2f} cy{cy:.2f} {tg[0]}x{tg[1]}@{up:.1f}x roi{'+' if roi else '-'}"


def crop_name(base, cw, ch, cy):
    """裁剪图文件名: 以 宽/高/锚点 组合命名, 供人眼核对每种裁剪到底框住了哪块。"""
    return f"{base}_cw{cw:.2f}_ch{ch:.2f}_cy{cy:.2f}.jpg"


def main():
    ap = argparse.ArgumentParser(description="裁剪/切块参数扫描器(新模组快速定档)")
    ap.add_argument("--jobs", default="", help='"图片:期望SN" 列表, 逗号分隔; 例 /tmp/a.jpg:BCXX,/tmp/b.jpg:BCYY')
    ap.add_argument("--jobs-file", dest="jobs_file", default="", help="每行 '路径 期望SN' 的 txt")
    ap.add_argument("--results-dir", dest="results_dir", default=os.path.join(BOARD_DIR, "sn_results"),
                    help="默认来源目录(从 sn_<SN>_<ts>.jpg 反解期望SN)")
    ap.add_argument("--out-dir", dest="out_dir", default="./profile_probe_out",
                    help="裁剪图输出目录(文件名用 宽/高/锚点 组合)")
    # 扫描维度(逗号分隔多值, 笛卡尔积展开)
    ap.add_argument("--crop-w", dest="crop_w", default="0.3,0.4,1.0", help="宽裁剪比列表")
    ap.add_argument("--crop-h", dest="crop_h", default="0.35,0.4,1.0", help="高裁剪比列表")
    ap.add_argument("--crop-cy", dest="crop_cy", default="0.32,0.5", help="垂直锚点列表(<0.5偏上)")
    ap.add_argument("--tile", default="1x1,2x2", help="切块网格列表, 例 1x1,2x2,3x3")
    ap.add_argument("--up", default="2.0,2.5,3.0", help="上采样倍数列表")
    ap.add_argument("--roi", default="off", help="白底/Sobel ROI 路: on/off(可 on,off 都扫)")
    ap.add_argument("--rots", default="0", help="旋转角(传给每次 ocr), 例 0 或 0,180")
    ap.add_argument("--repeat", type=int, default=2, help="每组合计时重复取最快")
    ap.add_argument("--min-score", dest="min_score", type=float, default=0.0, help="推荐时命中分下限")
    a = ap.parse_args()

    jobs = parse_jobs(a)
    if not jobs:
        print("没有可测图片。用 --jobs / --jobs-file 指定, 或确认 --results-dir 下有 sn_*.jpg。")
        return
    cws, chs, cys, tiles, ups, rois = parse_grid(a)
    rots = tuple(int(x) for x in re.split(r"[,\s]+", a.rots.strip()) if x)
    os.makedirs(a.out_dir, exist_ok=True)

    # 懒加载 OCR(照 spike 范式: 先进 PP-OCR 目录再 import sn_monitor)
    sys.path.insert(0, BOARD_DIR)
    os.chdir(PP_OCR_DIR)
    import cv2
    import sn_monitor as M
    eng = M.OCR(); eng.load()

    combos = list(itertools.product(cws, chs, cys, tiles, ups, rois))
    print(f"[probe] {len(jobs)} 图 × {len(combos)} 组合; 裁剪图→ {os.path.abspath(a.out_dir)}", flush=True)

    # per-combo 命中计数(跨所有图), 用于末尾推荐
    tally = {}   # combo_key -> {"hits":n, "time":累计最快耗时}
    for path, want in jobs:
        img = cv2.imread(path)
        if img is None:
            print(f"skip(读不到) {path}", flush=True); continue
        base = os.path.splitext(os.path.basename(path))[0]
        print(f"\n#### {os.path.basename(path)} want={want} 尺寸{img.shape[1]}x{img.shape[0]} ####", flush=True)
        saved_crops = set()   # 同一 (cw,ch,cy) 只存一张裁剪图
        for (cw, ch, cy, tg, up, roi) in combos:
            key = (cw, ch, cy)
            if key not in saved_crops:
                saved_crops.add(key)
                cv2.imwrite(os.path.join(a.out_dir, crop_name(base, cw, ch, cy)),
                            M.crop_center(img, cw, ch, cy))
            # 计时: 重复取最快, 抹掉抖动
            best = 9e9; tx = None
            for _ in range(max(1, a.repeat)):
                t0 = time.time()
                tx = eng.ocr(img, None, rots=rots, do_tiles=True,
                             crop_w=cw, crop_h=ch, crop_cy=cy, tile_grid=tg, tile_up=up, roi_path=roi)
                best = min(best, time.time() - t0)
            sc = hit(tx, want)
            lab = combo_label(cw, ch, cy, tg, up, roi)
            flag = f"✅{sc}" if sc is not None else "❌   "
            print(f"  {lab:34s} | {best:.2f}s | SN={flag} | 候选 {len(tx)}", flush=True)
            ck = (cw, ch, cy, tg, up, roi)
            rec = tally.setdefault(ck, {"hits": 0, "time": 0.0})
            if sc is not None and sc >= a.min_score:
                rec["hits"] += 1
            rec["time"] += best

    # 推荐: 命中图数最多 → 累计耗时最小
    if tally:
        best_ck = max(tally, key=lambda k: (tally[k]["hits"], -tally[k]["time"]))
        cw, ch, cy, tg, up, roi = best_ck
        r = tally[best_ck]
        print(f"\n>> 推荐(命中 {r['hits']}/{len(jobs)} 图, 累计 {r['time']:.2f}s): "
              f"{combo_label(cw, ch, cy, tg, up, roi)}", flush=True)
        print(f"   PROFILES 片段: \"crop_w\":{cw}, \"crop_h\":{ch}, \"crop_cy\":{cy}, "
              f"\"tile_grid\":({tg[0]},{tg[1]}), \"tile_up\":{up}, \"roi_path\":{roi}", flush=True)


if __name__ == "__main__":
    main()
