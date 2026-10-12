# MediaMTX + WebRTC 预览部署（★ V4.0）

> 从主 README §4.8 拆出并校正（原文有损坏）。讲调机预览的完整链路、板子侧 / `.57` 侧部署、验证与排障。
> 板子地址、端口等"改哪里"见主 [README.md](../README.md) §4 IP / 配置改动对照表。

## 完整通信流程

```
【板子】                          【.57 服务器】                 【浏览器】
sn-preview-ffmpeg.service
 ffmpeg -i live1(正面)
  → h264_bm 硬件编码 (<5ms)
  → RTMP push ───────────► MediaMTX (start.sh 内置)
                            RTMP  :1935  ← 接收推流
                            WebRTC:8889  WHEP endpoint
                            ICE/UDP :8189 媒体传输
 ffmpeg -i live1(背面)
  → h264_bm
  → RTMP push ───────────►  (cam_back)

                           uvicorn :8099 (FastAPI 后端)
                            /api/v1/camera/webrtc/config
                            /api/v1/camera/webrtc/whep/{cam}
                              → proxy → 127.0.0.1:8889
                                        ▲
                                        │ WHEP POST SDP offer
                                        ▼
                                                      CameraPreviewView.vue
                                                       RTCPeerConnection
                                                       setRemoteDescription(answer)
                                                       <video> × 2 (front/back)
                                                       ICE 断线自动重连
```

## 关键设计决策

- **子码流独立**：ffmpeg 拉 `live1` 子码流（768×572 HEVC，与识别主码流 `live0` 不同连接），不占识别资源。
- **MediaMTX 放 `.57`**：与浏览器同源网络（`10.80.40.x`），ICE 不跨网段。
- **WHEP 代理**：`.57` 后端转发 SDP → 本地 MediaMTX（`127.0.0.1:8889`），避免浏览器直连板子的跨域问题。
- **h264_bm 限制**：只接受 BM1688 硬件解码器输出的 BM-native 帧，不能用 `pipe:0` 软件管道（必须 `-i rtsp://...`）。

## 板子端部署

```bash
# 部署双路 ffmpeg 推流单元（V4.0.1 起双路合并在一个单元里）
sudo cp /data/soph_SN/deploy/sn-preview-ffmpeg.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sn-preview-ffmpeg
```

> 该单元随 `sn-monitor-big` 的 ExecStartPre 自动拉起，一般无需单独操作。
> ★ 重启推流前后别忘铁律 1：摄像头是单客户端源，重启服务要一并重启摄像头推流。

## `.57` 服务器端部署

MediaMTX 跑在 `.57`（`10.80.40.57`）上的 `product_test` 仓库内，由 `start.sh` 一键拉起。详细可修改项见该仓库 README 的「MediaMTX 部署」节。要点：

```bash
# 1) 下载 MediaMTX（仅首次，.57 是 x86_64）
cd /tmp && wget https://github.com/bluenviron/mediamtx/releases/download/v1.8.0/mediamtx_v1.8.0_linux_amd64.tar.gz
sudo mkdir -p /opt/mediamtx && sudo tar xzf mediamtx_v1.8.0_linux_amd64.tar.gz -C /opt/mediamtx/

# 2) 配置文件 product_test/mediamtx.yml（要点）
#    rtmpAddress: :1935   ← 板子推流入口（改端口要同步改板子 preview_supervisor.sh）
#    webrtcAddress: :8889 ← 浏览器 WHEP 端点
#    api: yes (:9997)     ← 查流状态
#    srt: no              ← 故意关掉，避端口 8000 冲突
#    paths: cam_front / cam_back（sourceOnDemand: no，常驻等推流）

# 3) 启动：start.sh 已集成（先 pkill -f mediamtx 再起，trap 随脚本退出清理）
cd /media/cvitek/xiaohao.liu/product_test
./start.sh
```

## 验证

```bash
# .57 MediaMTX 流状态（应 ready=true）
curl -s http://127.0.0.1:9997/v3/paths/list | python3 -m json.tool

# 板子 ffmpeg 服务
systemctl is-active sn-preview-ffmpeg    # 应: active

# 浏览器 F12 控制台
# 应: WebRTC front connected / WebRTC back connected
```

## 排障

- 一直"WebRTC 连接中…"：先确认 `.57` 的 `./start.sh` 已执行，再查 `curl 127.0.0.1:9997/v3/paths/list` 里 `cam_front ready=true`。
- 黑屏无画面：`systemctl is-active sn-preview-ffmpeg`（在跑？）→ `journalctl -u sn-preview-ffmpeg -n 5`（ffmpeg 报错？）→ 浏览器控制台是否 ICE 连接失败（办公网到板子 `:8889` 可达？）。
