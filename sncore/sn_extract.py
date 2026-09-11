#!/usr/bin/env python3
"""SN 提取与多帧投票 —— 纯逻辑, 不依赖 PP-OCR, 可离线单测。

替代旧 extract_sn 的两个致命缺陷:
  1) 旧逻辑按"长度优先"排序 -> 18 位纯数字计数器噪声顶掉 17 位正确 SN;
  2) 无格式校验 -> 时间戳/计数器/型号等被误当 SN。

策略(用户已定): 通用多帧逐字投票 + 格式校验, 不依赖已知 SN 白名单。
  - 格式校验: 长度窗 + 字母数字混合(毙纯数字/纯字母噪声);
  - 子序列合并: 掉字的短串(如掉 B 的 CCW6N...)是完整串(BCCW6N...)的子序列,
    投票归并到最长完整串, 天然修复单/多字掉落;
  - 聚合打分: 出现帧数为主、置信度为辅。
"""
import re


class SNConfig:
    def __init__(self, min_len=15, max_len=20, min_letters=2, min_digits=2,
                 prefixes=None, max_merge_gap=3, merge_score_margin=0.05):
        self.min_len = min_len
        self.max_len = max_len
        self.min_letters = min_letters      # 至少几个字母(毙纯数字计数器)
        self.min_digits = min_digits        # 至少几个数字(毙纯字母词)
        self.prefixes = prefixes            # 可选前缀白名单, 如 ["BC"]; None=不限
        self.max_merge_gap = max_merge_gap  # 子序列合并时允许的最大长度差
        # 防多字: 短串置信度比长串高出此余量时, 不折进长串(避免真SN被"多字垃圾串"吞掉)
        self.merge_score_margin = merge_score_margin


DEFAULT = SNConfig()

# 显式 SN: / S/N: 前缀(最高优先)
_PREFIX_RE = [re.compile(r'S\s*/?\s*N[:\s\-]*([A-Z0-9]{6,20})', re.I),
              re.compile(r'\bSN[:\s\-]+([A-Z0-9]{6,20})', re.I)]
_ALNUM_RUN = re.compile(r'[A-Z0-9]{10,24}')


def _norm(t):
    return t if isinstance(t, str) else (t[0] if t and isinstance(t, (list, tuple)) else str(t))


def validate_sn(s, cfg=DEFAULT):
    """是否符合 SN 结构。"""
    if not s or not (cfg.min_len <= len(s) <= cfg.max_len):
        return False
    if not re.fullmatch(r'[A-Z0-9]+', s):
        return False
    letters = sum(c.isalpha() for c in s)
    digits = sum(c.isdigit() for c in s)
    if letters < cfg.min_letters or digits < cfg.min_digits:
        return False
    if cfg.prefixes and not any(s.startswith(p) for p in cfg.prefixes):
        return False
    return True


def frame_candidates(texts, cfg=DEFAULT):
    """从单帧 OCR 结果里抽出所有合规 SN 候选。

    texts: [(text, score), ...] 或 [text, ...]
    return: [(sn, score, is_prefix_hit)], 已去重取最高分。
    """
    best = {}  # sn -> (score, is_prefix)

    def put(sn, score, is_prefix):
        sn = sn.strip().upper()
        if not validate_sn(sn, cfg):
            return
        old = best.get(sn)
        if old is None or score > old[0]:
            best[sn] = (max(score, old[0]) if old else score, is_prefix or (old[1] if old else False))

    for item in texts:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            t, sc = _norm(item), float(item[1])
        else:
            t, sc = _norm(item), 1.0
        if not isinstance(t, str):
            continue
        up = t.upper()
        # 1) 显式前缀
        hit = False
        for rgx in _PREFIX_RE:
            m = rgx.search(t)
            if m:
                put(m.group(1), sc, True)
                hit = True
        # 2) 整串本身
        put(up.replace(' ', ''), sc, False)
        # 3) 串内的字母数字长子串
        for m in _ALNUM_RUN.findall(up):
            put(m, sc, False)
    return [(sn, v[0], v[1]) for sn, v in best.items()]


def _is_subseq(a, b):
    """a 是否为 b 的子序列(a 由 b 删若干字符得到)。"""
    it = iter(b)
    return all(c in it for c in a)


def vote(frames, cfg=DEFAULT):
    """多帧投票选出最可信 SN。

    frames: [[(text,score),...], ...] 每帧的 OCR 原始结果。
    return: dict(sn, score, frames_hit, total_frames, merged) 或 None。
    """
    n = len(frames)
    agg = {}  # sn -> {frames:set, max_score, sum_score, prefix}
    for fi, texts in enumerate(frames):
        for sn, sc, is_prefix in frame_candidates(texts, cfg):
            a = agg.setdefault(sn, {"frames": set(), "max": 0.0, "sum": 0.0, "prefix": False})
            a["frames"].add(fi)
            a["max"] = max(a["max"], sc)
            a["sum"] += sc
            a["prefix"] = a["prefix"] or is_prefix
    if not agg:
        return None

    # 子序列合并: 短串(掉字)并入更长的完整串
    keys = sorted(agg, key=len, reverse=True)  # 长的在前, 作为"完整串"锚点
    merged_into = {}
    for i, short in enumerate(sorted(agg, key=len)):   # 短的先找归属
        for long in keys:
            if long == short:
                continue
            if len(long) <= len(short):
                continue
            if len(long) - len(short) > cfg.max_merge_gap:
                continue
            # 防多字: 短串置信明显高于长串 -> 视短串为可信完整串, 不折进(可能是多字噪声的)长串
            if agg[long]["max"] + cfg.merge_score_margin < agg[short]["max"]:
                continue
            if _is_subseq(short, long):
                merged_into[short] = long
                break

    final = {}
    for sn, a in agg.items():
        tgt = merged_into.get(sn, sn)
        f = final.setdefault(tgt, {"frames": set(), "max": 0.0, "sum": 0.0, "prefix": False, "merged": []})
        f["frames"] |= a["frames"]
        f["max"] = max(f["max"], a["max"])
        f["sum"] += a["sum"]
        f["prefix"] = f["prefix"] or a["prefix"]
        if sn != tgt:
            f["merged"].append(sn)

    def rank(kv):
        sn, f = kv
        # 前缀命中最高优先; 再看命中帧数; 再看最高分; 再看长度
        return (f["prefix"], len(f["frames"]), f["max"], len(sn))

    sn, f = max(final.items(), key=rank)
    return {
        "sn": sn,
        "score": round(f["max"], 4),
        "frames_hit": len(f["frames"]),
        "total_frames": n,
        "merged": f["merged"],
    }


# 兼容旧接口: 单帧提取一个 SN(给不做多帧的调用方)
def extract_sn(texts, cfg=DEFAULT):
    r = vote([texts], cfg)
    return r["sn"] if r else None
