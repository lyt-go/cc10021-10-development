"""质量趋势修订链路测试。

覆盖：评分卡日数据聚合 → 趋势修订包确认/撤下/作废 → 核对清单回写 →
质量报告发布取数，以及迟到评分在「审核前」与「发布中」两种时序下的曲线差异，
还有终态不可倒序、旧计算结束通知拒收、曲线可复算与突变日期回查。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.dq_report import DqReportService
from app.services.dq_scorecard import DqScorecardService
from app.services.dq_trend_revision import DqTrendRevisionService
from app.store import store


@pytest.fixture()
def services():
    return DqScorecardService(), DqTrendRevisionService(), DqReportService()


def _seed_three_days(score_svc: DqScorecardService, table: str) -> list[int]:
    """登记连续三天的首次评分，返回评分id列表（d3 故意制造一个低值便于突变回查）。"""
    ids = []
    ids.append(score_svc.register_daily_score(table, "2026-11-10", 90, 88, 85)["id"])
    ids.append(score_svc.register_daily_score(table, "2026-11-11", 91, 89, 86)["id"])
    ids.append(score_svc.register_daily_score(table, "2026-11-12", 60, 58, 55)["id"])
    return ids


class TestRevisionLifecycle:
    """修订包确认发布、发布序号与核对清单同步。"""

    def test_aggregate_confirm_and_seq_binding(self, services):
        score_svc, rev_svc, report_svc = services
        table = "rev_lifecycle_table"
        ids = _seed_three_days(score_svc, table)

        package = rev_svc.build_revision_package(table, "2026-11-10", "2026-11-12")
        assert package["status"] == "待确认"
        assert package["发布序号"] == ""
        assert len(package["曲线点"]) == 3
        checksum_before = package["校验和"]

        published, message = rev_svc.confirm_revision(package["id"])
        assert message == "趋势修订包已确认发布"
        assert published["status"] == "已发布"
        seq = int(published["发布序号"])
        assert seq >= 1
        assert published["曲线版本"] == f"curve-r{seq}"

        # 序号↔曲线：校验和在发布前后不变，曲线快照已固化
        assert published["校验和"] == checksum_before
        assert published["曲线快照"] == published["曲线点"]

        # 确认结论同步到评分卡核对清单与发布占用
        for sid in ids:
            view = score_svc.scorecard_checklist(sid)
            assert view["ok"] is True
            assert any(item["发布序号"] == seq for item in view["核对清单"])
            assert f"发布序号#{seq}" in view["发布占用"]

        # 质量报告取到同一条已发布曲线，且带同一校验和
        report = report_svc.publish_daily_curve(table)
        assert report["可发布"] is True
        assert report["发布序号"] == seq
        assert report["曲线校验和"] == checksum_before
        assert report["当期曲线"] == published["曲线快照"]

    def test_global_seq_is_monotonic_across_tables(self, services):
        score_svc, rev_svc, _ = services
        seq_holder = []
        for idx, table in enumerate(["rev_seq_table_a", "rev_seq_table_b"]):
            score_svc.register_daily_score(table, "2026-11-10", 80 + idx, 80, 80)
            pkg = rev_svc.build_revision_package(table, "2026-11-10", "2026-11-10")
            published, _ = rev_svc.confirm_revision(pkg["id"])
            seq_holder.append(int(published["发布序号"]))
        assert seq_holder[1] == seq_holder[0] + 1

    def test_curve_is_recomputable(self, services):
        score_svc, rev_svc, _ = services
        table = "rev_replay_table"
        _seed_three_days(score_svc, table)
        pkg = rev_svc.build_revision_package(table, "2026-11-10", "2026-11-12")
        rev_svc.confirm_revision(pkg["id"])
        replay = rev_svc.replay_curve(pkg["id"])
        assert replay["可复算"] is True
        assert replay["一致"] is True
        assert replay["原始校验和"] == replay["重算校验和"]
        assert int(replay["发布序号"]) >= 1


class TestLateScoreBeforeReview:
    """迟到评分在审核前（曲线仍处待确认）到达：旧待确认包作废，新包替代。"""

    def test_late_score_obsoletes_pending_package(self, services):
        score_svc, rev_svc, report_svc = services
        table = "rev_late_before_review"
        _seed_three_days(score_svc, table)
        first_pkg = rev_svc.build_revision_package(table, "2026-11-10", "2026-11-12")
        assert first_pkg["status"] == "待确认"

        # 同一天第二张评分迟到（11日重算，分数显著抬高）
        late = score_svc.register_daily_score(table, "2026-11-11", 97, 96, 95)
        assert late["迟到标记"] == "迟到评分"

        # 重新聚合：旧待确认包被作废，新包接管同区间
        second_pkg = rev_svc.build_revision_package(table, "2026-11-10", "2026-11-12",
                                                     remark="11日迟到评分")
        assert store.find("dq_trend_revision", first_pkg["id"])["status"] == "已作废"
        assert second_pkg["id"] != first_pkg["id"]
        assert second_pkg["status"] == "待确认"

        # 待确认曲线不能进日报
        blocked = report_svc.publish_daily_curve(table)
        assert blocked["可发布"] is False

        # 新包曲线取最新评分，迟到评分入选、首评在核对清单标注「同日被替代」
        day11 = next(p for p in second_pkg["曲线点"] if p["日期"] == "2026-11-11")
        assert day11["来源评分id"] == late["id"]
        checklist = {item["评分id"]: item["核对结果"] for item in second_pkg["核对清单"]}
        assert checklist[late["id"]] == "已纳入"
        replaced = [sid for sid, result in checklist.items() if result == "同日被替代"]
        assert len(replaced) == 1

        # 确认新包后日报才拿到曲线；已作废旧包不能被确认
        refused, msg = rev_svc.confirm_revision(first_pkg["id"])
        assert refused["status"] == "已作废"
        assert "不可倒序" in msg
        published, _ = rev_svc.confirm_revision(second_pkg["id"])
        report = report_svc.publish_daily_curve(table)
        assert report["发布序号"] == int(published["发布序号"])
        assert report["当期曲线"] == published["曲线快照"]


class TestLateScoreDuringPublish:
    """迟到评分在发布中到达：已发布曲线不可变，迟到评分只能进下一个修订包。"""

    def test_late_score_cannot_rewrite_published_curve(self, services):
        score_svc, rev_svc, report_svc = services
        table = "rev_late_during_publish"
        _seed_three_days(score_svc, table)
        first_pkg = rev_svc.build_revision_package(table, "2026-11-10", "2026-11-12")
        rev_svc.confirm_revision(first_pkg["id"])
        frozen_curve = [dict(p) for p in first_pkg["曲线快照"]]
        seq1 = int(first_pkg["发布序号"])

        # 发布之后迟到评分才到
        late = score_svc.register_daily_score(table, "2026-11-11", 97, 96, 95)
        assert late["迟到标记"] == "迟到评分"

        # 已发布包仍是原曲线，迟到评分没有改写任何快照
        assert first_pkg["曲线快照"] == frozen_curve
        report_before = report_svc.publish_daily_curve(table)
        assert report_before["发布序号"] == seq1
        day11 = next(p for p in report_before["当期曲线"] if p["日期"] == "2026-11-11")
        assert day11["来源评分id"] != late["id"]

        # 迟到评分进入下一个修订包，发布后产生更大的发布序号和新曲线版本
        next_pkg = rev_svc.build_revision_package(table, "2026-11-10", "2026-11-12",
                                                  remark="发布后迟到评分")
        rev_svc.confirm_revision(next_pkg["id"])
        assert int(next_pkg["发布序号"]) > seq1
        assert next_pkg["曲线版本"] != first_pkg["曲线版本"]
        report_after = report_svc.publish_daily_curve(table)
        assert report_after["发布序号"] == int(next_pkg["发布序号"])
        new_day11 = next(p for p in report_after["当期曲线"] if p["日期"] == "2026-11-11")
        assert new_day11["来源评分id"] == late["id"]
        # 序号与曲线仍一一对应：新包快照与日报取用曲线完全相同
        assert report_after["曲线校验和"] == next_pkg["校验和"]


class TestWithdrawAndTerminalGuard:
    """撤下流转、终态不可倒序、旧计算结束通知不恢复确认资格。"""

    def test_withdraw_blocks_rollback_and_rejects_stale_notification(self, services):
        score_svc, rev_svc, report_svc = services
        table = "rev_withdraw_table"
        ids = _seed_three_days(score_svc, table)
        pkg = rev_svc.build_revision_package(table, "2026-11-10", "2026-11-12")
        rev_svc.confirm_revision(pkg["id"])
        seq = int(pkg["发布序号"])

        # 撤下已发布曲线
        withdrawn, msg = rev_svc.withdraw_revision(pkg["id"], reason="下游日报撤段")
        assert msg == "趋势修订包已撤下"
        assert withdrawn["status"] == "已撤下"

        # 撤下后日报无曲线可取
        assert report_svc.publish_daily_curve(table)["可发布"] is False

        # 终态不能倒序：确认发布、撤下、作废全部被拒，状态不变
        for action in ("确认发布", "撤下修订", "作废修订"):
            entry, reject_msg = rev_svc.run_action(pkg["id"], action)
            assert entry["status"] == "已撤下"
            assert "不可倒序" in reject_msg

        # 旧计算结束通知到达：拒收，不恢复确认资格
        notice = rev_svc.receive_calc_finished(pkg["id"], pkg["计算批次号"])
        assert notice["受理"] is False
        assert notice["当前状态"] == "已撤下"
        assert "不能恢复确认资格" in notice["原因"]
        assert store.find("dq_trend_revision", pkg["id"])["status"] == "已撤下"

        # 评分卡保留被哪次发布使用的历史，并追加撤下结论
        view = score_svc.scorecard_checklist(ids[0])
        assert f"发布序号#{seq}" in view["发布占用"]
        conclusions = [item["核对结论"] for item in view["核对清单"]]
        assert "已纳入" in conclusions and "发布撤下" in conclusions

    def test_obsoleted_package_rejects_stale_notification(self, services):
        score_svc, rev_svc, _ = services
        table = "rev_obsolete_notice_table"
        score_svc.register_daily_score(table, "2026-11-10", 80, 80, 80)
        first = rev_svc.build_revision_package(table, "2026-11-10", "2026-11-10")
        # 迟到评分触发重聚合，首包作废
        score_svc.register_daily_score(table, "2026-11-10", 90, 90, 90)
        rev_svc.build_revision_package(table, "2026-11-10", "2026-11-10")
        assert store.find("dq_trend_revision", first["id"])["status"] == "已作废"

        notice = rev_svc.receive_calc_finished(first["id"], first["计算批次号"])
        assert notice["受理"] is False
        assert notice["当前状态"] == "已作废"

    def test_illegal_transition_from_pending_is_rejected(self, services):
        score_svc, rev_svc, _ = services
        table = "rev_illegal_transition"
        score_svc.register_daily_score(table, "2026-11-10", 80, 80, 80)
        pkg = rev_svc.build_revision_package(table, "2026-11-10", "2026-11-10")
        # 待确认包不能直接撤下（只有已发布包能撤）
        entry, msg = rev_svc.withdraw_revision(pkg["id"])
        assert entry["status"] == "待确认"
        assert "只有已发布" in msg
        # 动作路由层同样拦截
        entry, msg = rev_svc.run_action(pkg["id"], "撤下修订")
        assert entry["status"] == "待确认"
        assert "不允许执行" in msg


class TestReportCurveComparisonAndJumpTrace:
    """两分支日报取用曲线对比，以及从突变日期回查记录。"""

    def test_daily_report_differs_between_two_late_arrivals(self, services):
        score_svc, rev_svc, report_svc = services
        # 分支A：审核前迟到 → 首包作废，确认新包后日报取新曲线
        table_a = "rev_branch_before_review"
        _seed_three_days(score_svc, table_a)
        pkg_a1 = rev_svc.build_revision_package(table_a, "2026-11-10", "2026-11-12")
        late_a = score_svc.register_daily_score(table_a, "2026-11-11", 97, 96, 95)
        pkg_a2 = rev_svc.build_revision_package(table_a, "2026-11-10", "2026-11-12")
        rev_svc.confirm_revision(pkg_a2["id"])
        report_a = report_svc.publish_daily_curve(table_a)
        assert report_a["可发布"] is True
        assert store.find("dq_trend_revision", pkg_a1["id"])["status"] == "已作废"

        # 分支B：发布中迟到 → 日报仍取旧序号曲线，迟到评分不在当期曲线里
        table_b = "rev_branch_in_publish"
        _seed_three_days(score_svc, table_b)
        pkg_b1 = rev_svc.build_revision_package(table_b, "2026-11-10", "2026-11-12")
        rev_svc.confirm_revision(pkg_b1["id"])
        late_b = score_svc.register_daily_score(table_b, "2026-11-11", 97, 96, 95)
        report_b = report_svc.publish_daily_curve(table_b)
        assert report_b["可发布"] is True
        assert report_b["发布序号"] == int(pkg_b1["发布序号"])

        # 两表同源数据、同样的迟到评分，日报实际取用曲线不同
        day_a = next(p for p in report_a["当期曲线"] if p["日期"] == "2026-11-11")
        day_b = next(p for p in report_b["当期曲线"] if p["日期"] == "2026-11-11")
        assert day_a["来源评分id"] == late_a["id"]
        assert day_b["来源评分id"] != late_b["id"]
        assert day_a["综合质量"] != day_b["综合质量"]

    def test_trace_back_from_jump_date(self, services):
        score_svc, rev_svc, _ = services
        table = "rev_jump_trace_table"
        ids = _seed_three_days(score_svc, table)  # 11→12 日分数从约89掉到约58
        pkg = rev_svc.build_revision_package(table, "2026-11-10", "2026-11-12")
        rev_svc.confirm_revision(pkg["id"])

        trace = rev_svc.trace_curve_jump(table, "2026-11-12", threshold=5.0)
        assert trace["是否突变"] is True
        assert trace["发布序号"] == int(pkg["发布序号"])
        assert trace["变化幅度"] < -5
        # 回查得到当日评分版本、占用发布与修订流转痕迹
        assert len(trace["当日评分版本"]) == 1
        assert trace["当日评分版本"][0]["评分id"] == ids[2]
        assert f"发布序号#{pkg['发布序号']}" in trace["当日评分版本"][0]["发布占用"]
        assert any(item["修订包"] == pkg["修订包编号"] for item in trace["关联修订包"])
        actions = [step["动作"] for step in trace["关联修订包"][0]["流转记录"]]
        assert "确认发布" in actions

        # 未达阈值的日期不判突变
        calm = rev_svc.trace_curve_jump(table, "2026-11-11", threshold=5.0)
        assert calm["是否突变"] is False

    def test_build_without_scores_raises(self, services):
        _, rev_svc, _ = services
        with pytest.raises(ValueError):
            rev_svc.build_revision_package("rev_empty_table", "2026-11-10", "2026-11-12")

    def test_skeleton_row_from_generic_create_can_transition(self, services):
        """工作台通用登记只写列表字段，骨架行也必须能完成发布/撤下全流转。"""
        _, rev_svc, report_svc = services
        entry, missing = rev_svc.create_entry({
            "修订包编号": "DQ_TR-SKELETON",
            "目标表名": "rev_skeleton_table",
            "起始日期": "2026-11-15",
            "截止日期": "2026-11-15",
        })
        assert missing == [] and entry["status"] == "待确认"

        published, msg = rev_svc.run_action(entry["id"], "确认发布")
        assert msg == "趋势修订包已确认发布"
        assert published["发布序号"]
        assert published["曲线快照"] == []
        # 无曲线点的空快照也有稳定校验和，可复算
        assert rev_svc.replay_curve(entry["id"])["可复算"] is True

        withdrawn, _ = rev_svc.withdraw_revision(entry["id"])
        assert withdrawn["status"] == "已撤下"
        # 撤下后报告取不到该表曲线
        assert report_svc.publish_daily_curve("rev_skeleton_table")["可发布"] is False
        # 通知留痕不报错且不改变终态
        notice = rev_svc.receive_calc_finished(entry["id"], "unknown-batch")
        assert notice["受理"] is False

    def test_rebuild_obsoletes_skeleton_pending_package(self, services):
        """骨架待确认包遇到重新聚合时，也要能被正常作废而不抛异常。"""
        score_svc, rev_svc, _ = services
        table = "rev_rebuild_skeleton"
        entry, _ = rev_svc.create_entry({
            "修订包编号": "DQ_TR-SK2",
            "目标表名": table,
            "起始日期": "2026-11-15",
            "截止日期": "2026-11-15",
        })
        score_svc.register_daily_score(table, "2026-11-15", 80, 78, 76)
        rebuilt = rev_svc.build_revision_package(table, "2026-11-15", "2026-11-15")
        assert rev_svc.get_entry(entry["id"])["status"] == "已作废"
        assert rebuilt["status"] == "待确认"
        assert len(rebuilt["曲线点"]) == 1
