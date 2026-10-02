"""故障隔离核心逻辑的单元测试（仅依赖标准库）。

运行：python3 -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.planning import (  # noqa: E402
    Node,
    Pipe,
    diff_states,
    evaluate_plan,
    evaluate_restore,
    evaluate_source_switch,
    freeze_state,
    reachable_sink_ids,
    recommend_plan,
    validate_import,
    DUPLICATE_NODE_ID,
    DUPLICATE_PIPE_ID,
    PIPE_SELF_LOOP,
    PIPE_ENDPOINT_UNKNOWN,
    NON_VALVE_PIPE_CLOSED,
    FAULT_PIPE_NOT_FOUND,
    CLOSE_PIPE_NOT_FOUND,
    CLOSE_PIPE_NO_VALVE,
    CLOSE_PIPE_DUPLICATE,
    CLOSE_PIPE_IS_FAULT,
    FAULT_REWATER_RISK,
    NO_FEASIBLE_PLAN,
    SOURCE_NOT_FOUND,
    SOURCE_NOT_SWITCHABLE,
    SOURCE_TARGET_DUPLICATE,
    FLOW_SOURCE_NOT_FOUND,
    FLOW_SOURCE_NOT_SOURCE,
    FLOW_SOURCE_DUPLICATE,
    FLOW_SOURCE_MISSING,
    FLOW_PIPE_NOT_FOUND,
    FLOW_PIPE_DUPLICATE,
    FLOW_PIPE_MISSING,
    FLOW_SINK_NOT_FOUND,
    FLOW_SINK_NOT_SINK,
    FLOW_SINK_DUPLICATE,
    FLOW_NO_DEMAND,
    evaluate_flow_check,
)


def build_demo():
    """演示管网：

    S --P1(v)-- A --P2(v)-- B --P4(v)-- D --P6(无阀)-- U3
                 |           |
               P3(v)       P5(v)
                 |           |
                 U1          U2
    故障管段 P6（B? 不：D-U3）。
    """
    nodes = {
        "S": Node("S", "source"),
        "A": Node("A", "junction"),
        "B": Node("B", "junction"),
        "D": Node("D", "junction"),
        "U1": Node("U1", "sink"),
        "U2": Node("U2", "sink"),
        "U3": Node("U3", "sink"),
    }
    pipes = {
        "P1": Pipe("P1", "S", "A", has_valve=True),
        "P2": Pipe("P2", "A", "B", has_valve=True),
        "P3": Pipe("P3", "A", "U1", has_valve=True),
        "P4": Pipe("P4", "B", "D", has_valve=True),
        "P5": Pipe("P5", "B", "U2", has_valve=True),
        "P6": Pipe("P6", "D", "U3", has_valve=False),
    }
    return nodes, pipes


class ImportValidationTests(unittest.TestCase):
    def test_valid_import(self):
        nodes, pipes = build_demo()
        self.assertEqual(validate_import(list(nodes.values()), list(pipes.values())), [])

    def test_dangling_endpoint(self):
        nodes = [Node("S", "source")]
        pipes = [Pipe("P1", "S", "X")]
        issues = validate_import(nodes, pipes)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].code, PIPE_ENDPOINT_UNKNOWN)

    def test_self_loop(self):
        nodes = [Node("A", "junction")]
        pipes = [Pipe("P1", "A", "A")]
        issues = validate_import(nodes, pipes)
        self.assertTrue(any(i.code == PIPE_SELF_LOOP for i in issues))

    def test_duplicate_ids(self):
        nodes = [Node("A", "junction"), Node("A", "sink")]
        pipes = [
            Pipe("P1", "A", "A"),  # 自环也会报
            Pipe("P1", "A", "A"),
        ]
        issues = validate_import(nodes, pipes)
        codes = [i.code for i in issues]
        self.assertIn(DUPLICATE_NODE_ID, codes)
        self.assertIn(DUPLICATE_PIPE_ID, codes)

    def test_non_valve_closed_rejected(self):
        nodes = [Node("S", "source"), Node("U", "sink")]
        pipes = [Pipe("P1", "S", "U", has_valve=False, valve_closed=True)]
        issues = validate_import(nodes, pipes)
        self.assertTrue(any(i.code == NON_VALVE_PIPE_CLOSED for i in issues))


class IsolationPlanTests(unittest.TestCase):
    def test_isolatable_plan_with_one_closed_valve(self):
        nodes, pipes = build_demo()
        result = evaluate_plan(nodes, pipes, "P6", ["P4"])
        self.assertTrue(result.isolatable)
        self.assertEqual(result.affected_sink_ids, ["U3"])
        self.assertEqual(result.issues, [])

    def test_non_isolatable_plan(self):
        nodes, pipes = build_demo()
        # 只关 P5（B-U2）：D 仍可经 P4 从水源到达，故障管段隔离失败
        result = evaluate_plan(nodes, pipes, "P6", ["P5"])
        self.assertFalse(result.isolatable)
        # U2 因关阀失供，U3 因故障管段移除而失供，均按“前可达后不可达”统计
        self.assertEqual(result.affected_sink_ids, ["U2", "U3"])

    def test_already_closed_valve_keeps_effect_preview(self):
        nodes, pipes = build_demo()
        pipes["P4"] = Pipe("P4", "B", "D", has_valve=True, valve_closed=True)
        # 预演时无需重复关闭，已关阀门继续生效
        result = evaluate_plan(nodes, pipes, "P6", [])
        self.assertTrue(result.isolatable)
        # P4 此前已关闭，U3 在方案前就已失供，不计入本次新增失供用水点
        self.assertEqual(result.affected_sink_ids, [])

    def test_submit_rejects_closing_already_closed_valve(self):
        nodes, pipes = build_demo()
        pipes["P4"] = Pipe("P4", "B", "D", has_valve=True, valve_closed=True)
        result = evaluate_plan(nodes, pipes, "P6", ["P4"], for_submit=True)
        self.assertFalse(result.isolatable)
        self.assertTrue(any(i.code == CLOSE_PIPE_DUPLICATE for i in result.issues))

    def test_fault_pipe_not_found(self):
        nodes, pipes = build_demo()
        result = evaluate_plan(nodes, pipes, "PX", ["P4"])
        self.assertFalse(result.isolatable)
        self.assertEqual(result.issues[0].code, FAULT_PIPE_NOT_FOUND)

    def test_close_pipe_not_found(self):
        nodes, pipes = build_demo()
        result = evaluate_plan(nodes, pipes, "P6", ["PX"])
        self.assertFalse(result.isolatable)
        self.assertTrue(any(i.code == CLOSE_PIPE_NOT_FOUND for i in result.issues))

    def test_close_pipe_without_valve(self):
        nodes, pipes = build_demo()
        # P6 是故障管段，单独构造一个无阀且非故障的管段来测试
        nodes["E"] = Node("E", "junction")
        pipes["P7"] = Pipe("P7", "B", "E", has_valve=False)
        result = evaluate_plan(nodes, pipes, "P6", ["P7"])
        self.assertFalse(result.isolatable)
        self.assertTrue(any(i.code == CLOSE_PIPE_NO_VALVE for i in result.issues))

    def test_close_fault_pipe_itself_rejected(self):
        nodes, pipes = build_demo()
        result = evaluate_plan(nodes, pipes, "P6", ["P6"])
        self.assertFalse(result.isolatable)
        self.assertTrue(any(i.code == CLOSE_PIPE_IS_FAULT for i in result.issues))

    def test_duplicate_close_entries(self):
        nodes, pipes = build_demo()
        result = evaluate_plan(nodes, pipes, "P6", ["P4", "P4"])
        self.assertFalse(result.isolatable)
        dupes = [i for i in result.issues if i.code == CLOSE_PIPE_DUPLICATE]
        self.assertEqual(len(dupes), 1)

    def test_source_endpoint_always_reachable(self):
        # 故障管段直连水源且无额外阀门：水源端恒可达，不能隔离
        nodes, pipes = build_demo()
        result = evaluate_plan(nodes, pipes, "P1", [])
        self.assertFalse(result.isolatable)

    def test_affected_sinks_sorted(self):
        nodes, pipes = build_demo()
        # 关 P3 后移除 P6：U1、U3 失供（U1 字典序在前）
        result = evaluate_plan(nodes, pipes, "P6", ["P3"])
        self.assertEqual(result.affected_sink_ids, ["U1", "U3"])
        self.assertFalse(result.isolatable)

    def test_no_affected_sink_when_fault_is_on_dead_branch_without_sink(self):
        nodes = {
            "S": Node("S", "source"),
            "A": Node("A", "junction"),
            "B": Node("B", "junction"),
            "U1": Node("U1", "sink"),
        }
        pipes = {
            "P1": Pipe("P1", "S", "A", has_valve=True),
            "P2": Pipe("P2", "A", "B", has_valve=True),
            "P3": Pipe("P3", "A", "U1", has_valve=True),
        }
        # 故障 P2（A-B，B 为盲端连接点），关 P2 自身不可（是故障）；
        # 改为故障 P2、无阀门可关 -> 不可隔离；失供用水点为空
        result = evaluate_plan(nodes, pipes, "P2", [])
        self.assertFalse(result.isolatable)
        self.assertEqual(result.affected_sink_ids, [])

    def test_pending_fault_pipes_are_blocked_in_plan(self):
        # 待修复事件的故障管段在方案前后均视为不可通行：
        # 其下游用水点方案前已失供，不计入本次 affected
        nodes, pipes = build_demo()
        # P6（D-U3）已是另一条待修复事件的故障管段
        result = evaluate_plan(
            nodes,
            pipes,
            "P5",                      # 本次故障 B-U2
            ["P5"],
            pending_fault_pipe_ids=frozenset({"P6"}),
        )
        self.assertFalse(result.isolatable)  # B 仍从 P2 进水
        self.assertNotIn("U3", result.affected_sink_ids)


def build_restore_demo():
    """复供演示管网：

    S --P1(v)-- A --P2(v)-- B --P4(v,关)-- D --P6(无阀)-- U3
                 |                    |
               P3(v)                P7(无阀)
                 |                    |
                 U1            P8(v,开) -- E -- U4
    """
    nodes = {
        "S": Node("S", "source"),
        "A": Node("A", "junction"),
        "B": Node("B", "junction"),
        "D": Node("D", "junction"),
        "E": Node("E", "junction"),
        "U1": Node("U1", "sink"),
        "U3": Node("U3", "sink"),
        "U4": Node("U4", "sink"),
    }
    pipes = {
        "P1": Pipe("P1", "S", "A", has_valve=True),
        "P2": Pipe("P2", "A", "B", has_valve=True),
        "P3": Pipe("P3", "A", "U1", has_valve=True),
        "P4": Pipe("P4", "B", "D", has_valve=True, valve_closed=True),
        "P6": Pipe("P6", "D", "U3", has_valve=False),
        "P7": Pipe("P7", "D", "E", has_valve=False),
        "P8": Pipe("P8", "E", "U4", has_valve=True),
    }
    return nodes, pipes


class RestorePlanningTests(unittest.TestCase):
    def test_safe_restore_when_other_faults_stay_isolated(self):
        nodes, pipes = build_restore_demo()
        # 片区内事件（未关阀）复供：重开集合为空，仍关的 P4 保护故障 P6
        result = evaluate_restore(
            nodes, pipes, reopen_pipe_ids=frozenset(),
            other_fault_pipe_ids=frozenset({"P6"}),
        )
        self.assertTrue(result.safe)
        self.assertEqual(result.restored_sink_ids, [])
        self.assertEqual(result.blocks, [])

    def test_unsafe_restore_when_pending_fault_end_gets_water(self):
        nodes, pipes = build_restore_demo()
        # 外层事件（关 P4）复供，而片区内 P7 仍待修复：D 端重新进水
        result = evaluate_restore(
            nodes, pipes, reopen_pipe_ids=frozenset({"P4"}),
            other_fault_pipe_ids=frozenset({"P7"}),
        )
        self.assertFalse(result.safe)
        self.assertEqual(len(result.blocks), 1)
        block = result.blocks[0]
        self.assertEqual(block.fault_pipe_id, "P7")
        self.assertEqual(block.reachable_endpoints, ["D"])
        self.assertEqual(block.as_dict()["code"], FAULT_REWATER_RISK)
        # U3 恢复；U4 因 P7 自身阻断仍不可达
        self.assertEqual(result.restored_sink_ids, ["U3"])

    def test_restore_becomes_safe_after_inner_event_restored(self):
        nodes, pipes = build_restore_demo()
        # 没有其他待修复故障时，重开 P4，U3/U4 同时恢复
        result = evaluate_restore(
            nodes, pipes, reopen_pipe_ids=frozenset({"P4"}),
            other_fault_pipe_ids=frozenset(),
        )
        self.assertTrue(result.safe)
        self.assertEqual(result.restored_sink_ids, ["U3", "U4"])

    def test_other_closed_valves_keep_effect(self):
        # 复供只重开本事件阀门；另一事件关闭的 P2 继续生效
        nodes, pipes = build_restore_demo()
        pipes["P2"] = Pipe("P2", "A", "B", has_valve=True, valve_closed=True)
        result = evaluate_restore(
            nodes, pipes, reopen_pipe_ids=frozenset({"P4"}),
            other_fault_pipe_ids=frozenset(),
        )
        # P2 关闭时 B/D 侧整体仍无水，U3/U4 不会恢复，但也无风险
        self.assertTrue(result.safe)
        self.assertEqual(result.restored_sink_ids, [])


def build_series_demo():
    """串联阀门管网：任一单阀都能隔离故障，用于检验字典序 tie-break。

    S --PB(v)-- J1 --PA(v)-- J2 --PX(无阀,故障)-- U1
    """
    nodes = {
        "S": Node("S", "source"),
        "J1": Node("J1", "junction"),
        "J2": Node("J2", "junction"),
        "U1": Node("U1", "sink"),
    }
    pipes = {
        "PB": Pipe("PB", "S", "J1", has_valve=True),
        "PA": Pipe("PA", "J1", "J2", has_valve=True),
        "PX": Pipe("PX", "J2", "U1", has_valve=False),
    }
    return nodes, pipes


def build_ring_demo(with_branch_sinks=True):
    """环形管网：B 经 P1（A-B）与 P3+P8（A-C-B）双路进水。

    S --P9(v)-- A --P1(v)-- B --PF(无阀,故障)-- D --P5(v)-- U1
                 |          |
                 P3(v)      P2(v)-- U2
                 |
                 C --P8(v)--（回到 B）
    with_branch_sinks=True 时增加 A/C 侧用水点（P7->U3、P4->U4）。
    """
    nodes = {
        "S": Node("S", "source"),
        "A": Node("A", "junction"),
        "B": Node("B", "junction"),
        "C": Node("C", "junction"),
        "D": Node("D", "junction"),
        "U1": Node("U1", "sink"),
        "U2": Node("U2", "sink"),
    }
    pipes = {
        "P9": Pipe("P9", "S", "A", has_valve=True),
        "P1": Pipe("P1", "A", "B", has_valve=True),
        "P3": Pipe("P3", "A", "C", has_valve=True),
        "P8": Pipe("P8", "C", "B", has_valve=True),
        "PF": Pipe("PF", "B", "D", has_valve=False),
        "P5": Pipe("P5", "D", "U1", has_valve=True),
        "P2": Pipe("P2", "B", "U2", has_valve=True),
    }
    if with_branch_sinks:
        nodes["U3"] = Node("U3", "sink")
        nodes["U4"] = Node("U4", "sink")
        pipes["P7"] = Pipe("P7", "A", "U3", has_valve=True)
        pipes["P4"] = Pipe("P4", "C", "U4", has_valve=True)
    return nodes, pipes


class RecommendPlanTests(unittest.TestCase):
    def test_basic_recommendation(self):
        nodes, pipes = build_demo()
        result = recommend_plan(nodes, pipes, "P6")
        self.assertTrue(result.feasible)
        self.assertEqual(result.recommended_close_pipe_ids, ["P4"])
        self.assertEqual(result.affected_sink_ids, ["U3"])
        self.assertIsNone(result.reason)

    def test_fault_pipe_not_found(self):
        nodes, pipes = build_demo()
        result = recommend_plan(nodes, pipes, "PX")
        self.assertFalse(result.feasible)
        self.assertEqual(result.recommended_close_pipe_ids, [])
        self.assertEqual(result.affected_sink_ids, [])
        self.assertEqual(result.reason.code, FAULT_PIPE_NOT_FOUND)

    def test_no_feasible_plan_when_fault_touches_source(self):
        # 故障管段 P1 直连水源：S 端恒可达，关任何阀都无法隔离
        nodes, pipes = build_demo()
        result = recommend_plan(nodes, pipes, "P1")
        self.assertFalse(result.feasible)
        self.assertEqual(result.reason.code, NO_FEASIBLE_PLAN)

    def test_empty_plan_when_fault_already_isolated(self):
        nodes, pipes = build_demo()
        pipes["P4"] = Pipe("P4", "B", "D", has_valve=True, valve_closed=True)
        # P4 已关闭，故障 P6 两端本就不通水：建议空方案（关阀数 0 最优）
        result = recommend_plan(nodes, pipes, "P6")
        self.assertTrue(result.feasible)
        self.assertEqual(result.recommended_close_pipe_ids, [])
        self.assertEqual(result.affected_sink_ids, [])

    def test_closed_valves_and_pending_faults_are_respected(self):
        nodes, pipes = build_demo()
        # P4 已关闭、P6 为其他待修复事件的故障管段
        pipes["P4"] = Pipe("P4", "B", "D", has_valve=True, valve_closed=True)
        result = recommend_plan(
            nodes, pipes, "P5", pending_fault_pipe_ids=frozenset({"P6"})
        )
        # 建议不得包含已关闭的 P4；关 P2 即可隔离 B 侧，仅 U2 新增失供
        self.assertTrue(result.feasible)
        self.assertEqual(result.recommended_close_pipe_ids, ["P2"])
        self.assertEqual(result.affected_sink_ids, ["U2"])

    def test_fault_pipe_itself_never_recommended(self):
        # 故障管段 P5 自带阀门：建议仍须绕开它（提交接口拒绝关故障管段本身）
        nodes, pipes = build_demo()
        result = recommend_plan(nodes, pipes, "P5")
        self.assertTrue(result.feasible)
        self.assertNotIn("P5", result.recommended_close_pipe_ids)
        self.assertEqual(result.recommended_close_pipe_ids, ["P2"])

    def test_lexicographic_tie_break(self):
        # 关 PB 或 PA 都能隔离且失供相同（U1）：取编号字典序最小者 PA
        nodes, pipes = build_series_demo()
        result = recommend_plan(nodes, pipes, "PX")
        self.assertTrue(result.feasible)
        self.assertEqual(result.recommended_close_pipe_ids, ["PA"])
        self.assertEqual(result.affected_sink_ids, ["U1"])

    def test_fewer_affected_sinks_beats_fewer_valves(self):
        # 环形：关干管 P9 只动 1 个阀但 U1-U4 全部失供；
        # 关 P1+P8 两个阀只失供 U1/U2 -> 失供最少优先
        nodes, pipes = build_ring_demo(with_branch_sinks=True)
        result = recommend_plan(nodes, pipes, "PF")
        self.assertTrue(result.feasible)
        self.assertEqual(result.recommended_close_pipe_ids, ["P1", "P8"])
        self.assertEqual(result.affected_sink_ids, ["U1", "U2"])

    def test_fewer_valves_beats_lexicographically_smaller_set(self):
        # 无支线用水点：{P9}（1 阀）与 {P1,P3}/{P1,P8}（2 阀）失供相同，
        # 关阀数最少优先于编号字典序 -> 选 P9
        nodes, pipes = build_ring_demo(with_branch_sinks=False)
        result = recommend_plan(nodes, pipes, "PF")
        self.assertTrue(result.feasible)
        self.assertEqual(result.recommended_close_pipe_ids, ["P9"])
        self.assertEqual(result.affected_sink_ids, ["U1", "U2"])

    def test_recommendation_is_submittable(self):
        # 建议结果直接交给 evaluate_plan(for_submit=True) 复核：可隔离且无阻断问题
        nodes, pipes = build_demo()
        result = recommend_plan(nodes, pipes, "P6")
        check = evaluate_plan(
            nodes, pipes, "P6", result.recommended_close_pipe_ids, for_submit=True
        )
        self.assertTrue(check.isolatable)
        self.assertEqual(check.issues, [])
        self.assertEqual(check.affected_sink_ids, result.affected_sink_ids)


def build_two_source_demo():
    """双水源管网：

    S1 --Q1(v)-- C --Q2(v,故障)-- U1
    S2 --Q3(v)-- C --Q4(v)------ U2
    两座水源初始均运行；C 为公共连接点。
    """
    nodes = {
        "S1": Node("S1", "source"),
        "S2": Node("S2", "source"),
        "C": Node("C", "junction"),
        "U1": Node("U1", "sink"),
        "U2": Node("U2", "sink"),
    }
    pipes = {
        "Q1": Pipe("Q1", "S1", "C", has_valve=True),
        "Q3": Pipe("Q3", "S2", "C", has_valve=True),
        "Q2": Pipe("Q2", "C", "U1", has_valve=True),
        "Q4": Pipe("Q4", "C", "U2", has_valve=True),
    }
    return nodes, pipes


class SourceSwitchPlanningTests(unittest.TestCase):
    def test_stop_one_source_redundant_supply(self):
        # 双源顶供：停运 S1，S2 仍供 C，无用水点失供
        nodes, pipes = build_two_source_demo()
        result = evaluate_source_switch(nodes, pipes, [("S1", False)])
        self.assertTrue(result.changed)
        self.assertTrue(result.safe)
        self.assertEqual(result.affected_sink_ids, [])
        self.assertEqual(result.restored_sink_ids, [])

    def test_stop_all_sources_cuts_every_sink(self):
        # 允许全网无运行水源：全部 sink 新增失供
        nodes, pipes = build_two_source_demo()
        result = evaluate_source_switch(
            nodes, pipes, [("S1", False), ("S2", False)]
        )
        self.assertTrue(result.changed)
        self.assertTrue(result.safe)
        self.assertEqual(result.affected_sink_ids, ["U1", "U2"])

    def test_restore_source_restores_sinks(self):
        # 两源全停后恢复 S1：U1/U2 恢复
        nodes, pipes = build_two_source_demo()
        for sid in ("S1", "S2"):
            nodes[sid] = Node(sid, "source", running=False)
        result = evaluate_source_switch(nodes, pipes, [("S1", True)])
        self.assertTrue(result.changed)
        self.assertTrue(result.safe)
        self.assertEqual(result.restored_sink_ids, ["U1", "U2"])
        self.assertEqual(result.affected_sink_ids, [])

    def test_noop_when_target_states_already_satisfied(self):
        nodes, pipes = build_two_source_demo()
        # 已运行 -> 运行；已停运 -> 停运：changed=False
        nodes["S2"] = Node("S2", "source", running=False)
        result = evaluate_source_switch(
            nodes, pipes, [("S1", True), ("S2", False)]
        )
        self.assertFalse(result.changed)
        self.assertTrue(result.safe)
        self.assertEqual(result.affected_sink_ids, [])
        self.assertEqual(result.restored_sink_ids, [])

    def test_invalid_targets_not_found_non_source_duplicate(self):
        nodes, pipes = build_two_source_demo()
        result = evaluate_source_switch(
            nodes,
            pipes,
            [("SX", False), ("U1", False), ("S1", False), ("S1", True)],
        )
        codes = [i.code for i in result.issues]
        self.assertEqual(
            codes,
            [
                SOURCE_NOT_FOUND,
                SOURCE_NOT_SWITCHABLE,
                SOURCE_TARGET_DUPLICATE,
            ],
        )
        self.assertFalse(result.safe)
        self.assertFalse(result.changed)

    def test_restore_source_rewaters_pending_fault(self):
        # S2 停运、Q1 关闭隔离待修复故障 Q2（C/U1 均不可达）；
        # 恢复 S2 -> Q2 的 C 端重新进水，不安全；U2 恢复（Q2 仍阻断 U1）
        nodes, pipes = build_two_source_demo()
        nodes["S2"] = Node("S2", "source", running=False)
        pipes["Q1"] = Pipe("Q1", "S1", "C", has_valve=True, valve_closed=True)
        result = evaluate_source_switch(
            nodes,
            pipes,
            [("S2", True)],
            pending_fault_pipe_ids=frozenset({"Q2"}),
        )
        self.assertTrue(result.changed)
        self.assertFalse(result.safe)
        self.assertEqual(len(result.blocks), 1)
        block = result.blocks[0]
        self.assertEqual(block.fault_pipe_id, "Q2")
        self.assertEqual(block.reachable_endpoints, ["C"])
        self.assertEqual(block.as_dict()["code"], FAULT_REWATER_RISK)
        self.assertEqual(result.restored_sink_ids, ["U2"])

    def test_stop_source_never_introduces_risk(self):
        # 同一状态下停运 S1：可达范围只收缩，不产生重新进水风险
        nodes, pipes = build_two_source_demo()
        nodes["S2"] = Node("S2", "source", running=False)
        pipes["Q1"] = Pipe("Q1", "S1", "C", has_valve=True, valve_closed=True)
        result = evaluate_source_switch(
            nodes,
            pipes,
            [("S1", False)],
            pending_fault_pipe_ids=frozenset({"Q2"}),
        )
        self.assertTrue(result.changed)
        self.assertTrue(result.safe)
        self.assertEqual(result.blocks, [])

    def test_stopped_source_excluded_from_all_reachability(self):
        # 停运 S1 后：隔离/建议只需切断 S2 一路（关 Q3 即可隔离 C），
        # 且 Q1（停运源侧）不在建议中
        nodes, pipes = build_two_source_demo()
        nodes["S1"] = Node("S1", "source", running=False)
        plan = evaluate_plan(nodes, pipes, "Q2", ["Q3"])
        self.assertTrue(plan.isolatable)
        self.assertEqual(plan.affected_sink_ids, ["U1", "U2"])
        rec = recommend_plan(nodes, pipes, "Q2")
        self.assertTrue(rec.feasible)
        self.assertEqual(rec.recommended_close_pipe_ids, ["Q3"])
        # 两座水源都停运：故障端本就不可达，空方案即可“隔离”，无可达水源
        nodes["S2"] = Node("S2", "source", running=False)
        plan = evaluate_plan(nodes, pipes, "Q2", [])
        self.assertTrue(plan.isolatable)
        self.assertEqual(plan.affected_sink_ids, [])


class SnapshotStateTests(unittest.TestCase):
    def test_reachable_sink_ids_only_running_sources(self):
        nodes, pipes = build_demo()
        self.assertEqual(reachable_sink_ids(nodes, pipes), ["U1", "U2", "U3"])
        # 关闭 P4 与故障 P6 阻断后 U3 不在供水点
        blocked = frozenset({"P4", "P6"})
        self.assertEqual(reachable_sink_ids(nodes, pipes, blocked), ["U1", "U2"])

    def test_reachable_sink_ids_empty_when_all_sources_stopped(self):
        nodes, pipes = build_two_source_demo()
        nodes["S1"] = Node("S1", "source", running=False)
        nodes["S2"] = Node("S2", "source", running=False)
        self.assertEqual(reachable_sink_ids(nodes, pipes), [])

    def test_freeze_state_contains_all_parts(self):
        nodes, pipes = build_demo()
        pipes["P4"] = Pipe("P4", "B", "D", has_valve=True, valve_closed=True)
        state = freeze_state(nodes, pipes, [(7, "P6")])
        self.assertEqual([n["id"] for n in state["nodes"]],
                         ["A", "B", "D", "S", "U1", "U2", "U3"])
        self.assertEqual([p["id"] for p in state["pipes"]],
                         ["P1", "P2", "P3", "P4", "P5", "P6"])
        p4 = next(p for p in state["pipes"] if p["id"] == "P4")
        self.assertTrue(p4["valve_closed"])
        self.assertEqual(
            state["open_faults"], [{"event_id": 7, "fault_pipe_id": "P6"}]
        )
        # P4 关、P6 故障：U3 不供水
        self.assertEqual(state["supplied_sink_ids"], ["U1", "U2"])

    def test_diff_none_base_treats_all_added(self):
        nodes, pipes = build_demo()
        target = freeze_state(nodes, pipes)
        diff = diff_states(None, target)
        self.assertEqual(len(diff["nodes"]["added"]), 7)
        self.assertEqual(diff["nodes"]["removed"], [])
        self.assertEqual(len(diff["pipes"]["added"]), 6)
        self.assertEqual(
            diff["supplied_sinks"]["added"], ["U1", "U2", "U3"]
        )
        self.assertEqual(diff["supplied_sinks"]["removed"], [])
        self.assertEqual(diff["valve_positions"]["changed"], [])
        self.assertEqual(diff["source_states"]["changed"], [])
        self.assertEqual(diff["open_faults"]["added"], [])
        self.assertEqual(diff["open_faults"]["removed"], [])

    def test_diff_valve_fault_and_sink_changes(self):
        nodes, pipes = build_demo()
        before = freeze_state(nodes, pipes, [])
        pipes["P4"] = Pipe("P4", "B", "D", has_valve=True, valve_closed=True)
        after = freeze_state(nodes, pipes, [(1, "P6")])
        diff = diff_states(before, after)
        self.assertEqual(diff["nodes"]["added"], [])
        self.assertEqual(diff["pipes"]["added"], [])
        self.assertEqual(
            diff["valve_positions"]["changed"],
            [{"pipe_id": "P4", "valve_closed_before": False,
              "valve_closed_after": True}],
        )
        self.assertEqual(
            diff["open_faults"]["added"],
            [{"event_id": 1, "fault_pipe_id": "P6"}],
        )
        self.assertEqual(diff["open_faults"]["removed"], [])
        self.assertEqual(diff["supplied_sinks"]["added"], [])
        self.assertEqual(diff["supplied_sinks"]["removed"], ["U3"])

    def test_diff_source_state_changes(self):
        nodes, pipes = build_two_source_demo()
        before = freeze_state(nodes, pipes, [])
        nodes["S1"] = Node("S1", "source", running=False)
        after = freeze_state(nodes, pipes, [])
        diff = diff_states(before, after)
        self.assertEqual(
            diff["source_states"]["changed"],
            [{"source_id": "S1", "running_before": True,
              "running_after": False}],
        )
        # S2 仍经 C 顶供：用水点无净变化
        self.assertEqual(diff["supplied_sinks"]["added"], [])
        self.assertEqual(diff["supplied_sinks"]["removed"], [])

    def test_diff_network_replacement(self):
        nodes, pipes = build_demo()
        before = freeze_state(nodes, pipes, [])
        nodes2, pipes2 = build_two_source_demo()
        after = freeze_state(nodes2, pipes2, [])
        diff = diff_states(before, after)
        self.assertEqual(
            {n["id"] for n in diff["nodes"]["added"]}, {"S1", "S2", "C"}
        )
        self.assertEqual(
            {n["id"] for n in diff["nodes"]["removed"]},
            {"S", "A", "B", "D", "U3"},
        )
        self.assertEqual(
            {p["id"] for p in diff["pipes"]["added"]}, {"Q1", "Q2", "Q3", "Q4"}
        )
        self.assertEqual(
            {p["id"] for p in diff["pipes"]["removed"]},
            {"P1", "P2", "P3", "P4", "P5", "P6"},
        )

    def test_diff_is_symmetric_for_no_change(self):
        nodes, pipes = build_demo()
        state = freeze_state(nodes, pipes, [(1, "P1")])
        diff = diff_states(state, state)
        for section in ("nodes", "pipes", "open_faults", "supplied_sinks"):
            self.assertEqual(diff[section]["added"], [])
            self.assertEqual(diff[section]["removed"], [])
        self.assertEqual(diff["valve_positions"]["changed"], [])
        self.assertEqual(diff["source_states"]["changed"], [])


class FlowCheckTests(unittest.TestCase):
    """最低流量核算（整数最大流）。"""

    def _caps(self, nodes, pipes, source_cap=1000, pipe_cap=1000):
        sources = [(nid, source_cap) for nid, n in nodes.items() if n.type == "source"]
        plist = [(pid, pipe_cap) for pid in pipes]
        return sources, plist

    def test_single_path_capacity_bottleneck(self):
        # S --P1(100)-- A --P2(60)-- U1：需求 50 满足、70 不满足（上限 60）
        nodes = {
            "S": Node("S", "source"),
            "A": Node("A", "junction"),
            "U1": Node("U1", "sink"),
        }
        pipes = {"P1": Pipe("P1", "S", "A"), "P2": Pipe("P2", "A", "U1")}
        src, pc = [("S", 100)], [("P1", 100), ("P2", 60)]

        r = evaluate_flow_check(nodes, pipes, src, pc, [("U1", 50)])
        self.assertEqual(r.issues, [])
        self.assertEqual((r.demand_total, r.max_deliverable, r.feasible), (50, 50, True))

        r = evaluate_flow_check(nodes, pipes, src, pc, [("U1", 70)])
        self.assertEqual((r.demand_total, r.max_deliverable, r.feasible), (70, 60, False))

    def test_source_capacity_bottleneck(self):
        nodes = {
            "S": Node("S", "source"),
            "U1": Node("U1", "sink"),
        }
        pipes = {"P1": Pipe("P1", "S", "U1")}
        r = evaluate_flow_check(nodes, pipes, [("S", 30)], [("P1", 100)], [("U1", 40)])
        self.assertEqual(r.max_deliverable, 30)
        self.assertFalse(r.feasible)

    def test_stopped_source_supplies_nothing(self):
        nodes = {
            "S": Node("S", "source", running=False),
            "U1": Node("U1", "sink"),
        }
        pipes = {"P1": Pipe("P1", "S", "U1")}
        r = evaluate_flow_check(nodes, pipes, [("S", 100)], [("P1", 100)], [("U1", 1)])
        self.assertEqual(r.max_deliverable, 0)
        self.assertFalse(r.feasible)

    def test_closed_valve_and_pending_fault_block_flow(self):
        # 示例管网：P4 已关（或 P6 待修复）时 U3 完全不可交付
        nodes, pipes = build_demo()
        src, pc = self._caps(nodes, pipes)
        demands = [("U1", 10), ("U2", 10), ("U3", 10)]

        r = evaluate_flow_check(
            nodes, pipes, src, pc, demands, blocked_pipe_ids=frozenset({"P4"})
        )
        self.assertEqual(r.max_deliverable, 20)
        self.assertFalse(r.feasible)

        r = evaluate_flow_check(
            nodes, pipes, src, pc, demands, blocked_pipe_ids=frozenset({"P6"})
        )
        self.assertEqual(r.max_deliverable, 20)
        self.assertFalse(r.feasible)

    def test_two_sources_redundant_supply(self):
        # 双源经联络点 C 顶供：任一源单独可满足两用水点
        nodes, pipes = build_two_source_demo()
        r = evaluate_flow_check(
            nodes,
            pipes,
            [("S1", 100), ("S2", 100)],
            [("Q1", 100), ("Q2", 100), ("Q3", 100), ("Q4", 100)],
            [("U1", 80), ("U2", 80)],
        )
        self.assertEqual(r.max_deliverable, 160)
        self.assertTrue(r.feasible)

        # 停运 S1 后仅 S2 经 Q3 进水，Q3 上限 50 卡住
        nodes["S1"] = Node("S1", "source", running=False)
        r = evaluate_flow_check(
            nodes,
            pipes,
            [("S1", 100), ("S2", 100)],
            [("Q1", 100), ("Q2", 100), ("Q3", 50), ("Q4", 100)],
            [("U1", 80), ("U2", 80)],
        )
        self.assertEqual(r.max_deliverable, 50)
        self.assertFalse(r.feasible)

    def test_shared_undirected_pipe_capacity_counts_both_ways(self):
        # S1--A, S2--B，A-B 联络管容量 10：S2 最多只能借 10 给 A 侧的 U1
        nodes = {
            "S1": Node("S1", "source"),
            "S2": Node("S2", "source"),
            "A": Node("A", "junction"),
            "B": Node("B", "junction"),
            "U1": Node("U1", "sink"),
        }
        pipes = {
            "E1": Pipe("E1", "S1", "A"),
            "E2": Pipe("E2", "S2", "B"),
            "E3": Pipe("E3", "A", "B"),
            "E4": Pipe("E4", "A", "U1"),
        }
        src = [("S1", 100), ("S2", 100)]
        pc = [("E1", 100), ("E2", 100), ("E3", 10), ("E4", 1000)]
        r = evaluate_flow_check(nodes, pipes, src, pc, [("U1", 120)])
        self.assertEqual(r.max_deliverable, 110)
        self.assertFalse(r.feasible)
        r = evaluate_flow_check(nodes, pipes, src, pc, [("U1", 110)])
        self.assertEqual(r.max_deliverable, 110)
        self.assertTrue(r.feasible)

    def test_unlisted_sinks_consume_nothing(self):
        # U3 不列入需求：即使其所在管段故障阻断，也不影响核算
        nodes, pipes = build_demo()
        src, pc = self._caps(nodes, pipes)
        r = evaluate_flow_check(
            nodes, pipes, src, pc, [("U1", 50), ("U2", 50)],
            blocked_pipe_ids=frozenset({"P6"}),
        )
        self.assertEqual(r.demand_total, 100)
        self.assertEqual(r.max_deliverable, 100)
        self.assertTrue(r.feasible)

    def test_zero_demand_sink_always_satisfied(self):
        nodes = {
            "S": Node("S", "source"),
            "A": Node("A", "junction"),
            "U1": Node("U1", "sink"),
        }
        pipes = {"P1": Pipe("P1", "S", "A")}
        # 无管段连到 U1，但需求为 0：仍可满足，可交付 0
        r = evaluate_flow_check(nodes, pipes, [("S", 10)], [("P1", 10)], [("U1", 0)])
        self.assertEqual((r.demand_total, r.max_deliverable, r.feasible), (0, 0, True))

    def test_parallel_pipes_capacities_add(self):
        # 两条平行管段 S-A，各容量 30，合计可过 60
        nodes = {
            "S": Node("S", "source"),
            "A": Node("A", "junction"),
            "U1": Node("U1", "sink"),
        }
        pipes = {
            "P1": Pipe("P1", "S", "A"),
            "P2": Pipe("P2", "S", "A"),
            "P3": Pipe("P3", "A", "U1"),
        }
        r = evaluate_flow_check(
            nodes,
            pipes,
            [("S", 100)],
            [("P1", 30), ("P2", 30), ("P3", 100)],
            [("U1", 60)],
        )
        self.assertEqual(r.max_deliverable, 60)
        self.assertTrue(r.feasible)

    def test_validation_missing_coverage_duplicates_and_types(self):
        nodes, pipes = build_demo()

        # 缺少水源 S 与管段（只给部分）
        r = evaluate_flow_check(nodes, pipes, [], [("P1", 1)], [("U1", 1)])
        codes = {i.code for i in r.issues}
        self.assertIn(FLOW_SOURCE_MISSING, codes)
        # P2-P6 全部缺失
        self.assertEqual(
            {i.pipe_id for i in r.issues if i.code == FLOW_PIPE_MISSING},
            {pid for pid in pipes if pid != "P1"},
        )
        self.assertEqual(r.max_deliverable, 0)
        self.assertFalse(r.feasible)

        # 编号不存在 / 类型错误 / 同类重复
        r = evaluate_flow_check(
            nodes,
            pipes,
            [("SX", 1), ("U1", 1)],  # 不存在 + 非水源；S 缺失
            [("PX", 1)] + [(pid, 1) for pid in pipes],  # 不存在 + 全量
            [("U1", 1), ("U1", 2), ("A", 3), ("UX", 4)],  # 重复/非sink/不存在
        )
        codes = {i.code for i in r.issues}
        self.assertIn(FLOW_SOURCE_NOT_FOUND, codes)
        self.assertIn(FLOW_SOURCE_NOT_SOURCE, codes)
        self.assertIn(FLOW_SOURCE_MISSING, codes)
        self.assertIn(FLOW_PIPE_NOT_FOUND, codes)
        self.assertIn(FLOW_SINK_DUPLICATE, codes)
        self.assertIn(FLOW_SINK_NOT_SINK, codes)
        self.assertIn(FLOW_SINK_NOT_FOUND, codes)

    def test_validation_empty_demands(self):
        nodes, pipes = build_demo()
        src = [(nid, 1) for nid, n in nodes.items() if n.type == "source"]
        pc = [(pid, 1) for pid in pipes]
        r = evaluate_flow_check(nodes, pipes, src, pc, [])
        self.assertEqual([i.code for i in r.issues], [FLOW_NO_DEMAND])


if __name__ == "__main__":
    unittest.main()
