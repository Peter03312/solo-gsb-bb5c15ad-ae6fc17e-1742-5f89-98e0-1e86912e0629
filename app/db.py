"""SQLite 持久化：目录版本与规划方案。"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "app.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS catalog_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS courses (
    version_id INTEGER NOT NULL REFERENCES catalog_versions(id),
    code TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    credits INTEGER NOT NULL,
    seasons TEXT NOT NULL,                   -- 逗号分隔：SPRING,FALL
    prereq_groups TEXT NOT NULL DEFAULT '[]', -- JSON: [[code, ...], ...]
    coreqs TEXT NOT NULL DEFAULT '[]',        -- JSON: [code, ...]
    PRIMARY KEY (version_id, code)
);
CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY,
    version_id INTEGER NOT NULL REFERENCES catalog_versions(id),
    request_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed',  -- proposed / confirmed / draft
    created_at TEXT NOT NULL,
    source_plan_id TEXT REFERENCES plans(id), -- 滚动重排草案关联的原方案；非草案为 NULL
    replan_json TEXT                          -- 草案的重排上下文与差异；非草案为 NULL
);
CREATE TABLE IF NOT EXISTS equiv_groups (
    version_id INTEGER NOT NULL REFERENCES catalog_versions(id),
    group_no INTEGER NOT NULL,                -- 组序号（版本内 1 起）
    code TEXT NOT NULL,                       -- 组内课程代码
    PRIMARY KEY (version_id, code)            -- 一门课程在同一版本内至多归入一组
);
-- 席位上限：按“课程代码 + 学期”设置非负上限；未设置（无行）表示不限额。
-- 上限全局生效、不随目录版本重置；目录中已不存在的课程保留历史行但不可再设置/查询。
CREATE TABLE IF NOT EXISTS seat_caps (
    course_code TEXT NOT NULL,
    semester TEXT NOT NULL,                   -- YYYY-SPRING / YYYY-FALL
    cap INTEGER NOT NULL CHECK (cap >= 0),    -- 非负；0 表示该学期已不可再占
    updated_at TEXT NOT NULL,
    PRIMARY KEY (course_code, semester)
);
-- 席位占位：仅“已确认”的方案/重排结果占位（待确认方案与草案不写行）。
-- 互认按实际修读课程占位：方案结果里上什么课就占哪门课的名额。
CREATE TABLE IF NOT EXISTS seat_holds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES plans(id),
    course_code TEXT NOT NULL,
    semester TEXT NOT NULL,
    UNIQUE(plan_id, course_code, semester)
);
CREATE INDEX IF NOT EXISTS idx_seat_holds_seat ON seat_holds(course_code, semester);
"""

# 既有数据库增量升级（SQLite 不支持 CREATE TABLE IF NOT EXISTS 加列）
MIGRATIONS = (
    "ALTER TABLE plans ADD COLUMN source_plan_id TEXT REFERENCES plans(id)",
    "ALTER TABLE plans ADD COLUMN replan_json TEXT",
)


def db_path():
    return os.environ.get("DB_PATH", DEFAULT_DB_PATH)


def connect():
    conn = sqlite3.connect(db_path(), timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def session():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


@contextmanager
def transaction():
    """立即取写锁的事务：确认时在同一事务内复核目录版本与席位并占位，杜绝超卖。

    异常时整体回滚（包括事务内已删除的旧占位），保证“拒绝确认、保留原占位”。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_db():
    """建表；首次启动时写入种子目录（版本 1）；对既有库执行增量升级。"""
    directory = os.path.dirname(os.path.abspath(db_path()))
    os.makedirs(directory, exist_ok=True)
    with session() as conn:
        conn.executescript(SCHEMA)
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(plans)").fetchall()}
        for stmt in MIGRATIONS:
            column = stmt.split("ADD COLUMN")[1].split()[0]
            if column not in existing:
                conn.execute(stmt)
        n = conn.execute("SELECT COUNT(*) AS n FROM catalog_versions").fetchone()["n"]
        if n == 0:
            from .seed_data import SEED_COURSES
            create_catalog_version(conn, "初始目录", SEED_COURSES)


def create_catalog_version(conn, note, courses, equiv_groups=None):
    """全量提交一套课程（可选互认组），形成新的目录版本，返回版本号。"""
    cur = conn.execute(
        "INSERT INTO catalog_versions (note, created_at) VALUES (?, ?)", (note, _now()))
    vid = cur.lastrowid
    for c in courses:
        conn.execute(
            "INSERT INTO courses (version_id, code, name, credits, seasons, prereq_groups, coreqs)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                vid, c["code"], c.get("name", ""), c["credits"],
                ",".join(c["semesters"]),
                json.dumps(c.get("prereq_groups", []), ensure_ascii=False),
                json.dumps(c.get("coreqs", []), ensure_ascii=False),
            ),
        )
    for group_no, group in enumerate(equiv_groups or [], start=1):
        for code in group:
            conn.execute(
                "INSERT INTO equiv_groups (version_id, group_no, code) VALUES (?,?,?)",
                (vid, group_no, code),
            )
    return vid


def get_current_version(conn):
    return conn.execute("SELECT * FROM catalog_versions ORDER BY id DESC LIMIT 1").fetchone()


def get_version(conn, version_id):
    return conn.execute("SELECT * FROM catalog_versions WHERE id=?", (version_id,)).fetchone()


def load_courses(conn, version_id):
    rows = conn.execute(
        "SELECT * FROM courses WHERE version_id=? ORDER BY code", (version_id,)).fetchall()
    return [
        {
            "code": r["code"],
            "name": r["name"],
            "credits": r["credits"],
            "semesters": r["seasons"].split(","),
            "prereq_groups": json.loads(r["prereq_groups"]),
            "coreqs": json.loads(r["coreqs"]),
        }
        for r in rows
    ]


def load_equiv_groups(conn, version_id):
    """该目录版本声明的课程互认组；未声明的版本返回空列表。"""
    rows = conn.execute(
        "SELECT group_no, code FROM equiv_groups WHERE version_id=?"
        " ORDER BY group_no, code", (version_id,)).fetchall()
    groups = {}
    for r in rows:
        groups.setdefault(r["group_no"], []).append(r["code"])
    return [groups[k] for k in sorted(groups)]


def insert_plan(conn, plan_id, version_id, request, result):
    conn.execute(
        "INSERT INTO plans (id, version_id, request_json, result_json, status, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (plan_id, version_id, json.dumps(request, ensure_ascii=False),
         json.dumps(result, ensure_ascii=False), "proposed", _now()),
    )


def insert_draft(conn, plan_id, version_id, request, result, source_plan_id, replan):
    """滚动重排草案：关联原方案并保存重排上下文与差异，状态为 draft。"""
    conn.execute(
        "INSERT INTO plans (id, version_id, request_json, result_json, status, created_at,"
        " source_plan_id, replan_json) VALUES (?,?,?,?,?,?,?,?)",
        (plan_id, version_id, json.dumps(request, ensure_ascii=False),
         json.dumps(result, ensure_ascii=False), "draft", _now(),
         source_plan_id, json.dumps(replan, ensure_ascii=False)),
    )


def get_plan(conn, plan_id):
    return conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()


def list_plans(conn):
    return conn.execute("SELECT * FROM plans ORDER BY created_at DESC, id DESC").fetchall()


def set_plan_status(conn, plan_id, status):
    conn.execute("UPDATE plans SET status=? WHERE id=?", (status, plan_id))


# ---------------------------------------------------------------- 席位上限

def upsert_seat_cap(conn, course_code, semester, cap):
    """设置（或覆盖）某门课某学期的非负席位上限。"""
    conn.execute(
        "INSERT INTO seat_caps (course_code, semester, cap, updated_at) VALUES (?,?,?,?)"
        " ON CONFLICT(course_code, semester) DO UPDATE SET cap=excluded.cap,"
        " updated_at=excluded.updated_at",
        (course_code, semester, cap, _now()),
    )


def get_seat_cap(conn, course_code, semester):
    """返回上限整数；未设置（不限额）返回 None。"""
    row = conn.execute(
        "SELECT cap FROM seat_caps WHERE course_code=? AND semester=?",
        (course_code, semester)).fetchone()
    return None if row is None else row["cap"]


def list_seat_caps(conn):
    """全部已设置的席位上限。"""
    rows = conn.execute(
        "SELECT course_code, semester, cap FROM seat_caps"
        " ORDER BY semester, course_code").fetchall()
    return [{"course_code": r["course_code"], "semester": r["semester"], "cap": r["cap"]}
            for r in rows]


def load_seat_caps(conn):
    """规划使用：{(课程代码, 学期标签): 上限}，仅含已设置行（缺省即不限额）。"""
    return {(r["course_code"], r["semester"]): r["cap"]
            for r in conn.execute("SELECT course_code, semester, cap FROM seat_caps")}


# ---------------------------------------------------------------- 席位占位

# 学期序号换算辅助（顺序须与 planner.SEASONS 一致；放在这里避免与 planner 循环依赖）
_SEASON_ORDER = {"SPRING": 0, "FALL": 1}


def seat_occupied(conn, course_code, semester):
    """某门课某学期当前已占用席位数（来自全部已确认方案的实际修读记录）。"""
    return conn.execute(
        "SELECT COUNT(*) AS n FROM seat_holds WHERE course_code=? AND semester=?",
        (course_code, semester)).fetchone()["n"]


def seat_occupied_map(conn):
    """确认/规划使用：{(课程代码, 学期标签): 已占用数}。"""
    return {(r["course_code"], r["semester"]): r["n"]
            for r in conn.execute(
                "SELECT course_code, semester, COUNT(*) AS n FROM seat_holds"
                " GROUP BY course_code, semester")}


def seat_occupied_map_for_replan(conn, source_plan_id, start_year, start_season, elapsed):
    """滚动重排规划时使用的占用图：剔除来源方案“尚未结束学期”（序号 > elapsed）的占位。

    这些占位在重排确认时会被新课表替换，规划阶段不应与本方案自身的旧占位冲突；
    已结束学期占位（序号 <= elapsed）与其他方案占位仍计入余量。
    """
    rows = conn.execute("SELECT course_code, semester, plan_id FROM seat_holds").fetchall()
    counts = {}
    for r in rows:
        if r["plan_id"] == source_plan_id:
            year, season_text = r["semester"].split("-")
            idx = ((int(year) - start_year) * 2
                   + (_SEASON_ORDER[season_text] - _SEASON_ORDER[start_season]) + 1)
            if idx > elapsed:
                continue  # 来源方案未来占位：确认时将被替换
        key = (r["course_code"], r["semester"])
        counts[key] = counts.get(key, 0) + 1
    return counts


def insert_holds(conn, plan_id, placements):
    """按实际修读课程写入占位。placements: 可迭代 (课程代码, 学期标签)。"""
    conn.executemany(
        "INSERT INTO seat_holds (plan_id, course_code, semester) VALUES (?,?,?)",
        [(plan_id, code, semester) for code, semester in placements],
    )


def delete_holds_after_index(conn, plan_id, start_year, start_season, after_index):
    """删除方案“第 after_index 个学期之后”（学期序号 > after_index）的全部占位。

    重排确认时仅替换来源方案尚未结束学期的占位；已结束学期（序号 <= after_index）
    的占位保留。序号由方案的起始学期换算。
    """
    rows = conn.execute(
        "SELECT id, course_code, semester FROM seat_holds WHERE plan_id=?",
        (plan_id,)).fetchall()
    delete_ids = []
    for r in rows:
        year, season_text = r["semester"].split("-")
        idx = ((int(year) - start_year) * 2
               + (_SEASON_ORDER[season_text] - _SEASON_ORDER[start_season]) + 1)
        if idx > after_index:
            delete_ids.append(r["id"])
    if delete_ids:
        conn.executemany("DELETE FROM seat_holds WHERE id=?",
                         [(hid,) for hid in delete_ids])
    return len(rows) - len(delete_ids), len(delete_ids)  # (保留数, 删除数)


def count_active_replan(conn, source_plan_id):
    """同一来源方案下已生效（已确认）的重排结果数量——至多一个。"""
    return conn.execute(
        "SELECT COUNT(*) AS n FROM plans WHERE source_plan_id=? AND status='confirmed'",
        (source_plan_id,)).fetchone()["n"]
