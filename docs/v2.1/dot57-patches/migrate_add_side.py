#!/usr/bin/env python3
"""v2.1 一次性迁移: 给已存在的 sn_captures 表加 side 列 (SQLite)。

为什么要这个脚本: SQLAlchemy 的 create_all 只“建不存在的表”, 不会给已存在的表加列。
sn-capture 上线时 sn_captures 表已建好, 所以 v2.1 新增的 side 列必须手工 ALTER TABLE。

幂等: 已有 side 列时直接跳过, 可重复运行。
向后兼容: 存量行 side 默认填 'front'(它们都是正面), 与前端“无 side 视作 front”一致。

用法:
    python3 migrate_add_side.py                 # 用默认 DB 路径(见下方 DEFAULT_DB)
    python3 migrate_add_side.py /path/results.db  # 指定 DB

若你能接受清空历史抓拍, 也可以直接删掉 results.db 让后端重启时 create_all 按新模型重建:
    rm <data>/results.db   # 谨慎: 会丢历史抓拍行; 图片文件仍在 sn_captures/ 目录下
"""
import os
import sqlite3
import sys

# .57 产测系统默认 DB 路径; 若不同请改这里或用命令行参数传入
DEFAULT_DB = os.environ.get("SN_CAPTURE_DB", "data/results.db")


def migrate(db_path: str) -> None:
    if not os.path.exists(db_path):
        print(f"[migrate] DB 不存在: {db_path} —— 若后端尚未初始化, 首次启动 create_all 会按新模型直接建含 side 的表, 无需迁移")
        return
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cols = [row[1] for row in cur.execute("PRAGMA table_info(sn_captures)").fetchall()]
        if not cols:
            print("[migrate] 表 sn_captures 不存在 —— 后端首次启动会按新模型建含 side 的表, 无需迁移")
            return
        if "side" in cols:
            print("[migrate] side 列已存在, 跳过 (幂等)")
            return
        # 存量行都是正面 -> 默认 'front'
        cur.execute("ALTER TABLE sn_captures ADD COLUMN side VARCHAR(8) NOT NULL DEFAULT 'front'")
        conn.commit()
        print("[migrate] OK: 已给 sn_captures 加 side 列 (存量行 side=front)")
    finally:
        conn.close()


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
