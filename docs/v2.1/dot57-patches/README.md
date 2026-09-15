# v2.1 双路摄像头 —— .57 产测系统改动补丁包（方案A：加 `side` 字段）

> 适用仓库：`.57` 产测系统 `product_test`，分支 `feature/sn-capture`（承接 sn-capture 特性）。
> 本机（SE9 侧）无 .57 登录凭据、8099 当前未起，故把 .57 侧改动整理成**可直接应用的片段补丁 + 应用说明**，由你在 .57 上应用并自行提交推送。
>
> **总原则**：全部**加法式、向后兼容**。旧数据、旧上传（不带 `side`）一律默认 `side="front"`，行为不变；只有第二路摄像头 IP 调通、SE9 开始上传 `_back.jpg` 后，`back` 行才会出现。

## 为什么改

v2.1 要在一次识别命中时同时留证**正面（带 SN 标签）**和**背面**两张图，两张都入库、并在网页端**成对展示**。当前 `sn_captures` 表只有单图字段、无正反区分，故给它加一个 `side` 列，仍保持“一行一图”，前端按 `(sn, captured_at)` 把同一次命中的正/反两行配成一对显示。

## 改什么（5 处 + 1 次迁移）

| # | 文件 | 改动 | 补丁文件 |
|---|------|------|----------|
| 1 | `backend/models/sn_capture.py` | 加 `side` 列 | 见下 §1 |
| 2 | `backend/services/sn_capture_service.py` | `add()` 收 `side`、`_to_dict()` 返回 `side` | §2 |
| 3 | `backend/api/captures.py` | POST 收 `side` 表单字段 + 落盘路径带 side 防覆盖；GET 返回 side | §3 |
| 4 | `frontend/src/api/captures.ts` | `CaptureItem` 加 `side` | §4 |
| 5 | `frontend/src/views/SnCaptureView.vue` | 按 `(sn, captured_at)` 分组，正/反并排显示 | §5 |
| M | 一次性迁移 | `ALTER TABLE sn_captures ADD COLUMN side ...` | `migrate_add_side.py` |

> **为什么要单独迁移**：SQLAlchemy `create_all` 只“建不存在的表”，**不会给已存在的表加列**。sn-capture 上线时 `sn_captures` 表已建，故新增列必须手工 `ALTER TABLE`（或删库重建，见迁移脚本注释）。

---

## §1 `backend/models/sn_capture.py` —— 加 `side` 列

在 `source = Column(...)` 行**之后**、`uploaded_at = Column(...)` 行**之前**，插入一行：

```python
    source = Column(String(32), default="SE9")
    side = Column(String(8), default="front", nullable=False)  # v2.1 正/反面: front|back; 旧数据默认 front
    uploaded_at = Column(DateTime, default=datetime.now)  # 入库时间
```

（其余字段不动。）

---

## §2 `backend/services/sn_capture_service.py` —— `add()`/`_to_dict()` 收发 side

**(a) `add()` 签名加参数 `side`（默认 `"front"`，保证老调用点不传也能用）：**

```python
    def add(self, sn: str, image_path: str, score: float,
            captured_at: Optional[datetime], source: str = "SE9",
            side: str = "front") -> dict:            # v2.1 加 side, 默认 front 向后兼容
        session = self._session_factory()
        try:
            row = SnCapture(sn=sn, image_path=image_path, score=score or 0.0,
                            captured_at=captured_at, source=source, side=side)  # v2.1 落 side
            session.add(row)
            session.commit()
            return {"id": row.id, "sn": row.sn, "image_path": row.image_path}
        finally:
            session.close()
```

**(b) `_to_dict()` 返回体加 `side`：**

```python
    @staticmethod
    def _to_dict(r: SnCapture) -> dict:
        return {
            "id": r.id,
            "sn": r.sn,
            "score": r.score,
            "captured_at": r.captured_at.isoformat() if r.captured_at else None,
            "image_path": r.image_path,
            "source": r.source,
            "side": r.side,                          # v2.1 前端配对用
        }
```

---

## §3 `backend/api/captures.py` —— POST 收 side + 落盘防覆盖；GET 无需改（走 `_to_dict`）

**(a) `upload_capture` 表单参数加 `side`（放在 `ts` 之后、`image` 之前）：**

```python
@router.post("/captures")
async def upload_capture(
    sn: str = Form(""),
    score: float = Form(0.0),
    ts: str = Form(""),
    side: str = Form("front"),                       # v2.1 正/反面; SE9 uploader 带上, 旧客户端不带默认 front
    image: UploadFile = File(...),
):
```

**(b) 校验 side 合法（紧接 `sn` 校验之后，任选，防脏值）：**

```python
    side = (side or "front").strip().lower()
    if side not in ("front", "back"):
        side = "front"                               # 未知值兜底为 front, 不 400(容错优先)
```

**(c) 落盘路径带 side —— 防同一 SN 同一秒正反互相覆盖。** 把原来的：

```python
    rel = Path("sn_captures") / sn / f"{stamp}{ext}"
```

改为：

```python
    rel = Path("sn_captures") / sn / f"{stamp}_{side}{ext}"   # v2.1 带 side, 正反同秒不覆盖
```

**(d) `svc.add(...)` 传 side：**

```python
    svc = get_service()
    return svc.add(sn=sn, image_path=str(rel), score=score,
                   captured_at=captured_at, source="SE9", side=side)   # v2.1 落 side
```

> GET（`list_captures` / `get_capture_image`，Task3 追加）**不用改**：`list` 走 `_to_dict()` 已带 `side`（见 §2b）；取图按 `image_path` 无关 side。

---

## §4 `frontend/src/api/captures.ts` —— `CaptureItem` 加 `side`

在 `CaptureItem` 接口里加一个可选字段（放在 `image_url` 附近即可）：

```typescript
export interface CaptureItem {
  id: number
  sn: string
  score: number
  captured_at: string | null
  image_url: string
  source: string
  side?: 'front' | 'back'          // v2.1 正/反面; 旧后端不返回时为 undefined, 视作 front
}
```

（`captureApi.list(...)` 契约 `{ total, items[] }` 不变。）

---

## §5 `frontend/src/views/SnCaptureView.vue` —— 按 `(sn, captured_at)` 配对，正/反并排

**目标**：同一次命中的正、反两行（`sn` 相同、`captured_at` 相同 → 因 SE9 正反共用同一 ISO `ts`）合成一张卡片，左正面右背面；只到一面时另一面显示占位。检索仍按 sn。

**做法（计算属性分组，不改检索/分页逻辑）**：在 `<script setup>` 里，对 `captureApi.list` 返回的 `items` 增加一个分组计算属性：

```typescript
// v2.1 把一行一图的 items 按 (sn, captured_at) 配成对; key 相同的 front/back 落到同一组
interface CapturePair {
  key: string
  sn: string
  captured_at: string | null
  front?: CaptureItem
  back?: CaptureItem
}

const pairs = computed<CapturePair[]>(() => {
  const map = new Map<string, CapturePair>()
  for (const it of items.value) {                 // items 为原 list 响应的 ref
    const key = `${it.sn}|${it.captured_at ?? ''}`
    let p = map.get(key)
    if (!p) { p = { key, sn: it.sn, captured_at: it.captured_at }; map.set(key, p) }
    // 无 side 的旧数据当 front
    if ((it.side ?? 'front') === 'back') p.back = it
    else p.front = it
  }
  // 保持后端返回的时间倒序: 用各组首次出现顺序
  return Array.from(map.values())
})
```

**模板**：把原来遍历 `items` 渲染单图的卡片列表，改成遍历 `pairs`，每张卡片内并排两个图位（沿用现有 `el-image` + 加载失败占位写法）：

```html
<div v-for="p in pairs" :key="p.key" class="capture-card">
  <div class="capture-sn">{{ p.sn }}</div>
  <div class="capture-sides">
    <div class="side-col">
      <div class="side-label">正面</div>
      <el-image v-if="p.front" :src="p.front.image_url" fit="contain" lazy>
        <template #error><div class="img-fallback">图片加载失败</div></template>
      </el-image>
      <div v-else class="img-fallback">（暂无正面）</div>
    </div>
    <div class="side-col">
      <div class="side-label">背面</div>
      <el-image v-if="p.back" :src="p.back.image_url" fit="contain" lazy>
        <template #error><div class="img-fallback">图片加载失败</div></template>
      </el-image>
      <div v-else class="img-fallback">（暂无背面）</div>
    </div>
  </div>
  <div class="capture-meta">score: {{ (p.front ?? p.back)?.score?.toFixed(3) }} · {{ p.captured_at }}</div>
</div>
```

配套 CSS（`.capture-sides{display:flex;gap:8px}` `.side-col{flex:1}` 之类，按现有卡片风格补齐即可）。

> 注意：`computed` / `CaptureItem` 记得在 import 里补上（`import { computed } from 'vue'`、`import type { CaptureItem } from '../api/captures'`）。

---

## 应用步骤（在 .57 上）

```bash
# 0. 切到特性分支
cd <product_test 仓根>
git switch feature/sn-capture

# 1. 按 §1~§5 编辑 5 个文件（本包全是加法, 无删除）

# 2. 一次性迁移: 给已存在的 sn_captures 表加 side 列
python3 docs/v2.1/dot57-patches/migrate_add_side.py   # 见脚本, 幂等; 或按注释删 results.db 重建

# 3. 后端自测 + 起服务
cd backend && python3 -m pytest tests/ -q            # 期望无回归(旧 50 passed/1 skipped 基线)
# 起服务(沿用现有启动方式, 端口 8099)

# 4. 前端构建
cd ../frontend && npm run build                       # 期望 built 无类型错误

# 5. 提交(你自行 commit/push, AI 不做 .57 远端写)
git add backend/models/sn_capture.py backend/services/sn_capture_service.py \
        backend/api/captures.py frontend/src/api/captures.ts \
        frontend/src/views/SnCaptureView.vue
git commit -m "feat(captures): v2.1 加 side 字段, 正反双图成对展示"
```

## 验证（camera2 IP 就绪后端到端）

1. 迁移后 `sqlite3 <data>/results.db ".schema sn_captures"` 应看到 `side` 列。
2. SE9 双路跑起来命中一次 → `sn_results/` 出 `sn_<SN>_<ts>.jpg`(front) + `sn_<SN>_<ts>_back.jpg`(back)。
3. uploader 两张都 POST，各带 `side`；`.57` DB 出两行（同 `sn`、同 `captured_at`、`side` 各 front/back），落盘 `sn_captures/<SN>/<ts>_front.jpg` 与 `_back.jpg` 不互相覆盖。
4. 前端检索该 SN → 一张卡片并排显示正/反两图。
5. **向后兼容自检**：只上传正面（不带 side，如旧 SE9）→ 仍单图入库、`side=front`、前端卡片右侧显示“（暂无背面）”，无报错。
