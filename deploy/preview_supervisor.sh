#!/bin/bash
# SN 预览双路推流监督脚本
#
# 为什么需要它（现场故障）
# ------------------------
# 原单元用 `set -m; ffmpeg A & ffmpeg B & wait`：
#   任一路 ffmpeg 退出后, `wait` 只等**剩下的**那个, 不会重拉已退出的那路;
#   而 systemd 看到 bash 外壳还活着, 就认为单元健康 -> 永久单路。
#   连续两晚界面"背面没视频"就是这么来的: 相机是好的, 推流进程死了没人管。
#
# ffmpeg 遇到流抖动是**直接退出**(不做重试), 而 python reader 是**重试** ——
# 这解释了同一台相机上两种客户端的不对称。
#
# 本脚本做什么
# ------------
# 两路各自 fork + 各自记账: 谁退就只重拉谁, 另一路完全不受影响。
# 退避 3s -> 6s -> 12s -> 24s -> 30s(上限), 稳定运行 30s 后退避清零。
# ★ 全程非阻塞: 一路在退避等待时, 另一路照样能被立刻重拉(用时间戳记账,
#   不用 sleep 大值, 否则"等 back 重试"的 30s 里 front 死了也没人管)。
# 收到 SIGTERM/SIGINT 时先收掉所有子进程再退(保持单元 KillSignal/TimeoutSec 语义)。
set -u

FFMPEG=/opt/sophon/sophon-ffmpeg_2.2.0/bin/ffmpeg

# 子码流: live1(前) 与 live1(后)。h264_bm 只能吃 BM1688 硬解输出, 不能走 pipe:0。
declare -A URL=(
  [front]="rtsp://192.168.1.9:8554/live1"
  [back]="rtsp://192.168.1.8:8554/live1"
)
declare -A RTMP=(
  [front]="rtmp://10.80.40.57:1935/cam_front"
  [back]="rtmp://10.80.40.57:1935/cam_back"
)

NAMES="front back"
declare -A PID=()       # 当前子进程 pid
declare -A STARTED=()   # 当前子进程拉起时刻(用于"活够 30s 算稳定")
declare -A NEXT=()      # 下次允许重拉的时刻
declare -A BOFF=()      # 连续失败次数(决定退避档位)
RUNNING=1

log() { echo "[supervisor] $(date '+%H:%M:%S') $*"; }

# ★ 必须**直接后台启动**，不能写成「函数里启动再调用函数 &」：
#   `func &` 会 fork 一个子 shell，$! 拿到的是那个子 shell 的 pid；
#   真 ffmpeg 是它的孩子。于是 kill -TERM $! 只杀掉外壳，ffmpeg 变孤儿继续推流，
#   而 wait $! 又在等外壳(外壳在等 ffmpeg) -> 永远不返回。
#   现场实测: 收尾挂死 60s+，两路子进程全部残留。
#   直接 `cmd &` 时 $! 就是 ffmpeg 本身，TERM 与 wait 都作用在正确对象上。
start_front() {
  "$FFMPEG" -rtsp_transport tcp -i "${URL[front]}" \
    -an -c:v h264_bm -b:v 2M -g 30 -bf 0 -flvflags no_duration_filesize \
    -f flv "${RTMP[front]}" &
  PID[front]=$!
  STARTED[front]=$(date +%s)
  NEXT[front]=0
  log "front 已拉起 (pid=${PID[front]}) <- ${URL[front]}"
}
start_back() {
  "$FFMPEG" -rtsp_transport tcp -i "${URL[back]}" \
    -an -c:v h264_bm -b:v 2M -g 30 -bf 0 -flvflags no_duration_filesize \
    -f flv "${RTMP[back]}" &
  PID[back]=$!
  STARTED[back]=$(date +%s)
  NEXT[back]=0
  log "back 已拉起 (pid=${PID[back]}) <- ${URL[back]}"
}

# 退避档位: 5,10,20,30,30... 上限 30s(源长时间不可用时别暴力刷 ffmpeg)
# V4.1: 起步从 3s 提到 5s —— 非零退出时 ffmpeg 的 RTSP 释放可能不干净,
# 对单客户端源(.8)脏断开即卡死不自愈，多给 2s 让 TCP 超时释放残留半连接。
backoff_secs() {
  case "$1" in
    0) echo 5  ;;
    1) echo 10 ;;
    2) echo 20 ;;
    *) echo 30 ;;
  esac
}

cleanup() {
  RUNNING=0
  log "收到停止信号, 收尾中..."
  # 先 TERM, 让 ffmpeg 自己走完 TEARDOWN(对单客户端源很重要)。
  # ★ 时间预算必须留在单元的 TimeoutStopSec=15 以内, 否则 systemd 会直接
  #   SIGKILL 整个 cgroup, 打断 TEARDOWN。这里 8s 优雅 + 2s 等死。
  for name in $NAMES; do
    [ -n "${PID[$name]:-}" ] && kill -TERM "${PID[$name]}" 2>/dev/null
  done
  # 细粒度轮询: 一旦全退立刻返回, 不无条件等满(实测 ffmpeg 通常 1~2s 内退)
  local waited=0 alive=1
  while [ "$waited" -lt 8 ]; do
    alive=0
    for name in $NAMES; do
      pid="${PID[$name]:-}"
      [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && alive=1
    done
    [ "$alive" = "0" ] && break
    sleep 0.25
    waited=$(( waited + 1 ))
  done
  if [ "$alive" = "1" ]; then
    log "仍有子进程未响应 TERM, 强杀"
    for name in $NAMES; do
      pid="${PID[$name]:-}"
      [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" 2>/dev/null
    done
    sleep 0.5
  fi
  for name in $NAMES; do
    pid="${PID[$name]:-}"
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && log "⚠ $name(pid=$pid) 仍未退出"
  done
  log "已全部收尾 (耗时约 $(( waited / 4 ))s)"
  exit 0
}
trap cleanup TERM INT

BOFF[front]=0; BOFF[back]=0
start_front
start_back

# 主监督循环: 每 2s 扫一次两路状态。谁退重拉谁, 互不牵累。
while [ "$RUNNING" = "1" ]; do
  sleep 2
  now=$(date +%s)
  for name in $NAMES; do
    pid="${PID[$name]:-}"
    [ -z "$pid" ] && continue

    if kill -0 "$pid" 2>/dev/null; then
      # 还活着: 若已稳定运行 ≥30s 则退避清零(下次出错从最短档重来)
      if [ "${BOFF[$name]}" -gt 0 ] \
         && [ $(( now - ${STARTED[$name]:-$now} )) -ge 30 ]; then
        log "$name 稳定运行 30s, 退避计数清零"
        BOFF[$name]=0
      fi
      continue
    fi

    # 进程没了: 记账并(到点后)重拉。wait 回收, 避免僵尸
    wait "$pid" 2>/dev/null
    rc=$?
    b="${BOFF[$name]}"
    if [ "$now" -lt "${NEXT[$name]:-0}" ]; then
      continue                      # 还在退避窗口内, 下轮再看
    fi
    delay=$(backoff_secs "$b")
    PID[$name]=""
    BOFF[$name]=$(( b + 1 ))
    # V4.1: ffmpeg非零退出→额外冷却3s，释放残留RTSP半连接
    if [ "$rc" -ne 0 ]; then
      delay=$(( delay + 3 ))
    fi
    NEXT[$name]=$(( now + delay ))
    log "⚠ $name 退出 (rc=$rc), ${delay}s 后重拉 (连续第 ${BOFF[$name]} 次)"
  done

  # 到点且当前无进程的 -> 重拉。用 if/else 而非间接调用, 保证 $! 仍是 ffmpeg 本身
  now=$(date +%s)
  for name in $NAMES; do
    if [ -z "${PID[$name]:-}" ] && [ "$now" -ge "${NEXT[$name]:-0}" ]; then
      [ "$RUNNING" = "1" ] || break
      if [ "$name" = "front" ]; then start_front; else start_back; fi
    fi
  done
done
