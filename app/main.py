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
from .schemas import CatalogIn, PlanIn, ReplanIn, SeatCapIn


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


def _validate_seat_ref(conn, course_code, semester):
    """校验席位引用的学期格式与课程是否在当前目录中；返回规范化的 (课程, 学期)。

    非法学期 -> INVALID_INPUT(400)；未知当前目录课程 -> COURSE_MISSING(422)。
    """
    course_code = course_code.strip().upper()
    try:
        parse_semester(semester)
    except PlanError as exc:
        raise HTTPException(status_code=400,
                            detail={"code": exc.code, "message": exc.message})
    semester = semester.strip().upper()
    version = db.get_current_version(conn)
    codes = {r["code"] for r in db.load_courses(conn, version["id"])}
    if course_code not in codes:
        raise HTTPException(status_code=422, detail={
            "code": "COURSE_MISSING",
            "message": f"课程缺失：{course_code} 不在当前目录版本（{version['id']}）中"})
    return course_code, semester.strip().upper()


def _seat_out(conn, course_code, semester):
    """单个席位上限视图：cap 为 null 表示不限额，occupied 为已占用，remaining 为余量。"""
    cap = db.get_seat_cap(conn, course_code, semester)
    occupied = db.seat_occupied(conn, course_code, semester)
    return {
        "course_code": course_code,
        "semester": semester,
        "cap": cap,
        "occupied": occupied,
        "remaining": None if cap is None else cap - occupied,
    }


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


# ---------------------------------------------------------------- 席位上限

@app.put("/seat-caps/{course_code}/{semester}")
def set_seat_cap(course_code: str, semester: str, payload: SeatCapIn):
    """按课程代码与学期设置非负席位上限（重复设置为覆盖）；未设置表示不限额。

    未知当前目录课程（422 COURSE_MISSING）、非法学期（400 INVALID_INPUT）拒绝；
    上限低于该 (课程,学期) 当前已占用席位时拒绝（409 CAP_BELOW_OCCUPIED），原上限不变。
    """
    with db.transaction() as conn:
        course_code, semester = _validate_seat_ref(conn, course_code, semester)
        occupied = db.seat_occupied(conn, course_code, semester)
        if payload.cap < occupied:
            raise HTTPException(status_code=409, detail={
                "code": "CAP_BELOW_OCCUPIED",
                "message": f"课程 {course_code} 在 {semester} 已占用 {occupied} 个席位，"
                           f"上限 {payload.cap} 低于已占用数，拒绝设置",
                "course_code": course_code, "semester": semester,
                "cap": payload.cap, "occupied": occupied})
        db.upsert_seat_cap(conn, course_code, semester, payload.cap)
        return _seat_out(conn, course_code, semester)


@app.get("/seat-caps")
def list_seat_caps():
    """列出全部已设置的席位上限（未设置即不限额，不出现在列表中）。"""
    with db.session() as conn:
        return {"seat_caps": db.list_seat_caps(conn)}


@app.get("/seat-caps/{course_code}/{semester}")
def get_seat_cap(course_code: str, semester: str):
    """查询单个 (课程,学期) 的上限、已占用与余量；cap/remaining 为 null 表示不限额。"""
    with db.session() as conn:
        course_code, semester = _validate_seat_ref(conn, course_code, semester)
        return _seat_out(conn, course_code, semester)


# ---------------------------------------------------------------- 规划方案

@app.post("/plans", status_code=201)
def create_plan(payload: PlanIn):
    """生成规划方案并绑定当前目录版本；失败时说明原因，不返回部分课表。

    可选锁定安排（locked：课程+学期）与不可排课学期（unavailable_semesters）：
    锁定课程视为本次必修并须安排在指定学期；不可排课学期不安排任何课程。
    不带这两个字段的请求行为与返回结构同既有版本一致。
    """
    with db.session() as conn:
        version = db.get_current_version(conn)
        catalog = _to_catalog(db.load_courses(conn, version["id"]))
        equiv_groups = db.load_equiv_groups(conn, version["id"])
        # 规划基于“当时余量”：待确认方案不占位，已占用仅来自全部已确认方案
        seat_caps = db.load_seat_caps(conn)
        occupied = db.seat_occupied_map(conn)
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
                seat_caps=seat_caps,
                occupied=occupied,
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


def _placements_from_result(result):
    """方案结果 -> [(实际修读课程代码, 学期标签), ...]。

    互认按实际修读课程占位：方案列出替代课就占替代课名额，不计入原课名额。
    """
    placements = []
    for s in result.get("semesters", []):
        for c in s.get("courses", []):
            placements.append((c["code"], s["semester"]))
    return placements


def _raise_seat_conflict(conflicts):
    raise HTTPException(status_code=409, detail={
        "code": "SEAT_CONFLICT",
        "message": "确认时席位余量已变化，以下课表席位不足，确认被拒绝（原方案与原占位保留）："
                   + "；".join(
                       f"{code}@{semester}（需 {need}，上限 {cap}，已占用 {occ}，余量 {cap - occ}）"
                       for code, semester, cap, occ, need in conflicts),
        "conflicts": [
            {"course_code": code, "semester": semester,
             "cap": cap, "occupied": occ, "requested": need, "remaining": cap - occ}
            for code, semester, cap, occ, need in conflicts
        ],
    })


@app.post("/plans/{plan_id}/confirm")
def confirm_plan(plan_id: str):
    """确认方案或重排草案：单事务内原子复核目录版本与每门课席位并占位。

    - 已确认方案/草案重复确认幂等，直接返回；
    - 目录自方案生成后已修订 -> 409 CATALOG_CHANGED，不写入；
    - 重排草案：同一来源已有生效重排、或草案过期 -> 409 DRAFT_EXPIRED；
      确认时仅将来源方案尚未结束学期的占位替换为新课表，已结束学期占位保留；
    - 余量变化导致席位不足 -> 409 SEAT_CONFLICT，拒绝确认、保留原方案与原占位
      （事务回滚，不出现超卖）。
    """
    with db.transaction() as conn:
        row = db.get_plan(conn, plan_id)
        if row is None:
            raise HTTPException(status_code=404, detail={
                "code": "NOT_FOUND", "message": f"方案 {plan_id} 不存在"})
        if row["status"] == "confirmed":
            return _plan_out(row)  # 幂等：已确认直接返回，不重复占位

        current = db.get_current_version(conn)
        if row["version_id"] != current["id"]:
            raise HTTPException(status_code=409, detail={
                "code": "CATALOG_CHANGED",
                "message": f"目录已修订（当前版本 {current['id']}，方案基于版本 "
                           f"{row['version_id']}），确认被拒绝，请重新规划",
            })

        result = json.loads(row["result_json"])
        placements = _placements_from_result(result)

        if row["source_plan_id"]:
            # ---- 重排草案：过期判定 + 仅替换来源方案未结束学期占位 ----
            source = db.get_plan(conn, row["source_plan_id"])
            if source is None or source["status"] != "confirmed":
                raise HTTPException(status_code=409, detail={
                    "code": "DRAFT_EXPIRED",
                    "message": "来源方案不存在或未处于已确认状态，重排草案已过期，确认被拒绝"})
            if db.count_active_replan(conn, row["source_plan_id"]) > 0:
                raise HTTPException(status_code=409, detail={
                    "code": "DRAFT_EXPIRED",
                    "message": f"来源方案 {row['source_plan_id']} 已有生效的重排结果，"
                               "同一来源只能有一个生效重排，该草案过期，确认被拒绝"})
            replan_meta = json.loads(row["replan_json"])
            elapsed = int(replan_meta["elapsed_semesters"])
            source_request = json.loads(source["request_json"])
            start_year, start_season = parse_semester(source_request["start_semester"])
            # 先删除来源方案未结束学期占位（事务内，失败随回滚恢复）
            db.delete_holds_after_index(conn, source["id"], start_year, start_season, elapsed)

        # ---- 原子复核每门课席位（cap 缺失即不限额）----
        needs = {}
        for code, semester in placements:
            needs[(code, semester)] = needs.get((code, semester), 0) + 1
        occupied = db.seat_occupied_map(conn)
        caps = db.load_seat_caps(conn)
        conflicts = []
        for (code, semester), need in sorted(needs.items()):
            cap = caps.get((code, semester))
            if cap is None:
                continue
            occ = occupied.get((code, semester), 0)
            if occ + need > cap:
                conflicts.append((code, semester, cap, occ, need))
        if conflicts:
            _raise_seat_conflict(conflicts)

        # ---- 占位并生效 ----
        db.insert_holds(conn, plan_id, placements)
        db.set_plan_status(conn, plan_id, "confirmed")
        out = _plan_out(db.get_plan(conn, plan_id))
    return out


@app.post("/plans/{plan_id}/replan", status_code=201)
def replan_plan_route(plan_id: str, payload: ReplanIn):
    """已确认方案执行中途，凭已结束学期数与累计已修课程按当前目录滚动重排。

    结果作为关联原方案的待确认草案（status=draft）保存；原方案及其历史目录不变。
    目录在计算至落库间被修订时，拒绝旧版本结果（409）且不保存。
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
        seat_caps = db.load_seat_caps(conn)
        # 重排余量：来源方案尚未结束学期的占位确认时会被替换，规划时不计入
        start_year, start_season = parse_semester(original_request["start_semester"])
        occupied = db.seat_occupied_map_for_replan(
            conn, plan_id, start_year, start_season, payload.elapsed_semesters)
        try:
            effective_request, result, diff = replan_plan(
                catalog,
                original_request=original_request,
                original_result=original_result,
                elapsed_semesters=payload.elapsed_semesters,
                completed=set(payload.completed_courses),
                equiv_groups=equiv_groups,
                seat_caps=seat_caps,
                occupied=occupied,
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
