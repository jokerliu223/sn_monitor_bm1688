# SN 监控系统 (RTSP → PP-OCR 序列号识别)

SE9 / BM1688 算能板用 eth1 接一台路由器/交换机组成小局域网，从网上拉 RTSP 流，自动侦测板卡铭牌、识别序列号(SN)并落库。当前发布版本 **V2.1**（本仓库为可直接 git 部署的发布版；版本演进见第 7 节）。

- **单路（默认）**：eth1 上挂一台相机，对着板子正面，识别 SN。
- **双路（V2.1，加 `--rtsp2`）**：eth1 上再挂第二台相机对着背面，正面识别命中时同步抓一帧背面图，正/反两张一并上传、在产测网页端成对展示。两路是**各拉各的流、不合并**（见 4.8）。

> **V2.1 相对 V2.0.2 的增量**：双路摄像头（`--rtsp2`，正面识别命中时同步抓背面一帧，正/反配对展示，见 4.8）；`.57` 产测端 `side` 字段；`sn-uploader.service` 修正。**单路行为完全不变**——不加 `--rtsp2` 就是 V2.0.2。

---

## 1. 它做什么

摄像头对着流水线上的板卡，人放一块板 → 程序自动：
1. 侦测到画面稳定且出现铭牌 →
2. 对铭牌区域按**档位**裁剪 + OCR，跨帧投票 + 格式校验提取 SN →
3. 二次确认后写入 `sn_results/`（JSON + `sn_list.txt`）。

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
| 摄像头2（背面） | `rtsp://192.168.1.8:8554/live0`，**仅 V2.1 双路用**，只拉流不识别（见 4.8） |
| 摄像头特性 | **单客户端源**：任一客户端断开即退出、不自愈。断流会卡死，必须强制 TCP、常驻单连接、绝不主动断 |
| 已测板卡 | 大板 `BCCC8F25062000014` / `BCCC8F25062000011` / `BCAC6C25040200956`；小板 `BCCW6N24070100601` |
| 模型 | `ch_PP-OCRv4_{det,rec}_int8_2core.bmodel`（PaddleOCRv4，BM1688 int8 双核） |

> ⚠️ **铁律 1（摄像头）**：重启/停止服务会断开→**杀死单客户端源**。如需重启服务，必须**同时重启摄像头推流**，否则拉不到流。**双路时这条对两台相机都成立**，两路都要重启。
>
> ⚠️ **铁律 2（摆放）**：**SN 贴纸面必须朝相机、摆画面右上区**（小板像好帧 cx≈0.62 / cy≈0.30）。摆错（芯片丝印面朝上）→ 整幅画面无 SN → 必漏识，再调裁剪也没用。

### 2.3 关于"配置文件"

**本项目没有配置文件**——没有 `.json`/`.yaml`/`.ini`/`.env`，所有行为都由**命令行参数**决定。落地方式只有两种：

1. `deploy/sn-monitor*.service` 单元 `ExecStart` 那一行长命令（生产用，改完 `systemctl daemon-reload && systemctl restart <单元>`）；
2. 手动 `python3 sn_monitor.py <参数>`（调试用）。

参数全表见 4.3（档位）与 4.4（其它）。**换工位/换板型 = 改那行命令，不改代码。** 同理 `sn-uploader.py` 也只有 `--url` / `--results-dir` / `--interval` / `--timeout` 四个参数（见 4.6）。

---

## 3. 文件结构与部署

### 3.1 本仓库结构（git 跟踪的内容 = 可直接部署的代码）
```
sn_monitor_bm1688/
  sn_monitor.py          # ★ 唯一入口：RTSP 拉流 + 状态机 + 档位裁剪 + OCR + 投票 + 落库
  sncore/                # SN 处理内核（被 sn_monitor.py import）
    sn_extract.py        #   SN 提取: 多帧投票 + 格式校验 + 掉字子序列合并
    result_gate.py       #   结果二次确认门(ResultGate)
    sn_line.py           #   SN 行版式辅助
    __init__.py
  tools/                 # 辅助工具（不参与识别主链路，按需运行，见 3.4 与第 4 节）
    sn_uploader.py       #   命中结果 sidecar：上传 SN+命中帧到 .57 产测系统（见 4.6）
    profile_probe.py     #   参数扫描工具：新模组快速定档（见 4.7）
  deploy/
    sn-monitor.service       #   识别服务 systemd 单元（小板，单路；拷到 /etc/systemd/system/ 用）
    sn-monitor-big.service   #   识别服务 systemd 单元（大板，档位 big + 双路 --rtsp2）
    sn-uploader.service      #   上传 sidecar systemd 单元（可选，见 4.6）
  README.md              # 本文档
  .gitignore             # 排除模型/框架/运行时产物（见 3.3：这些不进 git）
```
> **只有代码进 git**。模型 `*.bmodel`、官方框架 `sophon-demo/`、运行时产物 `logs/`、`sn_results/`、`debug/` 全部 `.gitignore`——它们体积大且环境相关，另按 3.3 部署。

### 3.1b 工作站目录（`/media/sophgo/xiaohao.liu/SN_cratch/`，历史 + 调试脚本）
本仓库是**唯一代码主副本**；工作站的 `SN_cratch/` 是历史沉淀区，不再作为主副本：
```
SN_cratch/
  sn_monitor.new.py      # 已过时：旧称主副本，现与仓库 sn_monitor.py 内容一致（改代码请改仓库）
  sncore/                # 历史副本，同源
  archive/
    board_versions/      # v8~v17 全部历史版本备份
    board_legacy/        # 早期探索脚本（sn_barcode / fast / final / inline / tile_scan …）
  docs/                  # 设计/排障过程文档
（其余为调试/探活脚本：_gen_*.py 网格生成、keepalive_probe.py / probe_live.py / repro_reconnect.py 等）
```

### 3.2 板子运行时目录（`/data/soph_SN/`）
git 部署后的代码 + 3.3 装好的模型/框架，在板子上合成如下运行时布局：
```
/data/soph_SN/
  sn_monitor.py          # ← git 部署（本仓库根）
  sncore/                # ← git 部署
  tools/                 # ← git 部署（sn_uploader.py / profile_probe.py）
  sophon-demo/sample/PP-OCR/   # ← 3.3 装：官方框架 + models/BM1688/*.bmodel + datasets 字典
  logs/                  # 运行时：服务日志 sn_monitor.service.log（不进 journalctl）
  sn_results/            # 运行时：识别结果 JSON + sn_list.txt（目录名是 sn_results 不是 results）
  debug/                 # 运行时：漏检/待确认留证（原帧+ROI+候选，限 40 组）
/etc/systemd/system/sn-monitor.service       # ← deploy/sn-monitor.service 拷入（小板，单路）
/etc/systemd/system/sn-monitor-big.service   # ← deploy/sn-monitor-big.service 拷入（大板，big 档 + 双路；与上互斥，按工位启一个）
/etc/systemd/system/sn-uploader.service      # ← deploy/sn-uploader.service 拷入（上传 sidecar，见 4.6）
```

> ⚠️ **脚本位置有两种布局，两版都在用**：`tools/` 子目录（git 部署，见 3.1）与**扁平**（早期手工 scp，脚本直接在 `/data/soph_SN/` 下）。
> 因此 `deploy/sn-uploader.service` 的 `ExecStart` 用 shell 探测**两种路径都兼容**，取存在的那个；`sn-monitor*.service` 只依赖 `sn_monitor.py`（本来就在根，两种布局一致）。

### 3.3 模型 / 框架部署（PP-OCR bmodel + sail，一次性）
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

### 3.4 git 快速部署代码（更新识别脚本/工具）
代码（`sn_monitor.py`/`sncore/`/`tools/`/`deploy/`）全部走 git，**一条命令部署或更新**：

```bash
# 首次：板子上克隆本仓库到 /data/soph_SN（模型另按 3.3 装）
cd /data && git clone <本仓库地址> soph_SN     # 或克隆到别处再软链/拷贝

# 之后更新：板子上直接拉最新代码
cd /data/soph_SN && git pull
python3 -m py_compile sn_monitor.py tools/*.py     # 语法自检
sudo cp deploy/sn-monitor.service deploy/sn-monitor-big.service /etc/systemd/system/ && sudo systemctl daemon-reload   # service 有变更时（⚠️ 会覆盖 /etc 里手改过的版本，先备份）
sudo systemctl restart sn-monitor                  # 生效（★ 记得同时重启摄像头推流！）
```
> `git pull` 不影响正在跑的进程；生效才需 `restart`。`logs/`、`sn_results/`、`sophon-demo/` 已 `.gitignore`，`git pull` 不会动它们。

### 3.5 `tools/` 辅助工具一览
两个工具都**不参与识别主链路**，随 git 部署到 `/data/soph_SN/tools/`，按需手动运行。

| 工具 | 作用 | 使用方式 | 详见 |
|------|------|----------|------|
| `sn_uploader.py` | 命中结果 sidecar：监视 `sn_results/`，把 SN+命中帧 HTTP 上传到 .57 产测系统前端/DB。纯 urllib、`.uploaded` 防重传、与识别解耦不影响拉流 | 常驻服务 `sn-uploader`（`deploy/sn-uploader.service`）或前台手动跑 | **4.6** |
| `profile_probe.py` | 参数扫描器：对「图片+期望SN」笛卡尔积扫裁剪/切块参数，报耗时/命中/候选并推荐档位，新模组快速定档。只读跑 OCR | 前台手动跑，`sudo python3 tools/profile_probe.py ...` | **4.7** |

---

## 4. 使用

> 两种用法二选一：**A. 常驻服务**（生产，放板即识别，无需每次敲命令）；**B. 单条手动指令**（调试/临时，前台跑看实时日志）。
> 二者都抢同一个**单客户端摄像头源**，不能同时开——手动跑前先 `stop` 服务，跑完再按需 `start`。

### 4.0 快速使用示例（最常用）

**先决**：板子 SSH `linaro@10.80.40.53`；摄像头 `rtsp://192.168.1.9:8554/live0`；工作目录 `/data/soph_SN`。

#### 用法 A —— 常驻服务（生产，一次启用后长期自动跑）
> **一个工位固定一种板子**：按板型选对应服务，两者写死了各自档位，**无需手改 ExecStart**。
> 二者互斥（`Conflicts=`，单客户端源只能一个进程拉流），enable 其一会自动停掉另一个。

```bash
# ▼ 小板工位：启用 small 档识别服务（开机自启 + 立即启动）
sudo systemctl enable --now sn-monitor
# ▼ 大板工位：改用 big 档服务（二选一，不要和上面同时 enable）
sudo systemctl enable --now sn-monitor-big

# 启用上传 sidecar（把命中结果实时推到 .57 产测前端/DB，详见 4.6）
sudo systemctl enable --now sn-uploader
# 之后放板即自动识别 + 自动上传，无需再敲任何命令。看实时日志：
tail -f /data/soph_SN/logs/sn_monitor.service.log
```
> 换板型时先停旧的：小板→大板 `sudo systemctl disable --now sn-monitor && sudo systemctl enable --now sn-monitor-big`（反之亦然）。
>
> **大板工位默认就是双路**：`sn-monitor-big.service` 的 `ExecStart` 里已写死 `--rtsp2 rtsp://192.168.1.8:8554/live0`，**开箱即抓正/反两面，不用手改**。只装一台相机的大板工位，把它删掉即可回到单路。
> 小板单元 `sn-monitor.service` 是**单路**，要用双路得自己往末尾追加 `--rtsp2 ...`，再 `daemon-reload && restart`。详见 4.8。
>
> ⚠️ 这两个单元就是从 `deploy/` 拷进 `/etc/systemd/system/` 的，**部署时覆盖会冲掉本地手改的 IP/参数**，见 4.1 的警告。

#### 用法 B —— 不使用服务，单条手动指令（调试）
```bash
sudo systemctl stop sn-monitor        # 先停服务让出摄像头源

cd /data/soph_SN
# ▼ 小板（默认档 small，不带 --profile）
sudo python3 sn_monitor.py --rtsp rtsp://192.168.1.9:8554/live0 --idle 0
# ▼ 大板（必须带 --profile big）
sudo python3 sn_monitor.py --rtsp rtsp://192.168.1.9:8554/live0 --idle 0 --profile big
# ▼ 大板 + 双路（V2.1 正/反两面，见 4.8）
sudo python3 sn_monitor.py --rtsp rtsp://192.168.1.9:8554/live0 --idle 0 --profile big \
     --rtsp2 rtsp://192.168.1.8:8554/live0
# ★ 手动跑也会杀源：本条启动后 / Ctrl-C 结束后，都要同时重启摄像头推流
#   （双路时两台相机都要重启）

# 如需上传到前端/DB（可选，另开一个终端；不上传就不用起）：
sudo python3 /data/soph_SN/tools/sn_uploader.py --url http://10.80.40.57:8099/api/v1/captures --interval 3
```
> 大板/小板唯一区别就是 `--profile`（一次切换裁剪几何/上采样/确认门，见 4.3）。
> 结果落在 `/data/soph_SN/sn_results/`（`sn_<SN>_<ts>.json` + 命中帧 `.jpg`）。

### 4.1 常驻服务（生产）

**两个服务单元，按板型二选一**（`deploy/` 下，`git pull` 即带；装法见 3.4）：

| 服务 | 板型 | ExecStart 档位 | 日志 |
|------|------|----------------|------|
| `sn-monitor.service` | 小板 | `--profile small`（默认，不写即 small） | `logs/sn_monitor.service.log` |
| `sn-monitor-big.service` | 大板 | 写死 `--profile big` + **`--rtsp2` 双路** | 同上 |

> `sn-monitor-big.service` **本身带 `--rtsp2`（双路）**——大板工位默认就抓正/反两路。若某个大板工位只装了一台相机，把该行末尾的 `--rtsp2 ...` 删掉即可（不加就是 V2.0.x 单路行为）。`sn-monitor.service`（小板）则保持单路，需要双路时自己补 `--rtsp2`。

> ⚠️ **这两个单元就是从 `deploy/` 拷到 `/etc/systemd/system/` 的**。正常部署流程 `git pull` + `cp deploy/*.service /etc/systemd/system/` 会**覆盖本地改过的版本**——在 `/etc` 里手改过摄像头 IP 或档位的工位，覆盖前先备份：
> ```bash
> sudo cp /etc/systemd/system/sn-monitor-big.service /etc/systemd/system/sn-monitor-big.service.local_bak
> ```
> 覆盖后务必确认 `ExecStart` 里仍带着需要的 `--rtsp2`。

两者含 `Conflicts=`，**同一路摄像头只能起一个**；enable 其一会自动停另一个。把 `<svc>` 换成实际用的那个：

```bash
sudo systemctl start   <svc>      # 启动（<svc>=sn-monitor 或 sn-monitor-big）
sudo systemctl stop    <svc>      # 停止(会杀源, 需配合摄像头)
sudo systemctl restart <svc>      # 重启(务必同时重启摄像头推流!)
sudo systemctl status  <svc>
tail -f /data/soph_SN/logs/sn_monitor.service.log
```
小板 `ExecStart`：
```
/usr/bin/python3 /data/soph_SN/sn_monitor.py --rtsp rtsp://192.168.1.9:8554/live0 --idle 0 --mi 5
```
大板 `ExecStart`：同上末尾多 `--profile big` **和 `--rtsp2 rtsp://192.168.1.8:8554/live0`（双路，背面相机）**。
配置：`--idle 0`（永不超时）、`KillSignal=SIGINT`（干净 TEARDOWN 保住下次可拉流）、`Restart=always`。
> 若还是想用一个服务临时切档，也可手改对应单元的 `ExecStart` 再 `daemon-reload`——但固定工位推荐直接用上面两个现成单元。

### 4.2 手动前台跑（调试，最直观）
```bash
sudo systemctl stop sn-monitor
cd /data/soph_SN
sudo python3 sn_monitor.py --rtsp rtsp://192.168.1.9:8554/live0 --idle 0    # 小板(默认)
sudo python3 sn_monitor.py --rtsp rtsp://192.168.1.9:8554/live0 --idle 0 --profile big  # 大板
# 双路(大板 + 背面, V2.1):
sudo python3 sn_monitor.py --rtsp rtsp://192.168.1.9:8554/live0 --idle 0 --profile big \
     --rtsp2 rtsp://192.168.1.8:8554/live0
# 同时重启摄像头推流(双路时两台相机都要重启)
```

### 4.3 档位系统（V2.0.1 核心）

一个 `--profile` 打包一档参数（裁剪几何 + 切片 + 抽帧 + 确认门）。显式 CLI 标志仍覆盖档位。

| 参数 | small(默认) | big | 含义 |
|------|------|------|------|
| `crop_w × crop_h` | 0.40 × 0.35 | 0.30 × 0.40 | 各向异性中心裁剪(宽比×高比)，只留铭牌带 |
| `crop_cy` | 0.32(偏上) | 0.50(居中) | 裁剪垂直锚点；小板 SN 在画面上带 |
| `tile_grid` | (1,1) | (1,1) | **不切片单帧**(见设计决策⑩) |
| `tile_up` | 3.0 | 2.5 | 裁剪块上采样倍数 |
| `frames` | 2 | 2 | 单次识别抽帧数(跨帧投票修掉字) |
| `confirm` | 2 | 1 | 连续同一 SN 达此次数即落库 |
| `sn_min_len` | 17 | 17 | SN 最短长度门(挡短串误报) |
| `poll` | 1.0 | 0.4 | 侦测轮询间隔 |
| `rescue` | True | False | 救援旋转遍 |

覆盖档位的显式标志：`--crop-w` / `--crop-h` / `--crop-cy` / `--center-crop`(宽高同设) / `--frames`。

### 4.4 其它关键参数

| 参数 | 默认 | 说明 |
|------|------|------|
| `--rtsp` | (必填) | 摄像头1（正面）RTSP 地址，识别主路 |
| `--rtsp2` | (空) | **v2.1** 摄像头2（背面）RTSP；**空=单路**（=V2.0.2）。见 4.8 |
| `--rtsp2-grab-wait` | 0.5 | **v2.1** 命中时从第二路取最新帧的最大阻塞秒 |
| `--profile` | small | 档位: `small`/`big`(见 4.3) |
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

> 稳定性兜底参数 `--wedge-*` / `--stale-*` 一般不用动。

### 4.5 更新脚本到板子
代码走 git，见 **3.4 git 快速部署**：板子上 `git pull` → `py_compile` 自检 → `systemctl restart sn-monitor`（记得同时重启摄像头推流）。`git pull` 不影响在跑进程，生效才需 restart。

### 4.6 实时上传到前端/数据库（sn-uploader sidecar）

识别只负责把结果落到本地 `sn_results/`；**推到 .57 产测系统前端/DB 的是独立 sidecar `sn_uploader.py`**（与识别解耦，纯 urllib，网络故障只重试、`.uploaded` 标记防重传，绝不影响拉流）。

**数据流**：
```
放板 ─► sn-monitor(识别) ─► sn_results/  sn_<SN>_<ts>.json + 命中帧.jpg
     └─► sn-uploader(sidecar) 监视目录, jpg+json 配对 ─► HTTP POST(multipart)
     └─► http://10.80.40.57:8099/api/v1/captures  (product_test 后端)
     └─► DB 表 sn_captures + data/sn_captures/<SN>/<ts>.jpg
       └─► 前端「SN 抓拍」页：按 SN 检索 / 看设备图 + score + 时间
```

**是否每次都要单独启动？→ 不用。** 板子上把两个服务各 `enable --now` 一次即长期常驻：
```bash
# 首次：从 deploy/ 装单元（已装过可跳过；big 单元一并拷入，按工位启用其一）
# ⚠️ 就地覆盖会冲掉 /etc 里手改过的 IP/档位——改过的先备份，见 4.1
sudo cp /data/soph_SN/deploy/sn-monitor.service /data/soph_SN/deploy/sn-monitor-big.service /data/soph_SN/deploy/sn-uploader.service /etc/systemd/system/
sudo systemctl daemon-reload
# 启用（开机自启 + 立即启动）
sudo systemctl enable --now sn-monitor    # 识别(小板，单路；大板工位改用 sn-monitor-big，二选一)
sudo systemctl enable --now sn-uploader   # 上传(sidecar 必须 root，否则 .uploaded 写不进→重复上传)
journalctl -u sn-uploader -f           # 看上传实时日志
```
之后放板即「自动识别 → 自动上传 → 前端反映」，全程无需再敲命令。

**只想临时手动上传**（不启用服务时，另开终端；脚本在 `tools/` 或扁平位置，按你的部署取其一）：
```bash
sudo python3 /data/soph_SN/tools/sn_uploader.py --url http://10.80.40.57:8099/api/v1/captures --interval 3
# 扁平部署（早期手工 scp）则是: /data/soph_SN/sn_uploader.py
```

> - `.57` 产测后端需在 `:8099` 起着（`cd product_test && ./start.sh`）才能接收入库。
> - 接口契约 / 参数说明见 `tools/sn_uploader.py` 头部注释；`sn-uploader.service` 单元在 `deploy/`（路径兼容 `tools/` 与扁平两种布局）。
> - ⚠️ **首次启用会"回灌历史"**：uploader 只看 `.uploaded` 标记，不看时间。若 `sn_results/` 里积着大量**从未上传过**的旧命中帧，一 `enable` 就会**全量补传**到 `.57`，把库灌脏。
>   启用前先确认积压（或按 4.6 的排查：给历史帧批量打 `.uploaded` 标记宣告"不补传"）。
>   ```bash
>   # 看有多少未上传的积压(数量非 0 就要先决定补不补)
>   sudo bash -c 'n=0; for f in /data/soph_SN/sn_results/*.jpg; do [ -f "$f.uploaded" ] || n=$((n+1)); done; echo "未标记: $n"'
>   # 不想补传: 给当前所有历史帧打标记(此后只传新文件)
>   sudo bash -c 'for f in /data/soph_SN/sn_results/*.jpg; do [ -f "$f.uploaded" ] || touch "$f.uploaded"; done'
>   ```
> - ⚠️ **uploader 的去重是"文件级"，不是"内容级"，也不比数据库**——这是最常被问的一点，明确写死：
>   - 判据**只有一个**：同名 `.uploaded` 标记文件是否存在（`tools/sn_uploader.py` 的 `iter_pending()` 里就一个 `os.path.exists(base+".uploaded")`）。
>   - **不与 `.57` 数据库比对**：uploader 里没有 DB 连接、不装 sqlalchemy、不发任何查询，连"远端已有哪些 SN"都不知道。
>   - **不在单次扫描内去重**：同一轮里若两个文件内容相同（不同文件名），两张都传。
>   - 后果：**同一块板反复放置**产生的多次抓拍是**不同文件名**，`.uploaded` 拦不住 → 全部入库（业务级重复）；**SN 误读**（少位/错字）同样**照传不误**。
>   - 换句话说：它保证的是"每个本地文件最多成功上传一次"，**不保证"每块板在远端只出现一次"**。清理只能在 `.57` 侧按需做。
>   - 可选的上游加固（尚未实现）：给 uploader 加一道 **SN 格式闸**，拦掉明显错读的 SN，避免脏数据进库。

### 4.7 参数扫描工具 `profile_probe.py`（新模组快速定档）

每上一款**新模组/新板型**，SN 铭牌的位置与占比都变，档位参数（各向异性裁剪 `crop_w/crop_h/crop_cy` + 切块 `tile_grid/tile_up` + ROI 路）得重调。`profile_probe.py` 把这些参数**笛卡尔积扫一遍**，对「图片+期望SN」逐组合报 **耗时 / SN是否命中(带score) / 候选数**，末尾直接给「命中且最快」的推荐组合，可抄进 `PROFILES` 定新档。**只读**：仅跑 OCR，不落库、不上传、不碰在跑的服务。

> 需在**板子上跑**（要 sail 加载 OCR bmodel）。随本仓库 git 部署（`tools/profile_probe.py`），无需单独 scp。

**文件路径**：
| 项 | 路径 |
|----|------|
| 脚本（仓库内） | `tools/profile_probe.py` |
| 运行位置（板子，git 部署后） | `/data/soph_SN/tools/profile_probe.py` |
| 裁剪图输出目录（默认） | `./profile_probe_out/`（在板子即当前工作目录下），文件名用 **宽/高/锚点** 组合：`<图名>_cw0.30_ch0.40_cy0.50.jpg`，供人眼核对每种裁剪框住了哪块。可用 `--out-dir` 改 |

**使用方式**：
```bash
# 板子上跑。先停服务让出摄像头源(本工具只读本地图, 不拉流, 但避免占 NPU)
sudo systemctl stop sn-monitor
cd /data/soph_SN

# ① 默认:扫 sn_results/ 下历史命中帧(期望SN从文件名反解), 跑内置常用网格
sudo python3 tools/profile_probe.py

# ② 指定新模组测试图 + 期望SN(最常用)
sudo python3 tools/profile_probe.py --jobs "/tmp/newmod_a.jpg:BCXX...,/tmp/newmod_b.jpg:BCYY..."

# ③ 自定义扫描网格(收窄范围, 加快)
sudo python3 tools/profile_probe.py --jobs-file jobs.txt \
    --crop-w 0.3,0.4,1.0 --crop-h 0.35,0.4 --crop-cy 0.32,0.5 \
    --tile 1x1,2x2 --up 2.0,2.5,3.0 --roi off --rots 0
```
> `--jobs-file` 每行 `路径 期望SN`（空格/冒号分隔，`#` 开头为注释）。默认网格约 100+ 组合/图，用 `--crop-w` 等收窄可显著提速。

**预期输出形式**：
```
[probe] 2 图 × 108 组合; 裁剪图→ /data/soph_SN/profile_probe_out

#### newmod_a.jpg want=BCXX... 尺寸3840x2160 ####
  cw0.30 ch0.40 cy0.50 1x1@2.5x roi- | 0.83s | SN=✅0.971 | 候选 6
  cw0.40 ch0.35 cy0.32 1x1@3.0x roi- | 0.91s | SN=❌    | 候选 4
  ...
>> 推荐(命中 2/2 图, 累计 1.66s): cw0.30 ch0.40 cy0.50 1x1@2.5x roi-
   PROFILES 片段: "crop_w":0.3, "crop_h":0.4, "crop_cy":0.5, "tile_grid":(1,1), "tile_up":2.5, "roi_path":False
```
拿推荐行的 `PROFILES 片段` 抄进 `sn_monitor.py` 的 `PROFILES` 新增一档，再按 3.4 git 部署即可。

### 4.8 v2.1 双路摄像头（正/反两面抓拍）

#### 用在哪、解决什么

一个工位**只放一块板**，但要同时拍到它的**正面（SN 贴纸面）和背面**——正面用来读 SN，背面用来留证（比如核对背面丝印/条码是否与正面 SN 是同一块板）。

装**两台摄像头**：相机1 对着正面（原来的那台，负责识别），相机2 对着背面（新加的，**只拍照不识别**）。在相机1 识别命中的那一瞬间同步抓一帧背面图，正反两张一并上传，在产测网页端**成对展示**（见 4.9）。**这样一次放置就拿到两面证据，不用人工翻板重拍第二遍。**

#### 两路是分开拉流，不合并

这是本版最容易误解的一点，明确写清：

| 问题 | 答案 |
|------|------|
| 两路 RTSP 是合并成一路吗？ | **不是**。`sn_monitor.py` 起**两个独立的 `FrameReader`**，各自一条 RTSP 连接、各自一个后台线程排空解码缓冲，互不干扰。 |
| 第二路也做 OCR 吗？ | **不做**。`reader2` 全程**只 `read()` 丢帧**，把缓冲排干净、留住最新帧，一次 OCR 都不跑。识别链路只有正面一路。 |
| 硬件吃得下吗？ | 吃得下。BM1688 有 2 个硬件视频解码核（`/proc/soph/vpuinfo` 的 `vdec_coreid 1/2`），两路 4K HEVC 各分一核；背面不跑 OCR，NPU 负载几乎不增加。 |
| 正反怎么配成一对？ | **不是靠两路时间同步**。是正面识别**命中的那一刻**去 `reader2.latest()` 取背面最新帧，然后**把正面的 ISO 时间戳直接复制给背面**。DB 里两行 `captured_at` 相同是**写出来的**，不是分别测出来的——所以配对必然成功。 |
| 为什么在"识别起点"抓而不是"命中后"抓？ | 代码注释写明：识别起点抓，与正面 `frames[0]` 时间最接近；OCR 本身要耗时约 1s，等命中再抓，背面可能已经换板了。 |
| 万一背面拿不到帧？ | 跳过背面，只落正面，**绝不阻塞/影响正面识别落库**。 |

#### 怎么开

给 `--rtsp` 之外再加一个 `--rtsp2`。**空/不加 = 单路**（行为完全等同 V2.0.x，现有小/大板零影响）。

| 参数 | 默认 | 说明 |
|------|------|------|
| `--rtsp2` | (空) | 第二路（背面）RTSP。空=不开第二路 |
| `--rtsp2-grab-wait` | 0.5 | 命中时从第二路取最新帧的最大阻塞秒（小值防拖慢主识别） |

**前置条件**：第二台相机要接到 eth1 那台路由器/交换机上，跟相机1 同网段（见 2.1）。先用 `ping` + 端口探测确认板子能到它：

```bash
ping -c1 192.168.1.8                                    # 通不通
timeout 4 bash -c "cat < /dev/null > /dev/tcp/192.168.1.8/8554" && echo 8554可连
```

**生产（服务方式）**：**大板工位什么都不用改** —— `deploy/sn-monitor-big.service` 的 `ExecStart` 里已经写好了 `--rtsp2`，拷进去启用即双路：

```bash
sudo cp deploy/sn-monitor-big.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sn-monitor-big
```

**小板工位**（`sn-monitor.service` 是单路）才需要手动补：编辑该单元的 `ExecStart`，末尾追加 `--rtsp2 rtsp://192.168.1.8:8554/live0`，然后：

```bash
sudo cp deploy/sn-monitor.service /etc/systemd/system/   # 或直接在 /etc 里改
sudo systemctl daemon-reload
sudo systemctl restart sn-monitor
```

当前板子上跑的就是这行（`sn-monitor-big.service`）：
```
ExecStart=/usr/bin/python3 /data/soph_SN/sn_monitor.py --rtsp rtsp://192.168.1.9:8554/live0 \
  --idle 0 --mi 5 --profile big --rtsp2 rtsp://192.168.1.8:8554/live0
```

**手动前台（调试，最直观）**：
```bash
sudo python3 sn_monitor.py --rtsp rtsp://192.168.1.9:8554/live0 \
  --idle 0 --profile big --rtsp2 rtsp://192.168.1.8:8554/live0
```
启动时应看到两行分辨率打印，第二行确认背面已接上：
```
  流: 3840x2160
  第二路(背面): 3840x2160
```
若第二路不通，会打印 `⚠ 第二路 ... 暂不可达, 本次降级为单路(仅正面), 背面抓拍跳过` 然后照常单路运行——**这是刻意设计：不阻塞重试、直接降级**，等相机调通再重启服务即可。

> 手动前台跑**只管识别**，不管上传。要上传得**另开一个终端**跑 uploader（长驻，`Ctrl-C` 停）：
> ```bash
> sudo python3 tools/sn_uploader.py --url http://10.80.40.57:8099/api/v1/captures --interval 3
> ```
> **生产环境不用这么麻烦**：`sn-uploader.service` 已 enable，开机随 `sn-monitor*` 一起常驻，两个服务各占一个后台进程、互不干扰——只有"手动前台调试"这种非服务方式，才需要自己多开一个窗口。双路也不额外增加终端：一张正面图 + 一张背面图由同一个 uploader 一起拾取上传。

> **注意**：第二路**不做**断线自愈（周期重连）。跑起来之后若背面相机掉线，本进程不会自动接回来，需重启服务。正面那一路的自愈机制不受影响。

#### 落盘 / 配对 / 上传

| 端 | 结果 |
|----|------|
| 板子落盘（正面） | `sn_<SN>_<ts>.jpg` + `sn_<SN>_<ts>.json`，JSON 多一个 `"side":"front"` |
| 板子落盘（背面） | `sn_<SN>_<ts>_back.jpg` + `sn_<SN>_<ts>_back.json`，`"side":"back"`，其中 `sn`/`ts`/`score` **继承正面** |
| 上传 | `sn-uploader` 靠 glob `sn_*.jpg` **天然把两张都拾取**，POST 时各带 `side`；正反用同一 ISO 时刻上传 |
| `.57` DB | 两行（`side` 各为 front/back），`captured_at` 一致 |
| 前端 | 按 `(sn, captured_at)` 归组，**一张卡片并排显示正面/背面** |

> ⚠️ 第二路同样是**单客户端源**：停/重启服务会断开背面摄像头，需同时重启其推流（与铁律 1 一致）。

### 4.9 上传到产测系统后的配对展示（`.57` 侧）

`.57` 的「SN 抓拍」页把上面的两行配成一张卡片。相关约定（**升级已有 `.57` 部署时必读**）：

- `sn_captures` 表在 V2.1 新增了 `side` 列（`front`/`back`，默认 `front`）。**SQLAlchemy 的 `create_all` 只建表不做 ALTER**，老库不会自动多出这一列，接口会直接报 `no such column: sn_captures.side`。升级前必须手动补列：
  ```bash
  cp data/results.db data/results.db.bak_$(date +%Y%m%d_%H%M%S)
  sqlite3 data/results.db "ALTER TABLE sn_captures ADD COLUMN side VARCHAR(8) NOT NULL DEFAULT 'front';"
  ```
  老数据取默认值 `front`，正好等价于"老数据都是正面"，无需回填。
- 落盘文件名同步改了：`data/sn_captures/<SN>/<时间戳>_<side>.jpg`（带 `side` 后缀，否则同一命中正反同秒会互相覆盖）。
- 页面上时间显示的是**抓拍时间 `captured_at`**（缺失才回退入库时间 `uploaded_at`）；但**筛选框按入库时间 `uploaded_at` 过滤**——这是刻意的（入库时间永不为空、且不受板子时钟漂移影响），所以筛选出来的日期范围可能和卡片上显示的时间差一两天，属正常现象。
- **`.57` 端实现细节见该仓库自己的 `README.md`「SN 抓拍」章节**，不在本仓库维护。

---

## 5. 实现原理

### 5.1 整体流水线
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

### 5.2 关键设计决策（每条都对应踩过的坑）

**① 强制 TCP + 常驻单连接，绝不主动断。**
摄像头是单客户端源，UDP 会脏断卡死、断开即源退出。所以全程一条 TCP 持久连接，永不主动 release。

**② 后台线程持续 read()，主循环只取最新帧。**
FFMPEG 后端 `BUFFERSIZE=1` 无效，直接 read 会回放缓冲里的旧帧。后台线程不停排空解码缓冲，主循环 `latest()` 永远拿最新解码帧，根治旧帧回放。cap 的 read/release 全在该线程做，交接靠 `set_cap`，杜绝跨线程释放崩溃。

**③ 瞬时解码错误不重连（断"重连 churn"自激）。**
HEVC 常见 `PPS id out of range`（坏包）、`Could not find ref`/`Error constructing RPS`（丢参考帧）都是**瞬时**错误，解码器等下一个 IDR 关键帧自愈。若一见错就 release 重连，会从 GOP 中间重进 → 又一片 ref-error → 重连 churn 自激螺旋。策略：无新鲜帧且解码错在刷时**保持同连接等 IDR**，只有无解码活动持续无帧或超死线(`--wedge-dead-s`)才判真坏流重连。见第 7 节 V1.4 起的修复。

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

---

## 6. 排障速查

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
| **双路：第二路没接上** | 启动日志有 `⚠ 第二路 ... 暂不可达, 本次降级为单路` = 正常降级。查：相机2 是否接在同一台路由器/交换机？`ping 192.168.1.8`？`ip route get 192.168.1.8` 是否回 `dev eth1`？端口 `8554` 可连？ |
| **双路：背面图不变/是旧画面** | 相机2 推流是否真在动？背面靠 `reader2` 排空缓冲取最新帧，源停推就会一直是同一帧。另注意背面**无断线自愈**，掉线需重启服务 |
| **双路：卡片只显示正面** | ① 上传是否带 `side`？（板子 `sn_results/*_back.json` 里应有 `"side":"back"`）② `.57` 库里那两行 `captured_at` 是否真的一致？（差一点点就配不上对）③ 老库是否忘了 `ALTER TABLE ... ADD COLUMN side`？见 4.9 |
| **网页上时间与筛选日期差一两天** | 正常：卡片显示**抓拍时间**，筛选按**入库时间**。见 4.9 |
| **上传器灌了一堆重复** | uploader 只按 `.uploaded` 标记做**文件级**去重，不管内容/业务重复、也不比 DB。首次启用前的历史积压会全量补传（见 4.6）。清理需在 `.57` 侧做 |

日志与结果：
```
tail -f /data/soph_SN/logs/sn_monitor.service.log
cat /data/soph_SN/sn_results/sn_list.txt
ls /data/soph_SN/debug/          # 漏检/待确认留证
```

---

## 7. 版本演进

| 版本 | 阶段 | 关键改动 |
|------|------|----------|
| **V0.8 ~ V0.9** | 探索板起步 | 多帧投票 + 格式校验落地；常驻服务(TCP/SIGINT/Restart)；死亡螺旋修复(瞬时坏包不杀源)、召回硬化(白底∪Sobel)、自适应上采样 |
| **V1.4** | 迭代稳定 | **断重连 churn**：ref-error 等 IDR 自愈不重连，灭刷屏 |
| **V1.5** | 迭代提速 | 模型常驻 + 渐进式 L1/L2/L3 命中即停 |
| **V1.6** | 迭代提速 | 模型闲时释放懒加载 + `--poll` 快轮询降延迟 |
| **V1.7** | 迭代稳定 | **同板去抖**(静板不重识，连带修好模型释放过晚) |
| **V2.0.1** | ★ 发布版 | **档位系统**(big/small 各向异性裁剪+锚点) + **不切片单帧**(治好 2→7 错读) + 长度门 17。首个对外发布、可 git 部署 |
| **V2.0.2** | 发布后增量 | 参数扫描工具 `tools/profile_probe.py`(新模组快速定档) + 上传 sidecar `tools/sn_uploader.py` 文档化 + README 发布化(git 部署 / 模型部署 / tools 说明) |
| **V2.1** | ★ 当前 | **双路摄像头**(`--rtsp2` 正面识别命中时同步抓背面一帧,正反配对上传,见 4.8) + `.57` 产测端 `side` 字段与正/反配对展示 + `sn-uploader.service` 修正(路径兼容两种布局、补 `--interval`) + **`sn-monitor-big.service` 内置双路**(大板工位开箱即双路,不再需要手改 ExecStart) |

> 版本号约定：`V0.x` 探索板；`V1.x` 迭代稳定/提速；`V2.0.1` 首个发布版；`V2.0.2` 起为发布后增量更新。
