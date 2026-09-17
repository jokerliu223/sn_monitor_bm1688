# 版本演进（SN 监控系统）

> 本文件是主 [README.md](../README.md) 的**历史分册**（原 README 第 7 节「版本演进」搬到这里并展开）。
> 主 README 只写**当前版本怎么部署、怎么用**；这里记**改过什么、为什么改**。
>
> 当前发布版本：**V3.0**。

## 版本号约定

| 号段 | 含义 |
|------|------|
| `V0.x` | 探索板阶段 |
| `V1.x` | 迭代稳定 / 提速 |
| `V2.0.1` | 首个对外发布版（可部署） |
| `V2.0.2` 起 | 发布后增量更新 |
| `V3.0` 起 | 预览 / 识别同进程 |

---

## 总览

| 版本 | 阶段 | 关键改动 |
|------|------|----------|
| **V0.8 ~ V0.9** | 探索板起步 | 多帧投票 + 格式校验落地；常驻服务(TCP/SIGINT/Restart)；死亡螺旋修复(瞬时坏包不杀源)、召回硬化(白底∪Sobel)、自适应上采样 |
| **V1.4** | 迭代稳定 | **断重连 churn**：ref-error 等 IDR 自愈不重连，灭刷屏 |
| **V1.5** | 迭代提速 | 模型常驻 + 渐进式 L1/L2/L3 命中即停 |
| **V1.6** | 迭代提速 | 模型闲时释放懒加载 + `--poll` 快轮询降延迟 |
| **V1.7** | 迭代稳定 | **同板去抖**(静板不重识，连带修好模型释放过晚) |
| **V2.0.1** | ★ 发布版 | **档位系统**(big/small 各向异性裁剪+锚点) + **不切片单帧**(治好 2→7 错读) + 长度门 17。首个对外发布、可部署 |
| **V2.0.2** | 发布后增量 | 参数扫描工具 `tools/profile_probe.py`(新模组快速定档) + 上传 sidecar `tools/sn_uploader.py` 文档化 + README 发布化 |
| **V2.1** | 上一版 | **双路摄像头**(`--rtsp2` 正面识别命中时同步抓背面一帧，正反配对上传) + `.57` 产测端 `side` 字段与正/反配对展示 + `sn-uploader.service` 修正(路径兼容两种布局、补 `--interval`) + **`sn-monitor-big.service` 内置双路**(大板工位开箱即双路，不再需要手改 ExecStart) |
| **V3.0** | ★ 当前 | **调机预览内嵌**(`sn_preview_embed.py`)：预览并入 `sn_monitor.py`，与识别**共享同一路解码**(相机仍只有 1 条连接)，调镜头时识别照常跑，不再需要停预览↔起识别来回切。新增 6 个 `--preview*` 参数(含 `--no-preview` 一键回退) + 只读抓拍回显接口 + `.57` 底部抓拍横条；旧独立预览服务 `preview_service.py` 停用保留 |

---

## 逐版说明

### V3.0.1 —— 服务合并（一条命令启动全部）【最新】

**改动**：deploy/ 三个 systemd 单元各加 1–2 行（`Wants=` + `PartOf=` + `WantedBy=`），
实现 `systemctl enable --now sn-monitor-big` 一条命令同时拉起识别+预览+上传。
代码零改动。

| 操作 | 效果 |
|------|------|
| `systemctl start sn-monitor-big` | 自动 start uploader（`Wants=`） |
| `systemctl stop sn-monitor-big` | 同步 stop uploader（`PartOf=`） |
| `systemctl enable sn-monitor-big` | 自动 enable uploader（`WantedBy=`） |
| uploader 自身 crash | 独立重试（`Restart=always`），不影响识别 |
| `systemctl stop sn-uploader`（单独） | monitor 不受影响（含临时关上传） |

### V3.0 —— 调机预览内嵌

**解决的问题**：V1/V2 的预览是**独立服务** `preview_service.py`，它要自己拉一路 RTSP。而相机是**单客户端源**，第二路连接会把识别那路踢掉 —— 于是"调镜头"和"跑识别"只能二选一，来回 `systemctl stop/start`。

**做法**：把预览做成 `sn_preview_embed.py`，挂在识别**已经存在**的 `FrameReader` 上，用 `_peek` 非阻塞取最新帧 → Bmcv 硬件编 JPEG → MJPEG 推网页。相机源因此**仍然只有 1 条 RTSP 连接**。

**对识别主链路的影响**：`sn_monitor.py` 是 **+31 行 / −0 行**的纯加法 —— try-import 兜底、6 个 CLI 参数、一处 `attach()`、`finally` 里一处 `preview_stop()`。识别逻辑逐字节不变；`--no-preview` 一键回退到 V2.1 行为。

**同版附带**：只读抓拍回显接口（读 `sn_results/` 最新已配对帧，**不入库**）+ `.57` 页面底部抓拍横条。

**遗留**：旧独立预览服务 `preview_service.py` **停用但保留**（`systemctl stop` + `disable`），脚本与它自己的接口说明留作回退路径，不进任何 unit。

详见 **[features/preview-v3.md](features/preview-v3.md)**。

---

### V2.1 —— 双路摄像头（正/反两面）

**解决的问题**：一个工位只放一块板，但要同时留**正面（SN 贴纸面）**和**背面**的证据，人工翻板重拍一遍成本高。

**做法**：加 `--rtsp2` 起第二个 `FrameReader`，正面识别**命中的那一刻**同步抓一帧背面。背面那路**一帧 OCR 都不跑**（只要图，不要识别），省下整个 NPU 开销；BM1688 两个 vdec 核各扛一路 4K HEVC。

**配对不靠时间同步**：两路独立连接、没有共同时钟 —— 命中后把**正面的 ISO 时间戳原样赋给背面**再落盘。所以 DB 里两行 `captured_at` 一致是"写出来的"而非"测出来的"，配对必然成立。

**`--rtsp2` 不配 = 单路**，构造 `reader2=None` + 全程 `if reader2` 守卫，单路代码路径一字不改。

**同版附带**：`.57` 端 `sn_captures` 表新增 `side` 列（需手动 `ALTER TABLE`）、前端按 `(sn, captured_at)` 归组并排展示。

**单元变更**：`sn-monitor-big.service` 把 `--rtsp2` 直接写进 `ExecStart`，大板工位开箱即双路；`sn-uploader.service` 修正为路径兼容两种布局并补 `--interval`。

详见 **[features/dual-camera.md](features/dual-camera.md)**。

---

### V2.0.2 —— 发布后增量

- 新增参数扫描工具 `tools/profile_probe.py`：对新模组/新板型把裁剪+切片参数笛卡尔积扫一遍，直接给「命中且最快」的推荐组合。**只读**跑 OCR，不落库、不上传。详见 **[tools/profile-probe.md](tools/profile-probe.md)**。
- 上传 sidecar `tools/sn_uploader.py` 文档化。详见 **[features/uploader.md](features/uploader.md)**。
- README 发布化（部署说明 / 模型部署 / 辅助工具说明）。

---

### V2.0.1 —— 首个发布版

两个核心改动，都是"治本"性质的：

1. **档位系统**（`--profile small|big`）：不同板卡 SN 位置不同 —— 小板 SN 在画面**偏上、略偏右**（cy≈0.30），大板居中。用各向异性裁剪 `crop_w×crop_h`（宽比≠高比）+ 垂直锚点 `crop_cy` 只留铭牌带，把板上其它干扰印刷字段裁掉，候选池只剩干净 token。
2. **不切片单帧**（`tile_grid=(1,1)`），治好 **2→7 错读**。

> **2→7 根因**（值得记住）：`tile_grid=(1,2)` 切片带 0.3 重叠、切片够宽时把整行 `SN:BCCW6N24070100601` 装进**一个框** → 撞 rec bmodel 宽度天花板 `img_size=[[640,48]]`（长宽比 >13.3 被压扁，曾观测 14.07）→ 数字畸变 2→7，且错读帧分更高被投票选中。改 1×2→1×1 不切片后离线 36/36 确定性读对。

同版确定 SN 长度门 min=17。

---

### V1.7 —— 同板去抖

记住上次识别的画面帧 `recog_ref`；locked 状态下当前稳定画面与它 `fdiff<dt` 判"同板在位"，跳过重识。

根治**静止板被噪声/OSD 秒跳字（`1970-01-01 …`）的假运动反复触发** —— 该问题同时导致模型迟迟不释放，连带修好。

---

### V1.6 —— 闲时释放 + 快轮询

- **模型闲时释放**：距上次识别超 `--ocr-idle-unload`（默认 30s）自动释放模型省 NPU 内存，下块板懒加载（~0.4s）。活跃期常驻，命中秒回。
- **侦测轮询与退避解耦**：状态机轮询独立成 `--poll`，放板到识别反应降到 ~1~2s；`--mi`（5s）只留作识别异常退避。

---

### V1.5 —— 提速

模型常驻 + 渐进式 L1/L2/L3 识别，命中即停，不再每帧跑满全流程。

---

### V1.4 —— 断重连 churn

HEVC 常见 `PPS id out of range`（坏包）、`Could not find ref` / `Error constructing RPS`（丢参考帧）都是**瞬时**错误，解码器等下一个 IDR 关键帧自愈。

**若一见错就 release 重连，会从 GOP 中间重进 → 又一片 ref-error → 重连 churn 自激螺旋。** 策略改为：无新鲜帧且解码错在刷时**保持同连接等 IDR**，只有无解码活动持续无帧或超死线（`--wedge-dead-s`）才判真坏流重连。

---

### V0.8 ~ V0.9 —— 探索板起步

- 多帧投票 + 格式校验落地。
- 常驻服务化：强制 TCP + `KillSignal=SIGINT` + `Restart=always`。
- **死亡螺旋修复**：瞬时坏包不杀源（V1.4 的雏形）。
- 召回硬化：白底 ∪ Sobel 双路召回。
- 自适应上采样。
