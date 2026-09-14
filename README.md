# SN 监控系统 (RTSP → PP-OCR 序列号识别)

SE9 / BM1688 算能板通过 eth1 直连工业摄像头，实时拉 RTSP 流，自动侦测板卡铭牌、识别序列号(SN)并落库。当前稳定版本 **v18**。

---

## 1. 它做什么

摄像头对着流水线上的板卡，人放一块板 → 程序自动：
1. 侦测到画面稳定且出现铭牌 →
2. 对铭牌区域按**档位**裁剪 + OCR，跨帧投票 + 格式校验提取 SN →
3. 二次确认后写入 `sn_results/`（JSON + `sn_list.txt`）。

大板约 **1.5~3s** 出结果；小板不切片单帧命中约 **1.2s**；识别到铭牌变化的反应约 **1~2s**。

---

## 2. 硬件 / 网络环境

| 项 | 值 |
|----|----|
| 板子 | SE9 / BM1688，SSH `linaro@10.80.40.53`（密码 `linaro`，sudo 同） |
| 摄像头 | eth1 直连，`rtsp://192.168.1.9:8554/live0`，4K(3840×2160) HEVC |
| 摄像头特性 | **单客户端源**：任一客户端断开即退出、不自愈。断流会卡死，必须强制 TCP、常驻单连接、绝不主动断 |
| 已测板卡 | 大板 `BCCC8F25062000014` / `BCCC8F25062000011` / `BCAC6C25040200956`；小板 `BCCW6N24070100601` |
| 模型 | `ch_PP-OCRv4_{det,rec}_int8_2core.bmodel`（PaddleOCRv4，BM1688 int8 双核） |

> ⚠️ **铁律 1（摄像头）**：重启/停止服务会断开→**杀死单客户端源**。如需重启服务，必须**同时重启摄像头推流**，否则拉不到流。
>
> ⚠️ **铁律 2（摆放）**：**SN 贴纸面必须朝相机、摆画面右上区**（小板像好帧 cx≈0.62 / cy≈0.30）。摆错（芯片丝印面朝上）→ 整幅画面无 SN → 必漏识，再调裁剪也没用。

---

## 3. 文件结构

### 板子（`/data/soph_SN/`，只保留关键文件）
```
/data/soph_SN/
  sn_monitor.py # 唯一入口（= 工作站 sn_monitor.new.py 的部署副本, root 独立文件非 symlink）
  sncore/
    sn_extract.py        # SN 提取: 多帧投票 + 格式校验 + 掉字子序列合并
    result_gate.py       # 结果二次确认门(ResultGate)
    sn_line.py           # SN 行版式辅助
    __init__.py
  sophon-demo/.../PP-OCR/ # 官方 PP-OCR 框架 + models/BM1688/*.bmodel + datasets 字典
  logs/           # 服务日志 sn_monitor.service.log (StandardOutput=append, 不进 journalctl)
  sn_results/            # 识别结果 JSON + sn_list.txt (注意目录名是 sn_results 不是 results)
  debug/                 # 漏检/待确认留证(原帧+ROI+候选, 限 40 组)
  sn_uploader.py       # (可选) 命中结果 sidecar 上传到 .57 product_test 系统
/etc/systemd/system/sn-monitor.service
```

### 工作站（`/media/sophgo/xiaohao.liu/SN_cratch/`，主副本 + 历史）
```
sn_monitor.new.py    # ★ 主副本(master), 改这里再部署到板子
sn_monitor.py     # → 指向 sn_monitor.new.py 的软链
profile_probe.py     # 参数扫描工具(新模组快速定档, 见 4.7); scp 到板子跑
sncore/           # 与板子同源
sn-monitor.service       # service 单元文件
README.md            # 本文档
archive/
  board_versions/        # v8~v18 全部历史版本备份
  board_legacy/          # 早期探索脚本(sn_barcode/fast/final/inline/tile_scan…)
```

---

## 4. 使用

> 两种用法二选一：**A. 常驻服务**（生产，放板即识别，无需每次敲命令）；**B. 单条手动指令**（调试/临时，前台跑看实时日志）。
> 二者都抢同一个**单客户端摄像头源**，不能同时开——手动跑前先 `stop` 服务，跑完再按需 `start`。

### 4.0 快速使用示例（最常用）

**先决**：板子 SSH `linaro@10.80.40.53`；摄像头 `rtsp://192.168.1.9:8554/live0`；工作目录 `/data/soph_SN`。

#### 用法 A —— 常驻服务（生产，一次启用后长期自动跑）
```bash
# 启用识别服务（开机自启 + 立即启动）；默认 small(小板)。要监控大板见 4.1 改 ExecStart 加 --profile big
sudo systemctl enable --now sn-monitor
# 启用上传 sidecar（把命中结果实时推到 .57 产测前端/DB，详见 4.6）
sudo systemctl enable --now sn-uploader
# 之后放板即自动识别 + 自动上传，无需再敲任何命令。看实时日志：
tail -f /data/soph_SN/logs/sn_monitor.service.log
```

#### 用法 B —— 不使用服务，单条手动指令（调试）
```bash
sudo systemctl stop sn-monitor        # 先停服务让出摄像头源

cd /data/soph_SN
# ▼ 小板（默认档 small，不带 --profile）
sudo python3 sn_monitor.py --rtsp rtsp://192.168.1.9:8554/live0 --idle 0
# ▼ 大板（必须带 --profile big）
sudo python3 sn_monitor.py --rtsp rtsp://192.168.1.9:8554/live0 --idle 0 --profile big
# ★ 手动跑也会杀源：本条启动后 / Ctrl-C 结束后，都要同时重启摄像头推流

# 如需上传到前端/DB（可选，另开一个终端；不上传就不用起）：
sudo python3 /data/soph_SN/sn_uploader.py --url http://10.80.40.57:8099/api/v1/captures --interval 3
```
> 大板/小板唯一区别就是 `--profile`（一次切换裁剪几何/上采样/确认门，见 4.3）。
> 结果落在 `/data/soph_SN/sn_results/`（`sn_<SN>_<ts>.json` + 命中帧 `.jpg`）。

### 4.1 常驻服务（生产）
```bash
sudo systemctl start   sn-monitor      # 启动
sudo systemctl stop    sn-monitor      # 停止(会杀源, 需配合摄像头)
sudo systemctl restart sn-monitor# 重启(务必同时重启摄像头推流!)
sudo systemctl status  sn-monitor
tail -f /data/soph_SN/logs/sn_monitor.service.log
```
service 的 `ExecStart`：
```
/usr/bin/python3 /data/soph_SN/sn_monitor.py --rtsp rtsp://192.168.1.9:8554/live0 --idle 0 --mi 5
```
不带 `--profile` → 用默认档 **small**（监控小板）。若这台要监控**大板**，把 `--profile big` 加进 `ExecStart`。
配置：`--idle 0`（永不超时）、`KillSignal=SIGINT`（干净 TEARDOWN 保住下次可拉流）、`Restart=always`。

### 4.2 手动前台跑（调试，最直观）
```bash
sudo systemctl stop sn-monitor
cd /data/soph_SN
sudo python3 sn_monitor.py --rtsp rtsp://192.168.1.9:8554/live0 --idle 0    # 小板(默认)
sudo python3 sn_monitor.py --rtsp rtsp://192.168.1.9:8554/live0 --idle 0 --profile big  # 大板
# 同时重启摄像头推流
```

### 4.3 档位系统（v18 核心）

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
| `--rtsp` | (必填) | RTSP 地址 |
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
在工作站改 `sn_monitor.new.py` 后：
```bash
# 工作站
scp sn_monitor.new.py linaro@10.80.40.53:/tmp/sn_new.py
# 板子(先备份再覆盖，注意备份留工作站、板子只留最新)
sudo cp -a /data/soph_SN/sn_monitor.py /tmp/sn_bak && \
sudo cp /tmp/sn_new.py /data/soph_SN/sn_monitor.py && \
python3 -m py_compile /data/soph_SN/sn_monitor.py
```
> 拷贝 .py 不影响正在跑的进程；生效需重启服务(→ 记得同时重启摄像头)。

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
sudo systemctl enable --now sn-monitor    # 识别
sudo systemctl enable --now sn-uploader   # 上传(sidecar 必须 root，否则 .uploaded 写不进→重复上传)
journalctl -u sn-uploader -f           # 看上传实时日志
```
之后放板即「自动识别 → 自动上传 → 前端反映」，全程无需再敲命令。

**只想临时手动上传**（不启用服务时，另开终端）：
```bash
sudo python3 /data/soph_SN/sn_uploader.py --url http://10.80.40.57:8099/api/v1/captures --interval 3
```

> - `.57` 产测后端需在 `:8099` 起着（`cd product_test && ./start.sh`）才能接收入库。
> - 完整接口契约 / 落盘路径 / 排障见 **`SN_CAPTURE_README.md`**（本仓库根目录）。

### 4.7 参数扫描工具 `profile_probe.py`（新模组快速定档）

每上一款**新模组/新板型**，SN 铭牌的位置与占比都变，档位参数（各向异性裁剪 `crop_w/crop_h/crop_cy` + 切块 `tile_grid/tile_up` + ROI 路）得重调。`profile_probe.py` 把这些参数**笛卡尔积扫一遍**，对「图片+期望SN」逐组合报 **耗时 / SN是否命中(带score) / 候选数**，末尾直接给「命中且最快」的推荐组合，可抄进 `PROFILES` 定新档。**只读**：仅跑 OCR，不落库、不上传、不碰在跑的服务。

> 需在**板子上跑**（要 sail 加载 OCR bmodel）。主副本在工作站仓库，和 `sn_monitor.new.py` 一样 scp 到 `/data/soph_SN/` 后运行。

**文件路径**：
| 项 | 路径 |
|----|------|
| 脚本主副本（工作站） | `/media/sophgo/xiaohao.liu/SN_cratch/profile_probe.py` |
| 脚本部署位置（板子） | `/data/soph_SN/profile_probe.py` |
| 裁剪图输出目录（默认） | `./profile_probe_out/`（在板子即 `/data/soph_SN/profile_probe_out/`），文件名用 **宽/高/锚点** 组合：`<图名>_cw0.30_ch0.40_cy0.50.jpg`，供人眼核对每种裁剪框住了哪块 |

**使用方式**：
```bash
# 部署(工作站→板子)
scp profile_probe.py linaro@10.80.40.53:/data/soph_SN/

# 板子上跑。先停服务让出摄像头源(本工具只读本地图, 不拉流, 但避免占 NPU)
sudo systemctl stop sn-monitor
cd /data/soph_SN

# ① 默认:扫 sn_results/ 下历史命中帧(期望SN从文件名反解), 跑内置常用网格
sudo python3 profile_probe.py

# ② 指定新模组测试图 + 期望SN(最常用)
sudo python3 profile_probe.py --jobs "/tmp/newmod_a.jpg:BCXX...,/tmp/newmod_b.jpg:BCYY..."

# ③ 自定义扫描网格(收窄范围, 加快)
sudo python3 profile_probe.py --jobs-file jobs.txt \
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
拿推荐行的 `PROFILES 片段` 抄进 `sn_monitor.new.py` 的 `PROFILES` 新增一档，再按 4.5 部署即可。

---

## 5. 实现原理

### 5.1 整体流水线
```
RTSP(4K,TCP) ─► 后台线程持续 read 排空缓冲(只留最新帧)
        │
             主循环 poll(1s)
             │
          质量门 ─► 运动/稳定状态机 ─► 铭牌检测(白底标签∪Sobel条码)
   │(稳定+有铭牌+非同板)
            ▼
          按档位裁剪+(不)切片 单帧识别 ─► 多帧投票+格式校验 ─► 二次确认门 ─► 落库
       │
     (可选) sn-uploader sidecar ─► POST 到 .57 product_test ─► DB + 前端「SN 抓拍」页
```

### 5.2 关键设计决策（每条都对应踩过的坑）

**① 强制 TCP + 常驻单连接，绝不主动断。**
摄像头是单客户端源，UDP 会脏断卡死、断开即源退出。所以全程一条 TCP 持久连接，永不主动 release。

**② 后台线程持续 read()，主循环只取最新帧。**
FFMPEG 后端 `BUFFERSIZE=1` 无效，直接 read 会回放缓冲里的旧帧。后台线程不停排空解码缓冲，主循环 `latest()` 永远拿最新解码帧，根治旧帧回放。cap 的 read/release 全在该线程做，交接靠 `set_cap`，杜绝跨线程释放崩溃。

**③ 瞬时解码错误不重连（断"重连 churn"自激）。**
HEVC 常见 `PPS id out of range`（坏包）、`Could not find ref`/`Error constructing RPS`（丢参考帧）都是**瞬时**错误，解码器等下一个 IDR 关键帧自愈。若一见错就 release 重连，会从 GOP 中间重进 → 又一片 ref-error → 重连 churn 自激螺旋。策略：无新鲜帧且解码错在刷时**保持同连接等 IDR**，只有无解码活动持续无帧或超死线(`--wedge-dead-s`)才判真坏流重连。见 `archive` 中 v14 起的修复。

**④ 各向异性裁剪 + 垂直锚点（v18 档位）。**
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

**⑩ 裁剪后"不切片单帧"最准（v18，治好 2→7 错读）。**
裁剪收紧后干扰字段已被裁掉，此时**不切片**(`tile_grid=(1,1)`)让 PP-OCR 的 det 自己把 `SN:` 与编码分成**合适大小的框**，编码框长宽比不超标 → 数字读干净。
> **2→7 根因**：`tile_grid=(1,2)` 切片带 0.3 重叠、切片够宽时把整行 `SN:BCCW6N24070100601` 装进**一个框** → 撞 rec bmodel 宽度天花板 `img_size=[[640,48]]`（长宽比 >13.3 被压扁，曾观测 14.07）→ 数字畸变 2→7，且错读帧分更高被投票选中。改 1×2→1×1 不切片后离线 36/36 确定性读对。

---

## 6. 排障速查

| 现象 | 排查 |
|------|------|
| 拉不到流 / 一直重连 | 摄像头推流是否在推？eth1 `carrier=1`？`ping 192.168.1.9` 通？重启服务后是否重启了摄像头？ |
| 小板漏识 | **首查摆放**：SN 贴纸面是否朝相机、在右上区？诊断法：拉全幅 miss 帧**网格逐格 OCR** 看 SN 真实 cx/cy（`debug/` 有留证）。若整幅无 SN 即摆错。 |
| 大板漏识 | 确认加了 `--profile big`(裁剪几何不同)；留证在 `debug/` |
| 数字错读(如 2→7) | 已由 v18 不切片修复；若复现，确认 profile 的 `tile_grid` 是 (1,1) |
| 同一板反复识别 | 已由同板去抖修复；若仍有，适当调大 `--dt` |
| 反应慢 | 调小 `--poll`(如 0.5) |
| 模型释放太快/太慢 | 调 `--ocr-idle-unload`(慢→调小，常驻→设 0) |
| 误报别的字段 | 收紧 `--sn-min-len/max-len` 或加 `--sn-prefix` |

日志与结果：
```
tail -f /data/soph_SN/logs/sn_monitor.service.log
cat /data/soph_SN/sn_results/sn_list.txt
ls /data/soph_SN/debug/          # 漏检/待确认留证
```

---

## 7. 版本演进（历史备份在 `archive/board_versions/`）

| 版本 | 关键改动 |
|------|----------|
| v8→v9 | 多帧投票+格式校验落地，常驻服务(TCP/SIGINT/Restart) |
| v11 | 死亡螺旋修复(瞬时坏包不杀源)，召回硬化(白底∪Sobel)，自适应上采样 |
| v14 | **断重连 churn**：ref-error 等 IDR 自愈不重连；灭刷屏 |
| v15 | **提速**：模型常驻 + 渐进式 L1/L2/L3 命中即停 |
| v16 | 模型闲时释放懒加载 + `--poll` 快轮询降延迟 |
| v17 | **同板去抖**(静板不重识，连带修好模型释放过晚) |
| **v18** | **档位系统**(big/small 各向异性裁剪+锚点) + **不切片单帧**(治好 2→7 错读) + 长度门 17。当前稳定版 |
