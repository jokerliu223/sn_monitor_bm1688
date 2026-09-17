#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sn_monitor 的预览扩展 (调机预览台 V3.0)
==========================================
把"实时看画面"和"SN 识别"合到**同一个进程**里，相机源只被占用一次。

为什么单独一个文件，而不是写进 sn_monitor.py：
    sn_monitor.py 是已验收的稳定件(43KB)。本模块是**纯加法**，sn_monitor 侧只 import
    三行 + 调一个 attach()。这样预览出问题时，删掉这三行就等于回到 V2，零回归风险。

与识别的边界（这是本模块最重要的设计约束）：
    识别是本职，预览是锦上添花。因此——
      * 取帧只用 peek()（非阻塞），**绝不 wait**，绝不持有 FrameReader 的锁去等帧；
      * 拿不到帧就跳过，不补帧、不重试、不 backpressure 回主循环；
      * 预览线程内部任何异常一律吞掉，只影响自己；
      * 预览帧率是**上限**，不是保证（板子忙时自然掉帧）。
    sn_monitor 的识别主循环不知道本模块存在，其墙钟时间不受影响。

编码路径（2026-09-16 实测确认，见 docs/plans/2026-09-16-camera-preview-console-v3.md §2.1）：
    cv2 解出的 BGR ndarray
        → bmcv.mat_to_bm_image(frame)     # 实例方法、单参！传 handle 会 TypeError
        → bmcv.resize(bm, w, h)           # 仅在非 4K 档位
        → bmcv.imencode(".jpg", bm)       # 只吃 BMImage，喂 ndarray 会 TypeError
"""

import os
import re
import json
import time
import threading

# Flask 只在真正要起预览时才需要。做成懒导入 -> 本模块的纯逻辑(抓拍图挑选/档位表)
# 在没有 Flask 的环境里也能导入和单测, 且缺 Flask 时只报"预览不可用"而不是整个模块炸掉。
try:
    from flask import Flask, Response, jsonify, request
    _FLASK_OK = True
except Exception as _e:                        # noqa: BLE001
    Flask = Response = jsonify = request = None
    _FLASK_OK = False
    _FLASK_ERR = f"{type(_e).__name__}: {_e}"

try:
    import sophon.sail as sail
    _SAIL_OK = True
    _SAIL_ERR = ""
except Exception as _e:                        # noqa: BLE001
    sail = None
    _SAIL_OK = False
    _SAIL_ERR = f"{type(_e).__name__}: {_e}"

# ---- RTSP 强制 TCP（铁律 1）----
# 注意：本模块在 sn_monitor 之后 import，而 sn_monitor 走的是 cv2/FFMPEG 后端，
# 这些 SAIL_ 变量对本模块的编码路径无影响；保留是为了万一将来在此处直接拉流。
os.environ.setdefault("SAIL_DECODER_rtsp_transport", "tcp")

# 分辨率档位：网页下拉框的值 → (宽, 高)。(0,0) = 原始尺寸（4K 全幅，不缩放）。
RESOLUTIONS = {
    "4k":    (0, 0),
    "1080p": (1920, 1080),
    "720p":  (1280, 720),
    "360p":  (640, 360),
}

# MJPEG 分隔符。前端 camera.ts / camera.py 按这个名字解析，改名要同步改。
BOUNDARY = "snframe"

_PREVIEW_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "Pragma": "no-cache",
    # 反向代理/中间层不许攒块，否则 MJPEG 延迟飙升
    "X-Accel-Buffering": "no",
}


# ---------------------------------------------------------------------------
# 编码线程
# ---------------------------------------------------------------------------
class PreviewEncoder:
    """一个相机一路：慢节奏从 FrameReader 取最新帧，硬件编码成 JPEG，放进 latest-slot。

    latest-slot 语义（沿用 V1 preview_service 的设计）：
        生产者和消费者都不等对方。慢客户端永远拿最新的，绝不拖慢采集。
    """

    # 消费队列上限。4K 单帧 JPEG 约 1.3MB(真实画面)，20 帧≈26MB，够浏览器 1~1.5s 抖动。
    # 有上限是为了：① 客户端卡死时内存不无限涨 ② 掉帧而不是攒延迟。
    QUEUE_MAX = 20

    def __init__(self, reader, name, fps=8.0, res="4k", results_dir=None, jpeg_quality=80):
        self.reader = reader
        self.name = name
        self.results_dir = results_dir
        self.jpeg_quality = jpeg_quality      # 注：Bmcv 实测不可调，仅记录备查
        self.running = False

        self._fps = float(fps)
        self._res = res if res in RESOLUTIONS else "4k"
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)

        self._seq = 0
        self._jpg = None                      # 最新 JPEG bytes
        self._w = 0
        self._h = 0
        self._ts = 0.0                        # 编码完成时刻
        self._err = ""
        self._enc_n = 0                       # 累计编码成功帧数
        self._enc_ms = 0.0                    # 最近一次编码耗时(累计平均用)
        self._skipped = 0                     # 因节流/无帧跳过的轮次

        self._thread = None

    # ---- 生命周期 ----
    def start(self):
        if not _SAIL_OK:
            self._err = f"sail 不可用: {_SAIL_ERR}"
            return
        self.running = True
        self._thread = threading.Thread(target=self._loop, name=f"preview-{self.name}", daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False
        with self._cond:
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    # ---- 档位（网页下拉框调用）----
    def set_resolution(self, res):
        if res not in RESOLUTIONS:
            return False
        with self._lock:
            self._res = res
        return True

    def get_resolution(self):
        with self._lock:
            return self._res

    # ---- 消费端接口 ----
    def wait_newer(self, seq, timeout=5.0):
        """等一帧比 seq 新的。超时返回当前帧（可能是同一份，调用方自己比 seq）。"""
        with self._cond:
            self._cond.wait_for(lambda: self._seq > seq or not self.running, timeout)
            return self._jpg, self._seq, self._ts

    def latest(self):
        with self._lock:
            return self._jpg, self._seq, self._ts

    def status(self):
        with self._lock:
            age = (time.time() - self._ts) if self._ts else None
            return {
                "name": self.name,
                "resolution": self._res,
                "size": RESOLUTIONS.get(self._res, (0, 0)),
                # reader.frame is None 表示这一路还没解出任何帧（相机没起来/断流）
                "source_ok": getattr(self.reader, "frame", None) is not None,
                "frames": self._enc_n,
                "seq": self._seq,
                "age": age,
                "fps_cap": self._fps,
                "avg_ms": round(self._enc_ms / self._enc_n, 1) if self._enc_n else None,
                "skipped": self._skipped,
                "err": self._err,
            }

    # ---- 生产循环 ----
    def _loop(self):
        """预览生产线程。

        三条铁律（违反任一即可能拖累识别，见模块 docstring）：
          1. 取帧用 peek() 非阻塞，拿不到就 sleep 走人 —— 不抢 FrameReader 的锁
          2. 整个循环体 try/except 裹住，异常只记不抛
          3. 按 _fps 节流，不追满解码帧率
        """
        try:
            # handle/bmcv 建在**线程内部**：每个编码线程各持一份，无跨线程共享。
            # 实测并发无串行化（见方案 §2.2）。
            handle = sail.Handle(0)
            bmcv = sail.Bmcv(handle)
        except Exception as e:                # noqa: BLE001
            self._err = f"Bmcv 初始化失败: {type(e).__name__}: {e}"
            return

        period = 1.0 / self._fps if self._fps > 0 else 0.125
        next_t = time.time()

        while self.running:
            try:
                now = time.time()
                if now < next_t:
                    # 睡到下一帧时刻，但用 Event.wait 以便 stop() 能立刻唤醒
                    time.sleep(min(0.05, next_t - now))
                    continue
                next_t = now + period

                # ★ 铁律 1：非阻塞取帧
                frame, age, _fail = _peek(self.reader)
                if frame is None:
                    self._skipped += 1
                    time.sleep(0.1)
                    next_t = time.time()      # 没帧就不补，下一轮重新起算
                    continue

                with self._lock:
                    res = self._res
                t0 = time.time()
                jpg = self._encode(bmcv, frame, res)
                dt = (time.time() - t0) * 1000.0

                if jpg is None:
                    self._skipped += 1
                    continue

                with self._cond:
                    self._jpg = jpg
                    self._seq += 1
                    self._ts = time.time()
                    self._enc_n += 1
                    self._enc_ms += dt
                    self._w, self._h = len(jpg), 0
                    self._cond.notify_all()
                self._err = ""
            except Exception as e:            # noqa: BLE001  ★ 铁律 2
                self._err = f"{type(e).__name__}: {e}"
                time.sleep(0.2)

    def _encode(self, bmcv, frame, res):
        """BGR ndarray → JPEG bytes。失败返回 None（不抛）。"""
        try:
            bm = bmcv.mat_to_bm_image(frame)
            w, h = RESOLUTIONS.get(res, (0, 0))
            if w and h and (frame.shape[1] != w or frame.shape[0] != h):
                bm = bmcv.resize(bm, w, h)
            arr = bmcv.imencode(".jpg", bm)
            return arr.tobytes()
        except Exception as e:                # noqa: BLE001
            self._err = f"编码失败: {type(e).__name__}: {e}"
            return None


def _peek(reader):
    """非阻塞取最新帧。FrameReader 已把 read/release 全交给自己的线程，
    这里只读 self.frame 引用，不需要也不应该去等。"""
    with reader.lock:
        f = reader.frame
        return (f.copy() if f is not None else None,
                (time.time() - reader.ts) if reader.ts else 0.0,
                reader.fail)


# ---------------------------------------------------------------------------
# 最新抓拍图（只读，不入库）—— 回答"显示会不会进数据库"
# ---------------------------------------------------------------------------
_CAP_RE = re.compile(r"^sn_([A-Z0-9]+)_(\d{8}_\d{6})\.jpg$")


def find_latest_capture(results_dir):
    """找 sn_results/ 里最新的**正面**命中图。

    只认已配对的：必须有同名 .json（说明这次抓拍已经写完，不是正在写的半张图）。
    _back.jpg 不算 —— 回显要的是正面那张带 SN 的图。

    返回 dict 或 None。★ 本函数只读，绝不写库、绝不改文件。
    """
    if not results_dir or not os.path.isdir(results_dir):
        return None
    best = None
    try:
        names = os.listdir(results_dir)
    except OSError:
        return None
    for fn in names:
        m = _CAP_RE.match(fn)
        if not m:
            continue
        jpg = os.path.join(results_dir, fn)
        meta = jpg[:-4] + ".json"
        if not os.path.isfile(meta):
            continue                       # 半张图/无配对，跳过
        try:
            mtime = os.path.getmtime(jpg)
        except OSError:
            continue
        if best is None or mtime > best["mtime"]:
            best = {"mtime": mtime, "path": jpg, "meta": meta,
                    "sn": m.group(1), "ts": m.group(2), "name": fn}
    if best is None:
        return None
    # 读配对 json 补充 score/side（读失败不影响回显，图才是主角）
    info = {"sn": best["sn"], "ts": best["ts"], "name": best["name"],
            "mtime": best["mtime"], "score": None, "side": "front"}
    try:
        with open(best["meta"], "r") as f:
            meta = json.load(f)
        info["score"] = meta.get("score")
        info["side"] = meta.get("side", "front")
    except Exception:                     # noqa: BLE001
        pass
    info["path"] = best["path"]
    return info


# ---------------------------------------------------------------------------
# Flask 应用
# ---------------------------------------------------------------------------
def build_app(encoders, stop_flag=None, preview_port=8090, fps_cap=0.0):
    """encoders: {"front": PreviewEncoder, "back": PreviewEncoder}

    所有路由内部全部 try/except：Flask 线程崩溃在 V3.0 里会带走识别进程，
    这是不能接受的（R-304）。
    """
    app = Flask("sn_preview")
    # 关掉 werkzeug 的每请求日志，否则 8fps 的 MJPEG 会把日志刷爆
    import logging
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    def _get(cam):
        return encoders.get(cam)

    @app.route("/stream")
    def stream():
        cam = request.args.get("cam", "front")
        enc = _get(cam)
        if enc is None:
            return jsonify({"error": f"unknown cam: {cam}", "valid": list(encoders)}), 400
        res = request.args.get("res")
        if res and not enc.set_resolution(res):
            return jsonify({"error": f"bad res: {res}", "valid": list(RESOLUTIONS)}), 400

        def gen():
            """MJPEG over multipart/x-mixed-replace。

            每帧带 Content-Length —— 部分客户端（尤其 Safari）没有它会卡住。
            超时拿不到新帧就什么也不发，等下一轮；客户端断开时 Flask 会
            停止迭代本生成器，不会留下悬挂线程。
            """
            last_seq = -1
            while enc.running:
                jpg, seq, _ts = enc.wait_newer(last_seq, timeout=5.0)
                if jpg is None or seq == last_seq:
                    continue
                last_seq = seq
                yield (b"--" + BOUNDARY.encode() + b"\r\n"
                       b"Content-Type: image/jpeg\r\n"
                       b"Content-Length: " + str(len(jpg)).encode() + b"\r\n\r\n"
                       + jpg + b"\r\n")

        resp = Response(gen(), mimetype=f"multipart/x-mixed-replace; boundary={BOUNDARY}")
        resp.headers.update(_PREVIEW_HEADERS)
        return resp

    @app.route("/snapshot")
    def snapshot():
        """单张。给 MJPEG 放不出来的浏览器兜底。"""
        cam = request.args.get("cam", "front")
        enc = _get(cam)
        if enc is None:
            return jsonify({"error": f"unknown cam: {cam}"}), 400
        res = request.args.get("res")
        if res:
            enc.set_resolution(res)
        jpg, seq, _ts = enc.latest()
        if jpg is None:
            return jsonify({"error": "no frame yet", "err": enc.status()["err"]}), 503
        resp = Response(jpg, mimetype="image/jpeg")
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Frame-Seq"] = str(seq)
        return resp

    @app.route("/status")
    def status():
        return jsonify({
            "ok": True,
            "port": preview_port,
            "fps_cap": fps_cap,
            "cameras": [encoders[k].status() for k in encoders],
        })

    @app.route("/resolution")
    def set_resolution():
        """?res=4k|1080p|720p|360p[&cam=front|back]

        ★ 只改预览副本的缩放档位。识别走的是 FrameReader 的全幅帧，与本接口无关。
        """
        res = request.args.get("res", "")
        cam = request.args.get("cam")
        targets = [encoders[cam]] if cam in encoders else list(encoders.values())
        for enc in targets:
            if not enc.set_resolution(res):
                return jsonify({"error": f"bad res: {res}", "valid": list(RESOLUTIONS)}), 400
        return jsonify({"ok": True, "res": res, "cameras": [e.name for e in targets]})

    @app.route("/latest_capture")
    def latest_capture():
        """调机页回显用：返回**板子上刚抓到的**那张图 + SN。

        ★ 只读。不写 sn_capture 表、不动 sn_results/、不影响 uploader 的上传链路。
        产测页看到的仍是 uploader 正式上传后的记录（约 5s 延迟），两者互不干扰。
        """
        rdir = None
        for enc in encoders.values():
            if enc.results_dir:
                rdir = enc.results_dir
                break
        info = find_latest_capture(rdir)
        if info is None:
            return jsonify({"ok": True, "has": False, "reason": "板上暂无已配对的抓拍图"})
        return jsonify({
            "ok": True,
            "has": True,
            "sn": info["sn"],
            "ts": info["ts"],
            "score": info["score"],
            "side": info["side"],
            "mtime": info["mtime"],
            "image": f"/latest_capture/image",
        })

    @app.route("/latest_capture/image")
    def latest_capture_image():
        """回显图本体。每次现查最新，所以抓到新 SN 后立刻就能看到。"""
        rdir = None
        for enc in encoders.values():
            if enc.results_dir:
                rdir = enc.results_dir
                break
        info = find_latest_capture(rdir)
        if info is None:
            return jsonify({"error": "no capture yet"}), 404
        try:
            with open(info["path"], "rb") as f:
                data = f.read()
        except OSError as e:
            return jsonify({"error": f"read failed: {e}"}), 500
        resp = Response(data, mimetype="image/jpeg")
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-SN"] = info["sn"]
        return resp

    @app.route("/health")
    def health():
        return jsonify({"status": "ok"})

    return app


def attach(reader, reader2, results_dir, port=8090, fps=8.0,
           res="4k", preview=(True, True), jpeg_quality=80):
    """在 sn_monitor 里调用这一行即可挂上预览。

    preview: (开正面, 开背面)。reader2 为 None 时背面自动跳过。
    返回 (threads, stop_fn) —— sn_monitor 在 finally 里调 stop_fn() 干净收尾。
    任何异常都在这里吞掉并返回 (None, noop)：预览挂了绝不能影响识别。
    """
    try:
        if not _SAIL_OK:
            print(f"  ⚠ 预览不可用(sail 导入失败: {_SAIL_ERR}), 识别继续")
            return None, (lambda: None)
        if not _FLASK_OK:
            print(f"  ⚠ 预览不可用(flask 导入失败: {_FLASK_ERR}), 识别继续")
            return None, (lambda: None)

        encoders = {}
        if reader is not None and preview[0]:
            encoders["front"] = PreviewEncoder(reader, "front", fps, res, results_dir, jpeg_quality)
        if reader2 is not None and preview[1]:
            # 第二路(背面)只拉流不识别，预览挂同一个 FrameReader，不额外拉流
            encoders["back"] = PreviewEncoder(reader2, "back", fps, res, results_dir, jpeg_quality)

        if not encoders:
            print("  ○ 预览未启用")
            return None, (lambda: None)

        for enc in encoders.values():
            enc.start()

        app = build_app(encoders, None, port, fps)

        def _serve():
            # threaded=True：两路 MJPEG + status/latest_capture 并发，不能串行
            try:
                app.run(host="0.0.0.0", port=port, threaded=True,
                        debug=False, use_reloader=False)
            except Exception as e:            # noqa: BLE001
                print(f"  ⚠ 预览 HTTP 服务退出: {type(e).__name__}: {e} (识别继续)")

        t = threading.Thread(target=_serve, name="preview-http", daemon=True)
        t.start()

        names = "/".join(encoders)
        # ★ 这句是调机时首先要看的话：确认预览没有额外拉流
        print(f"  📺 预览已启动: http://<板子IP>:{port}  ({names}, {res}, {fps}fps 上限)")
        print(f"     识别与预览共享同一路解码，相机源仍只有 1 条 RTSP 连接")

        def _stop():
            for enc in encoders.values():
                try:
                    enc.stop()
                except Exception:             # noqa: BLE001
                    pass

        return (t, _stop)
    except Exception as e:                    # noqa: BLE001
        print(f"  ⚠ 预览启动失败({type(e).__name__}: {e}), 识别照常继续")
        return None, (lambda: None)
