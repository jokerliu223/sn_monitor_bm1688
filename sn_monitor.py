"""SN提取 v9 - 保持连接 + 干净断开 + 多帧投票版

相对 v8 的最小改动:
  1) RTSP 断开走干净 TEARDOWN(信号处理 SIGINT/SIGTERM 都 release), 保住"单客户端脏断开即退出"的源;
  2) 强制 TCP transport + stimeout, 重连指数退避, 重连前先探源端口是否在监听(避免狂刷 ffmpeg);
  3) SN 识别改为跨帧投票(sncore.sn_extract.vote): 格式校验防误报 + 子序列合并修复掉字。
"""
import sys, os, cv2, re, json, time, argparse, warnings, gc, signal, socket
from collections import Counter, deque
import threading
from urllib.parse import urlparse
import numpy as np
from datetime import datetime
warnings.filterwarnings("ignore")

# 强制 TCP + 读超时(微秒): 直连链路更稳, 且退出时 release 会发干净 TEARDOWN。
# 新版 ffmpeg(avcodec 62) 用 timeout 取代已弃用的 stimeout, 两者都给, 5s 内读失败即返回,
# 避免源半死(0x0坏流)时每次读阻塞默认 30s。
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS",
                      "rtsp_transport;tcp|stimeout;5000000|timeout;5000000")

# 让 sncore 可导入(脚本同级目录), 必须在 OCR.load() 的 chdir 之前把绝对路径入 path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sncore.sn_extract import vote, SNConfig
from sncore.result_gate import ResultGate

# V3.0 调机预览(可选): 与识别同进程, 共享同一路解码 -> 相机源仍只占用一次。
# 纯加法, 挂不上就返回 None, 识别行为完全不变(等同 V2)。
try:
    import sn_preview_embed
except Exception as _e:   # Flask 缺失等: 预览失效但不影响识别
    sn_preview_embed = None
    print(f"  ⚠ 预览模块不可用({type(_e).__name__}: {_e}), 仅识别")

PP_OCR_DIR = "/data/soph_SN/sophon-demo/sample/PP-OCR/python"
DET_MODEL = "../models/BM1688/ch_PP-OCRv4_det_int8_2core.bmodel"
REC_MODEL = "../models/BM1688/ch_PP-OCRv4_rec_int8_2core.bmodel"
CHAR_DICT = "../datasets/ppocr_keys_v1.txt"
DEV_ID = 0
RESULTS_DIR = "/data/soph_SN/sn_results"
DEBUG_DIR = "/data/soph_SN/debug"    # 漏检/待确认帧留证目录(证据驱动调参)
WEDGE_FAILS = 3     # 连续读失败达此次数即判坏流, 立即受控release+重连拿干净流(可--wedge-fails调)
                    # 2026-09-22 由 8 降为 3: 10s 检查周期下 3 次≈30s 就能发现故障;
                    # 背面图"多抓一张 vs 挂了不管", 误判重连的代价远小于长期静默。

SN_REGEX = re.compile(r'^[A-Z0-9]{12,20}$')
SN_PREFIX = [r'SN[:\s\-]*([A-Z0-9]{6,20})', r'S/N[:\s\-]*([A-Z0-9]{6,20})']

# 干净退出标志: 信号只置位, 主循环在安全点 break -> finally 里 cap.release() 发 TEARDOWN
STOP = False
def _on_signal(signum, _frame):
    global STOP
    STOP = True
    print(f"\n[信号 {signum}] 收到, 准备干净退出(release 发送 TEARDOWN)...")

# ===== 解码错误监控: 劫持 fd2 统计 HEVC 失步错误, 用于旧帧回放自愈 =====
_err_times = deque(maxlen=4000)
_err_lock = threading.Lock()
_real_stderr_fd = None
_last_reconnect = 0.0
_ERR_PATS = (b"Could not find ref", b"Error constructing")
_DROP_PATS = (b"PPS id out of range", b"Could not find ref", b"Error constructing")

def _stderr_pump(read_fd):
    buf = b""
    while True:
        try:
            chunk = os.read(read_fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if any(p in line for p in _ERR_PATS):
                with _err_lock:
                    _err_times.append(time.time())
            # 按行回显: 丢弃已知刷屏坏包行(_DROP_PATS), 其余透传到真 stderr
            if _real_stderr_fd is not None and not any(p in line for p in _DROP_PATS):
                try:
                    os.write(_real_stderr_fd, _ts() + b" " + line + b"\n")
                except OSError:
                    pass
        if len(buf) > 65536:
            buf = buf[-4096:]


# ===== 日志时间戳: 排查"流什么时候卡的"必须有时间轴, 否则只能靠重启点反推 =====
def _ts():
    return datetime.now().strftime("[%H:%M:%S.%f")[:-3].encode() + b"]"

class _TeeWriter:
    """给 print/stderr 每行加 [HH:MM:SS.mmm] 前缀。逐块透传, 只改写真实行首,
    避免半行被割裂时误插时间戳。原流保持原样(append 文件 + journal)。"""
    def __init__(self, raw):
        self.raw = raw
        self._at_line_start = True

    def write(self, s):
        if isinstance(s, bytes):
            self.raw.buffer.write(s); self.raw.flush(); return len(s)
        if not s:
            return 0
        out = []
        for seg in s.splitlines(keepends=True):
            if self._at_line_start and seg.strip():
                out.append(_ts().decode() + " " + seg)
            else:
                out.append(seg)
            self._at_line_start = seg.endswith(("\n", "\r"))
        if s and not s.endswith(("\n", "\r")):
            self._at_line_start = False
        self.raw.write("".join(out))
        self.raw.flush()
        return len(s)

    def flush(self):
        try: self.raw.flush()
        except Exception: pass

    def isatty(self): return False
    def fileno(self): return self.raw.fileno()

def start_log_timestamp():
    """在 stderr 劫持与 fd 复制之后调用, 否则会和 _stderr_pump 抢 fd。"""
    try:
        sys.stdout.flush()
        sys.stdout = _TeeWriter(sys.stdout)
        print("[日志] 已启用时间戳")
    except Exception as e:
        print(f"  (日志时间戳启用失败, 降级: {e})")

def start_stderr_monitor():
    global _real_stderr_fd
    try:
        r, w = os.pipe()
        _real_stderr_fd = os.dup(2)
        os.dup2(w, 2)
        os.close(w)
        threading.Thread(target=_stderr_pump, args=(r,), daemon=True).start()
    except Exception as e:
        print(f"  (stderr监控启动失败, 降级: {e})")

def reset_decode_errs():
    with _err_lock:
        _err_times.clear()

def mark_reconnect():
    global _last_reconnect
    _last_reconnect = time.time()

def since_reconnect():
    return time.time() - _last_reconnect

def decode_err_rate(window):
    now = time.time()
    with _err_lock:
        return sum(1 for ts in _err_times if now - ts <= window)

def rotate_img(img, a):
    if a==0: return img
    if a==90: return cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
    if a==180: return cv2.rotate(img, cv2.ROTATE_180)
    if a==270: return cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return img

def enhance(img):
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
    l,a,b = cv2.split(lab)
    cl = cv2.createCLAHE(2.0, (8,8)).apply(l)
    return cv2.cvtColor(cv2.merge((cl,a,b)), cv2.COLOR_LAB2BGR)

def sharpen(img):
    # 轻锐化(unsharp mask): 小字上采样后补回边缘, 提升 rec 召回
    blur = cv2.GaussianBlur(img, (0,0), 3)
    return cv2.addWeighted(img, 1.5, blur, -0.5, 0)

def split_grid(img, nx=3, ny=3, ov=0.3):
    h,w = img.shape[:2]
    sw,sh = int(w/nx),int(h/ny)
    ow,oh = int(sw*ov),int(sh*ov)
    t = []
    for y in range(ny):
        for x in range(nx):
            t.append(img[max(0,y*sh-oh):min(h,(y+1)*sh+oh), max(0,x*sw-ow):min(w,(x+1)*sw+ow)])
    return t

def split3x3(img, ov=0.3):
    return split_grid(img, 3, 3, ov)

def find_barcode(img):
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gx = cv2.convertScaleAbs(cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3))
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (21,7))
    c = cv2.morphologyEx(gx, cv2.MORPH_CLOSE, k)
    _,th = cv2.threshold(c, 0, 255, cv2.THRESH_BINARY+cv2.THRESH_OTSU)
    c2 = cv2.morphologyEx(th, cv2.MORPH_CLOSE, k)
    cnts,_ = cv2.findContours(c2, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    r = []
    ih,iw = img.shape[:2]
    for c in cnts:
        x,y,w,h = cv2.boundingRect(c)
        if w<100 or h<50 or w/max(h,1)<1.5: continue
        r.append((x, max(0,y-int(h*0.8)), x+w, min(ih, y+h+int(h*0.3))))
    return r

def find_white_labels(img):
    """白底标签检测: 铭牌/条码多为白底黑字贴纸, 用颜色(高亮度+低饱和)定位,
    比纯 Sobel 形态学更专一(不被彩色元件丝印干扰)。返回 label 型 ROI 列表。"""
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (0,0,180), (180,60,255))   # 高V低S = 白
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (25,15))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k)
    cnts,_ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    r = []; ih,iw = img.shape[:2]; area_img = ih*iw
    for c in cnts:
        x,y,w,h = cv2.boundingRect(c)
        if w<80 or h<40: continue       # 太小
        a = w*h
        if a < 0.0003*area_img: continue# 面积下限, 滤零星白点
        if a > 0.5*area_img: continue      # 过大, 排背光/白墙
        ar = w/max(h,1)
        if ar < 1.0 or ar > 12: continue           # 标签一般横向长条
        # 上扩一点把条码上方的人可读 SN 行带进来
        r.append((x, max(0,y-int(h*0.2)), x+w, min(ih, y+h+int(h*0.2))))
    return r

def _iou(a, b):
    ax1,ay1,ax2,ay2=a; bx1,by1,bx2,by2=b
    ix1,iy1=max(ax1,bx1),max(ay1,by1); ix2,iy2=min(ax2,bx2),min(ay2,by2)
    inter=max(0,ix2-ix1)*max(0,iy2-iy1)
    if inter==0: return 0.0
    ua=(ax2-ax1)*(ay2-ay1)+(bx2-bx1)*(by2-by1)-inter
    return inter/ua if ua>0 else 0.0

def find_regions(img):
    """标签候选 = 白底标签(颜色,更稳) ∪ 条码形态学(Sobel,兜底), 去重叠框。"""
    boxes = find_white_labels(img) + find_barcode(img)
    keep = []
    for bx in boxes:
        if any(_iou(bx,k)>0.6 for k in keep): continue
        keep.append(bx)
    return keep

def quality(f):
    g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
    m, s = float(np.mean(g)), float(np.std(g))
    if m<10: return False
    if m>245: return False
    if s<5: return False
    return True

def crop_center(img, wf=1.0, hf=1.0, cy=0.5):   # 各向异性裁剪: 宽(wf)可比高(hf)收更多; cy=垂直锚点(0.5居中, <0.5偏上, 小板SN在上带)
    if wf >= 0.999 and hf >= 0.999: return img
    h,w = img.shape[:2]; cw,ch = int(w*wf), int(h*hf)
    x0 = (w-cw)//2
    y0 = max(0, min(int(h*cy - ch/2), h-ch))   # 按cy锚点垂直偏移, 夹取到边界内
    return img[y0:y0+ch, x0:x0+cw]

def fdiff(f1, f2):
    if f1.shape != f2.shape: return 1.0
    s1 = cv2.resize(f1, (854, 480))
    s2 = cv2.resize(f2, (854, 480))
    d = cv2.absdiff(s1, s2)
    g = cv2.cvtColor(d, cv2.COLOR_BGR2GRAY)
    _,th = cv2.threshold(g, 20, 255, cv2.THRESH_BINARY)
    return float(np.sum(th>0))/th.size

# 旧单帧提取保留作兜底/对拍, 主流程已改用 sncore.vote 多帧投票
def extract_sn(texts):
    for item in texts:
        t = item[0] if isinstance(item,(tuple,list)) else item
        if not isinstance(t,str): continue
        for p in SN_PREFIX:
            m = re.search(p, t, re.IGNORECASE)
            if m: return m.group(1).strip().upper()
    cands = []
    for item in texts:
        t = item[0] if isinstance(item,(tuple,list)) else item
        s = item[1] if isinstance(item,(tuple,list)) else 1.0
        if not isinstance(t,str): continue
        t = t.strip()
        if SN_REGEX.match(t): cands.append((t,s,len(t)))
    if cands:
        cands.sort(key=lambda x:(-x[2],-x[1]))
        return cands[0][0]
    best,bestlen = None,0
    for item in texts:
        t = item[0] if isinstance(item,(tuple,list)) else item
        if not isinstance(t,str): continue
        for m in re.findall(r'[A-Z0-9]{12,20}', t.upper()):
            if len(m)>bestlen: bestlen,best = len(m),m
    return best

class OCR:
    def __init__(self): self.d=self.r=self.crop=None
    def load(self):
        if self.d: return
        sys.path.insert(0, PP_OCR_DIR)
        os.chdir(PP_OCR_DIR)
        import ppocr_det_opencv as pd, ppocr_rec_opencv as pr
        from ppocr_system_opencv import get_rotate_crop_image
        self.crop = get_rotate_crop_image
        class O:
            def __init__(self):
                self.dev_id=DEV_ID; self.bmodel_det=DET_MODEL; self.bmodel_rec=REC_MODEL
                self.img_size=[[640,48],[320,48]]; self.char_dict_path=CHAR_DICT
                self.use_space_char=True; self.use_beam_search=False; self.beam_size=5
                self.rec_thresh=0.3; self.use_angle_cls=False; self.det_limit_side_len=[4000]
                self.det_thresh=0.2; self.det_box_thresh=0.2
        t0=time.time()
        self.d=pd.PPOCRv2Det(O()); self.r=pr.PPOCRv2Rec(O())
        print(f"    [OCR] 加载: {time.time()-t0:.1f}s")
    def unload(self):
        self.d=self.r=self.crop=None; gc.collect()
        print(f"    [OCR] 已释放")
    def _ocr(self, imgs):
        res = []
        db = self.d(imgs)
        id = {"imgs":[],"boxes":[],"pids":[]}
        for i,boxes in enumerate(db):
            for b in range(len(boxes)):
                id["imgs"].append(self.crop(imgs[i], boxes[b].copy()))
                id["boxes"].append(boxes[b]); id["pids"].append(i)
        if id["imgs"]:
            r = self.r(id["imgs"])
            for i,pid in enumerate(r.get("ids")):
                res.append((r["res"][i][0], r["res"][i][1]))
        return res
    def ocr(self, img, regions=None, rots=(0,180), do_tiles=True,
            crop_w=1.0, crop_h=1.0, tile_grid=(3,3), tile_up=2.0, roi_path=True, crop_cy=0.5):
        texts = []; seen = set()
        wimg = crop_center(img, crop_w, crop_h, crop_cy)   # 各向异性裁剪(可上偏): 铭牌占比更大, 单张不切块也读得清
        if regions is None:            # 主循环已探过铭牌门时把 regions 传进来复用, 省一次 Sobel
            regions = find_regions(img)   # 白底标签 ∪ Sobel 条码
        for x1,y1,x2,y2 in (regions if roi_path else []):
            c = img[y1:y2, x1:x2]
            for a in rots:
                # ROI 上采样(自适应): 小标签放长边~1600px(最多6x), 大白区不盲目6x防拖慢占内存
                rc = sharpen(enhance(rotate_img(c,a)))
                f = min(6.0, max(1.0, 1600.0/max(rc.shape[0], rc.shape[1], 1)))
                s = cv2.resize(rc, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)
                for t,sc in self._ocr([s]):
                    if sc>=0.3 and t not in seen: seen.add(t); texts.append((t,sc))
        if do_tiles:     # 救援二遍只跑 ROI(do_tiles=False), 省时
            for a in rots:
                batch = []
                for tile in split_grid(wimg, tile_grid[0], tile_grid[1], 0.3):
                    s = cv2.resize(enhance(rotate_img(tile,a)), None, fx=tile_up,fy=tile_up, interpolation=cv2.INTER_CUBIC)
                    batch.append(s)
                for t,sc in self._ocr(batch):
                    if sc>=0.3 and t not in seen: seen.add(t); texts.append((t,sc))
        return texts

def jsonable(o):
    if isinstance(o,(np.floating,np.integer)): return o.item()
    if isinstance(o,dict): return {k:jsonable(v) for k,v in o.items()}
    if isinstance(o,(list,tuple)): return [jsonable(x) for x in o]
    return o

def save_debug(frames, frame_texts, tag, keep=40):
    """留证: 漏检/待确认的原帧 + find_barcode ROI 裁图 + 逐帧候选存盘, 供离线量化调参。
    只在 miss/pending 时调用(命中不存, 省盘); 限量保留最近 keep 组。"""
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        d = os.path.join(DEBUG_DIR, f"{tag}_{ts}")
        os.makedirs(d, exist_ok=True)
        for i, f in enumerate(frames):
            cv2.imwrite(os.path.join(d, f"frame{i}.jpg"), f, [cv2.IMWRITE_JPEG_QUALITY, 92])
            for j, (x1,y1,x2,y2) in enumerate(find_regions(f)):
                cv2.imwrite(os.path.join(d, f"frame{i}_roi{j}.jpg"), f[y1:y2, x1:x2])
        with open(os.path.join(d, "cands.json"), "w") as fp:
            json.dump(jsonable({"ts": ts, "tag": tag, "frame_texts": frame_texts}), fp, ensure_ascii=False, indent=2)
        # 限量: 只留最近 keep 组
        dirs = sorted(g for g in os.listdir(DEBUG_DIR) if os.path.isdir(os.path.join(DEBUG_DIR, g)))
        import shutil
        for old in dirs[:-keep]:
            shutil.rmtree(os.path.join(DEBUG_DIR, old), ignore_errors=True)
        print(f"    [留证] {d}")
    except Exception as e:
        print(f"    [留证失败] {type(e).__name__}: {e}")

class FrameReader:
    """后台线程持续 read() 排空解码缓冲, 只留最新帧。主循环 latest() 永远拿最新解码帧,
    根治 FFMPEG 缓冲的旧帧回放(BUFFERSIZE=1 对 FFMPEG 后端无效)。cap 的 read/release 全由
    本线程做, set_cap 交接, 循环顶安全 release, 杜绝跨线程释放崩溃。"""
    def __init__(self, name="front"):
        self.name = name
        self._cur = None
        self._new = None
        self._has_new = False
        self.frame = None
        self.ts = 0.0
        self.fail = 0
        self.lock = threading.Lock()
        self.run = True
        self._released = threading.Event()
        # ===== 观测(A方案): 区分"源死了" vs "流活着但我读得慢" =====
        # 读出帧数按秒计数 -> 健康时应≈码流帧率(25). 掉到 0 且 Recv-Q 在涨 = 读线程被卡住.
        self._win_t = time.time()
        self._win_n = 0
        self.rate = 0.0          # 解码出帧速率(帧/秒), 每秒刷新
        self.blk = 0.0           # 最近一次 read() 阻塞耗时(秒), 正常≈1/fps, 卡死时≈stimeout(5s)
        self.blk_max = 0.0       # 窗口内最大阻塞耗时
        threading.Thread(target=self._loop, daemon=True).start()
    def set_cap(self, cap):
        with self.lock:
            self._new = cap
            self._has_new = True
            self.frame = None
            self.ts = 0.0
            self.fail = 0
            self._cap_t = time.time()   # 健康检查的宽限期起点(新挂 cap 首帧未到属正常过渡)
            if cap is None:
                self._released.clear()
    def _loop(self):
        while self.run:
            swap = False
            newcap = None
            with self.lock:
                if self._has_new:
                    newcap = self._new
                    self._new = None
                    self._has_new = False
                    swap = True
            if swap:
                if self._cur is not None:
                    try:
                        self._cur.release()
                    except Exception:
                        pass
                self._cur = newcap
                if newcap is None:
                    self._released.set()
            if self._cur is None:
                time.sleep(0.03)
                continue
            _t0 = time.time()
            try:
                ret, f = self._cur.read()
            except Exception:
                ret, f = False, None
            _dt = time.time() - _t0
            self.blk = _dt
            if _dt > self.blk_max:
                self.blk_max = _dt
            if ret and f is not None and f.size > 0:
                with self.lock:
                    self.frame = f
                    self.ts = time.time()
                    self.fail = 0
            else:
                with self.lock:
                    self.fail += 1
                time.sleep(0.02)
            # 每秒刷一次吞吐(观测): 健康≈25 帧/s; 卡住时跌到 0
            _now = time.time()
            self._win_n += 1
            if _now - self._win_t >= 1.0:
                self.rate = self._win_n / (_now - self._win_t)
                self._win_n = 0
                self._win_t = _now
    def latest(self, wait=2.0):
        """取最新帧, 至多阻塞 wait 秒等首帧。返回 (frame_copy|None, age秒, 连续失败数)。"""
        t0 = time.time()
        while time.time() - t0 < wait:
            with self.lock:
                if self.frame is not None:
                    return self.frame.copy(), time.time() - self.ts, self.fail
            time.sleep(0.02)
        with self.lock:
            return None, 0.0, self.fail
    def last_age(self):
        """距最后一张成功帧的真实时长(秒); **从未有过帧返回 None**。

        为什么单开一个方法: latest() 在取不到帧时返回的 age 是占位值 0.0,
        那是"没测到"而不是"帧很新"。健康检查若直接拿它比阈值, 就会把
        "当前无帧"误当成"正常", 又或者在刚挂上 cap、首帧还没到的窗口里
        反复判异常 -> 把刚接上的流再次掐掉, 永远接不上。宁可显式区分三态:
        有帧(返回秒数) / 从未有帧(None) / 帧太旧(返回大秒数)。
        """
        with self.lock:
            if self.frame is None:
                return None
            return time.time() - self.ts
    def wait_released(self, timeout=6.0):
        """等线程放开并 release 旧 cap(重连前调用, 保证单客户端源可接新连接)。"""
        self._released.wait(timeout)


def save_hit_image(results_dir, sn, ts, frame):
    """存命中帧作为该 SN 的绑定图；返回文件路径。cv2 已在本模块导入。"""
    path = os.path.join(results_dir, f"sn_{sn}_{ts}.jpg")
    cv2.imwrite(path, frame)
    return path


def save_back_result(results_dir, sn, ts, ts_iso, frame, score):
    """v2.1 背面(第二路)留证: 存图 sn_{sn}_{ts}_back.jpg + 配对 JSON(side=back)。
    背面不识别, sn/ts/score 继承正面; ts_iso 与正面同值 -> .57 captured_at 一致, 前端按(sn,captured_at)配对成对显示。
    命名带 _back 后缀, 不与正面 sn_{sn}_{ts}.* 冲突; uploader 的 glob sn_*.jpg 天然拾取, 同名 _back.json 配对上传。"""
    jpg = os.path.join(results_dir, f"sn_{sn}_{ts}_back.jpg")
    cv2.imwrite(jpg, frame)
    meta = {"sn": sn, "score": float(score), "ts": ts_iso, "side": "back"}
    with open(os.path.join(results_dir, f"sn_{sn}_{ts}_back.json"), 'w') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    return jpg


def open_cap(rtsp):
    """统一建流入口: FFMPEG 后端 + 小缓冲(取最新帧)。"""
    cap = cv2.VideoCapture(rtsp, cv2.CAP_FFMPEG)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return cap

def open_verified(rtsp, tries=10):
    """开流并校验是"活流": isOpened + 分辨率>0 + 能真读到一帧。
    源半死时 open_cap 会返回 isOpened=True 但 0x0 的坏流, 这里挡掉, 交给调用方重连。
    返回可用 cap; 坏流返回 None。"""
    cap = open_cap(rtsp)
    if not cap.isOpened() or int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) <= 0:
        cap.release(); return None
    for _ in range(tries):
        if cap.grab():
            r, f = cap.read()
            if r and f is not None and f.size > 0:
                return cap
    cap.release(); return None

def source_up(rtsp, timeout=2.0):
    """探测 RTSP 源端口是否在监听。源(单客户端摄像头)脏断开后会退出,
    这里先探活再重连, 避免对着死端口狂刷 ffmpeg。"""
    u = urlparse(rtsp)
    host, port = u.hostname, (u.port or 554)
    s = socket.socket(); s.settimeout(timeout)
    try:
        s.connect((host, port)); return True
    except Exception:
        return False
    finally:
        s.close()


# ===== 观测(A方案): Recv-Q 积压 = 已收到但应用没读走的字节 =====
# 判据意义: 源死了 -> 积压不涨且帧速为0; 流活着但读线程被卡 -> 积压持续增长且帧速为0.
# 后者正是当前故障形态, 以前无法与前者区分, 只能靠"帧龄"猜, 于是误判成坏流去 release(杀源)。
def _tcp_recvq(host, port):
    """读本机到 host:port 的 ESTABLISHED 连接 Recv-Q 之和(字节)。非 root 也可读。
    返回 (recvq字节, 连接数); 读不到返回 (-1, 0)。"""
    ip = socket.gethostbyname(host) if host else None
    if not ip:
        return -1, 0
    def _hex4(a):
        return "%08X" % (int.from_bytes(socket.inet_aton(a), "little"))
    want_local = _hex4(ip)
    want_port = "%04X" % port
    total = 0; n = 0
    try:
        with open("/proc/net/tcp") as f:
            next(f, None)
            for line in f:
                p = line.split()
                if len(p) < 5 or p[3] != "01":        # 01 = ESTABLISHED
                    continue
                lip, lport = p[1].rsplit(":", 1)
                rip, rport = p[2].rsplit(":", 1)
                if rip == want_local and rport == want_port:
                    total += int(p[4]); n += 1
    except Exception:
        return -1, 0
    return total, n


def _conn_local_port(host, port):
    """本机连到 host:port 的 ESTABLISHED 连接里, 找出属于**本进程**那条的本地端口。
    用于按连接维度读 Recv-Q(和其他客户端如 ffmpeg 区分开)。取本地端口最大的那条
    (启发式: 后建立的连接端口号更大), 拿不到返回 None。"""
    ip = socket.gethostbyname(host) if host else None
    if not ip:
        return None
    def _hex4(a):
        return "%08X" % (int.from_bytes(socket.inet_aton(a), "little"))
    want_local = _hex4(ip)
    want_port = "%04X" % port
    my_inodes = set()
    try:
        for fd in os.listdir("/proc/self/fd"):
            try:
                tgt = os.readlink(f"/proc/self/fd/{fd}")
                if tgt.startswith("socket:["):
                    my_inodes.add(tgt[8:-1])
            except OSError:
                continue
        with open("/proc/net/tcp") as f:
            next(f, None)
            best = None
            for line in f:
                p = line.split()
                if len(p) < 10 or p[3] != "01":
                    continue
                lip, lport = p[1].rsplit(":", 1)
                rip, rport = p[2].rsplit(":", 1)
                if rip == want_local and rport == want_port and p[9] in my_inodes:
                    if len(lport) != 4:
                        continue
                    lpn = int(lport, 16)
                    if best is None or lpn > best:
                        best = lpn
            return best
    except Exception:
        return None


# 观测状态: 本进程那条 RTSP 连接的本地端口 + 最近几次 Recv-Q 采样(判积压涨/落)
_probe_port = None
_recvq_hist = []          # [(时刻, Recv-Q字节), ...] 最多留 8 个
def _recvq_reset():
    global _probe_port
    _probe_port = None
    _recvq_hist.clear()

def _parse_line(line):
    """/proc/net/tcp 一行 -> (local_port, recvq, inode) 或 None。
    防御: 字段数不足/字段形态异常一律返回 None, 绝不让观测代码抛异常
    把识别主循环带崩(观测是加法, 必须无副作用)。

    注意 p[4] 是 "tx_queue:rx_queue" 冒号连接的**两个**十六进制数, 不是单个数。
    只写 int(p[4]) 会在任何非零队列上抛 ValueError, 被外层 except 吞掉后
    恒返回 0 —— 恰好在"真有积压"时失明, 等于探针白装。故取冒号右侧为 Recv-Q。"""
    try:
        p = line.split()
        if len(p) < 10 or p[3] != "01":        # 01 = ESTABLISHED
            return None
        lport_hex = p[1].rsplit(":", 1)[-1]
        if len(lport_hex) != 4:
            return None
        rxq_hex = p[4].rsplit(":", 1)[-1]
        return int(lport_hex, 16), int(rxq_hex, 16), p[9]
    except (ValueError, IndexError):
        return None

def _read_socket_q(local_port):
    """按本地端口读本进程那条连接的 (Recv-Q, 是否存在)。"""
    try:
        with open("/proc/net/tcp") as f:
            next(f, None)
            for line in f:
                r = _parse_line(line)
                if r and r[0] == local_port:
                    return r[1], True
    except Exception:
        pass
    return 0, False

def _probe_rtsp(reader, rtsp, rtsp2=None):
    """定位本进程自己那条 RTSP 连接并记一笔初始 Recv-Q(其余客户端如 ffmpeg 不算进来)。"""
    global _probe_port
    _probe_port = _conn_local_port(urlparse(rtsp).hostname, urlparse(rtsp).port or 554)

def _probe_cached(reader):
    """取本进程连接当前 Recv-Q 并追加到采样历史; 未定位到时退化为"全部到该主机的连接之和"。"""
    q, n = 0, 0
    if _probe_port is not None:
        q, ok = _read_socket_q(_probe_port)
        n = 1 if ok else 0
    else:
        return 0, 0
    _recvq_hist.append((time.time(), q))
    del _recvq_hist[:-8]
    return q, n

def reconnect(rtsp, probe_gap=3, alert_after=5):
    """重连: 先探源端口在监听才建流; 源已退出则每 probe_gap 秒轻量探一次
    (仅 TCP 探活, 不狂刷 ffmpeg), 摄像头推流重启后自动恢复。
    实测该源单连接极稳(150s 0 掉帧), 故重连主要应对源侧退出/瞬时抖动。
    返回可用 cap; STOP 时返回 None。"""
    tries = 0
    while not STOP:
        if not source_up(rtsp):
            tries += 1
            if tries == alert_after:
                print("  ⚠ 源持续未监听(摄像头推流服务可能已退出), 请重启摄像头; 继续等待...")
            time.sleep(probe_gap)
            continue
        cap = open_verified(rtsp)        # 校验活流(挡 0x0 半死流)
        if cap is not None:
            print("  ✅ 重连成功")
            reset_decode_errs(); mark_reconnect()
            return cap
        time.sleep(probe_gap)
    return None

def reconnect_bounded(rtsp, max_tries=3, probe_gap=5, verify_wait=0.0, max_secs=0.0):
    """有界重连: 最多试 max_tries 轮(每轮先探活, 通了才开流), 用尽返回 None。

    为什么必须有界: 运行期绝不能让一个源拖死整个主循环 —— 现场故障里
    .8 摄像头掉线后, 无限循环的 reconnect() 把识别主循环永久卡在 06:20:29,
    正面 SN 识别与抓拍一起停摆 3 小时以上且完全静默。宁可降级为单路,
    也不能整机停摆。返回 None 由调用方转入低频后台重探。

    verify_wait: 每轮之间的额外等待; 后台重探传 0 以免拖慢主循环。
    max_secs:    墙钟上限(>0 时生效)。**必须用墙钟而非轮数兜底** ——
                 open_verified() 内部自带最长 30s 阻塞, 轮数相同耗时可能差
                 好几倍(实测 3 轮曾跑到 90s), 只有秒数能给出真实上界。
    """
    t_start = time.monotonic()
    for i in range(max_tries):
        if STOP:
            return None
        if max_secs > 0 and i > 0 and time.monotonic() - t_start >= max_secs:
            break
        if source_up(rtsp):
            cap = open_verified(rtsp)        # 校验活流(挡 0x0 半死流)
            if cap is not None:
                print("  ✅ 重连成功")
                reset_decode_errs(); mark_reconnect()
                return cap
        if probe_gap > 0:
            time.sleep(probe_gap)
    return None


# ===== 长稳韧性: 背面链路健康检查与有界自愈 =====
# 现场故障复盘: 背面(.8)健康检查原先嵌在"SN 命中"分支内, 工位静置 12 小时
# 一个板都没放, 于是检查一次都没跑 —— 摄像头挂了 3 小时 43 分系统零感知、
# 零日志。检查必须独立于识别事件; 恢复必须低频、非阻塞, 且失败不拖累主循环。

def back_health_tick(reader, min_interval=10.0, wedge_age=3.0, grace=15.0):
    """背面链路健康检查: 独立于识别事件, 静置期也在主循环里周期跑。

    返回 (need_recover, stale_age)。need_recover=True 表示这一路已经
    连续多次取不到新鲜帧, 该交给 probe_and_recover 去重连。
    只做非阻塞 peek, 绝不在这里开流 —— 开流的代价由调用方节流。

    ★ 三态判断, 不能拿 latest() 的 age 直接比阈值:
      - 有帧且新        -> 健康
      - 有帧但太旧      -> 异常计数 +1
      - 从未有过帧      -> 刚挂上 cap / 源还没出流, 给 grace 秒宽限期,
                           期内不算异常(否则会把刚接上的流立刻再掐掉)
    """
    now = time.time()
    if now - back_health_tick.last < min_interval:      # 节流: 别每轮都查
        return False, 0.0
    back_health_tick.last = now
    if reader is None:
        return False, 0.0
    age = reader.last_age()                             # 非阻塞; 从未有帧 = None
    if age is None:
        # 刚 set_cap 的 reader 还没有首帧, 属于正常过渡期, 给它时间出流
        if now - getattr(reader, "_cap_t", now) < grace:
            return False, 0.0
        stale_age = -1.0                                # 超宽限仍无帧 = 真的没流
    elif age <= wedge_age:
        back_health_tick.fails = 0
        return False, 0.0
    else:
        stale_age = age
    back_health_tick.fails += 1
    return True, stale_age
back_health_tick.last = 0.0
back_health_tick.fails = 0


def probe_and_recover(reader, rtsp, min_interval=30.0, wedge_fails=3, wedge_age=3.0,
                      fails=0, max_secs=45.0):
    """背面恢复: 低频、有界、非阻塞。

    先纯 TCP 探活(source_up, ~0s), 源真回来了才付"开流"的代价。
    无论成功失败都在有界时间内返回, 绝不把主循环拖住。
    返回 True 表示这一路当前不可用(供状态外显用)。

    ★ 前置守卫: 链路已经健康时立刻返回, 绝不碰流。
      否则这个函数每 30s 都会再开一条新流盖掉正在正常工作的那条 ——
      对"单客户端"相机源尤其危险(见 rtsp-camera-udp-teardown: 旧连接没
      TEARDOWN 干净, 新连接拉不起来)。恢复动作只在真离线时才允许发生。

    ★ 为什么背面必须和正面用同一套判据(现场: 正面子码流基本不挂死, 背面经常挂死):
      原先背面从"帧陈旧"直接跳到重连, 缺了正面已有的三层保护, 反把单客户端源
      往脏断开方向推 —— 这解释了同一台相机上两种客户端的不对称。
    """
    # 已经能持续拿到新鲜帧 -> 无需恢复, 也不该打断
    age = reader.last_age()
    if age is not None and age <= wedge_age:
        probe_and_recover.offline = False
        return False

    # ★ 门1: 失败计数门。告警门是连续 wedge_fails 次异常, 动作门原先却是**单次**
    #   last_age() 采样 —— 一次 3s 抖动就重连一次, 背面长期在重连 churn 里打转。
    #   只计数不动流, 等 back_health_tick 攒够次数。
    if fails < wedge_fails:
        return probe_and_recover.offline

    # ★ 门2: IDR 自愈判据(对齐正面)。解码错误正刷 = 解码器等下个 IDR 关键帧
    #   自愈, 是瞬时坏流。此时**绝不重连**: 重连从 GOP 中间重进, 又吃到一片
    #   ref-error -> churn 自激, 越治越坏。保持同连接等 IDR 即可。
    if decode_err_rate(2.0) > 0:
        print("  · 背面解码错在刷(ref-error), 等 IDR 自愈(不重连)...")
        return probe_and_recover.offline

    now = time.time()
    if now - probe_and_recover.last < min_interval:     # 节流: 低频重探
        return probe_and_recover.offline
    probe_and_recover.last = now

    if not source_up(rtsp, timeout=1.0):
        # ★ 每次都出声: 原先只在第 alert_after 次印一次, 之后进入静默空转,
        #   现场因此出现 1.5 小时无任何输出的"看不出还在不在跑"。
        print(f"  · 背面链路离线(低频重探中, 每{min_interval:.0f}s一次) —— 源端口未监听")
        probe_and_recover.offline = True
        return True

    # ★ 门3: 先释放、后开流(对齐正面)。单客户端源上, 旧连接没断干净就建新连接,
    #   会出现两条 RTSP 会话并存的窗口 —— 正是 rtsp-camera-udp-teardown 里
    #   "脏断开后再也拉不起来"的成因。必须先让 reader 线程放开旧 cap 再开新的。
    reader.set_cap(None)
    reader.wait_released(6.0)

    cap = reconnect_bounded(rtsp, max_tries=1, probe_gap=0, max_secs=max_secs)
    if cap is None:
        print(f"  · 背面源端口在监听但开流失败(可能仍无码流), 下轮再试")
        probe_and_recover.offline = True
        return True
    reader.set_cap(cap)
    print("  ✅ 背面链路已恢复")
    probe_and_recover.offline = False
    return False
probe_and_recover.last = 0.0
probe_and_recover.offline = False


def start_loop_watchdog(beat, threshold=60.0, interval=10.0, on_alert=None):
    """主循环看门狗: 兜住任何"未知阻塞"。

    P1/P2 修的是已知阻塞点(背面健康检查、reconnect); 看门狗保证将来
    任何没预料到的阻塞都不会再变成"静默几小时"。

    ★ 只告警, 不杀进程 —— 识别本身可能因大板/小板慢而合法地占数十秒,
      自动重启有误伤风险。先确保"一定会被看见", 由人工判断。
    beat: 一个带 .t 属性的对象或 {'t': float} 字典, 主循环每轮末更新。
    """
    def _get():
        return beat["t"] if isinstance(beat, dict) else beat.t

    def _watch():
        alerted = False
        while not STOP:
            time.sleep(interval)
            gap = time.time() - _get()
            if gap > threshold:
                msg = (f"  ⚠⚠ 主循环停滞 {gap:.0f}s 未推进(阈值{threshold:.0f}s)"
                       f" —— 疑似新阻塞点, 请查日志定位")
                print(msg)
                if on_alert:
                    try:
                        on_alert(msg)
                    except Exception:            # noqa: BLE001
                        pass
                alerted = True
            elif alerted:
                print(f"  ✅ 主循环已恢复推进(停滞结束)")
                alerted = False

    t = threading.Thread(target=_watch, name="loop-watchdog", daemon=True)
    t.start()
    return lambda: None        # 守护线程随进程退出, 无需显式停

def main():
    p = argparse.ArgumentParser(description="SN v10")
    p.add_argument("--rtsp", required=True)
    # v2.1 第二路摄像头(背面): 只拉流不识别, 命中瞬间抓一帧背面图。空=双路关闭, 行为完全等同单路(V2.0.2)
    p.add_argument("--rtsp2", type=str, default="",
                   help="第二路(背面)RTSP; 空=不开第二路(单路)。IP调通后填 rtsp://<cam2>:8554/live0")
    p.add_argument("--rtsp2-grab-wait", type=float, default=0.5, dest="rtsp2_grab_wait",
                   help="命中时从第二路取最新帧的最大阻塞秒(小值防拖慢主识别)")
    p.add_argument("--n", type=int, default=3)
    p.add_argument("--fi", type=float, default=1.0)
    p.add_argument("--mi", type=float, default=5.0, help="监控间隔(秒), 慢一点减少HEVC压力")
    p.add_argument("--poll", type=float, default=1.0, help="侦测轮询间隔(秒): 状态机多久看一次画面变化, 越小反应越快")
    p.add_argument("--dt", type=float, default=0.05, help="画面运动阈值: 超过=场景在变(换板/手), 等稳定后再识别")
    p.add_argument("--dt-strong", type=float, default=0.05, dest="dt_strong",
                   help="大变化阈值: 无铭牌但变化>此值时兜底OCR(防find_barcode漏检)")
    p.add_argument("--idle", type=float, default=600)
    p.add_argument("--ocr-idle-unload", type=float, default=30.0, dest="ocr_idle_unload",
                   help="模型闲时释放: 距上次识别超此秒数释放模型省NPU, 下块板懒加载。0=永不释放(常驻)")
    p.add_argument("--wedge-fails", type=int, default=3, dest="wedge_fails",
          help="连续无新鲜帧达此次数即判坏流并立即release+重连(默认3≈30s发现; 旧值8过保守)")
    # ★ 韧性三件套(P1/P2/P5): 防"一个源拖死主循环"、防"静置期失明"
    p.add_argument("--start-reconnect-tries", type=int, default=3, dest="start_reconnect_tries",
          help="启动期正面源不可达时的有界重连轮数, 用尽则退出交 systemd 重试(默认3)")
    p.add_argument("--start-reconnect-secs", type=float, default=120.0, dest="start_reconnect_secs",
          help="启动期有界重连的墙钟上限(秒)。open_verified 每次自身可阻塞30s, 只靠轮数"
               "给不出真实上界(实测3轮≈90s)。默认120交棒 systemd 重启")
    p.add_argument("--start-reconnect-gap", type=float, default=5.0, dest="start_reconnect_gap",
          help="启动期重连轮间隔(秒)。启动期要快探(相机刚上电到出流通常几秒), "
               "与运行期的 --back-reprobe-interval(30s) 意图不同, 故分开(默认5)")
    p.add_argument("--runtime-reconnect-secs", type=float, default=45.0, dest="runtime_reconnect_secs",
          help="运行期单次有界重连的墙钟上限(秒)。必须明显小于 --loop-watchdog, "
               "否则重连本身就够长到被看门狗误报(默认45)")
    p.add_argument("--back-reconnect-tries", type=int, default=3, dest="back_reconnect_tries",
          help="运行期(正面/背面)单次有界重连轮数, 用尽后回主循环下轮再试(默认3)")
    p.add_argument("--back-reprobe-interval", type=float, default=30.0, dest="back_reprobe_interval",
          help="背面离线后的低频重探间隔(秒); 只做 TCP 探活, 通了才开流(默认30)")
    p.add_argument("--back-check-interval", type=float, default=10.0, dest="back_check_interval",
          help="背面健康检查周期(秒), 独立于识别事件, 静置期也跑(默认10; 0=关闭)")
    p.add_argument("--loop-watchdog", type=float, default=60.0, dest="loop_watchdog",
          help="主循环停滞告警阈值(秒), 只告警不杀进程(默认60; 0=关闭)")
    p.add_argument("--wedge-age", type=float, default=3.0, dest="wedge_age",
 help="帧龄超此秒数判无新鲜帧(后台线程排不出新帧=流卡死), 触发重连(默认3s)")
    p.add_argument("--wedge-dead-s", type=float, default=25.0, dest="wedge_dead_s",
        help="无新鲜帧超此秒数=真坏流(即便ref-error在刷也强制重连兜底, 默认25s)")
    p.add_argument("--stale-window", type=float, default=4.0, dest="stale_window",
        help="解码错误统计窗口(秒)")
    p.add_argument("--stale-errs", type=int, default=20, dest="stale_errs",
        help="窗口内解码失步错误达此数即判旧帧回放并重连(默认20)")
    p.add_argument("--stale-cooldown", type=float, default=6.0, dest="stale_cooldown",
        help="重连后冷却期(秒)内不再触发旧帧回放重连")
    # REQ-005 SN 格式配置化(换板只调参不改码)
    p.add_argument("--sn-min-len", type=int, default=15, dest="sn_min_len")
    p.add_argument("--sn-max-len", type=int, default=20, dest="sn_max_len")
    p.add_argument("--sn-prefix", type=str, default="", dest="sn_prefix",
                   help="前缀白名单,逗号分隔;空=不限")
    p.add_argument("--merge-gap", type=int, default=3, dest="merge_gap")
    # REQ-007 结果二次确认
    p.add_argument("--confirm", type=int, default=2, help="连续同一SN达此次数即确认落库")
    p.add_argument("--confirm-score", type=float, default=0.95, dest="confirm_score",
                   help="单次达此分且满帧命中即确认")
    p.add_argument("--place-retries", type=int, default=6, dest="place_retries",
                   help="同一在位板未确认时最大重识次数(修B:不稳板在位持续重试攒确认,满数暂锁防空耗NPU)")
    p.add_argument("--profile", choices=["big","small"], default="small",
                   help="识别档: big=居中大板(W30H40裁剪+不切块@2.5x+2帧投票,~1s); small=小板(整帧3x3@2x+修B重试)")
    p.add_argument("--center-crop", type=float, default=None, dest="center_crop_cli",
                   help="中心裁剪比例(宽高同设, 覆盖profile): 1.0=整帧, 0.6=中心60%%")
    p.add_argument("--crop-w", type=float, default=None, dest="crop_w_cli",
                   help="中心裁剪宽比例(覆盖profile与center-crop): big=0.3")
    p.add_argument("--crop-h", type=float, default=None, dest="crop_h_cli",
                   help="中心裁剪高比例(覆盖profile与center-crop): big=0.4")
    p.add_argument("--crop-cy", type=float, default=None, dest="crop_cy_cli",
                   help="裁剪垂直锚点(覆盖profile): 0.5=居中, small=0.32偏上(SN在上带)")
    p.add_argument("--frames", type=int, default=None, dest="frames_cli",
                   help="单次识别抽帧数(覆盖profile): big=2跨帧投票修掉字, small=1")
    p.add_argument("--no-rescue", action="store_false", dest="rescue",
                   help="关闭救援二遍(首遍无SN时对ROI补±90°旋转重识别)")
    p.add_argument("--no-save-miss", action="store_false", dest="save_miss",
                   help="关闭漏检/待确认留证(存原帧+ROI+候选到 debug/)")
    p.add_argument("--miss-keep", type=int, default=40, dest="miss_keep",
                   help="留证目录最多保留组数(超出删最旧)")
    # V3.0 调机预览(可选, 全带默认值 -> 不给也照常跑纯识别)
    p.add_argument("--no-preview", action="store_false", dest="preview",
                   help="关闭内嵌预览(退回 V2 纯识别行为; 预览出问题时的干净回退开关)")
    p.add_argument("--preview-port", type=int, default=8090, dest="preview_port")
    p.add_argument("--preview-fps", type=float, default=8.0, dest="preview_fps",
                   help="预览编码上限帧率(节流阈值, 不追满解码帧率; 8 远低于实测上限, 给识别留足余量)")
    p.add_argument("--preview-res", choices=["4k", "1080p", "720p", "360p"], default="4k",
                   dest="preview_res", help="预览默认档位; 只影响预览副本的缩放, 识别图恒为全幅")
    p.add_argument("--preview-cam", choices=["both", "front", "back"], default="both",
                   dest="preview_cam", help="预览哪几路(背面/第二路仅拉流不识别)")
    p.add_argument("--preview-jpeg-quality", type=int, default=80, dest="preview_jpeg_quality",
                   help="记录用(Bmcv 实测不可调); 留档便于换编码器时对齐")
    args = p.parse_args()
    # profile: 一档参数打包; 用户显式CLI标志仍覆盖profile
    PROFILES = {
        "small": {"crop_w":0.40, "crop_h":0.35, "crop_cy":0.32, "tile_grid":(1,1), "tile_up":3.0, "roi_path":False, "poll":1.0, "place_retries":6, "rescue":True,  "progressive":False, "confirm":2, "min_report":0.0, "frames":2, "sn_min_len":17},
        "big":   {"crop_w":0.3,  "crop_h":0.4,  "crop_cy":0.5,  "tile_grid":(1,1), "tile_up":2.5, "roi_path":False, "poll":0.4, "place_retries":6, "rescue":False, "progressive":False, "confirm":1, "min_report":0.0, "frames":2, "sn_min_len":17},
    }
    _prof = PROFILES[args.profile]
    _expl = lambda fl: any(a==fl or a.startswith(fl+'=') for a in sys.argv[1:])
    _cc = args.center_crop_cli   # --center-crop 作为"宽高同设"的兼容入口
    args.crop_w = args.crop_w_cli if args.crop_w_cli is not None else (_cc if _cc is not None else _prof['crop_w'])
    args.crop_h = args.crop_h_cli if args.crop_h_cli is not None else (_cc if _cc is not None else _prof['crop_h'])
    args.crop_cy = args.crop_cy_cli if args.crop_cy_cli is not None else _prof.get('crop_cy', 0.5)
    args.tile_grid = _prof['tile_grid']; args.tile_up = _prof['tile_up']; args.roi_path = _prof['roi_path']
    if not _expl("--poll"): args.poll = _prof["poll"]
    if not _expl("--place-retries"): args.place_retries = _prof["place_retries"]
    if not _expl("--sn-min-len"): args.sn_min_len = _prof["sn_min_len"]
    if not _expl("--no-rescue"): args.rescue = _prof["rescue"]
    args.progressive = _prof['progressive']; args.min_report = _prof['min_report']
    if not _expl("--confirm"): args.confirm = _prof["confirm"]
    args.frames = args.frames_cli if args.frames_cli is not None else _prof['frames']
    start_stderr_monitor()
    start_log_timestamp()      # 必须在 stderr 劫持之后: 否则 _stderr_pump 拿到的是被包装过的 fd

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    prefixes = [x.strip().upper() for x in args.sn_prefix.split(",") if x.strip()] or None
    sn_cfg = SNConfig(min_len=args.sn_min_len, max_len=args.sn_max_len,
                      prefixes=prefixes, max_merge_gap=args.merge_gap)
    gate = ResultGate(confirm=args.confirm, confirm_score=args.confirm_score)

    print("="*60)
    print("  SN提取 v17 同板去抖(静板不重识)+模型闲释放懒加载+快轮询降延迟+渐进式识别(L1命中即停)+断重连churn+防多字+二次确认")
    print(f"  监控间隔={args.mi}s 运动阈值={args.dt}(超过=场景在变, 稳定后再识别)")
    roi_s = '开' if args.roi_path else '关'
    print(f"  档位={args.profile} 裁剪W×H={args.crop_w}×{args.crop_h}@cy{args.crop_cy} 切片={args.tile_grid[0]}x{args.tile_grid[1]}@{args.tile_up}x ROI路={roi_s} 抽帧={args.frames} 渐进L2L3={args.progressive} poll={args.poll}s 在位重试={args.place_retries} 确认={args.confirm} 上报下限={args.min_report}")
    print(f"  SN长度窗=[{args.sn_min_len},{args.sn_max_len}] 前缀={prefixes or '不限'} "
   f"确认次数={args.confirm} 强确认分={args.confirm_score}")
    print(f"  救援二遍={'开' if args.rescue else '关'} 漏检留证={'开' if args.save_miss else '关'}"
          f"(保留{args.miss_keep}组 -> {DEBUG_DIR})")
    print("="*60)

    os.makedirs(RESULTS_DIR, exist_ok=True)
    eng = OCR()
    last = None; idle_t = time.time(); sn_cnt = 0; mon = 0; fail = 0
    last_event = time.time()   # 上次识别事件时刻(驱动模型闲时释放)
    recog_ref = None           # 上次识别的画面帧(同板去抖: 与之无实质差异则不重识)
    state = "empty"; settle = False; place_tries = 0   # 状态机: empty=空场; settle=待稳定判场景; place_tries=同一在位板已重识次数
    mark_reconnect()

    # ★ 顺序很关键: 先把 FrameReader 建出来并**挂上预览**, 再去开流。
    #   现场故障复盘: attach() 原先排在初始 reconnect() 之后, 源不通时进程卡在
    #   建流处, 永远走不到 attach -> :8090 不监听 -> 前端拿不到状态、连"离线"
    #   都显示不出来。改成"先起预览线程, 后接流": 源好不好都能上报状态。
    reader = FrameReader("front")          # 后台线程持续排空缓冲, 主循环只取最新帧

    # v2.1 第二路(背面)只拉流: 复用 FrameReader; 空 --rtsp2 = 双路关(reader2=None, 全程 if reader2 守卫, 单路路径不变)
    reader2 = None
    if args.rtsp2:
        reader2 = FrameReader("back")      # 先挂空, 由下面的低频重探/启动开流补上

    print("\n[监控启动]\n")

    # V3.0: 预览挂在**已存在**的 FrameReader 上, 不新建连接 -> 相机源仍只有 1 条 RTSP。
    # 位置在 try 之外: 即使预览起不来, 也不会掩盖下面识别主循环的异常处理。
    preview_stop = lambda: None
    if args.preview and sn_preview_embed is not None:
        _pv_cam = {"both": (True, True), "front": (True, False), "back": (False, True)}[args.preview_cam]
        _, preview_stop = sn_preview_embed.attach(
            reader, reader2, RESULTS_DIR,
            port=args.preview_port, fps=args.preview_fps, res=args.preview_res,
            preview=_pv_cam, jpeg_quality=args.preview_jpeg_quality)

    # 正面(识别路): 冷启动允许短等待, 但有界 —— 源不通就退出, 不无限挂着。
    # 旧行为(reconnect 无限循环)会让服务在源缺失时永久卡在启动期, 连预览端口都起不来。
    cap = open_verified(args.rtsp)       # 校验活流, 挡 0x0 半死流
    if cap is None:
        print("首次打开失败/坏流, 有界重连等待源...")
        cap = reconnect_bounded(args.rtsp, max_tries=args.start_reconnect_tries,
                                probe_gap=args.start_reconnect_gap,
                                max_secs=args.start_reconnect_secs)
    if cap is None:
        print("[退出] 正面源不可达且重连用尽 —— 由 systemd 按 Restart=always 重试"
              "(预览端口已在监听, 可从前端看到离线状态)")
        reader.run = False
        if reader2:
            reader2.run = False
        preview_stop()
        return
    print(f"  流: {int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))}x{int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))}")
    reader.set_cap(cap)

    if reader2 is not None:
        cap2 = open_verified(args.rtsp2)
        if cap2 is not None:
            reader2.set_cap(cap2)
            print(f"  第二路(背面): {int(cap2.get(cv2.CAP_PROP_FRAME_WIDTH))}x{int(cap2.get(cv2.CAP_PROP_FRAME_HEIGHT))}")
        else:
            # ★ 不再"降级为单路就再也不管": 保留 reader2, 由主循环的低频重探自动接上。
            #   旧行为下这一路此后永久失明, 现场正是这么挂掉 3 小时无感知的。
            print(f"  ⚠ 第二路 {args.rtsp2} 暂不可达 —— 保留背面通道, 由低频重探自动接上")

    # P5: 主循环看门狗 —— 兜住将来任何未知阻塞, 只告警不杀进程
    _beat = {"t": time.time()}
    start_loop_watchdog(_beat, threshold=args.loop_watchdog, interval=10.0)

    try:
        while not STOP:
            _beat["t"] = time.time()      # 每轮标记推进, 供看门狗判停滞
            if args.idle > 0 and time.time()-idle_t > args.idle:
                print(f"\n[超时]"); break   # 常驻服务传 --idle 0 表示永不超时

            # 取最新帧: 后台线程持续排空解码缓冲, 这里永远拿到最新解码帧(根治旧帧回放)。
            frame, age, rfail = reader.latest(wait=2.0)
            if frame is None or age > args.wedge_age:
                fail += 1
                # 观测: 积压是否在涨 + 解码速率是否归零, 用于区分"源死了"和"读线程被卡住"
                if fail == 1:
                    _probe_rtsp(reader, args.rtsp, args.rtsp2)
                rq, rqn = _probe_cached(reader)
                grow = ""
                if len(_recvq_hist) >= 2:
                    d = _recvq_hist[0][1] - _recvq_hist[-1][1]
                    grow = f" 积压{'↑' if d > 0 else '↓'}{abs(d)//1024}KB/{_recvq_hist[0][0]-_recvq_hist[-1][0]:.0f}s"
                diag = (f"帧速{reader.rate:.1f}f/s blk={reader.blk:.2f}s(max{reader.blk_max:.1f})"
                        f" Recv-Q={rq//1024}KB({rqn}条){grow}")
                # 根因C: ref-error(丢参考帧)正刷=解码器等下个IDR关键帧自愈, 瞬时坏流。
                # 绝不重连(重连从GOP中间重进->又一片ref-error->churn自激)。保持同连接等IDR。
                active_errs = decode_err_rate(2.0)
                if active_errs > 0 and age < args.wedge_dead_s:
                    if fail == 1:
                        print(f"  无新鲜帧(帧龄{age:.1f}s/解码错{active_errs}条/2s) {diag}, 等IDR自愈(不重连)...")
                    time.sleep(0.3)
                    continue
                # 无解码活动的短暂无帧: 给瞬时抖动一点自愈时间
                if fail < args.wedge_fails and age < args.wedge_dead_s:
                    if fail == 1:
                        print(f"  无新鲜帧(帧龄{age:.1f}s/线程失败{rfail}) {diag}, 快速等待自愈...")
                    time.sleep(0.3)
                    continue
                # 真坏流: 无解码活动持续无帧, 或超死线仍无帧 -> release+有界重连
                # ★ 关键: 用 reconnect_bounded 而非无限循环的 reconnect —— 源回不来时
                #   必须让主循环继续转(背面检查/看门狗/状态行都靠它), 而不是整机停摆。
                print(f"  ⚠ 坏流确认(帧龄{age:.1f}s/失败{fail}/解码错{active_errs}) {diag}, 释放并重连...")
                reader.set_cap(None)
                reader.wait_released(6.0)
                cap = reconnect_bounded(args.rtsp, max_tries=args.back_reconnect_tries,
                                        probe_gap=args.back_reprobe_interval,
                                        max_secs=args.runtime_reconnect_secs)
                if cap is None:
                    # 正面暂不可用: 不清空 reader, 留在原地由下一轮继续尝试有界重连。
                    # 旧行为这里 break 直接退出进程, 服务靠 systemd 拉起 —— 但那样
                    # 预览端口每次都要重起, 前端会看到画面反复中断。
                    print(f"  ⚠ 正面源本轮重连未成功, 继续监控(下轮再试, 不退出)")
                    fail = 0
                    time.sleep(1.0)
                    continue
                reader.set_cap(cap)
                fail = 0
                _recvq_reset()
                _probe_rtsp(reader, args.rtsp, args.rtsp2)
                continue
            fail = 0
            mon += 1
            # ★ P1: 背面链路健康检查, 与识别事件完全解耦 —— 静置期(无人放板)也在跑。
            #   现场故障复盘: 原先这段嵌在"SN 命中"分支里, 工位静置 12 小时一个板都没放,
            #   检查一次都没执行 -> .8 挂了 3h43m 系统零感知、零日志。
            back_offline = False
            if reader2 is not None:
                need, bage = back_health_tick(
                    reader2, min_interval=args.back_check_interval, wedge_age=args.wedge_age)
                if need and back_health_tick.fails >= args.wedge_fails:
                    # 连续多次取不到新鲜帧 -> 交给低频重探(有界、非阻塞)。
                    # 用 last_age() 报真实帧龄: latest() 在无帧时给的是占位 0.0,
                    # 照抄到日志里会印出"帧龄0.0s 却判异常"这种自相矛盾的记录。
                    _la = reader2.last_age()
                    _desc = "从未出帧" if _la is None else f"帧龄{_la:.1f}s"
                    print(f"  ⚠ 背面链路异常({_desc}/连续{back_health_tick.fails}次), 转入低频重探...")
                # ★ 每次主循环都调, 由函数内部按 min_interval 节流 —— 它自己负责
                #   "探活->开流->set_cap", 健康检查只负责报"该重探了", 不在这里多手 set_cap,
                #   否则刚接上的流会被下一轮再次清掉, 永远接不上。
                # ★ 把当前的连续异常次数传进去: 动作门必须与告警门同源, 否则一次
                #   瞬时抖动就会动流(见 probe_and_recover 门1)。
                back_offline = probe_and_recover(
                    reader2, args.rtsp2,
                    min_interval=args.back_reprobe_interval,
                    wedge_fails=args.wedge_fails,
                    wedge_age=args.wedge_age,
                    fails=back_health_tick.fails,
                    max_secs=args.runtime_reconnect_secs)
                # 已达门且真动了流(或确认离线)才清零计数, 让下一轮从 0 重新攒。
                if need and back_health_tick.fails >= args.wedge_fails and (
                        back_offline or probe_and_recover.last > 0):
                    back_health_tick.fails = 0
            # 背面离线心跳: 每 30s 一条, 保证"静置期也能从日志看出链路状态"
            if reader2 is not None and mon % 30 == 0:
                _la = reader2.last_age()
                _state = "离线(重探中)" if back_offline else (
                    "无帧" if _la is None else f"正常(帧龄{_la:.1f}s)")
                print(f"  [背面链路] {_state}")
            # 周期状态行(观测): 每 100 次监控记一行健康快照, 有病时能看出是从哪一刻开始退化的
            if mon % 100 == 0:
                rq, rqn = _probe_cached(reader)
                print(f"  [状态{mon}] 帧速{reader.rate:.1f}f/s blk={reader.blk:.2f}s(max{reader.blk_max:.1f}) "
                      f"帧龄{age:.2f}s Recv-Q={rq//1024}KB({rqn}条) 解码错{decode_err_rate(2.0)}条/2s")
            # 模型闲时释放: 距上次识别超阈且当前加载着 -> 释放省NPU(下块板懒加载)
            if args.ocr_idle_unload > 0 and eng.d is not None and time.time()-last_event > args.ocr_idle_unload:
                eng.unload()

            if not quality(frame):
                if mon%10==0: print(f"  [{mon}] 质量差")
                time.sleep(args.poll); continue

            # 运动检测: 与上周期帧比。运动大=场景在变(换板/手), 先不识别, 记下"变过"等它稳定。
            # 运动/去抖 diff 只看裁剪区(big=W30H40): 铭牌占比大, 画面变化更敏感、少受周边干扰
            d = fdiff(crop_center(last, args.crop_w, args.crop_h), crop_center(frame, args.crop_w, args.crop_h)) if last is not None else 1.0
            if d >= args.dt:
                settle = True
                if mon%10==0: print(f"  [{mon}] 运动中 diff={d:.4f}, 等稳定")
                last = frame.copy(); time.sleep(args.poll); continue

            # 画面已稳定(diff<dt)
            retrying = (state != "locked" and 0 < place_tries < args.place_retries)   # 修B:在位重试中不跳过
            if not settle and not retrying:
                if mon%10==0: print(f"  [{mon}] 稳定无变化 diff={d:.4f} 帧龄{age:.2f}s")
                last = frame.copy(); time.sleep(args.poll); continue

            # 从一段变化中稳定下来 -> 判稳定后的场景
            settle = False
            regions = find_regions(crop_center(frame, args.crop_w, args.crop_h, args.crop_cy))   # 只在(可上偏)裁剪区探, 省4K全图Sobel
            if not regions:
                # 稳定为空场 = 板子被拿走 -> 回空场等下一块(不 OCR, 消除换板过渡帧兜底误触)
                if state != "empty": print("  ▫ 板子已移开, 回到空场(等下一块)")
                state = "empty"; gate.feed(None); place_tries = 0   # 修B:板离场重置累积
                last = frame.copy(); time.sleep(args.poll); continue

            # 同板去抖: locked下画面与上次识别帧无实质差异(仅噪声/OSD秒跳的假运动) -> 同板在位, 不重识
            if state == "locked" and recog_ref is not None:
                dref = fdiff(crop_center(recog_ref, args.crop_w, args.crop_h, args.crop_cy), crop_center(frame, args.crop_w, args.crop_h, args.crop_cy))
                if dref < args.dt:
                    if mon%10==0: print(f"  [{mon}] 同板在位 diff_ref={dref:.4f}, 跳过重识")
                    last = frame.copy(); time.sleep(args.poll); continue

            # 稳定 + 有铭牌 = 一块待识别的板(空场后新板, 或换上的不同板) -> 识别
            print("\n" + "="*60)
            print(f"  📺 稳定铭牌! diff={d:.4f} [铭牌{len(regions)}处] 帧龄{age:.2f}s")
            print("="*60)

            # 渐进式识别(提速): 模型常驻, 命中即停。
            # L1: 单帧·直立(0°)·tiles —— 实测读出大板SN的路径, 达强确认(分≥阈且满帧命中)立即上报。
            # L2: 同帧补三角度(180/90/270)+ROI —— 直立没读全时补旋转。
            # L3: 再抽1~N帧跨帧投票 —— 前两步未达标才付多帧代价(小板/低分)。
            frames = [frame]
            frame_texts = []
            voted = None; lock_now = False   # 修B:仅report/dup锁板,pending/miss不锁以重试
            last_event = time.time()   # 本次事件, 刷新闲时计时
            recog_ref = frame.copy()   # 记住本次识别的画面, 供同板去抖对比
            # v2.1 / V4.0.1: 识别起点顺手抓背面最新帧(与正面 frames[0] 时间最接近)。
            # ★ 这里只负责"取帧", 不再负责"治病": 健康检查/重连已上移到主循环每轮
            #   (见 back_health_tick + probe_and_recover)。原先在命中分支里 reconnect(),
            #   对已垂死的单客户端源再补一刀, 且源不回就永久阻塞 —— 正是现场 3h43m 卡死的成因。
            back_frame = None
            if reader2:
                bf, bage, _ = reader2.latest(wait=args.rtsp2_grab_wait)
                if bf is not None and bage < args.wedge_age:
                    back_frame = bf
            try:
                eng.load()   # 懒加载: 闲时已释放则此处重加载(常驻时秒回空操作)
                t0 = time.time()
                frame_texts = [eng.ocr(frame, regions, rots=(0,), do_tiles=True, crop_w=args.crop_w, crop_h=args.crop_h, tile_grid=args.tile_grid, tile_up=args.tile_up, roi_path=args.roi_path, crop_cy=args.crop_cy)]
                for _ in range(args.frames - 1):   # big档:多帧快投票修单帧掉字(同快参数,不转向)
                    time.sleep(0.12)
                    f2, _, _ = reader.latest(wait=2.0)
                    if f2 is None: continue
                    frames.append(f2)
                    frame_texts.append(eng.ocr(f2, regions, rots=(0,), do_tiles=True, crop_w=args.crop_w, crop_h=args.crop_h, tile_grid=args.tile_grid, tile_up=args.tile_up, roi_path=args.roi_path, crop_cy=args.crop_cy))
                _lab = ("抽%d帧快投票" % len(frames)) if len(frames)>1 else "单帧直立"
                _nc = sum(len(ft) for ft in frame_texts)
                print(f"  [L1 {_lab}] {time.time()-t0:.1f}s 候选{_nc}个")
                for tt,ss in sorted(frame_texts[0], key=lambda x:-float(x[1]))[:5]:
                    print(f"    [{float(ss):.3f}] {tt}")
                voted = vote(frame_texts, sn_cfg)
                strong = bool(voted) and voted.get("score",0) >= args.confirm_score and voted["frames_hit"]==voted["total_frames"]
                if args.progressive and not strong:      # L2 直立未达强确认 -> 同帧补旋转(big档关)
                    t0 = time.time()
                    frame_texts[0] = frame_texts[0] + eng.ocr(frame, regions, rots=(180,90,270), do_tiles=True, crop_w=args.crop_w, crop_h=args.crop_h, tile_grid=args.tile_grid, tile_up=args.tile_up, roi_path=args.roi_path, crop_cy=args.crop_cy)
                    print(f"  [L2 单帧多角] +{time.time()-t0:.1f}s 累计候选{len(frame_texts[0])}个")
                    voted = vote(frame_texts, sn_cfg)
                    strong = bool(voted) and voted.get("score",0) >= args.confirm_score and voted["frames_hit"]==voted["total_frames"]
                if args.progressive and not strong and args.n > 1:     # L3 仍未达标 -> 抽帧跨帧投票(big档关)
                    print(f"  [L3 补帧跨帧投票] 再抽{args.n-1}帧...")
                    for i in range(args.n-1):
                        time.sleep(args.fi)
                        f2, _, _ = reader.latest(wait=2.0)
                        if f2 is None: continue
                        frames.append(f2)
                        t0 = time.time()
                        tx = eng.ocr(f2, (find_regions(f2) if args.roi_path else None), rots=(0,180,90,270), do_tiles=True, crop_w=args.crop_w, crop_h=args.crop_h, tile_grid=args.tile_grid, tile_up=args.tile_up, roi_path=args.roi_path, crop_cy=args.crop_cy)
                        frame_texts.append(tx)
                        print(f"  [帧{len(frames)}] {time.time()-t0:.1f}s 候选{len(tx)}个")
                    voted = vote(frame_texts, sn_cfg)

                if voted and voted.get('score',0) < args.min_report:
                    print(f"  ⚠ SN={voted['sn']} 分{voted['score']:.3f}<下限{args.min_report}, 不上报(防误报)")
                    voted = None
                if voted:
                    action, cnt = gate.feed(voted)     # REQ-007 二次确认
                    if action == "report":
                        sn_cnt += 1; lock_now = True
                        print(f"\n  ✅ 确认 SN={voted['sn']} score={voted['score']:.3f} "
                              f"命中={voted['frames_hit']}/{voted['total_frames']} "
                              f"合并={voted['merged']} (确认{cnt}次)")
                        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                        ts_iso = datetime.now().isoformat()   # v2.1 正反共用同一 ISO 时刻 -> .57 captured_at 一致, 前端可配对
                        with open(os.path.join(RESULTS_DIR, f"sn_{voted['sn']}_{ts}.json"),'w') as f:
                            json.dump(jsonable({**voted, "ts":ts_iso, "side":"front",
                                                "frame_texts":frame_texts}), f, ensure_ascii=False, indent=2)
                        with open(os.path.join(RESULTS_DIR,"sn_list.txt"),'a') as f:
                            f.write(f"{voted['sn']}\t{voted['score']:.3f}\t{ts}\n")
                        # 存正面命中帧作为绑定图（供产测系统展示；失败不影响落库）
                        try:
                            if frames:
                                save_hit_image(RESULTS_DIR, voted['sn'], ts, frames[0])
                        except Exception as _e:
                            print(f"  ⚠ 存命中帧失败: {_e}")
                        # v2.1 背面留证(第二路): 抓到才存, 拿不到就跳过(不影响正面落库)
                        try:
                            if back_frame is not None:
                                save_back_result(RESULTS_DIR, voted['sn'], ts, ts_iso, back_frame, voted['score'])
                                print(f"  📷 背面留证 sn_{voted['sn']}_{ts}_back.jpg")
                        except Exception as _e:
                            print(f"  ⚠ 存背面帧失败: {_e}")
                    elif action == "dup":
                        lock_now = True
                        print(f"\n  ↩ SN={voted['sn']} 已上报过, 不重复落库")
                    else:  # pending
                        print(f"\n  ⏳ 候选 SN={voted['sn']} score={voted['score']:.3f} "
                              f"待确认({cnt}/{args.confirm})")
                        if args.save_miss: save_debug(frames, frame_texts, "pending", args.miss_keep)
                else:
                    # 修B: 在位期间偶发miss不喂gate(不重置pending), 由空场/换板重置
                    print("\n  ❌ 未找到SN")
                    if args.save_miss: save_debug(frames, frame_texts, "miss", args.miss_keep)
            except Exception as e:
                print(f"  ⚠ 本次识别异常({type(e).__name__}: {e}), 跳过, 连接保持(模型常驻不释放)")
                time.sleep(args.mi); continue

            if lock_now:
                state = "locked"; place_tries = 0      # 已确认(report/dup): 锁板, 同板去抖跳过
            else:
                place_tries += 1               # 修B: pending/miss累计重试, 在位期间继续攒确认
                if place_tries >= args.place_retries:
                    state = "locked"     # 重试到顶暂锁, 防空耗NPU(移开/换板经空场重置)
                    print(f"  ⚠ 在位板重试{place_tries}次仍未确认, 暂锁(移开或换板后重试)")
                else:
                    state = "trying"        # 未锁: 下个稳定周期继续重识
            last = frames[-1].copy()
            idle_t = time.time()
            print("\n[继续监控]\n")

    except KeyboardInterrupt:
        print("\n[退出]")
    finally:
        reader.set_cap(None)      # 让线程 release 当前 cap(发 TEARDOWN, 保住下次可拉流)
        reader.wait_released(3.0)
        reader.run = False
        if reader2:               # v2.1 第二路对称清理: 单客户端源必须发 TEARDOWN, 否则背面摄像头下次拉不到流
            reader2.set_cap(None)
            reader2.wait_released(3.0)
            reader2.run = False
        preview_stop()            # V3.0: 停预览编码线程(daemon, 这里等它退出, 不留僵尸)
        print(f"统计: 监控{mon}次 SN{sn_cnt}个")

if __name__ == "__main__":
    main()
