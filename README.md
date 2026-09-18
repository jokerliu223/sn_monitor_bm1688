# SN 监控系统 (RTSP → PP-OCR 序列号识别)

SE9 / BM1688 算能板用 eth1 接一台路由器/交换机组成小局域网，从相机拉 RTSP 流，自动侦测板卡铭牌、识别序列号(SN)并落库。

当前发布版本 **V4.0**。四种形态：

- **单路识别（默认）**：eth1 上挂一台相机对着板子正面，识别 SN。
- **双路（V2.1，加 `--rtsp2`）**：再挂第二台相机对着背面，正面识别命中时同步抓一帧背面图，正/反两张一并上传、在产测网页成对展示。两路**各拉各的流、不合并**。
- **调机预览（★ V4.0 WebRTC）**：板子上 `sophon-ffmpeg` 独立拉摄像头**子码流** (`live1`, 768×572 HEVC)，经 BM1688 硬件 H264 编码 (`h264_bm`, <5ms) → RTMP 推流到 **.57 服务器** → `.57` 上的 MediaMTX 转 WebRTC → 前端 `<video>` 实时播放。**不经过识别进程、不占主码流、不重编码为 JPEG。** 延迟 <500ms。
- **调机预览（V3.0 MJPEG，已废弃）**：识别进程内嵌 JPEG 编码 → MJPEG → `.57` 中继。V4.0 替换，不再使用。

> **V4.0 核心变更**：预览管道从 MJPEG(板子重编码) 换为 WebRTC(子码流直通)，延迟从分钟级降到实时。板子新增两个 ffmpeg 推流服务，`.57` 新增 MediaMTX 协议转换服务。
> **历史版本（V0.8 → V4.0）逐版说明见 [docs/CHANGELOG.md](docs/CHANGELOG.md)。**
> 本 README 只讲**当前版本怎么部署、怎么用**。

---

## 1. 它做什么

摄像头对着流水线上的板卡，人放一块板 → 程序自动：

1. 侦测到画面稳定且出现铭牌 →
2. 对铭牌区域按**档位**裁剪 + OCR，跨帧投票 + 格式校验提取 SN →
3. 二次确认后写入 `sn_results/`（JSON + `sn_list.txt`），可选由 sidecar 上传到产测系统。

大板约 **1.5~3s** 出结果；小板不切片单帧命中约 **1.2s**；识别到铭牌变化的反应约 **1~2s**。

---

## 2. 硬件 / 网络环境

### 2.1 网络拓扑（双网卡，各走一路）

板子有**两张网卡、两个互不相干的网络**，这是整套部署能同时"拉摄像头"和"上传产测"的前提：

```
 公司网 10.80.40.0/24                 相机网 192.168.1.0/24
 路由器 10.80.40.254                  路由器 / 交换机
│                                     │
│ eth0  10.80.40.53                   │ eth1  192.168.1.200
│                                     │
└────────────┬────────────────────────┘
             │
┌────────────┴───────┐
│  SE9 / BM1688 板子 │
└────────────┬───────┘
             │
┌────────────┴────────────────────────┐
 eth0: 管理 / 上传                    eth1: 只拉 RTSP 流
 ├─ SSH linaro@10.80.40.53            ├─ 相机1 正面 192.168.1.9
 └─ 上传 .57:8099                     └─ 相机2 背面 192.168.1.8
```

| 网卡 | 地址 | 用途 |
|------|------|------|
| `eth0` | `10.80.40.53/24`（DHCP，`default via 10.80.40.254`） | SSH 管理、上传到 `.57` 产测系统 |
| `eth1` | `192.168.1.200/24`（静态，**无网关**） | 只跑 RTSP 拉流，两路相机都在这条 |

> ⚠️ **eth1 绝不能设默认网关**。它一设网关就会和 eth0 抢默认路由，公司网的 SSH/上传全断。现在的配置是纯 `192.168.1.0/24 dev eth1 scope link` 主机路由，是正确的。摄像头侧也不需要上网，互不影响。
>
> 用 `ip route get 192.168.1.9` 可确认走的是 eth1（应回 `dev eth1 src 192.168.1.200`）。

### 2.2 设备清单

| 项 | 值 |
|----|----|
| 板子 | SE9 / BM1688，SSH `linaro@10.80.40.53`（密码 `linaro`，sudo 同） |
| 摄像头1（正面） | `rtsp://192.168.1.9:8554/live0`，4K(3840×2160) HEVC，识别主路 |
| 摄像头2（背面） | `rtsp://192.168.1.8:8554/live0`，**仅双路用**，只拉流不识别 |
| 摄像头特性 | **单客户端源**：任一客户端断开即退出、不自愈。断流会卡死，必须强制 TCP、常驻单连接、绝不主动断 |
| 已测板卡 | 大板 `BCCC8F25062000014` / `BCCC8F25062000011` / `BCAC6C25040200956`；小板 `BCCW6N24070100601` |
| 模型 | `ch_PP-OCRv4_{det,rec}_int8_2core.bmodel`（PaddleOCRv4，BM1688 int8 双核） |

> ⚠️ **铁律 1（摄像头）**：重启/停止服务会断开 → **杀死单客户端源**。如需重启服务，必须**同时重启摄像头推流**，否则拉不到流。**双路时这条对两台相机都成立**，两路都要重启。
>
> ⚠️ **铁律 2（摆放）**：**SN 贴纸面必须朝相机、摆画面右上区**（小板像好帧 cx≈0.62 / cy≈0.30）。摆错（芯片丝印面朝上）→ 整幅画面无 SN → 必漏识，再调裁剪也没用。

### 2.3 关于"配置文件"

**本项目没有配置文件**——没有 `.json`/`.yaml`/`.ini`/`.env`，所有行为都由**命令行参数**决定。落地方式只有两种：

1. `deploy/sn-monitor*.service` 单元 `ExecStart` 那一行长命令（生产用，改完 `systemctl daemon-reload && systemctl restart <单元>`）；
2. 手动 `python3 sn_monitor.py <参数>`（调试用）。

**换工位/换板型 = 改那行命令，不改代码。** 参数全表见 4.4（档位）与 4.5（其它）。同理 `sn_uploader.py` 也只有 `--url` / `--results-dir` / `--interval` / `--timeout` 四个参数。

---

## 3. 部署

### 3.1 ★ 服务关系与通信流程（V4.0）

```
【板子 10.80.40.53】
  sn-monitor-big.service  识别+上传 (OCR, 不变)
  ├─ live0 (4K HEVC) → OCR → sn_results/
  ├─ sn-uploader → POST .57:8099
  └─ MJPEG :8090 (V3.0, 前端不再使用)

  ★ sn-preview-ffmpeg.service  正面预览
  │  ffmpeg → live1 @ 192.168.1.9:8554 → h264_bm → RTMP → .57:1935/cam_front
  ★ sn-preview-ffmpeg-back.service  背面预览
  │  ffmpeg → live1 @ 192.168.1.8:8554 → h264_bm → RTMP → .57:1935/cam_back
                    │
                    │ RTMP (TCP, H264 byte stream)
                    ▼
【.57 10.80.40.57】
  ★ MediaMTX (start.sh 内置启动, :8889)
  │  RTMP :1935 ← 板子推流
  │  WebRTC :8889 WHEP endpoint
  │  ICE/UDP :8189 媒体传输
  │  零编码 — 只做 RTMP→WebRTC 协议转换
  uvicorn :8099
  ├─ /api/v1/camera/webrtc/config    → 返回配置
  └─ /api/v1/camera/webrtc/whep/{cam} → WHEP代理 → localhost:8889
                    │
                    │ WebRTC (WHEP + UDP/RTP)
                    ▼
【浏览器】
  CameraPreviewView.vue (纯 WebRTC, 无 MJPEG)
  ├─ <video> × 2 (front + back)
  ├─ RTCPeerConnection + WHEP 握手
  └─ ICE 断线自动重连
```

| 疑问 | 答案 |
|------|------|
| WebRTC 和识别是同一进程吗？ | **不是**。ffmpeg 独立拉**子码流** `live1`（768×572 HEVC），与识别占的 `live0`（4K HEVC）互不干扰。 |
| 需要启用几个服务？ | 板子: `systemctl enable --now sn-preview-ffmpeg sn-preview-ffmpeg-back`。.57: `./start.sh`。 |
| uploader 崩了影响识别吗？ | **不会**。独立进程，`Restart=always` 自行恢复。 |
| ffmpeg 推流崩了影响识别吗？ | **不会**。独立进程拉子码流，与识别的主码流无关。 |
| h264_bm 能用 pipe 输入吗？ | **不能**。只接受 BM1688 硬件解码器输出的 BM-native 帧，必须用 `-i rtsp://...`。 |

### 3.2 代码部署（板子上没有 git 仓库，走 scp）

> ⚠️ **板上 `/data/soph_SN/` 是扁平手工部署目录，不是 git 工作区**（`/data` 下唯一的 `.git` 是 `sophon-demo/` 自带的）。
> 所以**板上不能 `git pull`**，更新代码一律用 scp。

```bash
# 在工作站仓库根目录执行（release 分支）
scp sn_monitor.py sn_preview_embed.py sn_uploader.py linaro@10.80.40.53:/data/soph_SN/
scp -r sncore linaro@10.80.40.53:/data/soph_SN/

# 语法自检（板上没有 tools/，别写 tools/*.py）
ssh linaro@10.80.40.53 'cd /data/soph_SN && python3 -m py_compile sn_monitor.py sn_preview_embed.py sn_uploader.py sncore/*.py && echo OK'

# 生效（★ 记得同时重启摄像头推流，铁律 1）
ssh linaro@10.80.40.53 'echo linaro | sudo -S systemctl restart sn-monitor-big'
```

> 只改了 `.py` 就不用动 systemd 单元；只有 `deploy/*.service` 变了才按 3.3 重装。
> 部署前建议先备份板上现网文件（板上现有 `sn_monitor.py.bak_*` / `README.md.bak_*` 就是这个习惯留下的）。

### 3.3 ★ systemd 单元装到 `/etc/`（唯一权威说明，别处不再重复）

`deploy/` 里的 `.service` 是**模板**，**必须拷进 `/etc/systemd/system/` 才会被 systemd 认识**——
留在 `/data/soph_SN/deploy/` 只是备份，没有任何作用。

```bash
# ① 拷单元（三个可以一次全拷，启用谁看工位）
sudo cp /data/soph_SN/deploy/sn-monitor.service \
        /data/soph_SN/deploy/sn-monitor-big.service \
        /data/soph_SN/deploy/sn-uploader.service /etc/systemd/system/
sudo systemctl daemon-reload

# ② 启用（开机自启 + 立即启动）
sudo systemctl enable --now sn-monitor-big   # 识别（大板工位）。小板工位改用 sn-monitor，二选一！
sudo systemctl enable --now sn-uploader      # 上传 sidecar（可选，见 4.7）
```

| 单元 | 板型 | ExecStart 关键点 | 日志 |
|------|------|------------------|------|
| `sn-monitor.service` | 小板 | 默认 small 档，**单路** | `logs/sn_monitor.service.log` |
| `sn-monitor-big.service` | 大板 | 写死 `--profile big` + **`--rtsp2` 双路** | 同上 |
| `sn-uploader.service` | 通用 | shell 探测脚本位置（先 `tools/` 后根目录） | `journalctl -u sn-uploader -f` |

> - 两个 monitor 单元带 `Conflicts=`，**同一路摄像头只能起一个**；enable 其一会自动停另一个。
> - ⚠️ **装单元会覆盖 `/etc` 里手改过的版本**（比如你手改过摄像头 IP）。改过的工位先备份：
>   `sudo cp /etc/systemd/system/sn-monitor-big.service /etc/systemd/system/sn-monitor-big.service.local_bak`
>   覆盖后务必确认 `ExecStart` 里仍带着需要的 `--rtsp2`。
> - **`deploy/` 里没有 `sn-preview.service`，这是刻意的** —— 旧独立预览服务已停用，不放单元就没人会误启用它。

### 3.4 目录结构（仓库 vs 板上，路径**按板上实况**取）

```
仓库（sn_monitor_bm1688/，git 跟踪 = 可直接部署的代码）
  sn_monitor.py          # ★ 唯一入口：拉流 + 状态机 + 档位裁剪 + OCR + 投票 + 落库 + 预览
  sn_preview_embed.py    # ★ V3.0 调机预览模块：被 sn_monitor.py 内嵌 import，共享同一路解码
  preview_service.py     # V1/V2 独立预览服务，已停用保留
  preview_service_README.md  # 上面那个脚本自己的 README（停用状态 / 保留接口 / 为什么停）
  sncore/                # SN 处理内核
    sn_extract.py        #   SN 提取: 多帧投票 + 格式校验 + 掉字子序列合并
    result_gate.py       #   结果二次确认门(ResultGate)
    sn_line.py           #   SN 行版式辅助（历史遗留，当前无任何 import，可忽略）
  tools/                 # 辅助工具（不参与识别主链路，按需运行）
    sn_uploader.py       #   命中结果 sidecar：上传 SN+命中帧到 .57 产测系统
    profile_probe.py     #   参数扫描工具：新模组快速定档
  deploy/                # systemd 单元（模板，拷到 /etc 用）
    sn-monitor.service / sn-monitor-big.service / sn-uploader.service
    mediamtx.service / sn-preview-ffmpeg.service  # ★ V4.0 WebRTC 管道
  docs/                  # 分册文档（CHANGELOG + features/ + tools/）
  README.md              # 本文档

板上运行时（/data/soph_SN/）—— ⚠️ 扁平部署，**没有 tools/ 子目录**
  sn_monitor.py / sn_preview_embed.py / sncore/
  sn_uploader.py         # ← 仓库在 tools/，板上在根
  preview_service.py + preview_service_README.md   # 停用保留
  deploy/                # 三个单元（拷到 /etc 后也没用了，留作对照）
  sophon-demo/sample/PP-OCR/   # 官方框架 + models/BM1688/*.bmodel + datasets 字典
  logs/  sn_results/  debug/   # 运行时产物（日志 / 结果 / 漏检留证）
/etc/systemd/system/sn-monitor.service       # ← deploy/ 拷入（小板，单路）
/etc/systemd/system/sn-monitor-big.service   # ← deploy/ 拷入（大板，big 档 + 双路）
/etc/systemd/system/sn-uploader.service      # ← deploy/ 拷入（上传 sidecar）
/etc/systemd/system/mediamtx.service         # ★ V4.0 新增 MediaMTX
/etc/systemd/system/sn-preview-ffmpeg.service # ★ V4.0 新增 ffmpeg 推流
/opt/mediamtx/mediamtx                       # ★ V4.0 新增 MediaMTX 二进制 (ARM64, ~28MB)
```

> **`profile_probe.py` 不在板上**（未随部署上板），要用得先 scp 到板子根目录，见 [docs/tools/profile-probe.md](docs/tools/profile-probe.md)。
> 模型 `*.bmodel`、官方框架 `sophon-demo/`、运行时产物 `logs/`、`sn_results/`、`debug/` 全部 `.gitignore`，按 3.5 单独部署。

### 3.5 模型 / 框架部署（PP-OCR bmodel + sail，一次性）

识别依赖官方 PP-OCR 框架与 BM1688 的 `*.bmodel`（**不在 git 里**）。首装步骤（详见 wiki《PP-OCR 搭建及测试》：
<https://wiki.sophgo.com/pages/viewpage.action?pageId=228896451>）：

```bash
# ① 克隆 sophon-demo 并下载 PP-OCR 模型（bmodel 随 download.sh 拉取）
cd /data/soph_SN
git clone https://github.com/sophgo/sophon-demo.git
cd sophon-demo/sample/PP-OCR
chmod -R +x scripts/
./scripts/download.sh

# ② 安装依赖 + sophon-sail（板子上 OCR 推理靠 sail）
pip3 install -r python/requirements.txt
pip3 install opencv-python-headless
pip3 install dfss && python3 -m dfss --install sail
echo 'export LD_LIBRARY_PATH=/opt/sophon/sophon-sail/lib/:$LD_LIBRARY_PATH' >> ~/.bashrc
source ~/.bashrc

# ③ 验证模型已就位
ls -la models/BM1688/          # 应见 ch_PP-OCRv4_{det,rec}_*_2core.bmodel

# ④ 用官方样例图快速验证模型能跑（wiki 第 4 步）
cd /data/soph_SN/sophon-demo/sample/PP-OCR/python
python3 ppocr_system_opencv.py \
    --input=../datasets/train_full_images_0 --batch_size=1 \
    --bmodel_det=../models/BM1688/ch_PP-OCRv4_det_fp16_2core.bmodel \
    --bmodel_rec=../models/BM1688/ch_PP-OCRv4_rec_fp16_2core.bmodel \
    --dev_id=0 --img_size "[[640,48],[320,48]]" \
    --char_dict_path=../datasets/ppocr_keys_v1.txt
# ⑤ 换 --input=/data/soph_SN/sn_test 可用自己的铭牌图再验一遍
```

> 本项目生产用 **int8 双核** `ch_PP-OCRv4_{det,rec}_int8_2core.bmodel`（比 fp16 快）；wiki 验证步骤用 fp16 只为跑通。`download.sh` 会把 int8/fp16/fp32 都拉下来。

---

## 4. 使用

### 4.1 ★ 两步跑起来（V3.0.1 起 uploader 自动跟随）

**先决**：板子 SSH `linaro@10.80.40.53`；摄像头 `rtsp://192.168.1.9:8554/live0`；工作目录 `/data/soph_SN`。

```bash
# ① 装单元（首次部署；三个可以一次全拷，启用谁看工位）
sudo cp /data/soph_SN/deploy/sn-monitor.service \
        /data/soph_SN/deploy/sn-monitor-big.service \
        /data/soph_SN/deploy/sn-uploader.service /etc/systemd/system/
sudo systemctl daemon-reload

# ② 一条命令启用（识别+预览+上传全部拉起）
sudo systemctl enable --now sn-monitor-big      # 大板。小板工位改用 sn-monitor

# 之后放板即自动「识别 → 落盘 → 上传」，无需再敲任何命令。看实时日志：
tail -f /data/soph_SN/logs/sn_monitor.service.log
journalctl -u sn-uploader -f                    # 上传日志（独立）
```

> 换板型：`sudo systemctl disable --now sn-monitor && sudo systemctl enable --now sn-monitor-big`（反之亦然）。
> 两个单元互斥（`Conflicts=`），enable 其一会自动停另一个。
> **大板工位默认就是双路**（`--rtsp2` 已写死在单元里）；只装一台相机的大板工位，把那行末尾的 `--rtsp2 ...` 删掉即回到单路。
> **小板单元是单路**，要双路得自己往末尾追加 `--rtsp2 ...`，再 `daemon-reload && restart`。详见 [docs/features/dual-camera.md](docs/features/dual-camera.md)。
>
> **临时关上传**：`sudo systemctl stop sn-uploader`（不影响识别）。永久关：`sudo systemctl mask sn-uploader`。

### 4.2 常用运维命令

把 `<svc>` 换成实际的单元名（`sn-monitor` / `sn-monitor-big`）：

```bash
sudo systemctl start   <svc>      # 启动
sudo systemctl stop    <svc>      # 停止（★ 会杀源，需配合摄像头重启）
sudo systemctl restart <svc>      # 重启（★ 务必同时重启摄像头推流）
sudo systemctl status  <svc>
tail -f /data/soph_SN/logs/sn_monitor.service.log     # 识别日志（不进 journalctl）
journalctl -u sn-uploader -f                          # 上传日志
```

单元配置要点：`--idle 0`（永不超时）、`KillSignal=SIGINT`（干净 TEARDOWN 保住下次可拉流）、`Restart=always`。

### 4.3 手动前台跑（调试，最直观）

```bash
sudo systemctl stop sn-monitor        # 先停服务让出摄像头源（手动跑也抢同一个源，不能并存）
cd /data/soph_SN

# 小板（默认档 small，不带 --profile）
sudo python3 sn_monitor.py --rtsp rtsp://192.168.1.9:8554/live0 --idle 0
# 大板（必须带 --profile big）
sudo python3 sn_monitor.py --rtsp rtsp://192.168.1.9:8554/live0 --idle 0 --profile big
# 大板 + 双路（V2.1 正/反两面）
sudo python3 sn_monitor.py --rtsp rtsp://192.168.1.9:8554/live0 --idle 0 --profile big \
     --rtsp2 rtsp://192.168.1.8:8554/live0
# 只想要识别、不要预览（等同 V2.1 行为）
sudo python3 sn_monitor.py --rtsp rtsp://192.168.1.9:8554/live0 --idle 0 --no-preview
# ★ 手动跑也会杀源：启动后 / Ctrl-C 结束后，都要同时重启摄像头推流（双路时两台都要）

# 如需上传（可选，另开一个终端；不上传就不用起）
sudo python3 /data/soph_SN/sn_uploader.py --url http://10.80.40.57:8099/api/v1/captures --interval 3
```

> 大板/小板唯一区别就是 `--profile`（一次切换裁剪几何/上采样/确认门，见 4.4）。
> 结果落在 `/data/soph_SN/sn_results/`（`sn_<SN>_<ts>.json` + 命中帧 `.jpg`）。

### 4.4 档位系统（V2.0.1 核心）

一个 `--profile` 打包一档参数（裁剪几何 + 切片 + 抽帧 + 确认门）。显式 CLI 标志仍覆盖档位。

| 参数 | small(默认) | big | 含义 |
|------|------|------|------|
| `crop_w × crop_h` | 0.40 × 0.35 | 0.30 × 0.40 | 各向异性中心裁剪(宽比×高比)，只留铭牌带 |
| `crop_cy` | 0.32(偏上) | 0.50(居中) | 裁剪垂直锚点；小板 SN 在画面上带 |
| `tile_grid` | (1,1) | (1,1) | **不切片单帧**(见 6.2 ⑩) |
| `tile_up` | 3.0 | 2.5 | 裁剪块上采样倍数 |
| `frames` | 2 | 2 | 单次识别抽帧数(跨帧投票修掉字) |
| `confirm` | 2 | 1 | 连续同一 SN 达此次数即落库 |
| `sn_min_len` | 17 | 17 | SN 最短长度门(挡短串误报) |
| `poll` | 1.0 | 0.4 | 侦测轮询间隔 |
| `rescue` | True | False | 救援旋转遍 |

覆盖档位的显式标志：`--crop-w` / `--crop-h` / `--crop-cy` / `--center-crop`(宽高同设) / `--frames`。

### 4.5 其它关键参数

| 参数 | 默认 | 说明 |
|------|------|------|
| `--rtsp` | (必填) | 摄像头1（正面）RTSP 地址，识别主路 |
| `--rtsp2` | (空) | 摄像头2（背面）RTSP；**空=单路**（=V2.0.2） |
| `--rtsp2-grab-wait` | 0.5 | 命中时从第二路取最新帧的最大阻塞秒 |
| `--profile` | small | 档位: `small`/`big`(见 4.4) |
| `--poll` | 1.0 | 侦测轮询间隔(秒)，越小反应越快 |
| `--ocr-idle-unload` | 30.0 | 模型闲时释放：距上次识别超此秒数释放模型省 NPU，下块板懒加载。`0`=永不释放(常驻) |
| `--dt` | 0.05 | 画面运动阈值；同时用作"同板去抖"阈值(与上次识别帧差<dt 视为同板不重识) |
| `--n` | 3 | L3 补帧张数(跨帧投票) |
| `--fi` | 1.0 | L3 抽帧间隔(秒) |
| `--confirm` | 2 | 连续同一 SN 达此次数即确认落库 |
| `--confirm-score` | 0.95 | 单次达此分且满帧命中即强确认(立即上报) |
| `--sn-min-len`/`--sn-max-len` | 15 / 20 | SN 长度窗(profile 会把 min 设 17) |
| `--sn-prefix` | (不限) | SN 前缀白名单，逗号分隔 |
| `--mi` | 5.0 | 异常退避间隔(秒)，仅识别异常时用 |
| `--no-rescue` | — | 关闭救援旋转遍 |
| `--no-save-miss` | — | 关闭漏检留证 |
| `--no-preview` | 关 | **一键回退 V2 行为**：完全不启预览，识别逻辑逐字节等同 |
| `--preview-port` | 8090 | 预览 Flask 端口 |
| `--preview-fps` | 8.0 | 预览编码**上限**（不是保证值），识别不受影响 |
| `--preview-res` | 4k | `4k`/`1080p`/`720p`/`360p`，网页上也能实时切 |
| `--preview-cam` | both | `both`/`front`/`back`，只预览哪几路 |
| `--preview-jpeg-quality` | 80 | 仅记录，当前 Bmcv 编码质量不可调（改这个值不生效） |

> 稳定性兜底参数 `--wedge-*` / `--stale-*` 一般不用动。预览参数完整说明见 [docs/features/preview-v3.md](docs/features/preview-v3.md)。

### 4.6 更新脚本到板子

见 **3.2 代码部署**：scp 上板 → `py_compile` 自检 → `systemctl restart <svc>`（★ 记得同时重启摄像头推流）。
只有 `deploy/*.service` 变了才需要按 3.3 重装单元 + `daemon-reload`。

### 4.7 功能分册索引（正文已搬走，这里只留入口）

| 主题 | 一句话 | 分册 |
|------|--------|------|
| **上传 sidecar** | `sn_uploader.py` 怎么监视目录、怎么去重、为什么"首次启用会回灌历史"、为什么要 root | [docs/features/uploader.md](docs/features/uploader.md) |
| **双路摄像头** | 两路为什么"分开拉流不合并"、正反靠时间戳复制配对、`.57` 侧升级要 `ALTER TABLE` | [docs/features/dual-camera.md](docs/features/dual-camera.md) |
| **调机预览 V3.0 (MJPEG)** | 为什么必须内嵌（单客户端源）、6 个参数、实测编码耗时、三级回退路径 | [docs/features/preview-v3.md](docs/features/preview-v3.md) |
| **调机预览 ★ V4.0 (WebRTC)** | 子码流独立拉流 + `h264_bm` 硬件编码 + MediaMTX 推 WebRTC，延迟 <500ms。部署见下方 4.8 |  |
| **参数扫描工具** | 新模组怎么快速定档、`profile_probe.py` 怎么用、输出怎么读 | [docs/tools/profile-probe.md](docs/tools/profile-probe.md) |
| **版本演进** | V0.8 → V3.0 逐版改了什么、每个坑的根因 | [docs/CHANGELOG.md](docs/CHANGELOG.md) |

### 4.8 ★ V4.0 WebRTC 预览部署'):c.find('## 5. 排障速查')]

new = """### 4.8 ★ V4.0 WebRTC 预览部署

#### 完整通信流程

```
板子 10.80.40.53                              .57 10.80.40.57                    浏览器
sn-preview-ffmpeg
  ffmpeg -i live1                             mediamtx (start.sh)
   -> h264_bm HW编码 (<5ms)                      RTMP :1935 <- 接收推流
   -> RTMP push ------------------------------>   WebRTC :8889
                                                   ICE/UDP :8189
sn-preview-ffmpeg-back
  ffmpeg -i live1(.8)                           uvicorn :8099
   -> h264_bm                                    /webrtc/config
   -> RTMP push ------------------------------>   /webrtc/whep/{cam} <- 浏览器 POST
                                                    -> proxy -> localhost:8889
                                                                               CameraPreviewView.vue
                                                                                 RTCPeerConnection
                                                                                 WHEP POST SDP offer
                                                                                 setRemoteDescription(answer)
                                                                                 <video> x 2
                                                                                 ICE disconnect -> auto retry
```

关键设计决策:
- **子码流独立**: ffmpeg 拉 `live1` 子码流 (768x572 HEVC, 与识别主码流 `live0` 不同连接), 不占识别资源
- **MediaMTX 放 .57**: 与浏览器同源网络 (10.80.40.x), ICE 不跨网段
- **WHEP 代理**: .57 后端转发 SDP -> 本地 MediaMTX (127.0.0.1:8889), 避免浏览器直连板子的跨域问题
- **h264_bm 限制**: 只接受 BM1688 硬件解码器输出的 BM-native 帧, 不能用 pipe:0 软件管道

#### 板子端部署

```bash
# 部署 ffmpeg 推流单元
sudo cp /data/soph_SN/deploy/sn-preview-ffmpeg.service /etc/systemd/system/
sudo cp /data/soph_SN/deploy/sn-preview-ffmpeg-back.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sn-preview-ffmpeg sn-preview-ffmpeg-back
```

#### .57 服务器端部署

```bash
# 1) 下载 MediaMTX (仅首次, x86_64)
cd /tmp && wget https://github.com/bluenviron/mediamtx/releases/download/v1.8.0/mediamtx_v1.8.0_linux_amd64.tar.gz
sudo mkdir -p /opt/mediamtx && sudo tar xzf mediamtx_v1.8.0_linux_amd64.tar.gz -C /opt/mediamtx/

# 2) 配置文件: product_test/mediamtx.yml
#    rtmp: yes, rtmpAddress: :1935
#    webrtc: yes, webrtcAddress: :8889
#    srt: no  (避免端口 8000 冲突)

# 3) 启动: start.sh 已集成
#    pkill -f mediamtx 2>/dev/null  # 杀旧进程
#    /opt/mediamtx/mediamtx mediamtx.yml &  # 后台启动
#    trap "kill $MEDIAMTX_PID" EXIT  # 随 start.sh 退出自动清理
cd /media/cvitek/xiaohao.liu/product_test
./start.sh
```

#### 验证

```bash
# .57 MediaMTX 流状态 (应该 ready=true)
curl -s http://127.0.0.1:9997/v3/paths/list | python3 -m json.tool

# 板子 ffmpeg 服务
systemctl is-active sn-preview-ffmpeg sn-preview-ffmpeg-back
# 应: active active

# 浏览器: F12 控制台
# 应: WebRTC front connected / WebRTC back connected
```

> **排障**: 如果一直"WebRTC 连接中...", 先确认 .57 的 `./start.sh` 已执行, 再检查 `cam_front ready=true`。## 5. 排障速查

| 现象 | 排查 |
|------|------|
| 拉不到流 / 一直重连 | 摄像头推流是否在推？eth1 `carrier=1`？`ping 192.168.1.9` 通？重启服务后是否重启了摄像头？ |
| 小板漏识 | **首查摆放**：SN 贴纸面是否朝相机、在右上区？诊断法：拉全幅 miss 帧**网格逐格 OCR** 看 SN 真实 cx/cy（`debug/` 有留证）。若整幅无 SN 即摆错。 |
| 大板漏识 | 确认加了 `--profile big`(裁剪几何不同)；留证在 `debug/` |
| 数字错读(如 2→7) | 已由 V2.0.1 不切片修复；若复现，确认 profile 的 `tile_grid` 是 (1,1) |
| 同一板反复识别 | 已由同板去抖修复；若仍有，适当调大 `--dt` |
| 反应慢 | 调小 `--poll`(如 0.5) |
| 模型释放太快/太慢 | 调 `--ocr-idle-unload`(慢→调小，常驻→设 0) |
| 误报别的字段 | 收紧 `--sn-min-len/max-len` 或加 `--sn-prefix` |
| **`:8090` 打不开 / 预览起不来** | 是不是加了 `--no-preview`？Flask 装了没？日志里应有 `📺 预览已启动` 或一行警告——预览起不来不影响识别 |
| **预览画面卡顿** | 网页工具栏切 `1080p`（识别用的图不受影响）。或切换到 **WebRTC 模式**（见 4.8） |
| **WebRTC 黑屏无画面** | ① `systemctl is-active mediamtx sn-preview-ffmpeg` — 两个都在跑？② `journalctl -u sn-preview-ffmpeg -n 5` — ffmpeg 是否报错？③ 浏览器控制台是否有 WebRTC ICE 连接失败？（办公网到 `10.80.40.53:8889` 是否可达？） |
| **双路：第二路没接上** | 启动日志有 `⚠ 第二路 ... 暂不可达, 本次降级为单路` = 正常降级。查：相机2 是否接在同一台路由器/交换机？`ping 192.168.1.8`？`ip route get 192.168.1.8` 是否回 `dev eth1`？端口 `8554` 可连？ |
| **双路：背面图不变/是旧画面** | 相机2 推流是否真在动？背面**无断线自愈**，掉线需重启服务。详见 [dual-camera.md](docs/features/dual-camera.md) |
| **双路：卡片只显示正面** | ① 上传是否带 `side`？② `.57` 库里那两行 `captured_at` 是否真的一致？③ 老库是否忘了 `ALTER TABLE ... ADD COLUMN side`？见 [dual-camera.md](docs/features/dual-camera.md) |
| **网页上时间与筛选日期差一两天** | 正常：卡片显示**抓拍时间**，筛选按**入库时间** |
| **上传器灌了一堆重复** | 预期行为：uploader 只做**文件级**去重，不管内容/业务重复。原因与善后见 [uploader.md](docs/features/uploader.md) |

日志与结果：

```
tail -f /data/soph_SN/logs/sn_monitor.service.log
cat   /data/soph_SN/sn_results/sn_list.txt
ls    /data/soph_SN/debug/          # 漏检/待确认留证
```

---

## 6. 实现原理

### 6.1 整体流水线

```
RTSP 4K/TCP ─► 后台线程持续 read 排空缓冲(只留最新帧)
│
▼
  主循环 poll(1s) ─► 质量门
      │ 稳定 + 有铭牌 + 非同板
      ▼
  ★抓背面最新帧(V2.1, 见⑪)
      │
      ▼
  按档位裁剪 (+不)切片 ─► 单帧识别 ─► 多帧投票 + 格式校验
      │
      ▼
  二次确认门 ─► 落库(命中则背面帧一并落盘)
      │
      ▼
  (可选) sn-uploader sidecar
      │
      ▼
  POST .57 product_test ─► DB + 前端「SN 抓拍」页
        正/反按 sn + captured_at 配对成一张卡片
```

**双路时是两条并行的取帧链，只有"抓一帧"这一处交汇**（背面不参与 OCR）：

```
RTSP-cam1(正面) ─► FrameReader1 ─► 主循环识别(裁剪/OCR/投票/确认)    ┐
        └─ 命中时: 把正面 ISO 时间戳                                 │
           复制给背面(两行 captured_at 相同)                         │
RTSP-cam2(背面) ─► FrameReader2 ─► 只排空缓冲, 无 OCR                ┘
```

**预览是第三条旁路**（V3.0）：从上面这两条链**已解码的帧**里 `_peek` 取最新帧 → Bmcv 硬件编 JPEG → MJPEG，
**不新增任何 RTSP 连接**：

```
【板子 10.80.40.53】                        【.57 10.80.40.57】        【浏览器】
FrameReader1/2 ─ 已解码帧
   └► _peek(非阻塞, 拿不到就跳过)
       └► Bmcv 编 JPEG ─► :8090 ──同源中继──► 8099 ─────────────────► 网页
                          (板子)   /api/v1/camera/*  (产测后端)       <img src=...>
```

> ⚠️ **`:8090` 和 `:8099` 不是同一个服务，也不在同一台机器上**：
> `:8090` 是**板子**上的预览服务（V3.0 起内嵌在 `sn_monitor.py` 里，随识别服务启停）；
> `:8099` 是 **`.57`** 上的产测系统后端（FastAPI/uvicorn）。
> **浏览器只跟 `.57` 的 `:8099` 说话**，它自己不直连板子 —— 由 `.57` 的 `/api/v1/camera/*` 接口做一次中继转发。
> 为什么绕这一跳：浏览器直连板子要开跨域，而且板子网段对办公网不直接暴露，每换一台调机电脑都得改配置；中继后换电脑零配置。
> 板子地址写在 `.57` 的 `backend/api/camera.py` 里（`BOARD_BASE = http://10.80.40.53:8090`，可用环境变量 `PREVIEW_BOARD_BASE` 覆盖）。

### 6.2 关键设计决策（每条都对应踩过的坑）

**① 强制 TCP + 常驻单连接，绝不主动断。**
摄像头是单客户端源，UDP 会脏断卡死、断开即源退出。所以全程一条 TCP 持久连接，永不主动 release。

**② 后台线程持续 read()，主循环只取最新帧。**
FFMPEG 后端 `BUFFERSIZE=1` 无效，直接 read 会回放缓冲里的旧帧。后台线程不停排空解码缓冲，主循环 `latest()` 永远拿最新解码帧，根治旧帧回放。cap 的 read/release 全在该线程做，交接靠 `set_cap`，杜绝跨线程释放崩溃。

**③ 瞬时解码错误不重连（断"重连 churn"自激）。**
HEVC 常见 `PPS id out of range`（坏包）、`Could not find ref`/`Error constructing RPS`（丢参考帧）都是**瞬时**错误，解码器等下一个 IDR 关键帧自愈。若一见错就 release 重连，会从 GOP 中间重进 → 又一片 ref-error → 重连 churn 自激螺旋。策略：无新鲜帧且解码错在刷时**保持同连接等 IDR**，只有无解码活动持续无帧或超死线(`--wedge-dead-s`)才判真坏流重连。

**④ 各向异性裁剪 + 垂直锚点（V2.0.1 档位）。**
不同板卡 SN 位置不同：小板 SN 在画面**偏上、略偏右**（cy≈0.30），大板居中。用 `crop_w×crop_h`(宽比≠高比) + `crop_cy`(垂直锚点) 只留铭牌带，把板上其它干扰印刷字段（如小板的 `AAAJ2B224AL04`）裁掉，候选池只剩干净 token。

**⑤ 模型闲时释放 + 事件懒加载。**
模型启动不加载，首个事件懒加载(~0.4s)；活跃期常驻(命中秒回)，距上次识别超 `--ocr-idle-unload`(默认 30s) 自动释放省 NPU 内存。

**⑥ 侦测轮询与退避解耦（降延迟）。**
状态机轮询独立成 `--poll`，放板到识别反应 ~1~2s；`--mi`(5s) 只留作识别异常退避。

**⑦ 同板去抖。**
记住上次识别的画面帧 `recog_ref`；locked 状态下当前稳定画面与它 `fdiff<dt` 判"同板在位"，跳过重识。根治**静止板被噪声/OSD 秒跳字(`1970-01-01 …`)的假运动反复触发**——该问题同时导致模型迟迟不释放，一并修好。

**⑧ 多帧投票 + 格式校验 + 掉字合并（sncore.sn_extract）。**
跨帧对候选串逐字对齐投票；长度窗(profile 设 min=17)+字符集校验挡掉误报；掉字帧用子序列合并修复(如 `CCW6N→BCCW6N`)。

**⑨ 结果二次确认门（sncore.result_gate.ResultGate）。**
vote 出的 SN 不立即落库：需连续 `--confirm` 次同一 SN，或单次分≥`--confirm-score`(0.95) 且满帧命中(强确认)才上报；已上报的同 SN 不重复落库。压制单次误报。

**⑩ 裁剪后"不切片单帧"最准（V2.0.1，治好 2→7 错读）。**
裁剪收紧后干扰字段已被裁掉，此时**不切片**(`tile_grid=(1,1)`)让 PP-OCR 的 det 自己把 `SN:` 与编码分成**合适大小的框**，编码框长宽比不超标 → 数字读干净。
> **2→7 根因**：`tile_grid=(1,2)` 切片带 0.3 重叠、切片够宽时把整行 `SN:BCCW6N24070100601` 装进**一个框** → 撞 rec bmodel 宽度天花板 `img_size=[[640,48]]`（长宽比 >13.3 被压扁，曾观测 14.07）→ 数字畸变 2→7，且错读帧分更高被投票选中。改 1×2→1×1 不切片后离线 36/36 确定性读对。

**⑪ 双路：第二路只排空不识别，配对靠"时间戳复制"而非"时间同步"（V2.1）。**
背面那路复用同一个 `FrameReader`（后台线程持续 read 排空缓冲），但**一帧 OCR 都不跑**——因为背面只要"命中瞬间的那一张图"，不需要识别，也就省下整个 NPU 开销（BM1688 两个 vdec 核各扛一路 4K HEVC 解码，解码没问题）。
抓帧时机选在**识别起点**（`frames[0]` 那一刻）而非命中之后：OCR 本身约耗时 1s，若等确认命中再抓，背面可能已经换板，拍到的就不是同一块板了。
配对**不做两路时间同步**（两路独立连接、没有共同时钟）：命中后把**正面的 ISO 时间戳原样赋给背面**再落盘。所以 DB 里两行 `captured_at` 一致是"写出来的"而非"测出来的"——配对必然成立，不依赖任何同步精度。
`--rtsp2` 未配或拉不到流 → 构造 `reader2=None`，全程 `if reader2` 守卫，**单路代码路径一字不改**，这是"零影响"的实现保证；退出时对第二路也走对称的 `set_cap(None)`+`wait_released()`（单客户端源必须发 TEARDOWN，否则背面相机下次拉不到流）。

**⑫ 预览内嵌而非独立进程（V3.0）。**
单客户端源决定了预览**不能自己再连一次 RTSP**，只能用识别进程已解码的帧。所以 `sn_preview_embed.py` 被 `sn_monitor.py` **import 内嵌**，通过 `_peek()` **非阻塞**取最新帧（拿不到就跳过本轮，绝不阻塞识别主循环），Bmcv 硬件编 JPEG 后以 MJPEG 推给网页。
整块是**纯加法**：`sn_monitor.py` V2.1→V3.0 只有 **+31 行、0 删除**（try-import 兜底 + 6 个 CLI 参数 + 一处 `attach()` + `finally` 里一处 `preview_stop()`）。Flask 是**懒导入**，缺 Flask 时模块的纯逻辑仍可 import/单测，`sn_monitor.py` 的 import 兜底接住，只打一行「预览不可用」，识别照常。详见 [docs/features/preview-v3.md](docs/features/preview-v3.md)。
