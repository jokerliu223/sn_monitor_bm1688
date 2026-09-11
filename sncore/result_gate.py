#!/usr/bin/env python3
"""结果二次确认门(纯逻辑, 可离线单测)。

vote 出的 SN 不立即落库: 需连续多次得到同一 SN, 或单次分数达高阈值且满帧命中,
才判定"确认"。已确认过的同一 SN 不重复落库。目的: 压制单次误报。
"""


class ResultGate:
    def __init__(self, confirm=2, confirm_score=0.95):
        self.confirm = confirm              # 连续同一 SN 达此次数即确认
        self.confirm_score = confirm_score  # 单次达此分且满帧命中即确认
        self.pending_sn = None
        self.pending_count = 0
        self.last_reported = None

    def feed(self, voted):
        """喂入一次 vote 结果(dict 或 None)。

        return: (action, count)
          action: "report"  = 确认, 应落库上报
                  "pending" = 待确认, 暂不落库
                  "dup"     = 已上报过的同一 SN, 不重复落库
                  "none"    = 本次无 SN
        """
        if not voted:
            self.pending_sn = None
            self.pending_count = 0
            return ("none", 0)

        sn = voted["sn"]
        if sn == self.pending_sn:
            self.pending_count += 1
        else:
            self.pending_sn = sn
            self.pending_count = 1

        # 单次强确认: 分数达阈值且所有帧都命中
        strong = (voted.get("score", 0) >= self.confirm_score
                  and voted.get("frames_hit", 0) == voted.get("total_frames", -1))
        confirmed = self.pending_count >= self.confirm or strong

        if not confirmed:
            return ("pending", self.pending_count)
        if sn == self.last_reported:
            return ("dup", self.pending_count)
        self.last_reported = sn
        return ("report", self.pending_count)
