#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TC-9201~9204: 背面链路保活 —— 对齐正面已有的三段保活逻辑（改码前必须先跑失败基线）

用途
----
现场：子码流里正面基本不挂死，背面经常挂死。取证发现背面路径缺了正面已有的
三段保活判断，反把"单客户端源"往脏断开方向推。本文件对**函数行为**做验收，
不启动 OCR/NPU，不需要真相机。

为什么能离线测
--------------
probe_and_recover / back_health_tick / PreviewEncoder.status 都是**依赖注入**的：
传一个假 reader 即可，不碰真实 RTSP。解码错误计数是模块级 deque，直接注入即可。

跑法
----
    python3 tests/test_resilience_back.py            # 全部
    python3 tests/test_resilience_back.py TC-9201    # 单个

失败基线（改码前应 4 条 FAIL）
-----------------------------
TC-9201  解码错在刷时背面仍会重连（应从 GOP 中间重进 -> ref-error churn 自激）
TC-9202  背面先开新流再换掉旧流（双会话并存 -> 单客户端源脏断开）
TC-9203  单次 3s 抖动即可触发背面重连（无失败计数门）
TC-9204  /status 在源已死时仍报 source_ok=True（假绿）
"""
import os
import sys
import time

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sn_monitor as M

_results = []


def _record(tc, ok, detail):
    _results.append((tc, ok, detail))
    print(f"  {'✅ PASS' if ok else '❌ FAIL'}  {tc}  {detail}")


class FakeCap:
    """假 cap：只用来占位，release 由 FakeReader 记录顺序。"""
    pass


class FakeReader:
    """满足 probe_and_recover 所需接口的假 reader。

    age=None 表示"从未有帧"（源死了/刚挂上还没出流）。
    events 记录 set_cap 的调用顺序，用于验证"先释放后开流"。
    """

    def __init__(self, age=None, cap_t=None, events=None):
        self._age = age
        self._cap_t = cap_t if cap_t is not None else time.time()
        self._cap = None
        self.events = events if events is not None else []
        self.fail = 0

    def last_age(self):
        return self._age

    def set_cap(self, cap):
        if cap is None:
            self.events.append("release")
        else:
            self.events.append("open")
        self._cap = cap

    def wait_released(self, timeout=6.0):
        """真要重连时会被调用(等 reader 线程放开旧 cap)。桩里即时返回。"""
        self.events.append("released")
        return True

    def latest(self, wait=0.0):
        return None, 0.0, self.fail


def _install_decode_errs(n=5, age_s=0.5):
    """往模块级解码错误 deque 里注入 n 条"刚刚发生"的记录。"""
    now = time.time()
    with M._err_lock:
        M._err_times.clear()
        for i in range(n):
            M._err_times.append(now - age_s * i)


def _clear_decode_errs():
    with M._err_lock:
        M._err_times.clear()


# ============================================================
# TC-9201: 解码错误在刷时，背面绝不重连（等 IDR 自愈）
# ------------------------------------------------------------
# 理由: ref-error(丢参考帧)正刷 = 解码器等下个 IDR 关键帧自愈。重连会从 GOP
#       中间重进，又吃到一片 ref-error -> churn 自激。正面已有此判据，背面没有。
# 期望: 全程不调用 reconnect_bounded（opened 计数为 0）
# 基线: 直接重连（opened >= 1）-> FAIL
# ============================================================
def tc_9201():
    print("\n[TC-9201] 解码错误在刷 -> 背面必须等 IDR 自愈，不重连")
    opened = {"n": 0}
    _orig = M.reconnect_bounded

    def _spy(*a, **kw):
        opened["n"] += 1
        return FakeCap()

    M.reconnect_bounded = _spy
    try:
        _install_decode_errs(n=5)
        r = FakeReader(age=8.0)          # 帧已陈旧 8s，但解码活动在刷
        M.probe_and_recover.last = 0.0
        M.probe_and_recover.offline = False
        M.probe_and_recover(r, "rtsp://192.168.1.8:8554/live0",
                            min_interval=0.0, wedge_fails=1, wedge_age=3.0)
        if opened["n"] == 0:
            _record("TC-9201", True, "解码错在刷时未重连（同连接等 IDR）")
        else:
            _record("TC-9201", False,
                    f"解码错在刷仍重连了 {opened['n']} 次 -> 会 ref-error churn 自激")
    except Exception as e:                          # noqa: BLE001
        _record("TC-9201", False, f"调用异常: {type(e).__name__}: {e}")
    finally:
        M.reconnect_bounded = _orig
        _clear_decode_errs()


# ============================================================
# TC-9202: 恢复必须"先释放旧 cap，后开新流"
# ------------------------------------------------------------
# 理由: 单客户端源(见 rtsp-camera-udp-teardown)，旧连接没 TEARDOWN 干净时
#       新连接拉不起来。背面现在先开新流再换掉旧的 -> 存在双会话并存窗口。
# 期望: events 中 "release" 出现在 "open" 之前
# 基线: events == ["open"]（只记录到 set_cap(新cap)，从未释放）-> FAIL
# ============================================================
def tc_9202():
    print("\n[TC-9202] 背面恢复必须 先释放旧 cap 再开新流")
    events = []
    _orig = M.reconnect_bounded

    def _fake_reconnect(rtsp, **kw):
        events.append("connect")        # 开新流(此刻应已释放旧连接)
        return FakeCap()

    M.reconnect_bounded = _fake_reconnect
    try:
        _clear_decode_errs()
        r = FakeReader(age=9.0, events=events)   # 陈旧且无解码活动 = 真坏流
        # 必须清掉上一用例留下的 last: 节流门是 wall-clock, 不清则本轮直接被挡,
        # 一条 event 都不会记 —— 那测出来的是"没走到", 不是"顺序对不对"。
        M.probe_and_recover.last = 0.0
        M.probe_and_recover.offline = False
        M.probe_and_recover(r, "rtsp://192.168.1.8:8554/live0",
                            min_interval=0.0, wedge_fails=1, wedge_age=3.0,
                            fails=1)
        order = [e for e in events if e in ("release", "connect")]
        if order[:2] == ["release", "connect"]:
            _record("TC-9202", True, f"顺序正确 {order}（先释放，后开流）")
        elif not order:
            _record("TC-9202", False,
                    "一次都没动流(节流门挡住?) -> 本用例没测到顺序, 属用例缺陷")
        elif "release" not in order:
            _record("TC-9202", False,
                    f"从未释放旧 cap 就开新流: {events} -> 双会话并存，脏断开")
        else:
            _record("TC-9202", False, f"顺序错误 {order}（先开新流，后释放旧流）")
    except Exception as e:                          # noqa: BLE001
        _record("TC-9202", False, f"调用异常: {type(e).__name__}: {e}")
    finally:
        M.reconnect_bounded = _orig


# ============================================================
# TC-9203: 单次陈旧抖动不触发重连（须达失败计数门）
# ------------------------------------------------------------
# 理由: 告警门是 fails>=wedge_fails(3)，动作门却是单次 last_age()>wedge_age。
#       于是一次 3s 抖动就重连一次 —— 背面因此长期在"重连 churn"里打转。
# 期望: hits < wedge_fails 时不动流（events 为空）
# 基线: 单次即重连（events 非空）-> FAIL
# ============================================================
def tc_9203():
    print("\n[TC-9203] 单次陈旧抖动（未达失败门）不得触发重连")
    events = []
    _orig = M.reconnect_bounded

    def _fake_reconnect(rtsp, **kw):
        events.append("connect")
        return FakeCap()

    M.reconnect_bounded = _fake_reconnect
    try:
        _clear_decode_errs()
        r = FakeReader(age=3.5, events=events)   # 刚过阈值一点点的瞬时抖动
        M.probe_and_recover.last = 0.0
        M.probe_and_recover.offline = False
        # hits=1 < wedge_fails=3 -> 应只计数，不动流
        M.probe_and_recover(r, "rtsp://192.168.1.8:8554/live0",
                            min_interval=0.0, wedge_fails=3, wedge_age=3.0,
                            fails=1)
        if events:
            _record("TC-9203", False,
                    f"单次抖动(hits=1<3)就动了流: {events} -> 重连 churn")
        else:
            _record("TC-9203", True, "hits<门限，未动流（只计数）")
    except TypeError as e:
        _record("TC-9203", False,
                f"probe_and_recover 不接受 fails 入参 -> 无失败计数门: {e}")
    except Exception as e:                          # noqa: BLE001
        _record("TC-9203", False, f"调用异常: {type(e).__name__}: {e}")
    finally:
        M.reconnect_bounded = _orig


# ============================================================
# TC-9204: 源已死时 /status 必须报离线，而不是假绿
# ------------------------------------------------------------
# 理由: age 反映的是预览**编码**时刻；源死了仍报 source_ok=True。
#       假绿比没指示更危险 —— 现场连续两晚"看着是好的"。
# 期望: 旧帧(age 远超阈值) -> source_ok=False
# 基线: source_ok 只看 frame is not None -> 仍为 True -> FAIL
# ============================================================
def _fake_encoder_cls():
    import sn_preview_embed as P
    return P.PreviewEncoder


def tc_9204():
    print("\n[TC-9204] 源已死 -> /status 必须报离线（不得假绿）")
    try:
        Enc = _fake_encoder_cls()
    except Exception as e:                          # noqa: BLE001
        _record("TC-9204", False, f"导入 sn_preview_embed 失败: {e}")
        return
    try:
        # 造一个"从未死过、但很旧"的 reader：frame 有值，ts 停在 30s 前
        class DeadReader(FakeReader):
            def __init__(self):
                super().__init__(age=30.0)
                self.frame = "stale-frame-object"
                self.ts = time.time() - 30.0
                self.lock = __import__("threading").Lock()

        rd = DeadReader()
        enc = Enc(rd, "back", fps=8.0, res="720p", results_dir=None)
        enc._ts = time.time() - 30.0        # 预览自身也不出新帧（源死）
        st = enc.status()
        # 源帧龄: 优先用 reader.last_age()
        src_age = st.get("source_age")
        ok = st.get("source_ok")
        if ok is False:
            _record("TC-9204", True,
                    f"源死如实报离线 (source_ok=False, source_age={src_age})")
        else:
            _record("TC-9204", False,
                    f"假绿: source_ok={ok} (帧龄30s) -> 现场看着是好的")
    except Exception as e:                          # noqa: BLE001
        _record("TC-9204", False, f"status() 调用异常: {type(e).__name__}: {e}")


ALL = {"TC-9201": tc_9201, "TC-9202": tc_9202,
       "TC-9203": tc_9203, "TC-9204": tc_9204}


def main():
    pick = sys.argv[1:] or list(ALL)
    print("=" * 68)
    print("  SN 背面链路保活 TC（IDR自愈 / 释放优先 / 失败门 / 真实帧龄）")
    print(f"  被测模块: {M.__file__}")
    print("=" * 68)
    for tc in pick:
        fn = ALL.get(tc)
        if fn is None:
            print(f"  ⚠ 未知用例 {tc}")
            continue
        try:
            fn()
        except Exception as e:                      # noqa: BLE001
            _record(tc, False, f"用例自身异常: {type(e).__name__}: {e}")

    npass = sum(1 for _, ok, _ in _results if ok)
    ntot = len(_results)
    print("\n" + "=" * 68)
    print(f"  结果: {npass}/{ntot} 通过")
    for tc, ok, _ in _results:
        print(f"    {'✅' if ok else '❌'} {tc}")
    print("=" * 68)
    return 0 if npass == ntot else 1


if __name__ == "__main__":
    sys.exit(main())
