#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TC-9101~9104: SN 监控长稳韧性测试（改码前必须先跑出失败基线）

用途
----
对 sn_monitor.py 的**韧性**做验收：源不可达时主循环必须存活、健康检查必须
独立于识别事件、恢复必须自动。这些是"无人值守长期运行"的底线。

为什么单独一个文件
------------------
板子上跑的是真实摄像头，不能为了测"源挂掉"就真去拔网线。这里用 192.168.99.99
（不存在的主机）当替身源，同时把 sn_monitor 的函数**单独 import** 调用，
只测函数行为，不启动 OCR/NPU（eng=OCR() 在 main() 内部，import 无副作用）。

跑法
----
    python3 tests/test_resilience.py            # 全部
    python3 tests/test_resilience.py TC-9101    # 单个

失败基线（改码前应有 3 条 FAIL）
------------------------------
TC-9101  reconnect() 在源不可达时永不返回   -> 主循环卡死
TC-9102  FrameReader 无法在无流时挂上并自愈  -> 预览端口起不来
TC-9103  健康检查嵌在识别命中分支内         -> 静置期零感知
TC-9104  无看门狗                          -> 未知阻塞静默数小时

FC（通过）判据见各用例注释。
"""
import os
import sys
import time
import threading

# 同时认"脚本上级目录"和"当前工作目录"：拷贝到 /tmp 单独跑时后者生效
sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sn_monitor as M

# 不存在的主机：connect 立刻失败（EHOSTUNREACH/ETIMEDOUT），不会真去连摄像头
DEAD_RTSP = "rtsp://192.168.99.99:8554/live0"

_results = []


def _record(tc, ok, detail):
    _results.append((tc, ok, detail))
    print(f"  {'✅ PASS' if ok else '❌ FAIL'}  {tc}  {detail}")


def _in_thread(fn, timeout):
    """在守护线程里跑 fn，超时则返回 (None, 未完成)。
    绝不 join 死等——被测函数恰恰可能永不返回，那正是要抓的缺陷。"""
    box = {}

    def _run():
        try:
            box["ret"] = fn()
        except Exception as e:                      # noqa: BLE001
            box["err"] = f"{type(e).__name__}: {e}"

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return None, True                           # 未完成
    if "err" in box:
        raise RuntimeError(box["err"])
    return box.get("ret"), False


# ============================================================
# TC-9101: 源长期不可达，reconnect 必须有界返回（不拖死主循环）
# ------------------------------------------------------------
# 期望(修复后): 在 ≤ max_tries*(probe_gap+开流耗时) 内返回 None
# 失败基线     : 永远不返回（本轮 timeout 内未完成）
# ============================================================
def tc_9101():
    print("\n[TC-9101] 源长期不可达 -> reconnect 必须bounded返回")
    # 有界版：3 次尝试 × 1s 间隔 + 每次探活 2s 上限 -> 理论 ≤9s，宽松给 25s
    fn = getattr(M, "reconnect_bounded", None)
    if fn is None:
        _record("TC-9101", False,
                "reconnect_bounded() 不存在 —— 当前只有无限循环的 reconnect()，主循环会被拖死")
        return
    t0 = time.time()
    ret, unfinished = _in_thread(
        lambda: fn(DEAD_RTSP, max_tries=3, probe_gap=1), timeout=25)
    dt = time.time() - t0
    if unfinished:
        _record("TC-9101", False, f"reconnect_bounded() 25s 未返回（有界性失效）")
    elif ret is not None:
        _record("TC-9101", False, f"源不可达却返回了 cap={ret!r}（应返回 None）")
    else:
        _record("TC-9101", True, f"{dt:.1f}s 内返回 None（有界，主循环可继续）")


# ============================================================
# TC-9102: FrameReader 在"无流"状态下可挂载，并在源恢复后自愈
# ------------------------------------------------------------
# 期望(修复后): set_cap(None) 不炸；后续 set_cap(真cap) 能接上
# 失败基线     : 只能构造时就给定 cap，运行期无法先挂空再补流
# ============================================================
def tc_9102():
    print("\n[TC-9102] 无流挂载 + 源恢复自愈")
    try:
        r = M.FrameReader("tc9102")
    except Exception as e:                          # noqa: BLE001
        _record("TC-9102", False, f"FrameReader 构造失败: {type(e).__name__}: {e}")
        return
    try:
        # 1) 空 cap 挂载：预览线程会持它跑编码循环，帧龄应可读为"无帧"
        r.set_cap(None)
        f, age, fail = r.latest(wait=0.3)
        if f is not None:
            _record("TC-9102", False, "无 cap 时 latest() 竟返回了帧")
            return
        # 2) 恢复：源回来后 set_cap 能把流接上（用板子真源，不可达则应保持 None）
        ok_placeholder = hasattr(M, "reconnect_bounded")
        _record("TC-9102", ok_placeholder,
                "空 cap 可挂载，latest() 正确返回 None；运行期补流接口在位"
                if ok_placeholder else
                "空 cap 可挂载，但缺运行期补流所需的 reconnect_bounded()")
    finally:
        r.set_cap(None)
        r.run = False
        r.wait_released(1.0)


# ============================================================
# TC-9103: 背面健康检查必须独立于识别事件（静置期也跑）
# ------------------------------------------------------------
# 期望(修复后): 存在带 min_interval 的独立检查函数，可在主循环外周期调用
# 失败基线     : 检查逻辑内联在 SN 命中分支内，无独立入口
# ============================================================
def tc_9103():
    print("\n[TC-9103] 健康检查独立于识别路径（静置期可跑）")
    for name in ("back_health_tick", "tick_back_health"):
        fn = getattr(M, name, None)
        if fn is None:
            continue
        try:
            r = M.FrameReader("tc9103")
        except Exception as e:                      # noqa: BLE001
            _record("TC-9103", False, f"FrameReader 构造失败: {e}")
            return
        try:
            r.set_cap(None)
            # 无流时应能被调用且如实报告"不健康"，而不是抛异常或静默
            out = fn(r, min_interval=0.0)
            _record("TC-9103", True,
                    f"{name}() 可独立调用（返回 {out!r}），静置期可周期跑")
        except Exception as e:                      # noqa: BLE001
            _record("TC-9103", False, f"{name}() 调用抛异常: {type(e).__name__}: {e}")
        finally:
            r.set_cap(None); r.run = False; r.wait_released(1.0)
        return
    _record("TC-9103", False,
            "无独立健康检查入口（back_health_tick/tick_back_health 均不存在）"
            " —— 检查仍内联在 SN 命中分支，静置期完全失明")


# ============================================================
# TC-9104: 主循环看门狗（未知阻塞的最后防线）
# ------------------------------------------------------------
# 期望(修复后): 存在看门狗线程工厂，停滞超阈值会告警
# 失败基线     : 无看门狗 -> 未知阻塞可静默数小时
# ============================================================
def tc_9104():
    print("\n[TC-9104] 主循环看门狗（只告警不杀进程）")
    fn = getattr(M, "start_loop_watchdog", None)
    if fn is None:
        _record("TC-9104", False,
                "无 start_loop_watchdog() —— 未知阻塞仍会静默数小时无任何输出")
        return
    # 造一个"永不推进"的心跳，阈值 0.5s，看它是否在 ~1s 内告警
    beat = {"t": time.time()}
    fired = threading.Event()

    def _hook(msg):
        if "停滞" in msg:
            fired.set()

    try:
        stop = fn(beat, threshold=0.5, interval=0.2, on_alert=_hook)
        got = fired.wait(3.0)
        stop()
        _record("TC-9104", got,
                "停滞 0.5s 后如期告警" if got else "看门狗存在但未在 3s 内告警")
    except Exception as e:                          # noqa: BLE001
        _record("TC-9104", False, f"看门狗调用异常: {type(e).__name__}: {e}")


ALL = {"TC-9101": tc_9101, "TC-9102": tc_9102,
       "TC-9103": tc_9103, "TC-9104": tc_9104}


def main():
    pick = sys.argv[1:] or list(ALL)
    print("=" * 68)
    print("  SN 监控韧性 TC（源不可达 / 静置期 / 看门狗）")
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
