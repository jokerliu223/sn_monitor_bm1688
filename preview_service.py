#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
调机预览服务 (Camera Preview Console) —— 板子侧
================================================
用途：产测网页端在**调机阶段**提供双路摄像头实时画面，供人工调节镜头高度/焦距；
      调节完成后再由人启动识别服务（sn-monitor / sn-monitor-big）。

设计要点（每条都有实测依据，改前先看 README §4.9）：
  * 相机是**单客户端源**：谁先连上谁独占，且它不会自愈。故本服务对每路相机
    只维持**一条**连接（一个 Puller 线程），多个网页观众共享同一份最新帧。
  * 每路相机独立线程，任一路挂掉不影响另一路（README 铁律 1 的双路版）。
  * 解码走 sail.Decoder（硬件），编码走 Bmcv.imencode（硬件 JPEG）。
    实测 4K 编码 54.9ms / 720p 8.1ms；软件 cv2 编 4K 要 271ms，不可用。
  * 帧缓冲用 **latest-slot**：慢客户端永远拿到最新帧，绝不拖慢采集。
  * RTSP **显式强制 TCP**：铁律 1 要求，sail 默认已是 TCP，这里再钉一遍做双保险。
  * 本服务**只读拉流**：不落库、不写 sn_results、不上传。

与识别服务的互斥：由 systemd 单元里的 Conflicts= 保证（见 deploy/sn-preview.service），
不靠本脚本自己判断。
"""

import os
import sys
import time
import glob
import signal
import threading
import logging

# ---- RTSP 强制 TCP（必须在 import sail 之前设，见 README §4.9 R1 结论）----
# 注意：sail.set_decoder_env() 内部会自动补 "SAIL_DECODER_" 前缀，
#      而这里直接写 os.environ 需要全名。两者都可，别混用成双前缀。
os.environ.setdefault("SAIL_DECODER_rtsp_transport", "tcp")
os.environ.setdefault("SAIL_DECODER_rtsp_flags", "prefer_tcp")
os.environ.setdefault("SAIL_DECODER_stimeout", "5000000")      # 5s，RTSP 建连超时
os.environ.setdefault("SAIL_DECODER_buffer_size", "1024000")

import sophon.sail as sail                                   # noqa: E402
from flask import Flask, Response, request, jsonify          # noqa: E402

# 相机出厂带 RTSP 服务，但"谁在推"由相机自己决定。8554 是默认口。
CAMERAS = {
    "front": os.environ.get("PREVIEW_RTSP_FRONT", "rtsp://192.168.1.9:8554/live0"),
    "back":  os.environ.get("PREVIEW_RTSP_BACK",  "rtsp://192.168.1.8:8554/live0"),
}

# 分辨率档位：网页下拉框的值 → (宽, 高)。0 表示原始尺寸（4K 全幅）。
RESOLUTIONS = {
    "4k":   (0, 0),          # 3840x2160，不缩放，调焦距/高度时看清全图
    "1080p": (1920, 1080),
    "720p": (1280, 720),
    "360p": (640, 360),
}

JPEG_QUALITY = int(os.environ.get("PREVIEW_JPEG_QUALITY", "80"))  # 注：Bmcv 实测不可调，仅记录
# 采集侧上限帧率。定 20 是实测校准的结果，别往下调：
#   Bmcv 质量不可调，4K 单帧固定 ~2.2MB；解码 p95 78ms（上限 12.8fps）。
#   本机实测 4K 能到 8.1fps、720p 到 14.9fps —— 原设 15 会离硬件上限太近，
#   负载一高就误伤，把"运行状态"和"被节流"搞混。留到 20，让采集跑满真实能力，
#   实际帧率受限于编解码/网络而非本参数。
SRC_FPS_CAP = float(os.environ.get("PREVIEW_SRC_FPS", "20"))
# 断流重连的探端口间隔，避免对着已经死掉的端口猛刷 ffmpeg（沿用识别脚本的做法）
RECONNECT_PROBE_GAP = float(os.environ.get("PREVIEW_PROBE_GAP", "3"))
LISTEN_PORT = int(os.environ.get("PREVIEW_PORT", "8090"))
BOUNDARY = "snframe"


# --------------------------------------------------------------------------
# 单路相机采集器
# --------------------------------------------------------------------------
class CameraPuller:
    """一路相机的采集线程 + 最新帧槽位。

    为什么不直接用 cv2.VideoCapture：sail.Decoder 能吃到板子的硬件解码器，
    省掉 CPU 软解 4K 的开销；且 sail 的 RTSP 默认就是 TCP（铁律 1 要求）。
    """

    def __init__(self, name: str, rtsp: str):
        self.name = name
        self.rtsp = rtsp
        self.lock = threading.Lock()        # 保护 _frame / _seq / 状态字段
        self._frame = None                  # (jpeg_bytes, seq, ts)
        self._seq = 0
        self._cond = threading.Condition(self.lock)
        self._stop = threading.Event()
        self._thread = None
        # 默认 4K 全幅：调机要先看清整图，网页可手动降到 720p/360p
        self.resolution = "4k"
        # 运行状态（/status 用，也方便前端显示"哪一路断了"）
        self.state = "init"                 # init|running|reconnecting|stopped
        self.err = ""
        self.frames = 0
        self.last_ok = 0.0
        self._decoder = None
        self._handle = None
        self._bmcv = None

    # ---- 生命周期 ----
    def start(self):
        self._thread = threading.Thread(target=self._run, name=f"pull-{self.name}",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        with self._cond:
            self._cond.notify_all()

    def stopped(self) -> bool:
        return self._stop.is_set()

    # ---- 打开 / 重开设备 ----
    def _open(self) -> bool:
        """返回是否成功打开。失败一律返回 False，不抛异常。"""
        self._close()
        try:
            # Decoder(file, compressed_nv12, dev_id)：compressed_nv12=True 表示
            # 压缩码流直接进硬件解码器（我们的输入正是 H.265 码流）。
            self._handle = sail.Handle(0)
            self._bmcv = sail.Bmcv(self._handle)
            self._decoder = sail.Decoder(self.rtsp, True, 0)
            # 试读一帧确认真能出图（只 isOpened 不够，实测过死流也会"打开成功"）
            img = sail.BMImage()
            if self._decoder.read(self._handle, img) != 0:
                self.err = "open ok but first read failed"
                self._close()
                return False
            LOG.info("[%s] 已连接 %s (%dx%d)", self.name, self.rtsp, img.width(), img.height())
            return True
        except Exception as e:              # noqa: BLE001 — 相机没推流时就是抛异常
            self.err = str(e)[:200]
            self._close()
            return False

    def _close(self):
        dec, self._decoder = self._decoder, None
        self._handle, self._bmcv = None, None
        if dec is not None:
            try:
                del dec
            except Exception:               # noqa: BLE001
                pass

    # ---- 采集主循环 ----
    def _run(self):
        interval = 1.0 / SRC_FPS_CAP
        while not self._stop.is_set():
            if self._decoder is None:
                self.state = "reconnecting"
                if not self._open():
                    # 打不开就等一会再试，别对着死端口刷
                    self._stop.wait(RECONNECT_PROBE_GAP)
                    continue
                self.state = "running"
                self.last_ok = time.time()

            t0 = time.time()
            try:
                img = sail.BMImage()
                ret = self._decoder.read(self._handle, img)
                if ret != 0:
                    # 读失败：先 reconnect 一次，还不行就整体重开（死流不自愈）
                    LOG.warning("[%s] read 失败 ret=%s，重连中", self.name, ret)
                    try:
                        self._decoder.reconnect()
                    except Exception:       # noqa: BLE001
                        pass
                    self._stop.wait(0.3)
                    if self._decoder.read(self._handle, img) != 0:
                        self.err = "read failed after reconnect"
                        self._close()
                        continue
                    # 重连后 read 成功 → 落到下面正常编码

                jpg = self._encode(img)
                if jpg:
                    with self._cond:
                        self._seq += 1
                        self._frame = (jpg, self._seq, time.time())
                        self.frames += 1
                        self.last_ok = time.time()
                        self._cond.notify_all()
                self.err = ""
            except Exception as e:          # noqa: BLE001
                LOG.warning("[%s] 采集异常: %s", self.name, str(e)[:120])
                self.err = str(e)[:200]
                self._close()
                self._stop.wait(0.5)
                continue

            # 节流：剩余时间睡掉，保证不超过 SRC_FPS_CAP
            dt = time.time() - t0
            if dt < interval:
                self._stop.wait(interval - dt)

        self.state = "stopped"
        self._close()

    def _encode(self, img) -> bytes:
        """按当前档位缩放 + 硬件 JPEG 编码。返回 JPEG 字节。"""
        w, h = RESOLUTIONS.get(self.resolution, (0, 0))
        if w and h and (img.width() != w or img.height() != h):
            img = self._bmcv.resize(img, w, h)
        arr = self._bmcv.imencode(".jpg", img)
        return arr.tobytes()

    def set_resolution(self, res: str) -> bool:
        if res not in RESOLUTIONS:
            return False
        self.resolution = res
        LOG.info("[%s] 档位切到 %s", self.name, res)
        return True

    # ---- 取帧 ----
    def latest(self, timeout: float = 2.0):
        """等一帧比 min_seq 新的（调用方自己比对），超时返回 None。

        用 Condition 而非轮询：慢客户端不会占着锁，采集线程永不阻塞。
        """
        with self._cond:
            ok = self._cond.wait_for(lambda: self._frame is not None, timeout)
            return self._frame if ok else None

    def wait_newer(self, seq: int, timeout: float = 5.0):
        """等到出现 seq 更大的帧；没有新帧就超时返回同一份（心跳用）。"""
        with self._cond:
            self._cond.wait_for(lambda: self._seq > seq, timeout)
            return self._frame

    def status(self):
        with self.lock:
            return {
                "name": self.name,
                "rtsp": self.rtsp,
                "state": self.state,
                "resolution": self.resolution,
                "size": RESOLUTIONS.get(self.resolution, (0, 0)),
                "frames": self.frames,
                "last_ok": self.last_ok,
                "age": (time.time() - self.last_ok) if self.last_ok else None,
                "err": self.err,
            }


# --------------------------------------------------------------------------
# Flask 应用
# --------------------------------------------------------------------------
LOG = logging.getLogger("preview")
app = Flask(__name__)
PULLERS = {name: CameraPuller(name, url) for name, url in CAMERAS.items()}


def _stream(puller: CameraPuller):
    """MJPEG over multipart/x-mixed-replace。

    浏览器用 <img src=...> 就能放，不需要 JS 解码，也不需要 MSE。
    每帧前面带 Content-Length，部分客户端（尤其 Safari）没有它会卡住。
    """
    last_seq = 0
    while not puller.stopped():
        frame = puller.wait_newer(last_seq, timeout=5.0)
        if frame is None:
            continue
        jpg, seq, _ts = frame
        if seq == last_seq:
            continue                                     # 超时且无新帧：什么也不发
        last_seq = seq
        yield (b"--" + BOUNDARY.encode() + b"\r\n"
               b"Content-Type: image/jpeg\r\n"
               b"Content-Length: " + str(len(jpg)).encode() + b"\r\n\r\n"
               + jpg + b"\r\n")


@app.route("/stream")
def stream():
    name = request.args.get("cam", "front")
    puller = PULLERS.get(name)
    if puller is None:
        return jsonify({"error": f"unknown cam: {name}",
                        "valid": list(PULLERS)}), 400
    res = request.args.get("res")
    if res:
        puller.set_resolution(res)

    resp = Response(_stream(puller),
                    mimetype=f"multipart/x-mixed-replace; boundary={BOUNDARY}")
    # 关掉各类缓冲，否则 MJPEG 会攒一大块才吐，延迟飙升
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


@app.route("/snapshot")
def snapshot():
    """单张抓拍。给 MJPEG 放不出来的浏览器兜底（定时刷新用）。"""
    name = request.args.get("cam", "front")
    puller = PULLERS.get(name)
    if puller is None:
        return jsonify({"error": f"unknown cam: {name}"}), 400
    res = request.args.get("res")
    if res:
        puller.set_resolution(res)
    frame = puller.latest(timeout=3.0)
    if frame is None:
        return jsonify({"error": "no frame yet",
                        "state": puller.state,
                        "err": puller.err}), 503
    jpg, seq, ts = frame
    resp = Response(jpg, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Frame-Seq"] = str(seq)
    return resp


@app.route("/status")
def status():
    return jsonify({
        "ok": True,
        "port": LISTEN_PORT,
        "jpeg_quality": JPEG_QUALITY,
        "src_fps_cap": SRC_FPS_CAP,
        "cameras": [PULLERS[k].status() for k in PULLERS],
    })


@app.route("/resolution")
def set_resolution():
    """网页下拉框调档：?res=4k|1080p|720p|360p[&cam=front|back]"""
    res = request.args.get("res", "")
    cam = request.args.get("cam")
    targets = [PULLERS[cam]] if cam in PULLERS else list(PULLERS.values())
    for p in targets:
        if not p.set_resolution(res):
            return jsonify({"error": f"bad res: {res}",
                            "valid": list(RESOLUTIONS)}), 400
    return jsonify({"ok": True, "res": res,
                    "cameras": [p.name for p in targets]})


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


def _shutdown(*_a):
    LOG.info("收到退出信号，停采集线程…")
    for p in PULLERS.values():
        p.stop()
    sys.exit(0)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    LOG.info("调机预览服务启动，端口 %d", LISTEN_PORT)
    for p in PULLERS.values():
        LOG.info("  相机 %-5s → %s", p.name, p.rtsp)
        p.start()
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    # threaded=True：两路 MJPEG 各占一个长连接，必须多线程
    # use_reloader=False：重载器会 fork，把相机连接搞成两条（违反单客户端铁律）
    app.run(host="0.0.0.0", port=LISTEN_PORT, threaded=True,
            debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
