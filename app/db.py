"""SQLite 持久化：目录版本、规划方案与课程席位容量。"""
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
CREATE TABLE IF NOT EXISTS capacities (
    course_code TEXT NOT NULL,                -- 课程代码（须存在于当前目录版本）
    semester TEXT NOT NULL,                   -- 学期标签：YYYY-SPRING / YYYY-FALL
    seat_limit INTEGER NOT NULL CHECK (seat_limit >= 0),  -- 非负席位上限；无记录表示不限额
    PRIMARY KEY (course_code, semester)
);
CREATE TABLE IF NOT EXISTS seat_reservations (
    plan_id TEXT NOT NULL REFERENCES plans(id),  -- 仅已确认方案/草案产生占位
    course_code TEXT NOT NULL,                   -- 实际修读课程（互认替代按实际课程占位）
    semester TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (plan_id, course_code, semester)
);
"""

# 既有数据库增量升级（SQLite 不支持 CREATE TABLE IF NOT EXISTS 加列）
MIGRATIONS = (
    "ALTER TABLE plans ADD COLUMN source_plan_id TEXT REFERENCES plans(id)",
    "ALTER TABLE plans ADD COLUMN replan_json TEXT",
)


def db_path():
    return os.environ.get("DB_PATH", DEFAULT_DB_PATH)


def connect():
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    # 写事务等待锁而非立即报“database is locked”，配合确认路径的 BEGIN IMMEDIATE
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def session():
    conn = connect()
    try:
        yield conn
        conn.commit()
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


# ---------------------------------------------------------------- 席位容量与占位

def set_capacity(conn, course_code, semester, seat_limit):
    """设置（或更新）某课程在某学期的非负席位上限。"""
    conn.execute(
        "INSERT INTO capacities (course_code, semester, seat_limit) VALUES (?,?,?)"
        " ON CONFLICT (course_code, semester) DO UPDATE SET seat_limit=excluded.seat_limit",
        (course_code, semester, seat_limit),
    )


def delete_capacity(conn, course_code, semester):
    """删除限额设置（恢复不限额）；返回是否有记录被删除。"""
    cur = conn.execute(
        "DELETE FROM capacities WHERE course_code=? AND semester=?", (course_code, semester))
    return cur.rowcount > 0


def list_capacities(conn):
    """全部已设置的限额，按课程代码、学期排序。"""
    return conn.execute(
        "SELECT course_code, semester, seat_limit FROM capacities"
        " ORDER BY course_code, semester").fetchall()


def get_occupancy(conn):
    """当前已确认占位统计：{(课程代码, 学期): 已占用席位数}。"""
    rows = conn.execute(
        "SELECT course_code, semester, COUNT(*) AS n FROM seat_reservations"
        " GROUP BY course_code, semester").fetchall()
    return {(r["course_code"], r["semester"]): r["n"] for r in rows}


def add_reservations(conn, plan_id, items):
    """为已确认方案/草案写入占位；items 为 (课程代码, 学期) 列表。"""
    conn.executemany(
        "INSERT INTO seat_reservations (plan_id, course_code, semester, created_at)"
        " VALUES (?,?,?,?)",
        [(plan_id, c, s, _now()) for c, s in items],
    )


def get_reservations(conn, plan_id):
    """某方案当前的全部占位，返回 (课程代码, 学期) 列表。"""
    rows = conn.execute(
        "SELECT course_code, semester FROM seat_reservations WHERE plan_id=?",
        (plan_id,)).fetchall()
    return [(r["course_code"], r["semester"]) for r in rows]


def delete_reservations(conn, plan_id, semesters):
    """删除某方案在指定学期集合内的占位（重排草案确认时释放来源方案未结束学期的占位）。"""
    if not semesters:
        return
    conn.executemany(
        "DELETE FROM seat_reservations WHERE plan_id=? AND semester=?",
        [(plan_id, s) for s in semesters],
    )


def confirmed_replan_of(conn, source_plan_id, exclude_id=None):
    """来源方案已生效（已确认）的重排草案；不存在返回 None。"""
    row = conn.execute(
        "SELECT id FROM plans WHERE source_plan_id=? AND status='confirmed' AND id!=?",
        (source_plan_id, exclude_id or "")).fetchone()
    return row["id"] if row else None
