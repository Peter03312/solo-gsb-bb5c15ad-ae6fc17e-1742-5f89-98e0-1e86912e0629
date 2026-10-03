"""课程席位容量：规划引擎单元测试与 HTTP API 集成测试（独立临时数据库）。"""
import os

import pytest
from fastapi.testclient import TestClient

from app.planner import Course, PlanError, build_plan


def C(code, credits, seasons=("SPRING", "FALL"), groups=(), coreqs=()):
    return Course(code, code, credits, frozenset(seasons),
                  tuple(tuple(g) for g in groups), tuple(coreqs))


def cat(*courses):
    return {c.code: c for c in courses}


def codes(result):
    return [[c["code"] for c in s["courses"]] for s in result["semesters"]]


# ---------------------------------------------------------------- 规划引擎：席位余量

def test_seats_full_semester_is_avoided():
    catalog = cat(C("A", 3))
    r = build_plan(catalog, set(), ["A"], "2026-SPRING", 10, 4,
                   seats={("A", "2026-SPRING"): 0})
    assert codes(r) == [[], ["A"]]
    assert r["completion_semester"] == "2026-FALL"


def test_seats_unset_combinations_are_unlimited():
    catalog = cat(C("A", 3), C("B", 3))
    # 仅限制 B；A 不受限。同完成学期下字典序最小解把课程尽量推后（空学期序列更小）
    r = build_plan(catalog, set(), ["A", "B"], "2026-SPRING", 10, 4,
                   seats={("B", "2026-SPRING"): 0})
    assert codes(r) == [[], ["A", "B"]]


def test_seats_no_solution_reports_capacity_reason():
    catalog = cat(C("A", 3, seasons=("SPRING",)))
    with pytest.raises(PlanError) as e:
        build_plan(catalog, set(), ["A"], "2026-SPRING", 10, 2,
                   seats={("A", "2026-SPRING"): 0})
    assert e.value.code == "NO_SOLUTION"
    assert "席位" in e.value.message and "A" in e.value.message
    # 放宽到 3 个学期后 2027-SPRING 有余量，可排
    r = build_plan(catalog, set(), ["A"], "2026-SPRING", 10, 3,
                   seats={("A", "2026-SPRING"): 0})
    assert codes(r) == [[], [], ["A"]]


def test_seats_none_keeps_legacy_behavior():
    catalog = cat(C("A", 3))
    r = build_plan(catalog, set(), ["A"], "2026-SPRING", 10, 4, seats=None)
    assert codes(r) == [["A"]]


# ---------------------------------------------------------------- HTTP API

@pytest.fixture()
def client(tmp_path):
    os.environ["DB_PATH"] = str(tmp_path / "test.db")
    from app.main import app
    with TestClient(app) as c:
        yield c


def plan_payload(**kw):
    payload = {
        "completed_courses": [],
        "target_courses": ["CS401"],
        "start_semester": "2026-SPRING",
        "max_credits_per_semester": 12,
        "num_semesters": 6,
    }
    payload.update(kw)
    return payload


def _set_capacity(client, course, semester, limit):
    return client.put("/capacity", json={
        "course": course, "semester": semester, "seat_limit": limit})


def _occupied(client, course, semester):
    r = client.get("/capacity", params={"course": course, "semester": semester})
    assert r.status_code == 200
    return r.json()[0]


def _create_plan(client, **kw):
    r = client.post("/plans", json=plan_payload(**kw))
    assert r.status_code == 201, r.text
    return r.json()


def _create_confirmed(client, **kw):
    plan = _create_plan(client, **kw)
    r = client.post(f"/plans/{plan['plan_id']}/confirm")
    assert r.status_code == 200, r.text
    return plan


# ---------------------------------------------------------------- 设置与查询

def test_set_and_query_capacity(client):
    r = _set_capacity(client, "CS301", "2027-SPRING", 30)
    assert r.status_code == 200
    assert r.json() == {"course": "CS301", "semester": "2027-SPRING",
                        "seat_limit": 30, "occupied": 0, "remaining": 30}
    # 列表查询
    items = client.get("/capacity").json()
    assert items == [r.json()]
    # 单点查询：返回上限、已占用与余量
    item = _occupied(client, "CS301", "2027-SPRING")
    assert item["seat_limit"] == 30 and item["occupied"] == 0 and item["remaining"] == 30
    # 未设置表示不限额：上限与余量为 null
    item = _occupied(client, "CS101", "2026-SPRING")
    assert item["seat_limit"] is None and item["remaining"] is None and item["occupied"] == 0
    # 更新上限
    r = _set_capacity(client, "CS301", "2027-SPRING", 10)
    assert r.json()["seat_limit"] == 10 and r.json()["remaining"] == 10
    # 删除后恢复不限额
    r = client.delete("/capacity/CS301/2027-SPRING")
    assert r.status_code == 200 and r.json()["deleted"] is True
    assert client.get("/capacity").json() == []
    assert client.delete("/capacity/CS301/2027-SPRING").status_code == 404


def test_set_capacity_rejects_invalid_input(client):
    # 未知当前目录课程
    r = _set_capacity(client, "NOPE101", "2027-SPRING", 10)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "COURSE_MISSING"
    # 非法学期
    r = _set_capacity(client, "CS301", "2027春", 10)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "INVALID_INPUT"
    # 负上限
    r = _set_capacity(client, "CS301", "2027-SPRING", -1)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INVALID_INPUT"
    # 均未写入
    assert client.get("/capacity").json() == []


def test_set_capacity_below_occupied_rejected(client):
    _set_capacity(client, "CS301", "2027-SPRING", 1)
    _create_confirmed(client)  # 占位 CS301@2027-SPRING
    assert _occupied(client, "CS301", "2027-SPRING")["occupied"] == 1
    r = _set_capacity(client, "CS301", "2027-SPRING", 0)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "CAPACITY_BELOW_OCCUPIED"
    # 等于已占用可以设置
    assert _set_capacity(client, "CS301", "2027-SPRING", 1).status_code == 200


# ---------------------------------------------------------------- 规划与占位

def test_pending_plan_does_not_occupy(client):
    _set_capacity(client, "CS101", "2026-SPRING", 1)
    plan_a = _create_plan(client)
    plan_b = _create_plan(client)  # 待确认不占位，仍可排出相同课表
    assert plan_b["result"]["semesters"][0]["courses"][0]["code"] == "CS101"
    item = _occupied(client, "CS101", "2026-SPRING")
    assert item["occupied"] == 0 and item["remaining"] == 1
    # 确认 A 后占位生效，新规划须避开 2026-SPRING 的 CS101
    client.post(f"/plans/{plan_a['plan_id']}/confirm")
    assert _occupied(client, "CS101", "2026-SPRING")["remaining"] == 0
    plan_c = _create_plan(client)
    # CS101 在 2026-SPRING 无余量：改走 MATH101→CS202 先修路径，2027-FALL 完成
    sems = [[c["code"] for c in s["courses"]] for s in plan_c["result"]["semesters"]]
    assert sems == [["MATH101"], ["CS202", "MATH103"], ["CS301"], ["CS401"]]
    assert plan_c["result"]["completion_semester"] == "2027-FALL"


def test_planning_avoids_full_semester(client):
    _set_capacity(client, "CS301", "2027-SPRING", 0)
    plan = _create_plan(client)
    sems = {s["semester"]: [c["code"] for c in s["courses"]]
            for s in plan["result"]["semesters"]}
    # CS301 在 2027-SPRING 无余量，顺延到 2028-SPRING，完成学期相应推迟
    assert "CS301" not in sems["2027-SPRING"]
    assert sems["2028-SPRING"] == ["CS301"]
    assert plan["result"]["completion_semester"] == "2028-FALL"


def test_capacity_shortage_no_solution_saves_nothing(client):
    for sem in ("2026-SPRING", "2026-FALL", "2027-SPRING",
                "2027-FALL", "2028-SPRING", "2028-FALL"):
        _set_capacity(client, "CS301", sem, 0)
    r = client.post("/plans", json=plan_payload())
    assert r.status_code == 422
    body = r.json()["detail"]
    assert body["code"] == "NO_SOLUTION"
    assert "席位" in body["message"] and "CS301" in body["message"]
    assert client.get("/plans").json() == []  # 不保存部分方案


def test_confirm_occupies_and_prevents_oversell(client):
    _set_capacity(client, "CS301", "2027-SPRING", 1)
    plan_a = _create_plan(client)
    plan_b = _create_plan(client)  # 与 A 使用同一席位
    # 确认 A：占位成功
    r = client.post(f"/plans/{plan_a['plan_id']}/confirm")
    assert r.status_code == 200
    item = _occupied(client, "CS301", "2027-SPRING")
    assert item["occupied"] == 1 and item["remaining"] == 0
    # 确认 B：席位不足，拒绝且不超卖；B 保持 proposed
    r = client.post(f"/plans/{plan_b['plan_id']}/confirm")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "CAPACITY_FULL"
    assert client.get(f"/plans/{plan_b['plan_id']}").json()["status"] == "proposed"
    assert _occupied(client, "CS301", "2027-SPRING")["occupied"] == 1
    # 重复确认 A 幂等
    r = client.post(f"/plans/{plan_a['plan_id']}/confirm")
    assert r.status_code == 200
    assert _occupied(client, "CS301", "2027-SPRING")["occupied"] == 1


def test_confirm_rejected_when_capacity_shrinks(client):
    plan = _create_plan(client)  # CS301 排在 2027-SPRING
    _set_capacity(client, "CS301", "2027-SPRING", 0)  # 余量变化
    r = client.post(f"/plans/{plan['plan_id']}/confirm")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "CAPACITY_FULL"
    # 原方案保持 proposed，无占位写入
    assert client.get(f"/plans/{plan['plan_id']}").json()["status"] == "proposed"
    assert _occupied(client, "CS301", "2027-SPRING")["occupied"] == 0


def test_confirm_without_capacity_settings_unchanged(client):
    plan = _create_plan(client)
    r = client.post(f"/plans/{plan['plan_id']}/confirm")
    assert r.status_code == 200
    assert r.json()["status"] == "confirmed"


# ---------------------------------------------------------------- 重排草案的占位置换

def test_replan_draft_confirm_replaces_future_seats(client):
    plan = _create_confirmed(client)
    # 已确认占位：第 1、2 学期（已结束）与第 3、4 学期（未结束）
    assert _occupied(client, "CS101", "2026-SPRING")["occupied"] == 1
    assert _occupied(client, "CS301", "2027-SPRING")["occupied"] == 1
    # 限额 1：草案沿用 CS301@2027-SPRING 时，来源占位须先释放才能确认（净零变化）
    _set_capacity(client, "CS301", "2027-SPRING", 1)
    r = client.post(f"/plans/{plan['plan_id']}/replan", json={
        "elapsed_semesters": 2,
        "completed_courses": ["CS101", "CS101L", "CS201", "MATH103"],
    })
    assert r.status_code == 201, r.text
    draft = r.json()
    assert [c["code"] for c in draft["result"]["semesters"][0]["courses"]] == ["CS301"]
    # 草案待确认不占位
    assert _occupied(client, "CS301", "2027-SPRING")["occupied"] == 1
    r = client.post(f"/plans/{draft['plan_id']}/confirm")
    assert r.status_code == 200, r.text
    # 未结束学期占位被置换（仍 1 席，不超卖）；已结束学期占位保留
    assert _occupied(client, "CS301", "2027-SPRING")["occupied"] == 1
    assert _occupied(client, "CS401", "2027-FALL")["occupied"] == 1
    assert _occupied(client, "CS101", "2026-SPRING")["occupied"] == 1
    assert _occupied(client, "CS201", "2026-FALL")["occupied"] == 1
    # 原方案保持不变
    orig = client.get(f"/plans/{plan['plan_id']}").json()
    assert orig["status"] == "confirmed"
    assert len(orig["result"]["semesters"]) == 4


def test_replan_draft_confirm_frees_removed_course_seat(client):
    plan = _create_confirmed(client)
    _set_capacity(client, "CS301", "2027-SPRING", 1)
    # 提前修完 CS301：重排后 2027-SPRING 不再需要 CS301，确认后来源占位被释放
    r = client.post(f"/plans/{plan['plan_id']}/replan", json={
        "elapsed_semesters": 2,
        "completed_courses": ["CS101", "CS101L", "CS201", "MATH103", "CS301"],
    })
    assert r.status_code == 201, r.text
    draft = r.json()
    r = client.post(f"/plans/{draft['plan_id']}/confirm")
    assert r.status_code == 200, r.text
    item = _occupied(client, "CS301", "2027-SPRING")
    assert item["occupied"] == 0 and item["remaining"] == 1


def test_only_one_effective_replan_per_source(client):
    plan = _create_confirmed(client)
    payload = {"elapsed_semesters": 2,
               "completed_courses": ["CS101", "CS101L", "CS201", "MATH103"]}
    d1 = client.post(f"/plans/{plan['plan_id']}/replan", json=payload).json()
    d2 = client.post(f"/plans/{plan['plan_id']}/replan", json=payload).json()
    # 先确认 D1 生效
    assert client.post(f"/plans/{d1['plan_id']}/confirm").status_code == 200
    # D2 成为过期草案，拒绝
    r = client.post(f"/plans/{d2['plan_id']}/confirm")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "REPLAN_EXPIRED"
    assert client.get(f"/plans/{d2['plan_id']}").json()["status"] == "draft"
    # 重复确认 D1 幂等
    r = client.post(f"/plans/{d1['plan_id']}/confirm")
    assert r.status_code == 200
    assert r.json()["status"] == "confirmed"


def test_replan_respects_current_capacity(client):
    # 来源方案不持有 CS201@2027-FALL 的占位，该组合余量 0 时重排须绕开
    _set_capacity(client, "CS201", "2027-FALL", 0)
    plan = _create_confirmed(client)
    # 执行 2 学期后仅修完 CS101/CS101L（CS201、MATH103 掉队），剩余 4 学期重排
    r = client.post(f"/plans/{plan['plan_id']}/replan", json={
        "elapsed_semesters": 2,
        "completed_courses": ["CS101", "CS101L"],
    })
    assert r.status_code == 201, r.text
    draft = r.json()
    sems = {s["semester"]: [c["code"] for c in s["courses"]]
            for s in draft["result"]["semesters"]}
    # CS201 无法排在余量 0 的 2027-FALL，提前到 2027-SPRING
    assert sems["2027-SPRING"] == ["CS201"]
    assert "CS201" not in sems["2027-FALL"]
    assert draft["result"]["completion_semester"] == "2028-FALL"
    # 确认草案：来源未结束学期占位（CS301@2027-SPRING、CS401@2027-FALL）被新课表置换
    r = client.post(f"/plans/{draft['plan_id']}/confirm")
    assert r.status_code == 200, r.text
    assert _occupied(client, "CS201", "2027-SPRING")["occupied"] == 1
    assert _occupied(client, "CS301", "2027-SPRING")["occupied"] == 0
    assert _occupied(client, "CS401", "2027-FALL")["occupied"] == 0
    assert _occupied(client, "CS401", "2028-FALL")["occupied"] == 1


# ---------------------------------------------------------------- 互认按实际修读课程占位

def test_equivalence_occupies_actual_course(client):
    current = client.get("/catalog/current").json()
    r = client.post("/catalog/versions", json={
        "note": "互认", "courses": current["courses"],
        "equivalence_groups": [["CS401", "CS402"]]})
    assert r.status_code == 201
    # 目标 CS401，实际择修同组的 CS402（2026-SPRING）
    plan = _create_plan(
        client,
        completed_courses=["CS101", "CS101L", "CS201", "MATH101", "MATH102",
                           "MATH103", "CS301"],
        target_courses=["CS401"])
    sems = [[c["code"] for c in s["courses"]] for s in plan["result"]["semesters"]]
    assert sems == [["CS402"]]
    r = client.post(f"/plans/{plan['plan_id']}/confirm")
    assert r.status_code == 200, r.text
    # 替代课 CS402 占位；原课 CS401 不计入名额
    assert _occupied(client, "CS402", "2026-SPRING")["occupied"] == 1
    assert _occupied(client, "CS401", "2026-SPRING")["occupied"] == 0
    assert _occupied(client, "CS401", "2026-FALL")["occupied"] == 0
