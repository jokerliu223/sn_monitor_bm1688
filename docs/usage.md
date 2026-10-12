# 使用 —— 启动、档位、参数

> 从主 README §4 拆出。讲"怎么用"：启动命令、档位系统、全部 CLI 参数。
> 快速上手见主 [README.md](../README.md) §2；部署见 §3。

## 一条命令启动全部

```bash
# 首次部署：装四个单元
sudo cp /data/soph_SN/deploy/sn-monitor-big.service \
        /data/soph_SN/deploy/sn-uploader.service \
        /data/soph_SN/deploy/sn-preview-ffmpeg.service /etc/systemd/system/
sudo systemctl daemon-reload

# 一条命令：识别 + 上传 + WebRTC 预览全拉起来
sudo systemctl enable --now sn-monitor-big
```

> 换板型: `disable --now sn-monitor && enable --now sn-monitor-big`。
> ExecStartPre 自动拉起 uploader + ffmpeg 双路推流，ExecStopPost 自动停止，失败不阻塞识别。

调试时也可手动跑：`python3 sn_monitor.py <参数>`（见下方参数表）。

## 档位系统（V2.0.1 核心）

一个 `--profile` 打包一档参数（裁剪几何 + 切片 + 抽帧 + 确认门）。显式 CLI 标志仍覆盖档位。

| 参数 | small(默认) | big | 含义 |
|------|------|------|------|
| `crop_w × crop_h` | 0.40 × 0.35 | 0.30 × 0.40 | 各向异性中心裁剪(宽比×高比)，只留铭牌带 |
| `crop_cy` | 0.32(偏上) | 0.50(居中) | 裁剪垂直锚点；小板 SN 在画面上带 |
| `tile_grid` | (1,1) | (1,1) | **不切片单帧**(见 [internals.md](internals.md) ⑩) |
| `tile_up` | 3.0 | 2.5 | 裁剪块上采样倍数 |
| `frames` | 2 | 2 | 单次识别抽帧数(跨帧投票修掉字) |
| `confirm` | 2 | 1 | 连续同一 SN 达此次数即落库 |
| `sn_min_len` | 17 | 17 | SN 最短长度门(挡短串误报) |
| `poll` | 1.0 | 0.4 | 侦测轮询间隔 |
| `rescue` | True | False | 救援旋转遍 |

覆盖档位的显式标志：`--crop-w` / `--crop-h` / `--crop-cy` / `--center-crop`(宽高同设) / `--frames`。

## 其它关键参数

| 参数 | 默认 | 说明 |
|------|------|------|
| `--rtsp` | (必填) | 摄像头1（正面）RTSP 地址，识别主路 |
| `--rtsp2` | (空) | 摄像头2（背面）RTSP；**空=单路**（=V2.0.2） |
| `--rtsp2-grab-wait` | 0.5 | 命中时从第二路取最新帧的最大阻塞秒 |
| `--profile` | small | 档位: `small`/`big`(见上) |
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

> 稳定性兜底参数 `--wedge-*` / `--stale-*` 一般不用动。预览参数完整说明见 [features/preview-v3.md](features/preview-v3.md)。
