# preview_service.py —— 调机预览服务（板子侧，V1/V2 遗留，已停用保留）

> **状态：已停用（`systemctl stop sn-preview` + `disable`），脚本保留不删，也未纳入部署。**
> V3.0 起，预览已合并进 `sn_monitor.py`（同一进程共享同一路解码），见下方「为什么停了」。
> 本文档只讲**这个脚本自己**的接口与用法；预览功能整体的用法看主 README。

---

## 1. 这个脚本是干什么的

产测网页在**调机阶段**需要看到两路摄像头的实时画面，供人工调节镜头高度、焦距、对焦。
本脚本就是当时实现这件事的独立服务：在板子上起一个 Flask（`:8090`），
从两路 RTSP 拉流 → 硬件解码 → 硬件 JPEG 编码 → 以 MJPEG 推给浏览器。

它只做**预览**，不做任何识别：不落库、不写 `sn_results/`、不上传产测系统。

---

## 2. 保留的可用接口

### 2.1 HTTP 接口（Flask，默认端口 8090）

| 方法 | 路径 | 参数 | 返回 | 说明 |
|---|---|---|---|---|
| GET | `/stream` | `cam=front\|back`、`res=4k\|1080p\|720p\|360p` | `multipart/x-mixed-replace` MJPEG | 主用接口，浏览器 `<img>` 直接吃 |
| GET | `/snapshot` | `cam`、`res` | `image/jpeg` | 单帧抓图，调试/截图用 |
| GET | `/status` | — | JSON | 每路相机的 `seq/age/source_ok/avg_ms/skipped` 等 |
| GET | `/resolution` | `res=<档位>` | JSON | **设置**分辨率（不是查询）；非法值回 `error` + `valid` 候选 |
| GET | `/health` | — | `{"status":"ok"}` | 存活探针 |
| — | `/` | — | HTML | 简易预览页，板子上直接开浏览器看 |

`res` 四档：`4k`(3840×2160，原始) / `1080p` / `720p` / `360p`。
**4k 表示不缩放**，其余档位在编码前 `bmcv.resize`，用来在网页卡顿时降码率。

### 2.2 Python 接口（`CameraPuller` 类，第 72 行）

```python
from preview_service import CameraPuller

p = CameraPuller(rtsp_url="rtsp://192.168.1.9:8554/live0", name="front")
p.start()                      # 起独立拉流线程
frame = p.latest(timeout=2.0)  # 取最新帧（latest-slot，非阻塞语义）
p.set_resolution("720p")       # 运行中切换分辨率
p.stop()                       # 干净退出：发 TEARDOWN 释放单客户端源
st = p.status()                # 字典：seq / age / source_ok / avg_ms ...
```

`CameraPuller` 是自包含的：**每路相机一个实例、一个线程、一条 RTSP 连接**。
多路之间互不影响，任一路挂了另一路照常。

### 2.3 CLI

```bash
python3 preview_service.py \
    --rtsp  rtsp://192.168.1.9:8554/live0 \
    --rtsp2 rtsp://192.168.1.8:8554/live0 \
    --port  8090 \
    --res   4k
```

`--rtsp2` 留空 = 只开一路。`SIGINT`(Ctrl-C) 触发干净退出并释放相机。

---

## 3. 当时实测的关键数据（选型依据，别丢）

| 编码方式 | 4K 单帧耗时 |
|---|---|
| `bmcv.imencode`（硬件） | **54.9 ms** |
| `bmcv.imencode` 720p | **8.1 ms** |
| `cv2.imencode`（软件，CPU） | **271 ms** ← 不可用 |

结论：预览必须走 Bmcv 硬件编码，软件编码在 4K 下根本跟不上。
这条数据在 V3.0 里依然有效，`sn_preview_embed.py` 沿用了同一条调用链。

---

## 4. 铁律（改这个脚本前必须先读）

1. **相机是单客户端源，且脏断开即卡死不自愈。**
   任何退出路径都必须让 `CameraPuller.stop()` 跑到，由它发 RTSP `TEARDOWN`。
   - 测这个脚本只能用 `timeout -s INT`（SIGINT 走干净退出），
     **严禁**裸 `timeout`(SIGTERM 硬杀，不 release) —— 会把摄像头推到半死状态。
   - 一旦把相机搞卡死，**远程无法恢复**，只能人工去现场重启摄像头推流服务。
2. **每路相机只允许一条 RTSP 连接。** 别为了"多一个观众"再开一条流——
   那会把已有连接踢掉。多观众共享同一个 `latest-slot`。
3. **RTSP 显式强制 TCP。** `sail` 默认已是 TCP，脚本里再钉一遍做双保险；
   走 UDP 时脏断开会把单客户端源卡死（实测 3 轮里只有 1 轮能重连成功）。
4. **预览线程绝不影响主流程。** 慢客户端只拿最新帧，不产生背压。

---

## 5. 为什么停了（V1/V2 → V3.0）

**核心矛盾：单客户端源。**
预览服务和识别服务都要拉同一路相机，但相机只接受一个客户端。
V1/V2 的解法是"互斥"——调机时停识别、调完停预览再起识别，
由 systemd 的 `Conflicts=` 保证。代价是**调机和识别不能同时进行**，
调镜头的时候看不到识别结果，得来回切，现场很别扭。

V3.0 换了思路：**让预览和识别跑在同一个进程里，共享同一次解码**。
`sn_monitor.py` 已经在解 4K 流了，预览线程只是顺手把最新帧再编一张 JPEG，
相机那一路连接数还是 1（REQ-103）。

所以这个脚本的**功能被 `sn_preview_embed.py` 取代**：

| | `preview_service.py`（本脚本） | `sn_preview_embed.py`（V3.0） |
|---|---|---|
| 进程 | 独立服务 | 内嵌在 `sn_monitor.py` 里 |
| 与识别关系 | 互斥，二选一 | 同时，共享解码 |
| 相机连接数 | 1（但独占总线） | 1（与识别共享） |
| 抓拍回显 | 无 | 有（读 `sn_results/`，不入库） |
| 启动方式 | `systemctl start sn-preview` | `systemctl start sn-monitor-big` |

**保留了什么**：脚本本身、它的 HTTP 接口约定、`CameraPuller` 的 latest-slot 设计、
Bmcv 编解码链路的实测数据。这些在 V3.0 里全部沿用，
`sn_preview_embed.py` 可以看作本脚本的一次"去进程化"改写。

**为什么保留而不删**：它是 V3.0 的设计来源，
万一以后要回到"独立预览服务"（比如识别服务需要长时间独占 NPU 时），
这套代码和接口约定可以直接复用。它现在不启动、不部署、不占端口。

---

## 6. 相关文件

| 文件 | 说明 |
|---|---|
| `sn_preview_embed.py` | V3.0 的预览实现（当前生效） |
| `sn_monitor.py` | 识别主程序，V3.0 里内嵌了预览 |
| `deploy/sn-preview.service` | 本脚本的 systemd 单元（已 `disable`） |
| `README.md` | 主 README，预览功能整体用法 |
