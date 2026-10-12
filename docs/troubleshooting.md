# 排障速查

> 从主 README §5 拆出。按现象查排查动作。
> MediaMTX / WebRTC 预览排障见 [deployment-mediamtx.md](deployment-mediamtx.md)。

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
| **预览画面卡顿** | 网页工具栏切 `1080p`（识别用的图不受影响）。或切换到 **WebRTC 模式**（见 [deployment-mediamtx.md](deployment-mediamtx.md)） |
| **WebRTC 黑屏无画面** | ① `systemctl is-active mediamtx sn-preview-ffmpeg` — 两个都在跑？② `journalctl -u sn-preview-ffmpeg -n 5` — ffmpeg 是否报错？③ 浏览器控制台是否有 WebRTC ICE 连接失败？（办公网到板子 `:8889` 是否可达？） |
| **双路：第二路没接上** | 启动日志有 `⚠ 第二路 ... 暂不可达, 本次降级为单路` = 正常降级。查：相机2 是否接在同一台路由器/交换机？`ping 192.168.1.8`？`ip route get 192.168.1.8` 是否回 `dev eth1`？端口 `8554` 可连？ |
| **双路：背面图不变/是旧画面** | 相机2 推流是否真在动？背面**无断线自愈**，掉线需重启服务。详见 [features/dual-camera.md](features/dual-camera.md) |
| **双路：卡片只显示正面** | ① 上传是否带 `side`？② `.57` 库里那两行 `captured_at` 是否真的一致？③ 老库是否忘了 `ALTER TABLE ... ADD COLUMN side`？见 [features/dual-camera.md](features/dual-camera.md) |
| **网页上时间与筛选日期差一两天** | 正常：卡片显示**抓拍时间**，筛选按**入库时间** |
| **上传器灌了一堆重复** | 预期行为：uploader 只做**文件级**去重，不管内容/业务重复。原因与善后见 [features/uploader.md](features/uploader.md) |

日志与结果：

```
tail -f /data/soph_SN/logs/sn_monitor.service.log
cat /data/soph_SN/sn_results/sn_list.txt
ls /data/soph_SN/debug/ # 漏检/待确认留证
```
