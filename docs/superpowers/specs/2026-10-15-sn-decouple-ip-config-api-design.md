# SN 抓拍系统：数据层解耦补齐 + IP 配置化/改 IP 接口 设计文档

- **日期**：2026-10-15
- **作者**：duriankunsman（经 Claude 协助）
- **关联 worklog**：`.claude/worklogs/sn-decouple-ip-config-api.md`
- **状态**：待用户评审

---

## 1. 背景与目标

系统由两台物理独立主机组成：

| 角色 | 机器 | 仓库 | 部署归宿 |
|---|---|---|---|
| SN 抓拍服务 | SE9 盒子 `10.80.40.68`（`linaro`） | 本机 `sn_monitor_bm1688` → `/data/soph_SN` | 工厂 SE9 算力盒子 |
| webserver（前后端+MediaMTX+DB） | `10.80.40.57`（`martin-xuan`） | `.57:/media/cvitek/xiaohao.liu/product_test` | 工厂服务器 |

数据单向流动：SE9 识别落盘 → `sn_uploader.py` POST 到 webserver `/api/v1/captures` 入库。

**三个目标**（对应用户三问）：
1. **数据层解耦/CRUD 完整性**：数据的真·CRUD 在 webserver，已有增/查/取图，**缺删除**。补齐 `DELETE` 接口，使后续增删功能可直接调 API。
2. **IP 可操作化 + start.sh 首跑提示**：当前无配置文件，IP 散落多处写死；start.sh 无首跑 IP 提示。
3. **改 webserver IP 快速接口**：webserver IP 变更目前需手改 2~3 处文件。提供一键脚本。

### 工作边界（硬约束）
- `.57`：只能改 `/media/cvitek/xiaohao.liu/product_test/`。
- 本机：只能改 `sn_monitor_bm1688/`。

---

## 2. 现状全景（已验证）

```
┌──── SE9 10.80.40.68  (本机仓库 sn_monitor_bm1688 → /data/soph_SN) ────┐
│ sn_monitor.py ─落盘→ sn_results/ (sn_*.jpg/.json)   [无DB / 无数据API] │
│ tools/sn_uploader.py ─POST→           持有 webserver IP: --url / env   │
│ deploy/preview_supervisor.sh ─RTMP→   持有 webserver IP: rtmp:// 写死★  │
│ preview_service.py / sn_preview_embed.py (Flask)   只读预览 API        │
└───────────────────────────────┬──────────────────────────────────────┘
                                 ▼ (eth0 公司网)
┌──── webserver 10.80.40.57 (product_test, 监听 0.0.0.0) ───────────────┐
│ backend/api/captures.py + services/sn_capture_service.py (SQLAlchemy) │
│   POST /captures ✅  GET /captures ✅  GET /captures/{id}/image ✅      │
│   DELETE ❌ ← 本设计补齐                                               │
│ backend/api/camera.py  预览中继 (PREVIEW_BOARD_BASE=板子IP, 有env)     │
│ start.sh  构建前端+起MediaMTX+起后端 (--host 0.0.0.0, 无首跑IP提示)    │
└───────────────────────────────────────────────────────────────────────┘
```

### 关键事实
- **F1**：SE9 不含 DB，只落盘文件。真·数据层（`results.db` SQLite + `data/sn_captures/`）在 webserver。
- **F2**：`SnCaptureService` 用 SQLAlchemy ORM（`create_session_factory`），有 `add/list/get`，**无 `delete`**。
- **F3**：webserver 监听 `0.0.0.0`，**自身不持有对外 IP `10.80.40.57`**。真正持有该值的是 **SE9 侧**（uploader `--url` + supervisor RTMP）。
- **F4**：webserver 侧持有的是**板子 IP**（`camera.py:35` `PREVIEW_BOARD_BASE=http://10.80.40.68:8090`，有 env 覆盖）。
- **F5**：SE9 侧 IP 落点——webserver IP 3 处（`sn-uploader.service --url` 写死 / `sn_uploader.py:60` 默认(有 `SN_UPLOAD_URL`) / `preview_supervisor.sh:31,32` RTMP **写死无覆盖★**）；摄像头 IP 多处（service 的 `--rtsp/--rtsp2`、`preview_service.py:44-45` 有 env、supervisor 写死）。

---

## 3. 设计（三条工作线）

三条线相互独立，可分别实现、分别验证。

### 线 A（SE9 / 本机仓库）配置集中化 + 一键改 webserver IP

**决策**：两端各自 env（用户选）。SE9 侧新增一个集中 env 持有"本机要连的 webserver / 摄像头 IP"。

#### A.1 新增 `deploy/sn.env`（集中事实源）
```sh
# SN 抓拍服务 — 对外地址集中配置（SE9 侧持有：要连的 webserver 与摄像头）
# 改这里 + 重启相关单元即可，不改 .py / .service
SN_WEBSERVER_IP=10.80.40.57
SN_UPLOAD_URL=http://${SN_WEBSERVER_IP}:8099/api/v1/captures
RTMP_BASE=rtmp://${SN_WEBSERVER_IP}:1935
# 摄像头（用户称固定，纳入便于一览；留空则用各自默认）
CAM_FRONT_RTSP=rtsp://192.168.1.9:8554
CAM_BACK_RTSP=rtsp://192.168.1.8:8554
```
> 注：systemd `EnvironmentFile` 不做 `${VAR}` 展开，故在脚本侧 `source` 后展开；service 侧直接引用最终值。实现时二选一：
> (a) `sn.env` 同时写出展开后的 `SN_UPLOAD_URL`/`RTMP_FRONT`/`RTMP_BACK` 终值（供 service `EnvironmentFile` 直接用）；
> (b) 由 `set-webserver-ip.sh` 在写文件时把终值算好写入。采用 (b)：脚本是唯一写入口，保证 service 读到的是裸终值。

#### A.2 改造消费端
- `deploy/sn-uploader.service`：`ExecStart` 的 `--url` 改为不带参（让脚本读 `SN_UPLOAD_URL` 默认），并加 `EnvironmentFile=-/data/soph_SN/deploy/sn.env`。（`-` 前缀：文件缺失不致单元失败，向后兼容。）
- `deploy/preview_supervisor.sh`：顶部 `[ -f .../sn.env ] && source .../sn.env`；RTMP 目标改 `rtmp://...` ← `${RTMP_BASE:-rtmp://10.80.40.57:1935}/cam_front`（**消除"写死无覆盖"痛点 F5★**）。摄像头 URL 同理可选 `${CAM_FRONT_RTSP:-...}`。
- `sn-monitor*.service`、`tools/sn_uploader.py`、`preview_service.py`：保留现有默认值不破坏；仅让 env 存在时优先。

#### A.3 新增 `deploy/set-webserver-ip.sh <新IP>`
职责：唯一写入口。
1. 校验入参是合法 IP。
2. 把 `SN_WEBSERVER_IP` 及派生的 `SN_UPLOAD_URL` / `RTMP_BASE` 终值写入 `deploy/sn.env`（整文件重写，幂等）。
3. 重启相关单元 **并守住"铁律1：重启服务必同时重启摄像头推流"**：`systemctl restart sn-uploader sn-preview-ffmpeg`（monitor 不动流、推流单元重启即可；若 monitor 也需感知则一并提示）。
4. 打印新旧值对照，供人确认。

**结果**：改 webserver IP = `sudo ./deploy/set-webserver-ip.sh 10.80.40.99`，一条命令。

#### A.4 数据流（线 A）
```
人: set-webserver-ip.sh <新IP>
      │ 校验IP
      ├─ 写 deploy/sn.env (SN_UPLOAD_URL / RTMP_BASE 终值)
      └─ systemctl restart sn-uploader sn-preview-ffmpeg
                 │
   sn-uploader ──EnvironmentFile→ 读新 SN_UPLOAD_URL ──POST→ 新 webserver
   supervisor  ──source sn.env──→ 读新 RTMP_BASE    ──推流→ 新 webserver
```

### 线 B（webserver / .57）补齐 DELETE 数据 API

**决策**：硬删——删 DB 行 + 删磁盘图片（用户选）。

#### B.1 `SnCaptureService.delete(cap_id) -> bool`
```python
def delete(self, cap_id: int) -> Optional[str]:
    """删 DB 行；返回被删行的 image_path（供上层删文件），不存在返回 None。"""
    session = self._session_factory()
    try:
        r = session.query(SnCapture).filter(SnCapture.id == cap_id).first()
        if not r:
            return None
        image_path = r.image_path
        session.delete(r)
        session.commit()
        return image_path
    finally:
        session.close()
```

#### B.2 `DELETE /api/v1/captures/{cap_id}`（captures.py）
```python
@router.delete("/captures/{cap_id}")
async def delete_capture(cap_id: int):
    svc = get_service()
    image_path = svc.delete(cap_id)
    if image_path is None:
        raise HTTPException(status_code=404, detail="抓拍不存在")
    # 删磁盘图片（文件缺失不报错，DB 行已删为准）
    abs_path = Config.DATA_DIR / image_path
    try:
        abs_path.unlink(missing_ok=True)
    except OSError as e:
        logger.warning("删图片失败(DB行已删): %s", e)
    return {"deleted": cap_id}
```
- **顺序**：先删 DB 行成功再删文件。文件删失败只告警（避免"DB 删了文件没删"=孤儿文件可清理；反之"文件删了 DB 没删"=死链更糟）。

#### B.3 测试（线 B）
`backend/tests/test_captures_api.py` 补：删存在的→200 且行与文件都没了；删不存在→404；删后再 GET→404。沿用项目既有 pytest 范式。

### 线 C（webserver / .57）start.sh 首跑 IP 提示与确认

**决策**：两端各自 env。webserver 侧新增 `cfg/webserver.env` 持有"板子 IP"（webserver 要连的对端）+ 记录"本机对外 IP"（仅用于提示展示）。

#### C.1 新增 `cfg/webserver.env`
```sh
# webserver 侧对端地址（.57 持有：要连的板子；本机对外 IP 仅供提示）
BOARD_IP=10.80.40.68
PREVIEW_BOARD_BASE=http://${BOARD_IP}:8090
SELF_IP=10.80.40.57          # 仅用于 start.sh 提示展示；webserver 实际监听 0.0.0.0
CAM_HINT=相机网 192.168.1.9/.8（SE9 侧 eth1，webserver 不直连）
```

#### C.2 start.sh 首跑交互
在 start.sh 构建前端前插入：
```sh
ENV_FILE="$SCRIPT_DIR/cfg/webserver.env"
if [ ! -f "$ENV_FILE" ]; then
  echo "=== 首次运行：环境确认 ==="
  echo "板子(SE9/抓拍端) IP: 默认 10.80.40.68"
  read -rp "  回车用默认，或输入新 IP: " in_board
  echo "本机(webserver) 对外 IP（仅展示，实际监听 0.0.0.0）: 默认 10.80.40.57"
  read -rp "  回车用默认，或输入新 IP: " in_self
  # 写入 cfg/webserver.env（整文件，派生 PREVIEW_BOARD_BASE 终值）
  ...
  echo "已写入 $ENV_FILE，下次启动不再询问（改 IP 可直接编辑该文件或删除后重跑）。"
fi
set -a; . "$ENV_FILE"; set +a    # 导出给 uvicorn 子进程 → camera.py 读 PREVIEW_BOARD_BASE
```
- **非首跑**：文件已存在 → 直接 `source`，静默用既有值。
- **改 IP**：编辑 `cfg/webserver.env` 或删文件重跑 start.sh 触发再次询问。
- camera.py 无需改——它已读 `PREVIEW_BOARD_BASE` env，start.sh 负责把 env 导给 uvicorn 子进程。

#### C.3 数据流（线 C）
```
首跑 start.sh ── 文件不存在? ──是→ 交互询问板子IP/本机IP → 写 cfg/webserver.env
                              └否→ 直接 source
     └─ set -a; . webserver.env  → 导出 PREVIEW_BOARD_BASE
          └─ uvicorn backend.main:app  → camera.py os.environ 读到板子IP → 预览中继连对端板子
```

---

## 4. 文件改动清单

### 本机仓库 `sn_monitor_bm1688`（线 A）
| 文件 | 动作 |
|---|---|
| `deploy/sn.env` | 新增（集中事实源） |
| `deploy/set-webserver-ip.sh` | 新增（一键改 IP） |
| `deploy/sn-uploader.service` | 改（加 EnvironmentFile、--url 去硬编码） |
| `deploy/preview_supervisor.sh` | 改（source sn.env、RTMP 加 env 覆盖） |
| `README.md` §4 | 改（对照表补 sn.env + 一键脚本说明） |

### webserver `.57:product_test`（线 B + C）
| 文件 | 动作 |
|---|---|
| `backend/services/sn_capture_service.py` | 改（加 delete 方法） |
| `backend/api/captures.py` | 改（加 DELETE 路由） |
| `backend/tests/test_captures_api.py` | 改（加删除 TC） |
| `cfg/webserver.env` | 新增（板子 IP + 本机提示 IP） |
| `start.sh` | 改（首跑交互 + source env 导出） |

---

## 5. 验证计划（FFV）

| 线 | TC | 方法 | 预期 |
|---|---|---|---|
| B | TC-DEL-01 | 上传一条 → `DELETE /captures/{id}` | 200 `{deleted:id}`，DB 行与图片文件均消失 |
| B | TC-DEL-02 | `DELETE` 不存在的 id | 404 |
| B | TC-DEL-03 | 删后 `GET /captures/{id}/image` | 404 |
| B | pytest | `.57` 上 `pytest backend/tests/test_captures_api.py` | 全通过（既有用例不回归） |
| A | TC-IP-01 | `set-webserver-ip.sh 10.80.40.99` 后查 `sn.env` 与单元 env | 终值已更新，相关单元重启 |
| A | TC-IP-02 | supervisor 启动后 `journalctl` 看 RTMP 目标 | 为新 IP（证明 env 覆盖生效） |
| C | TC-START-01 | 删 `cfg/webserver.env` 后跑 start.sh | 出现首跑询问，回车用默认，写出文件 |
| C | TC-START-02 | 再次跑 start.sh | 不再询问，camera.py 读到 env 中板子 IP |

> 实测需上板/起服务，按用户节奏单独一轮执行；本设计不含实测。

---

## 6. 未决 / 风险

- **R1**：`sn-uploader.service` 当前 `--url` 显式传参会**盖过** env。改造必须去掉该显式参数，否则 env 不生效。已在 A.2 明确。
- **R2**：start.sh 当前末行是 `exec uvicorn`，首跑交互须插在 `exec` 之前；`read` 在非交互式启动（如 systemd）下会卡住——需用 `[ -t 0 ]` 判断 tty，非 tty 时跳过询问直接用默认/既有文件。实现时补这个守卫。
- **R3**：两端 env 文件应加入各自 `.gitignore`？讨论：env 含 IP 非密钥，建议**提交一份 `.env.example` 模板**、真 env 文件 gitignore，避免把现场 IP 固化进仓库。实现时与用户确认。
