# SN 监控系统 (RTSP → PP-OCR 序列号识别)

SE9 / BM1688 算能板用 eth1 接一台路由器/交换机组成小局域网，从相机拉 RTSP 流，自动侦测板卡铭牌、识别序列号(SN)并落库，可选上传到 `.57` 产测系统。当前发布版本 **V4.0.1**。

> 本 README 只讲**环境怎么部署、IP 改了动哪里、怎么快速跑起来**。
> 使用参数、排障、实现原理、版本演进等细节已搬到 [`docs/`](docs/)，见文末 §6 分册索引。

---

## 1. 它做什么

摄像头对着流水线上的板卡，人放一块板 → 程序自动：

1. 侦测到画面稳定且出现铭牌 →
2. 对铭牌区域按**档位**裁剪 + OCR，跨帧投票 + 格式校验提取 SN →
3. 二次确认后写入 `sn_results/`（JSON + `sn_list.txt`），可选由 sidecar 上传到产测系统。

大板约 **1.5~3s** 出结果；小板不切片单帧命中约 **1.2s**；识别到铭牌变化反应约 **1~2s**。

四种形态：

- **单路识别（默认）**：eth1 挂一台相机对正面，识别 SN。
- **双路（V2.1，加 `--rtsp2`）**：再挂第二台对背面，正面命中时同步抓一帧背面图，正/反一并上传、在产测网页成对展示。两路各拉各的流、不合并。
- **调机预览（★ V4.0 WebRTC）**：板子 ffmpeg 独立拉**子码流** `live1` → BM1688 硬件 H264 编码 → RTMP 推到 `.57` → `.57` 的 MediaMTX 转 WebRTC → 前端实时播放，延迟 <500ms。不占识别主码流。部署见 [docs/deployment-mediamtx.md](docs/deployment-mediamtx.md)。
- **调机预览（V3.0 MJPEG，已废弃）**：识别进程内嵌 JPEG → MJPEG 中继，V4.0 已替换。

---

## 2. 快速上手

板子已按 §3 部署好、模型就位后，日常启动就一条命令：

```bash
# 大板（big 档 + 双路 + 预览全跟随）
ssh linaro@<板子IP> 'echo linaro | sudo -S systemctl enable --now sn-monitor-big'
# 小板（single 路）：把 sn-monitor-big 换成 sn-monitor
```

> ⚠️ 两个 monitor 单元互斥，换板型用 `disable --now sn-monitor && enable --now sn-monitor-big`。
> ExecStartPre 自动拉起 uploader + ffmpeg 推流；重启服务务必**同时重启摄像头推流**（铁律 1）。

看结果 / 日志：

```bash
ssh linaro@<板子IP> 'tail -f /data/soph_SN/logs/sn_monitor.service.log'
ssh linaro@<板子IP> 'cat  /data/soph_SN/sn_results/sn_list.txt'
```

`.57` 产测网页侧：浏览器打开 `http://10.80.40.57:8099`，「SN 抓拍」页按 SN/日期查图，「调机预览」页看实时画面。

---

## 3. 环境部署

### 3.1 硬件 / 网络拓扑（双网卡，各走一路）

板子有**两张网卡、两个互不相干的网络**，这是整套能同时"拉摄像头"和"上传产测"的前提：

```
 公司网 10.80.40.0/24                    相机网 192.168.1.0/24
 路由器 10.80.40.254                      路由器 / 交换机
       │ eth0 (板子, 公司网)                    │ eth1 192.168.1.200
       └──────────────┬─────────────────────────┘
              ┌────────┴────────┐
              │ SE9 / BM1688 板子 │
              └────────┬────────┘
   eth0: 管理 / 上传            eth1: 只拉 RTSP 流
   ├─ SSH 管理                  ├─ 相机1 正面 192.168.1.9
   └─ 上传 .57:8099             └─ 相机2 背面 192.168.1.8
```

| 网卡 | 地址 | 用途 |
|------|------|------|
| `eth0` | 公司网 DHCP，`default via 10.80.40.254` | SSH 管理、上传到 `.57` 产测系统 |
| `eth1` | `192.168.1.200/24`（静态，**无网关**） | 只跑 RTSP 拉流，两路相机都在这条 |

> ⚠️ **eth1 绝不能设默认网关**。一设网关就会和 eth0 抢默认路由，公司网 SSH/上传全断。正确配置是纯 `192.168.1.0/24 dev eth1 scope link` 主机路由。
> 用 `ip route get 192.168.1.9` 确认走 eth1（应回 `dev eth1 src 192.168.1.200`）。

### 3.2 设备清单

| 项 | 值 |
|----|----|
| 板子 | SE9 / BM1688，SSH `linaro@<板子IP>`（密码 `linaro`，sudo 同）。板子 IP 见 §4 对照表 |
| 摄像头1（正面） | `rtsp://192.168.1.9:8554/live0`，4K(3840×2160) HEVC，识别主路 |
| 摄像头2（背面） | `rtsp://192.168.1.8:8554/live0`，**仅双路用**，只拉流不识别 |
| 摄像头特性 | **单客户端源**：任一客户端断开即退出、不自愈。必须强制 TCP、常驻单连接、绝不主动断 |
| 已测板卡 | 大板 `BCCC8F25062000014` / `BCCC8F25062000011` / `BCAC6C25040200956`；小板 `BCCW6N24070100601` |
| 模型 | `ch_PP-OCRv4_{det,rec}_int8_2core.bmodel`（PaddleOCRv4，BM1688 int8 双核） |

> ⚠️ **铁律 1（摄像头）**：重启/停止服务会断开 → **杀死单客户端源**。重启服务必须**同时重启摄像头推流**，双路时两台都要重启。
> ⚠️ **铁律 2（摆放）**：**SN 贴纸面必须朝相机、摆画面右上区**（小板像好帧 cx≈0.62 / cy≈0.30）。摆错 → 整幅无 SN → 必漏识。

### 3.3 代码部署（板上没有 git 仓库，走 scp）

> ⚠️ 板上 `/data/soph_SN/` 是扁平手工部署目录，**不能 `git pull`**，更新一律用 scp。

```bash
# 在工作站仓库根目录执行
scp sn_monitor.py sn_preview_embed.py sn_uploader.py linaro@<板子IP>:/data/soph_SN/
scp -r sncore linaro@<板子IP>:/data/soph_SN/

# 语法自检（板上没有 tools/，sn_uploader.py 在根目录）
ssh linaro@<板子IP> 'cd /data/soph_SN && python3 -m py_compile sn_monitor.py sn_preview_embed.py sn_uploader.py sncore/*.py && echo OK'

# 生效（★ 记得同时重启摄像头推流，铁律 1）
ssh linaro@<板子IP> 'echo linaro | sudo -S systemctl restart sn-monitor-big'
```

> 只改 `.py` 不用动 systemd 单元；只有 `deploy/*.service` 变了才按 3.4 重装。

### 3.4 systemd 单元装到 `/etc/`（唯一权威说明）

`deploy/` 里的 `.service` 是**模板**，**必须拷进 `/etc/systemd/system/` 才生效**——留在 `/data/soph_SN/deploy/` 只是备份。

```bash
# ① 拷单元（四个一次全拷，启用谁看工位）
sudo cp /data/soph_SN/deploy/sn-monitor.service \
        /data/soph_SN/deploy/sn-monitor-big.service \
        /data/soph_SN/deploy/sn-uploader.service \
        /data/soph_SN/deploy/sn-preview-ffmpeg.service /etc/systemd/system/
sudo systemctl daemon-reload

# ② 一条命令启用全部（识别 + 上传 + WebRTC 预览自动跟随）
sudo systemctl enable --now sn-monitor-big
```

| 单元 | 板型 | 关键点 | 日志 |
|------|------|--------|------|
| `sn-monitor.service` | 小板 | 默认 small 档，**单路** | `logs/sn_monitor.service.log` |
| `sn-monitor-big.service` | 大板 | 写死 `--profile big` + **`--rtsp2` 双路** | 同上 |
| `sn-uploader.service` | 通用 | shell 探测脚本位置（先 `tools/` 后根目录） | `journalctl -u sn-uploader -f` |
| `sn-preview-ffmpeg.service` | 通用 | 双路 ffmpeg 推流到 `.57` | `journalctl -u sn-preview-ffmpeg -f` |

> - 两个 monitor 单元带 `Conflicts=`，**同一路相机只能起一个**；enable 其一自动停另一个。
> - ⚠️ 装单元会**覆盖 `/etc` 里手改过的版本**（比如你手改过摄像头 IP）。改过的工位先 `cp` 备份，覆盖后确认 `ExecStart` 里仍带着需要的 `--rtsp2`。

### 3.5 模型 / 框架部署（PP-OCR bmodel + sail，一次性）

识别依赖官方 PP-OCR 框架与 BM1688 的 `*.bmodel`（**不在 git 里**）。首装（详见 wiki《PP-OCR 搭建及测试》<https://wiki.sophgo.com/pages/viewpage.action?pageId=228896451>）：

```bash
# ① 克隆 sophon-demo 并下载模型
cd /data/soph_SN
git clone https://github.com/sophgo/sophon-demo.git
cd sophon-demo/sample/PP-OCR && chmod -R +x scripts/ && ./scripts/download.sh

# ② 装依赖 + sophon-sail（板上 OCR 推理靠 sail）
pip3 install -r python/requirements.txt opencv-python-headless
pip3 install dfss && python3 -m dfss --install sail
echo 'export LD_LIBRARY_PATH=/opt/sophon/sophon-sail/lib/:$LD_LIBRARY_PATH' >> ~/.bashrc && source ~/.bashrc

# ③ 验证模型就位（应见 ch_PP-OCRv4_{det,rec}_*_2core.bmodel）
ls -la models/BM1688/
```

> 生产用 **int8 双核** `ch_PP-OCRv4_{det,rec}_int8_2core.bmodel`（比 fp16 快）；`download.sh` 会把 int8/fp16/fp32 都拉下来。
> 用官方样例图跑通 `ppocr_system_opencv.py` 的完整命令见 wiki 第 4 步。

### 3.6 ★ MediaMTX + WebRTC 预览部署

调机预览链路（板子 ffmpeg → `.57` MediaMTX → 浏览器 WebRTC）的完整部署、验证、排障，见 **[docs/deployment-mediamtx.md](docs/deployment-mediamtx.md)**。MediaMTX 本体跑在 `.57` 的 `product_test` 仓库里，由其 `start.sh` 一键拉起，可修改项见该仓库 README 的「MediaMTX 部署」节。

---

## 4. IP / 配置改动对照表

> **配置落地方式**：本项目**无配置文件**（没有 `.json`/`.yaml`/`.ini`/`.env`），所有行为由 systemd 单元 `ExecStart` 那行命令或手动 CLI 参数决定。**换工位/换板型/改 IP = 改那行命令或脚本，不改 `.py`。**

| 改什么 | 文件 : 行（仓库相对路径） | 现默认值 | 备注 |
|--------|---------------------------|----------|------|
| **相机1（正面）IP** | `deploy/sn-monitor.service:15`<br>`deploy/sn-monitor-big.service:16`<br>`deploy/preview_supervisor.sh:27`<br>`preview_service.py:44` | `192.168.1.9` | 单元里 `--rtsp …live0`（识别主路）；supervisor 用 `live1`（子码流推流）。**改一处要同步改其余几处**，`sn-monitor-big.service:15` 原注释已提醒 |
| **相机2（背面）IP** | `deploy/sn-monitor-big.service:16`（`--rtsp2`）<br>`deploy/preview_supervisor.sh:28`<br>`preview_service.py:45` | `192.168.1.8` | 仅大板双路用 |
| **板子 eth1（相机网）** | 板上网络配置（`netplan`/`networkd`，不在仓库） | `192.168.1.200/24` | 无网关（见 §3.1）；改完 `ip route get 192.168.1.9` 验证 |
| **上传目标（.57 产测后端）** | `deploy/sn-uploader.service:21`<br>`tools/sn_uploader.py:60` | `http://10.80.40.57:8099/api/v1/captures` | 可用环境变量 `SN_UPLOAD_URL` 覆盖 |
| **RTMP 推流目标（.57）** | `deploy/preview_supervisor.sh:31,32` | `rtmp://10.80.40.57:1935/cam_{front,back}` | ⚠️ **无环境变量覆盖，写死在脚本里**——换 `.57` 地址只此一处 |
| **预览中继源（板子 IP，.57 侧视角）** | `.57` 上 `backend/api/camera.py:35` | `http://10.80.40.68:8090` | 可用环境变量 `PREVIEW_BOARD_BASE` 覆盖；**此值以 `.57` 的 `camera.py` 为准**（下行说明） |

> **⚠️ 板子 IP 的不一致**：本仓库历史文档与脚本里出现过板子 `10.80.40.53`，而 `.57` 的 `backend/api/camera.py` 当前写的是 `10.80.40.68`。**预览链路实际连哪个板子以 `.57` 的 `camera.py` 为准**（或用 `PREVIEW_BOARD_BASE` 覆盖，不改代码）。换板子时两边都要核对。
> **换 `.57` 服务器 IP**：`.57` 自己的 IP 写在**板子侧** `deploy/preview_supervisor.sh:31,32`（RTMP 目标）和 `deploy/sn-uploader.service` / `tools/sn_uploader.py`（上传目标）里，改服务器要改这几处。

改完统一动作：`scp` 覆盖 → 板上 `sudo systemctl daemon-reload && sudo systemctl restart sn-monitor-big` → **同时重启相机推流**（铁律 1）。

---

## 5. 常用命令

```bash
# 启停 / 切板型
sudo systemctl enable --now sn-monitor-big          # 大板（双路）
sudo systemctl disable --now sn-monitor-big         # 停
sudo systemctl disable --now sn-monitor && sudo systemctl enable --now sn-monitor-big  # 小→大

# 看状态 / 日志 / 结果
systemctl is-active sn-monitor-big sn-uploader sn-preview-ffmpeg
tail -f /data/soph_SN/logs/sn_monitor.service.log
cat    /data/soph_SN/sn_results/sn_list.txt
ls     /data/soph_SN/debug/                          # 漏检/待确认留证

# 调试：手动跑（参数见 docs/usage.md）
python3 sn_monitor.py --rtsp rtsp://192.168.1.9:8554/live0 --profile big
```

---

## 6. 分册索引（细节已搬到 docs/）

| 主题 | 一句话 | 分册 |
|------|--------|------|
| **使用（档位/参数/启动）** | 全部 `--profile` 档位、CLI 参数表、启动命令 | [docs/usage.md](docs/usage.md) |
| **排障速查** | 按现象查排查动作 | [docs/troubleshooting.md](docs/troubleshooting.md) |
| **实现原理** | 整体流水线 + 12 条关键设计决策（每条对应踩过的坑） | [docs/internals.md](docs/internals.md) |
| **MediaMTX / WebRTC 预览部署** | 调机预览链路、板子侧 / `.57` 侧部署、验证排障 | [docs/deployment-mediamtx.md](docs/deployment-mediamtx.md) |
| **上传 sidecar** | `sn_uploader.py` 怎么监视目录、去重、为什么首次会回灌历史 | [docs/features/uploader.md](docs/features/uploader.md) |
| **双路摄像头** | 两路为什么分开拉流、正反靠时间戳配对、`.57` 侧升级要 `ALTER TABLE` | [docs/features/dual-camera.md](docs/features/dual-camera.md) |
| **调机预览 V3.0（MJPEG）** | 内嵌预览模块、`_peek` 共享解码、纯加法实现 | [docs/features/preview-v3.md](docs/features/preview-v3.md) |
| **参数扫描工具** | 新模组快速定档、`profile_probe.py` 用法 | [docs/tools/profile-probe.md](docs/tools/profile-probe.md) |
| **版本演进** | V0.8 → V4.0 逐版改了什么、每个坑的根因 | [docs/CHANGELOG.md](docs/CHANGELOG.md) |

> 停用保留的 V1/V2 独立预览服务 `preview_service.py` 说明见 [preview_service_README.md](preview_service_README.md)。
