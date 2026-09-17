# feature：实时上传到产测系统（sn-uploader sidecar）

> 主 [README.md](../../README.md) 只写"怎么起这个服务"；本文件写**边界、踩坑与善后**。
> 相关：[dual-camera.md](dual-camera.md)（正/反配对上传）、[../CHANGELOG.md](../CHANGELOG.md)。

## 它是什么

识别只负责把结果落到板子本地 `sn_results/`。**推到 `.57` 产测系统前端/DB 的是独立 sidecar `sn_uploader.py`** ——
与识别**解耦**、纯 `urllib`（无第三方依赖）、网络故障只重试、`.uploaded` 标记防重传，**绝不影响拉流**。

也就是说：**识别服务不含它**。`sn_monitor.py` 里没有 `subprocess` / `Popen` / `fork`，
两个是各自独立的 systemd 单元，要各 `enable` 一次。

## 数据流

```
放板 ─► sn-monitor(识别) ─► sn_results/  sn_<SN>_<ts>.json + 命中帧.jpg
     └─► sn-uploader(sidecar) 监视目录, jpg+json 配对 ─► HTTP POST(multipart)
     └─► http://10.80.40.57:8099/api/v1/captures  (product_test 后端)
     └─► DB 表 sn_captures + data/sn_captures/<SN>/<ts>.jpg
       └─► 前端「SN 抓拍」页：按 SN 检索 / 看设备图 + score + 时间
```

## 参数

只有四个：

| 参数 | 默认 | 说明 |
|------|------|------|
| `--url` | (必填) | 产测后端上传接口 |
| `--results-dir` | `/data/soph_SN/sn_results` | 监视目录 |
| `--interval` | 3 | 扫描间隔（秒） |
| `--timeout` | — | 单次 POST 超时（秒） |

## 手动跑（不启用服务时）

```bash
sudo python3 /data/soph_SN/sn_uploader.py --url http://10.80.40.57:8099/api/v1/captures --interval 3
```

> `.57` 产测后端需在 `:8099` 起着（`cd product_test && ./start.sh`）才能接收入库。
> 接口契约见 `sn_uploader.py` 头部注释。

---

## ⚠️ 坑一：首次启用会"回灌历史"

uploader **只看 `.uploaded` 标记，不看时间**。若 `sn_results/` 里积着大量**从未上传过**的旧命中帧，
一 `enable` 就会**全量补传**到 `.57`，把库灌脏。

启用前先确认积压：

```bash
# 看有多少未上传的积压（数量非 0 就要先决定补不补）
sudo bash -c 'n=0; for f in /data/soph_SN/sn_results/*.jpg; do [ -f "$f.uploaded" ] || n=$((n+1)); done; echo "未标记: $n"'

# 不想补传：给当前所有历史帧打标记（此后只传新文件）
sudo bash -c 'for f in /data/soph_SN/sn_results/*.jpg; do [ -f "$f.uploaded" ] || touch "$f.uploaded"; done'
```

## ⚠️ 坑二：去重是"文件级"，不是"内容级"，也不比数据库

最常被问的一点，写死在这里，别处不再重复：

- 判据**只有一个**：同名 `.uploaded` 标记文件是否存在
  （`sn_uploader.py` 的 `iter_pending()` 里就一个 `os.path.exists(base + ".uploaded")`）。
- **不与 `.57` 数据库比对**：uploader 里没有 DB 连接、不装 sqlalchemy、不发任何查询，
  连"远端已有哪些 SN"都不知道。
- **不在单次扫描内去重**：同一轮里若两个文件内容相同（不同文件名），两张都传。

**后果**：

| 情况 | 结果 |
|------|------|
| 同一块板反复放置 | 多次抓拍是**不同文件名**，`.uploaded` 拦不住 → **全部入库**（业务级重复） |
| SN 误读（少位/错字） | **照传不误** |

换句话说：它保证的是"**每个本地文件最多成功上传一次**"，
**不保证"每块板在远端只出现一次"**。清理只能在 `.57` 侧按需做。

## SN 错读不靠 uploader 拦，靠扫描端的格式闸

uploader 侧**不需要**再加一道格式闸 —— 那只会把已经校验过的字符串再验一遍。

SN 在 OCR 之后先过 `SNConfig`（`--sn-min-len` / `--sn-max-len` / `--sn-prefix` + 字符集与合并规则），
不合格的**在识别进程内就被丢掉**，根本走不到落盘，也就不会产生待传文件。

## 权限

单元里必须 `User=root`。否则 `.uploaded` 标记写不进 `sn_results/` → 每轮重传同一个文件。
