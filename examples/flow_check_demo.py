#!/usr/bin/env python3
"""最低流量核算调用示例（仅依赖标准库）。

用法：
    python3 examples/flow_check_demo.py [base_url]

默认 base_url 为 http://localhost:8000。

示例管网（examples/flow_network.json）：

    S1 --R1[阀]-- C --R3[阀]-- U1
    S2 --R2[阀]-/ | \--R4[阀]-- U2
                  R5[阀]
                    |
                    D --R6(无阀)-- U3

脚本依次演示：
  1. 导入管网（版本 1，两座水源初始均运行）
  2. 全覆盖上限 + 三点需求：可全部满足 -> feasible=true
  3. 收紧出厂干管 R1/R2：总供水受限 -> feasible=false，给出最大可交付总量
  4. 已读版本过期（version=99）：仍按当前状态核算，只置 version_match=false
  5. 停运 S1（确认后版本 2）：S2 经联络点顶供，需求过大时不可全部满足
  6. 关阀隔离故障 R6（关 R5，版本 3）：U3 不可通行，核算结果为 0
  7. 非法请求（缺水源上限、重复管段、用水点不存在）-> 422 FLOW_CHECK_INVALID
  8. 数值/类型错误（负容量、小数、字符串、空需求、未知字段）-> 422
  9. 核算全程只读：阀位、事件、版本均不被核算接口改变
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
    "BASE_URL", "http://localhost:8000"
)
SAMPLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "flow_network.json")


def call(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def show(title: str, status: int, body: dict) -> None:
    print(f"\n=== {title} -> HTTP {status} ===")
    print(json.dumps(body, ensure_ascii=False, indent=2))


def flow_body(version, *, s1=500, s2=500, caps=None, demands=None):
    """构造全覆盖的核算请求（管网固定为 flow_network.json：S1/S2 + R1..R6）。"""
    if caps is None:
        caps = {"R1": 500, "R2": 500, "R3": 300, "R4": 300, "R5": 300, "R6": 300}
    if demands is None:
        demands = {"U1": 200, "U2": 200, "U3": 200}
    return {
        "version": version,
        "sources": [
            {"source_id": "S1", "supply_capacity": s1},
            {"source_id": "S2", "supply_capacity": s2},
        ],
        "pipes": [{"pipe_id": pid, "capacity": cap} for pid, cap in caps.items()],
        "demands": [{"sink_id": sid, "demand": d} for sid, d in demands.items()],
    }


def main() -> None:
    with open(SAMPLE, encoding="utf-8") as f:
        network = json.load(f)

    # 1. 导入管网
    status, body = call("POST", "/api/network/import", network)
    show("1. 导入管网（双水源联网）", status, body)
    assert status == 200 and body["version"] == 1, body

    # 2. 全覆盖上限：三点共需 600，两源各 500、管段均充裕 -> 全部满足
    status, body = call("POST", "/api/dispatch/flow-check", flow_body(1))
    show("2. 最低流量核算（预期需求 600 / 可交付 600 / feasible=true）", status, body)
    assert status == 200 and body["feasible"] is True, body
    assert body["demand_total"] == 600 and body["max_deliverable"] == 600, body
    assert body["version_match"] is True, body

    # 3. 收紧两座水厂出厂干管：R1=200、R2=200，最多送入 C 400
    tight = {"R1": 200, "R2": 200, "R3": 300, "R4": 300, "R5": 300, "R6": 300}
    status, body = call(
        "POST", "/api/dispatch/flow-check", flow_body(1, caps=tight)
    )
    show("3. 出厂干管受限（预期可交付 400 / feasible=false）", status, body)
    assert status == 200 and body["feasible"] is False, body
    assert body["max_deliverable"] == 400, body

    # 4. 过期版本仍按当前状态核算，只标记 version_match=false
    status, body = call(
        "POST", "/api/dispatch/flow-check", flow_body(99, caps=tight)
    )
    show("4. 过期版本核算（version_match=false，结果不变）", status, body)
    assert status == 200 and body["version_match"] is False, body
    assert body["version"] == 1 and body["max_deliverable"] == 400, body

    # 5. 停运 S1 后：仅 S2（500）经 R2 供 C，需求 600 无法全部满足
    status, body = call(
        "POST",
        "/api/sources/switch",
        {"targets": [{"source_id": "S1", "running": False}], "version": 1},
    )
    show("5a. 确认停运 S1（版本 -> 2）", status, body)
    assert status == 200 and body["version"] == 2, body
    status, body = call("POST", "/api/dispatch/flow-check", flow_body(2))
    show("5b. 停运 S1 后核算（预期可交付 500 / feasible=false）", status, body)
    assert body["max_deliverable"] == 500 and body["feasible"] is False, body

    # 6. 隔离故障 R6（关 R5）：U3 所在片区不可通行
    status, body = call(
        "POST",
        "/api/isolation/submit",
        {"fault_pipe_id": "R6", "close_pipe_ids": ["R5"], "version": 2},
    )
    show("6a. 提交隔离（关 R5，版本 -> 3）", status, body)
    assert status == 200 and body["version"] == 3, body
    status, body = call("POST", "/api/dispatch/flow-check", flow_body(3))
    show("6b. 阀位与故障下核算（U3 不可交付，预期 400 / false）", status, body)
    assert body["max_deliverable"] == 400 and body["feasible"] is False, body
    # 只核算 U1/U2（未列需求的 U3 不消耗）：400 需求全部满足
    partial = flow_body(3, demands={"U1": 200, "U2": 200})
    status, body = call("POST", "/api/dispatch/flow-check", partial)
    show("6c. 仅核算 U1/U2（预期 400 / feasible=true）", status, body)
    assert body["max_deliverable"] == 400 and body["feasible"] is True, body

    # 7. 非法请求：缺水源 S2、重复管段 R3、用水点不存在
    bad = flow_body(3)
    bad["sources"] = bad["sources"][:1]
    bad["pipes"].append({"pipe_id": "R3", "capacity": 1})
    bad["demands"].append({"sink_id": "UX", "demand": 5})
    status, body = call("POST", "/api/dispatch/flow-check", bad)
    show("7. 结构非法整体拒绝（FLOW_CHECK_INVALID）", status, body)
    assert status == 422 and body["error"]["code"] == "FLOW_CHECK_INVALID", body
    codes = {i["code"] for i in body["error"]["issues"]}
    assert {"FLOW_SOURCE_MISSING", "FLOW_PIPE_DUPLICATE", "FLOW_SINK_NOT_FOUND"} <= codes

    # 8. 类型/数值错误：Pydantic 422（不进入业务核算）
    for label, patched in [
        ("负容量", {"sources": [{"source_id": "S1", "supply_capacity": -1},
                                {"source_id": "S2", "supply_capacity": 500}]}),
        ("小数值", {"pipes": [{"pipe_id": pid, "capacity": 1.5}
                             for pid in ("R1", "R2", "R3", "R4", "R5", "R6")]}),
        ("字符串值", {"demands": [{"sink_id": "U1", "demand": "10"}]}),
        ("空需求", {"demands": []}),
        ("未知字段", {"extra": 1}),
    ]:
        body_in = flow_body(3)
        body_in.update(patched)
        status, out = call("POST", "/api/dispatch/flow-check", body_in)
        print(f"8. {label} -> HTTP {status}")
        assert status == 422, (label, out)

    # 9. 核算全程只读：版本仍为 3，阀位/事件未被核算改变
    status, body = call("GET", "/api/network")
    show("9. 读取管网（核算只读，版本仍为 3、R5 仍关闭）", status, body)
    assert body["version"] == 3, body
    r5 = next(p for p in body["pipes"] if p["id"] == "R5")
    assert r5["valve_closed"] is True, body
    status, events = call("GET", "/api/isolation/events")
    assert status == 200 and len(events) == 1, events

    print("\n全部最低流量核算示例执行完成。")


if __name__ == "__main__":
    main()
