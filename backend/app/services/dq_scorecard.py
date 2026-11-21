"""质量评分卡服务：计算表级数据质量多维度评分，聚合规则执行结果生成综合评分。

domain_chain: 质量评分卡 (dq_scorecard)
  └─ 计算 → 质量趋势 (dq_trend)
  └─ 聚合 → 质量报告 (dq_report)
"""

from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class DqScorecardService(BaseModuleService):
    MODULE = "dq_scorecard"
    ENTITY = "评分记录"
    OPERATION_TERM = "计算"
    LIST_FIELDS = ["记录编号", "目标表名", "完整性得分", "准确性得分", "时效性得分", "综合得分", "评分时间", "评分版本", "评分日期", "迟到标记", "发布占用"]
    REQUIRED_FIELDS = ["记录编号", "目标表名", "完整性得分"]
    STATUS_ORDER = ["草稿", "计算中", "已完成", "已归档"]
    ACTION_RULES = {"发起评分": "计算中", "确认完成": "已完成", "归档记录": "已归档"}
    NEGATIVE_ACTIONS = []

    def calculate_quality_score(self, table_name: str, rule_results: list[dict] | None = None) -> dict[str, Any]:
        """计算表级综合质量评分：完整性、准确性、时效性、一致性、唯一性五个维度。

        聚合该表所有质量规则执行结果，分别计算各维度得分，加权汇总为综合得分。
        计算完成后将评分结果关联写入质量趋势记录。
        """
        if rule_results is None:
            # 模拟规则执行结果
            rule_results = [
                {"规则类型": "空值检查", "结果": random.choice(["通过", "通过", "通过", "失败"]), "权重": 0.25},
                {"规则类型": "值域范围", "结果": random.choice(["通过", "通过", "通过", "失败"]), "权重": 0.20},
                {"规则类型": "格式校验", "结果": random.choice(["通过", "通过", "失败"]), "权重": 0.15},
                {"规则类型": "唯一性检查", "结果": random.choice(["通过", "通过", "通过", "通过", "失败"]), "权重": 0.20},
                {"规则类型": "时效性检查", "结果": random.choice(["通过", "通过", "失败"]), "权重": 0.10},
                {"规则类型": "参照完整性", "结果": random.choice(["通过", "通过", "通过", "失败"]), "权重": 0.10},
            ]

        dimension_scores = {}
        passed = 0
        total_weight = 0
        for rule in rule_results:
            dim = rule["规则类型"]
            weight = rule["权重"]
            score = 100 if rule["结果"] == "通过" else random.uniform(20, 60)
            dimension_scores[dim] = round(score, 1)
            if rule["结果"] == "通过":
                passed += 1
            total_weight += weight * score

        completeness = dimension_scores.get("空值检查", 85)
        accuracy = (dimension_scores.get("值域范围", 85) + dimension_scores.get("格式校验", 85)) / 2
        timeliness = dimension_scores.get("时效性检查", 85)
        overall = round(total_weight, 1)

        # 写入评分卡记录
        rows = store.rows(self.MODULE)
        new_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        entry = {
            "id": new_id,
            "记录编号": f"DQ_S-{new_id:04d}",
            "目标表名": table_name,
            "完整性得分": str(round(completeness, 1)),
            "准确性得分": str(round(accuracy, 1)),
            "时效性得分": str(round(timeliness, 1)),
            "综合得分": str(round(overall, 1)),
            "评分时间": datetime.now(timezone.utc).isoformat(),
            "评分日期": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "评分版本": "v1.0",
            "迟到标记": "首次评分",
            "发布占用": "",
            "趋势核对结论": "",
            "核对清单": [],
            "status": "已完成",
            "pending": False,
            "abnormal": overall < 60,
        }
        rows.append(entry)

        # 聚合评分结果写入趋势记录
        self._write_trend_record(table_name, completeness, accuracy, timeliness, overall)

        return {
            "目标表名": table_name,
            "规则总数": len(rule_results),
            "通过数": passed,
            "通过率": f"{passed / len(rule_results) * 100:.1f}%",
            "完整性得分": round(completeness, 1),
            "准确性得分": round(accuracy, 1),
            "时效性得分": round(timeliness, 1),
            "综合得分": round(overall, 1),
            "等级": "A" if overall >= 90 else "B" if overall >= 75 else "C" if overall >= 60 else "D",
            "记录编号": entry["记录编号"],
        }

    def _write_trend_record(self, table_name: str, completeness: float, accuracy: float,
                            timeliness: float, overall: float) -> None:
        """聚合评分数据写入质量趋势记录。"""
        trend_rows = store.rows("dq_trend")
        new_tid = max((int(r.get("id", 0)) for r in trend_rows), default=0) + 1
        trend_rows.append({
            "id": new_tid,
            "记录编号": f"DQ_T-{new_tid:04d}",
            "目标表名": table_name,
            "日期": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "完备率": f"{completeness:.1f}%",
            "准确率": f"{accuracy:.1f}%",
            "及时率": f"{timeliness:.1f}%",
            "综合质量": f"{overall:.1f}",
            "趋势标记": "正常" if overall >= 75 else "下降趋势",
            "status": "正常" if overall >= 75 else "下降趋势",
            "pending": False,
            "abnormal": overall < 60,
        })

    def register_daily_score(
        self,
        table_name: str,
        score_date: str,
        completeness: float,
        accuracy: float,
        timeliness: float,
        *,
        calc_batch: str | None = None,
    ) -> dict[str, Any]:
        """登记一张按日评分（迟到评分也走这里）。

        同一天的第二张评分自动打「迟到评分」标记；已存在的首评保留不变。
        评分先落评分卡，是否进入趋势曲线由趋势修订包的聚合与确认决定，
        已发布曲线不会被迟到评分改写。
        """
        rows = store.rows(self.MODULE)
        same_day = [
            r for r in rows
            if r.get("目标表名") == table_name and str(r.get("评分日期")) == score_date
        ]
        is_late = bool(same_day)
        overall = round(completeness * 0.5 + accuracy * 0.3 + timeliness * 0.2, 1)
        new_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        entry = {
            "id": new_id,
            "记录编号": f"DQ_S-{new_id:04d}",
            "目标表名": table_name,
            "完整性得分": f"{completeness:.1f}",
            "准确性得分": f"{accuracy:.1f}",
            "时效性得分": f"{timeliness:.1f}",
            "综合得分": f"{overall:.1f}",
            "评分时间": datetime.now(timezone.utc).isoformat(),
            "评分日期": score_date,
            "评分版本": f"v{len(same_day) + 1}.0",
            "迟到标记": "迟到评分" if is_late else "首次评分",
            "计算批次号": calc_batch or f"batch-{new_id}",
            "发布占用": "",
            "趋势核对结论": "",
            "核对清单": [],
            "status": "已完成",
            "pending": False,
            "abnormal": overall < 60,
        }
        rows.append(entry)
        return entry

    def scorecard_checklist(self, score_id: int) -> dict[str, Any]:
        """查看单张评分的核对清单：被哪次发布使用、各修订包给过什么核对结论。"""
        score = store.find(self.MODULE, score_id)
        if score is None:
            return {"ok": False, "原因": f"评分记录{score_id}不存在"}
        return {
            "ok": True,
            "评分记录": score.get("记录编号"),
            "目标表名": score.get("目标表名"),
            "评分日期": score.get("评分日期"),
            "迟到标记": score.get("迟到标记", "首次评分"),
            "发布占用": score.get("发布占用", ""),
            "趋势核对结论": score.get("趋势核对结论", ""),
            "核对清单": score.get("核对清单", []),
        }

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["operation"] = "计算"
        snapshot["desc"] = "计算表级多维度质量评分并聚合为评分卡"
        snapshot["artifact"] = "质量评分卡数据集"
        snapshot["artifact_desc"] = "聚合质量规则执行结果的评分卡数据集"
        return snapshot