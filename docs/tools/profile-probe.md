# tool：`profile_probe.py` —— 参数扫描工具（新模组快速定档）

> 主 [README.md](../../README.md) 只提一句"有这个工具、板上没有、需 scp"；本文件写**怎么用、怎么读输出、怎么抄回档位**。
> 相关：[../CHANGELOG.md](../CHANGELOG.md)（V2.0.2 引入）、[../features/uploader.md](../features/uploader.md)（另一个辅助工具）。

## 干什么

每上一款**新模组/新板型**，SN 铭牌的位置与占比都变，档位参数（各向异性裁剪 `crop_w/crop_h/crop_cy` +
切块 `tile_grid/tile_up` + ROI 路）得重调。`profile_probe.py` 把这些参数**笛卡尔积扫一遍**，
对「图片 + 期望 SN」逐组合报 **耗时 / SN 是否命中(带 score) / 候选数**，
末尾直接给「命中且最快」的推荐组合，可抄进 `PROFILES` 定新档。

**只读**：仅跑 OCR，不落库、不上传、不碰在跑的服务。

## 前提：先 scp 上板

脚本在仓库的 `tools/` 下，但**板上当前没有这个文件**（未随部署上板）。用前先拷到板子根目录：

```bash
scp tools/profile_probe.py linaro@10.80.40.53:/data/soph_SN/     # 与 sn_uploader.py 同放根，保持扁平
```

## 文件路径

| 项 | 路径 |
|----|------|
| 脚本（仓库内） | `tools/profile_probe.py` |
| 运行位置（板子，手动 scp 后） | `/data/soph_SN/profile_probe.py` |
| 裁剪图输出目录（默认） | `./profile_probe_out/`（在板子即当前工作目录下），文件名用 **宽/高/锚点** 组合：`<图名>_cw0.30_ch0.40_cy0.50.jpg`，供人眼核对每种裁剪框住了哪块。可用 `--out-dir` 改 |

## 使用方式

```bash
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

> `--jobs-file` 每行 `路径 期望SN`（空格/冒号分隔，`#` 开头为注释）。
> 默认网格 3×3×2×2×3 = **约 108 组合/图**，用 `--crop-w` 等收窄可显著提速。
> 另有 `--repeat`（每组合计时重复取最快，默认 2）与 `--min-score`（推荐时命中分下限，默认 0）。

## 预期输出

```
[probe] 2 图 × 108 组合; 裁剪图→ /data/soph_SN/profile_probe_out

#### newmod_a.jpg want=BCXX... 尺寸3840x2160 ####
  cw0.30 ch0.40 cy0.50 1x1@2.5x roi- | 0.83s | SN=✅0.971 | 候选 6
  cw0.40 ch0.35 cy0.32 1x1@3.0x roi- | 0.91s | SN=❌    | 候选 4
  ...
>> 推荐(命中 2/2 图, 累计 1.66s): cw0.30 ch0.40 cy0.50 1x1@2.5x roi-
   PROFILES 片段: "crop_w":0.3, "crop_h":0.4, "crop_cy":0.5, "tile_grid":(1,1), "tile_up":2.5, "roi_path":False
```

拿推荐行的 `PROFILES 片段` 抄进 `sn_monitor.py` 的 `PROFILES` 新增一档，再重新部署脚本到板子即可。

## 为什么要"只读、单独跑"

它跑的是同一套 PP-OCR bmodel，会**占满 NPU**；虽然不拉流、不抢摄像头源，但和识别服务同时跑会让两边都变慢，
所以先 `systemctl stop` 再跑。跑完记得 `start` 回来（并重启摄像头推流，见 README 铁律 1）。

裁剪图留在 `profile_probe_out/` 是**刻意**的：光看"命中/不命中"不够，
要看**裁剪框到底框住了哪块**——定档失败的典型原因是框歪了（框到别的印刷字段、或没框全 SN），
肉眼对图比看分数快得多。
