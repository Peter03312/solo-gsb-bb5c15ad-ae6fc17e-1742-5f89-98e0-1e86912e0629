"""高校课程学期规划服务 —— FastAPI 入口。"""
from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import db
from .planner import Course, PlanError, build_plan, parse_semester
from .replan import replan_plan
from .schemas import CapacityIn, CatalogIn, PlanIn, ReplanIn


@asynccontextmanager
async def lifespan(_):
    db.init_db()  # 建表；首次启动写入种子目录（版本 1）
    yield


app = FastAPI(
    title="高校课程学期规划服务",
    version="1.0.0",
    description="基于课程目录版本的学期排课规划：先修组、同修、开课季节、学分上限约束下的最早完成方案。",
    lifespan=lifespan,
)


@app.exception_handler(RequestValidationError)
async def invalid_input_handler(_, exc):
    errors = [
        {"loc": list(e.get("loc", [])), "msg": e.get("msg", ""), "type": e.get("type", "")}
        for e in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={"detail": {"code": "INVALID_INPUT", "message": "非法输入", "errors": errors}},
    )


def _to_catalog(rows):
    return {
        r["code"]: Course(
            code=r["code"], name=r["name"], credits=r["credits"],
            seasons=frozenset(r["semesters"]),
            prereq_groups=tuple(tuple(g) for g in r["prereq_groups"]),
            coreqs=tuple(r["coreqs"]),
        )
        for r in rows
    }


def _plan_out(row):
    out = {
        "plan_id": row["id"],
        "status": row["status"],
        "catalog_version": row["version_id"],
        "request": json.loads(row["request_json"]),
        "result": json.loads(row["result_json"]),
        "created_at": row["created_at"],
    }
    source = row["source_plan_id"] if "source_plan_id" in row.keys() else None
    if source:
        out["source_plan_id"] = source
        out["replan"] = json.loads(row["replan_json"])
    return out


# ---------------------------------------------------------------- 席位容量辅助

def _parse_semester_or_400(label):
    """校验并规范化学期标签；非法时 400。"""
    try:
        year, season = parse_semester(label)
    except PlanError as exc:
        raise HTTPException(status_code=400,
                            detail={"code": exc.code, "message": exc.message})
    return f"{year}-{season}"


def _remaining_seats(conn, freed=()):
    """当前席位余量：{(课程代码, 学期): 剩余席位}；未设置任何限额时返回 None（不约束排课）。

    freed：拟视为可用的占位 [(课程代码, 学期), ...]（如重排时来源方案未结束学期的占位，
    确认草案时将被释放替换）。
    """
    caps = db.list_capacities(conn)
    if not caps:
        return None
    occ = db.get_occupancy(conn)
    for key in freed:
        if occ.get(key):
            occ[key] -= 1
    return {
        (r["course_code"], r["semester"]):
            r["seat_limit"] - occ.get((r["course_code"], r["semester"]), 0)
        for r in caps
    }


def _seat_shortage(conn, needed):
    """复核占位所需席位：返回第一个不足项的说明 dict；全部满足（或无限额）返回 None。

    needed 为方案实际修读课程的 (课程代码, 学期) 列表（互认按实际修读课程占位）。
    """
    caps = {(r["course_code"], r["semester"]): r["seat_limit"] for r in db.list_capacities(conn)}
    if not caps:
        return None
    need = {}
    for key in needed:
        if key in caps:
            need[key] = need.get(key, 0) + 1
    if not need:
        return None
    occ = db.get_occupancy(conn)
    for (course, semester), n in sorted(need.items()):
        limit = caps[(course, semester)]
        used = occ.get((course, semester), 0)
        if used + n > limit:
            return {"course": course, "semester": semester,
                    "seat_limit": limit, "occupied": used}
    return None


def _capacity_item(course, semester, seat_limit, occupied):
    remaining = None if seat_limit is None else seat_limit - occupied
    return {"course": course, "semester": semester, "seat_limit": seat_limit,
            "occupied": occupied, "remaining": remaining}


# ---------------------------------------------------------------- 基础

@app.get("/")
def root():
    return {"service": "高校课程学期规划服务", "docs": "/docs", "health": "/health"}


@app.get("/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------- 课程目录

@app.post("/catalog/versions", status_code=201)
def create_catalog(payload: CatalogIn):
    """目录修订：全量提交课程集合（可选互不重叠的课程互认组），生成新版本并成为当前版本。

    互认组含不存在课程、重复归组或少于两门时拒绝（422），不生成版本。
    """
    courses = [
        {
            "code": c.code, "name": c.name, "credits": c.credits,
            "semesters": c.semesters, "prereq_groups": c.prereq_groups, "coreqs": c.coreqs,
        }
        for c in payload.courses
    ]
    groups = [list(g) for g in payload.equivalence_groups]
    with db.session() as conn:
        vid = db.create_catalog_version(conn, payload.note, courses, groups)
        version = db.get_version(conn, vid)
    return {
        "version_id": vid,
        "note": version["note"],
        "course_count": len(courses),
        "equivalence_groups": groups,
        "created_at": version["created_at"],
    }


@app.get("/catalog/current")
def current_catalog():
    with db.session() as conn:
        version = db.get_current_version(conn)
        courses = db.load_courses(conn, version["id"])
        groups = db.load_equiv_groups(conn, version["id"])
    return {
        "version_id": version["id"],
        "note": version["note"],
        "created_at": version["created_at"],
        "courses": courses,
        "equivalence_groups": groups,
    }


@app.get("/catalog/versions/{version_id}")
def catalog_version(version_id: int):
    """查看历史目录版本（供回看方案所绑定的目录）。"""
    with db.session() as conn:
        version = db.get_version(conn, version_id)
        if version is None:
            raise HTTPException(status_code=404, detail={
                "code": "NOT_FOUND", "message": f"目录版本 {version_id} 不存在"})
        courses = db.load_courses(conn, version_id)
        groups = db.load_equiv_groups(conn, version_id)
    return {
        "version_id": version["id"],
        "note": version["note"],
        "created_at": version["created_at"],
        "courses": courses,
        "equivalence_groups": groups,
    }


# ---------------------------------------------------------------- 席位容量

@app.put("/capacity")
def set_capacity(payload: CapacityIn):
    """按课程代码与学期设置非负席位上限（未设置的课程+学期不限额）。

    课程须存在于当前目录版本；上限不得低于该课程该学期已占用席位。
    """
    semester = _parse_semester_or_400(payload.semester)
    with db.session() as conn:
        conn.execute("BEGIN IMMEDIATE")  # 占用统计与写入须原子
        version = db.get_current_version(conn)
        codes = {r["code"] for r in db.load_courses(conn, version["id"])}
        if payload.course not in codes:
            raise HTTPException(status_code=422, detail={
                "code": "COURSE_MISSING",
                "message": f"课程缺失：{payload.course} 不在当前目录版本中"})
        occupied = db.get_occupancy(conn).get((payload.course, semester), 0)
        if payload.seat_limit < occupied:
            raise HTTPException(status_code=409, detail={
                "code": "CAPACITY_BELOW_OCCUPIED",
                "message": f"课程 {payload.course} 在 {semester} 已占用 {occupied} 席，"
                           f"上限 {payload.seat_limit} 低于已占用席位，设置被拒绝"})
        db.set_capacity(conn, payload.course, semester, payload.seat_limit)
    return _capacity_item(payload.course, semester, payload.seat_limit, occupied)


@app.get("/capacity")
def list_capacity(course: str | None = None, semester: str | None = None):
    """查询席位：返回上限、已占用与余量；未设置的课程+学期上限与余量为 null（不限额）。

    不带参数返回全部已设置项；course/semester 可单独或组合过滤；
    两者同时给出时，即使未设置也返回该组合的单项（seat_limit/remaining 为 null）。
    """
    code = course.strip().upper() if course else None
    label = _parse_semester_or_400(semester) if semester else None
    with db.session() as conn:
        occ = db.get_occupancy(conn)
        caps = db.list_capacities(conn)
        if code and label:
            limit = next((r["seat_limit"] for r in caps
                          if r["course_code"] == code and r["semester"] == label), None)
            return [_capacity_item(code, label, limit, occ.get((code, label), 0))]
        items = []
        for r in caps:
            if code and r["course_code"] != code:
                continue
            if label and r["semester"] != label:
                continue
            used = occ.get((r["course_code"], r["semester"]), 0)
            items.append(_capacity_item(r["course_code"], r["semester"],
                                        r["seat_limit"], used))
    return items


@app.delete("/capacity/{course}/{semester}")
def delete_capacity(course: str, semester: str):
    """删除某课程某学期的限额设置，恢复不限额。"""
    code = course.strip().upper()
    label = _parse_semester_or_400(semester)
    with db.session() as conn:
        if not db.delete_capacity(conn, code, label):
            raise HTTPException(status_code=404, detail={
                "code": "NOT_FOUND",
                "message": f"课程 {code} 在 {label} 未设置席位限额"})
    return {"course": code, "semester": label, "deleted": True}


# ---------------------------------------------------------------- 规划方案

@app.post("/plans", status_code=201)
def create_plan(payload: PlanIn):
    """生成规划方案并绑定当前目录版本；失败时说明原因，不返回部分课表。

    可选锁定安排（locked：课程+学期）与不可排课学期（unavailable_semesters）：
    锁定课程视为本次必修并须安排在指定学期；不可排课学期不安排任何课程。
    不带这两个字段的请求行为与返回结构同既有版本一致。
    排课基于当时席位余量（仅已确认方案占位；待确认方案不占位）。
    """
    with db.session() as conn:
        version = db.get_current_version(conn)
        catalog = _to_catalog(db.load_courses(conn, version["id"]))
        equiv_groups = db.load_equiv_groups(conn, version["id"])
        seats = _remaining_seats(conn)  # 当时余量；待确认方案不占位
        try:
            result = build_plan(
                catalog,
                completed=set(payload.completed_courses),
                targets=payload.target_courses,
                start_semester=payload.start_semester,
                max_credits=payload.max_credits_per_semester,
                num_semesters=payload.num_semesters,
                locked=[(l.course, l.semester) for l in payload.locked],
                unavailable=payload.unavailable_semesters,
                equiv_groups=equiv_groups,
                seats=seats,
            )
        except PlanError as exc:
            status = 400 if exc.code == "INVALID_INPUT" else 422
            raise HTTPException(status_code=status,
                                detail={"code": exc.code, "message": exc.message})
        # 兼容：未提供新字段时，保存的请求回显不含这两个键，与既有格式一致
        request_data = payload.model_dump()
        for key in ("locked", "unavailable_semesters"):
            if not request_data.get(key):
                request_data.pop(key, None)
        plan_id = uuid.uuid4().hex[:12]
        db.insert_plan(conn, plan_id, version["id"], request_data, result)
        row = db.get_plan(conn, plan_id)
    return _plan_out(row)


@app.get("/plans")
def list_plans():
    with db.session() as conn:
        rows = db.list_plans(conn)
    out = []
    for r in rows:
        item = {
            "plan_id": r["id"],
            "status": r["status"],
            "catalog_version": r["version_id"],
            "completion_semester": json.loads(r["result_json"]).get("completion_semester"),
            "created_at": r["created_at"],
        }
        if r["source_plan_id"]:
            item["source_plan_id"] = r["source_plan_id"]
        out.append(item)
    return out


@app.get("/plans/{plan_id}")
def get_plan(plan_id: str):
    """回看方案（含其绑定的目录版本号）。"""
    with db.session() as conn:
        row = db.get_plan(conn, plan_id)
        if row is None:
            raise HTTPException(status_code=404, detail={
                "code": "NOT_FOUND", "message": f"方案 {plan_id} 不存在"})
    return _plan_out(row)


@app.post("/plans/{plan_id}/confirm")
def confirm_plan(plan_id: str):
    """确认方案或重排草案：原子复核目录版本与每门课席位，通过后占位。

    - 目录自方案生成后已修订：409 拒绝且不写入；
    - 席位余量不足（余量变化导致冲突）：409 拒绝，原方案与原占位保持不变，不超卖；
    - 重排草案：仅以来源方案尚未结束学期的占位置换为新课表占位（已结束学期保留）；
      同一来源只能有一个生效的重排结果，过期草案 409 拒绝；重复确认幂等。
    """
    with db.session() as conn:
        conn.execute("BEGIN IMMEDIATE")  # 复核与占位须原子，防止并发超卖
        row = db.get_plan(conn, plan_id)
        if row is None:
            raise HTTPException(status_code=404, detail={
                "code": "NOT_FOUND", "message": f"方案 {plan_id} 不存在"})
        if row["status"] == "confirmed":
            return _plan_out(row)  # 幂等：已确认的方案直接返回
        current = db.get_current_version(conn)
        if row["version_id"] != current["id"]:
            raise HTTPException(status_code=409, detail={
                "code": "CATALOG_CHANGED",
                "message": f"目录已修订（当前版本 {current['id']}，方案基于版本 "
                           f"{row['version_id']}），确认被拒绝，请重新规划",
            })
        source_id = row["source_plan_id"] if "source_plan_id" in row.keys() else None
        if source_id:
            # 同一来源只能有一个生效的重排结果：已有其他草案生效则本草案过期
            other = db.confirmed_replan_of(conn, source_id, exclude_id=plan_id)
            if other is not None:
                raise HTTPException(status_code=409, detail={
                    "code": "REPLAN_EXPIRED",
                    "message": f"来源方案 {source_id} 已有生效的重排结果（草案 {other}），"
                               f"本草案已过期，确认被拒绝",
                })
            # 释放来源方案尚未结束学期的占位（已结束学期的占位保留）
            source = db.get_plan(conn, source_id)
            meta = json.loads(row["replan_json"])
            elapsed = int(meta["elapsed_semesters"])
            source_result = json.loads(source["result_json"])
            future_semesters = [s["semester"]
                                for s in source_result.get("semesters", [])[elapsed:]]
            db.delete_reservations(conn, source_id, future_semesters)
        # 原子复核每门课席位（按实际修读课程），不足则整体拒绝，原占位不变
        result = json.loads(row["result_json"])
        needed = [(c["code"], s["semester"])
                  for s in result.get("semesters", []) for c in s["courses"]]
        shortage = _seat_shortage(conn, needed)
        if shortage is not None:
            raise HTTPException(status_code=409, detail={
                "code": "CAPACITY_FULL",
                "message": f"课程 {shortage['course']} 在 {shortage['semester']} 的席位不足"
                           f"（上限 {shortage['seat_limit']}，已占用 "
                           f"{shortage['occupied']}），确认被拒绝，原方案及占位保持不变",
            })
        db.add_reservations(conn, plan_id, needed)
        db.set_plan_status(conn, plan_id, "confirmed")
        row = db.get_plan(conn, plan_id)
    return _plan_out(row)


@app.post("/plans/{plan_id}/replan", status_code=201)
def replan_plan_route(plan_id: str, payload: ReplanIn):
    """已确认方案执行中途，凭已结束学期数与累计已修课程按当前目录滚动重排。

    结果作为关联原方案的待确认草案（status=draft）保存；原方案及其历史目录不变。
    目录在计算至落库间被修订时，拒绝旧版本结果（409）且不保存。
    排课基于当时席位余量（来源方案尚未结束学期的既有占位视为可用，确认草案时置换）。
    """
    with db.session() as conn:
        row = db.get_plan(conn, plan_id)
        if row is None:
            raise HTTPException(status_code=404, detail={
                "code": "NOT_FOUND", "message": f"方案 {plan_id} 不存在"})
        if row["status"] != "confirmed":
            raise HTTPException(status_code=409, detail={
                "code": "PLAN_NOT_CONFIRMED",
                "message": f"方案 {plan_id} 状态为 {row['status']}，仅已确认方案可滚动重排",
            })

        original_request = json.loads(row["request_json"])
        original_result = json.loads(row["result_json"])

        version = db.get_current_version(conn)
        catalog = _to_catalog(db.load_courses(conn, version["id"]))
        equiv_groups = db.load_equiv_groups(conn, version["id"])
        # 当时余量：来源方案尚未结束学期的既有占位在确认草案时将被释放替换，
        # 重排规划时计入可用余量
        future_semesters = {s["semester"]
                            for s in original_result.get("semesters", [])[payload.elapsed_semesters:]}
        freed = [(c, s) for c, s in db.get_reservations(conn, plan_id)
                 if s in future_semesters]
        seats = _remaining_seats(conn, freed=freed)
        try:
            effective_request, result, diff = replan_plan(
                catalog,
                original_request=original_request,
                original_result=original_result,
                elapsed_semesters=payload.elapsed_semesters,
                completed=set(payload.completed_courses),
                equiv_groups=equiv_groups,
                seats=seats,
            )
        except PlanError as exc:
            status = 400 if exc.code == "INVALID_INPUT" else 422
            raise HTTPException(status_code=status,
                                detail={"code": exc.code, "message": exc.message})

        # 落库前复查：目录在计算期间被修订则拒绝旧版本结果，不保存
        current = db.get_current_version(conn)
        if current["id"] != version["id"]:
            raise HTTPException(status_code=409, detail={
                "code": "CATALOG_CHANGED",
                "message": f"目录在重排期间已修订（计算基于版本 {version['id']}，"
                           f"当前版本 {current['id']}），旧版本结果被拒绝，请重新发起重排",
            })

        draft_id = uuid.uuid4().hex[:12]
        replan_meta = {
            "source_plan_id": plan_id,
            "elapsed_semesters": payload.elapsed_semesters,
            "completed_courses": payload.completed_courses,
            "difference": diff,
        }
        db.insert_draft(conn, draft_id, version["id"], effective_request, result,
                        source_plan_id=plan_id, replan=replan_meta)
        draft = db.get_plan(conn, draft_id)
    return _plan_out(draft)
