"""质量趋势修订服务：把评分卡日数据聚合为趋势修订包，管理曲线的确认、发布与撤下。

domain_chain: 质量趋势修订 (dq_trend_revision)
  └─ 聚合 → 质量评分卡 (dq_scorecard)：确认结论回写评分卡核对清单与发布占用
  └─ 发布 → 质量报告 (dq_report)：报告只取已发布修订包的不可变曲线快照
  └─ 回溯 → 质量趋势 (dq_trend)：从突变日期回查修订流转与评分来源

修订生命周期（状态机，只允许正向流转）：
  待确认 ──确认发布──▶ 已发布 ──撤下修订──▶ 已撤下（终态）
     └────作废修订──▶ 已作废（终态，迟到评分触发重新聚合时旧包归宿）

约束：
  - 一个发布序号只对应一套可复算曲线：发布时分配全局单调序号并固化校验和；
  - 已撤下/已作废不能倒序流转，旧计算结束通知也不能恢复确认资格；
  - 已发布曲线不可变：迟到评分只能进入下一个修订包，不改写已发布快照。
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 终态：任何动作都不得再把这些状态改回去
TERMINAL_STATUSES = ("已撤下", "已作废")
# 合法流转表：(当前状态, 动作) -> 目标状态
LEGAL_TRANSITIONS: dict[tuple[str, str], str] = {
    ("待确认", "确认发布"): "已发布",
    ("已发布", "撤下修订"): "已撤下",
    ("待确认", "作废修订"): "已作废",
}


class DqTrendRevisionService(BaseModuleService):
    MODULE = "dq_trend_revision"
    ENTITY = "趋势修订包"
    OPERATION_TERM = "聚合"
    LIST_FIELDS = [
        "修订包编号", "目标表名", "起始日期", "截止日期",
        "发布序号", "曲线版本", "来源评分数", "校验和",
    ]
    REQUIRED_FIELDS = ["修订包编号", "目标表名", "起始日期", "截止日期"]
    STATUS_ORDER = ["待确认", "已发布", "已撤下", "已作废"]
    ACTION_RULES = {"确认发布": "已发布", "撤下修订": "已撤下", "作废修订": "已作废"}
    NEGATIVE_ACTIONS = ["撤下修订", "作废修订"]

    # ------------------------------------------------------------------ 聚合
    def build_revision_package(
        self,
        table_name: str,
        start_date: str,
        end_date: str,
        *,
        calc_batch: str | None = None,
        remark: str = "",
    ) -> dict[str, Any]:
        """聚合区间内评分卡日数据，生成待确认趋势修订包。

        同日存在多张评分（迟到评分）时，按评分时间/记录编号确定性地取最新一张
        作为曲线点，其余评分在核对清单中标注「同日被替代」，保证曲线可复算。
        若同表同区间已有待确认修订包，先将其作废——迟到评分到达时旧包不得再发布。
        """
        scores = self._collect_scores(table_name, start_date, end_date)
        if not scores:
            raise ValueError(
                f"目标表 {table_name} 在 {start_date}~{end_date} 没有可聚合的评分卡数据"
            )

        # 迟到评分触发重新聚合：同区间待确认旧包作废，终态包不动
        for older in store.rows(self.MODULE):
            if (
                older.get("目标表名") == table_name
                and older.get("起始日期") == start_date
                and older.get("截止日期") == end_date
                and older.get("status") == "待确认"
            ):
                self._ensure_package_fields(older)
                self._transition(
                    older, "作废修订",
                    reason=f"迟到评分触发重新聚合，被新修订包替代{('：' + remark) if remark else ''}",
                )

        points, winner_ids = self._aggregate_points(scores)
        checksum = self._checksum(points)
        checklist = [
            {
                "评分记录": row.get("记录编号"),
                "评分id": row.get("id"),
                "评分日期": self._score_date(row),
                "综合得分": row.get("综合得分"),
                "核对结果": "已纳入" if row.get("id") in winner_ids else "同日被替代",
                "发布序号": None,
            }
            for row in scores
        ]

        rows = store.rows(self.MODULE)
        new_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        package = {
            "id": new_id,
            "修订包编号": f"DQ_TR-{new_id:04d}",
            "目标表名": table_name,
            "起始日期": start_date,
            "截止日期": end_date,
            "发布序号": "",
            "曲线版本": "curve-draft",
            "来源评分数": str(len(scores)),
            "校验和": checksum,
            "计算批次号": calc_batch or f"batch-{new_id}",
            "status": "待确认",
            "pending": True,
            "abnormal": False,
            "来源评分": [row.get("id") for row in scores],
            "曲线点": points,
            "曲线快照": copy.deepcopy(points),
            "核对清单": checklist,
            "流转记录": [
                self._flow("聚合生成", f"聚合{len(scores)}张评分，{len(points)}个曲线点，校验和{checksum}")
            ],
        }
        rows.append(package)
        return package

    def _collect_scores(
        self, table_name: str, start_date: str, end_date: str,
    ) -> list[dict[str, Any]]:
        """取区间内评分卡，按日期、评分时间、记录id排序，保证聚合确定性。"""
        scores = [
            row for row in store.rows("dq_scorecard")
            if row.get("目标表名") == table_name
            and start_date <= self._score_date(row) <= end_date
        ]
        return sorted(scores, key=lambda r: (self._score_date(r), str(r.get("评分时间", "")), int(r.get("id", 0))))

    def _aggregate_points(
        self, scores: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], set[int]]:
        """同日多张评分取最新一张为曲线点，返回曲线点和入选评分id集合。"""
        latest_by_date: dict[str, dict[str, Any]] = {}
        for row in scores:
            latest_by_date[self._score_date(row)] = row  # 已排序，后者即最新
        points: list[dict[str, Any]] = []
        for day in sorted(latest_by_date):
            row = latest_by_date[day]
            points.append({
                "日期": day,
                "完备率": f"{self._to_float(row.get('完整性得分')):.1f}%",
                "准确率": f"{self._to_float(row.get('准确性得分')):.1f}%",
                "及时率": f"{self._to_float(row.get('时效性得分')):.1f}%",
                "综合质量": f"{self._to_float(row.get('综合得分')):.1f}",
                "来源评分记录": row.get("记录编号"),
                "来源评分id": row.get("id"),
            })
        return points, {int(r.get("id", 0)) for r in latest_by_date.values()}

    # ------------------------------------------------------------------ 流转
    def _ensure_package_fields(self, package: dict[str, Any]) -> None:
        """通用登记产生的骨架行只有列表字段，流转前补齐聚合结构的默认值。"""
        package.setdefault("发布序号", "")
        package.setdefault("曲线版本", "curve-draft")
        if not package.get("校验和"):
            package["校验和"] = self._checksum(package.get("曲线点") or [])
        package.setdefault("来源评分", [])
        package.setdefault("曲线点", [])
        package.setdefault("曲线快照", copy.deepcopy(package.get("曲线点") or []))
        package.setdefault("核对清单", [])
        package.setdefault("流转记录", [])

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """重写基类动作：修订状态只能按合法流转表正向移动，终态拒绝一切动作。"""
        package = store.find(self.MODULE, entry_id)
        if package is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if action not in self.ACTION_RULES:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"

        current = str(package.get("status"))
        if current in TERMINAL_STATUSES:
            return package, f"修订包当前状态为{current}（终态），不可倒序流转，动作「{action}」被拒绝"
        target = LEGAL_TRANSITIONS.get((current, action))
        if target is None:
            return package, f"修订包当前为{current}，不允许执行「{action}」"

        self._ensure_package_fields(package)
        self._transition(package, action)
        return package, f"{self.ENTITY}已{action}"

    def _transition(self, package: dict[str, Any], action: str, *, reason: str = "") -> None:
        target = LEGAL_TRANSITIONS[(str(package["status"]), action)]
        package["status"] = target
        package["pending"] = target == "待确认"
        package["abnormal"] = target in TERMINAL_STATUSES
        package["流转记录"].append(self._flow(action, reason))

        if action == "确认发布":
            self._publish(package)
        elif action == "撤下修订":
            self._withdraw(package)
        elif action == "作废修订":
            package["曲线版本"] = f"void-{package['修订包编号']}"

    def _publish(self, package: dict[str, Any]) -> None:
        """确认发布：分配全局单调发布序号，固化快照，并把结论同步到评分卡核对清单。"""
        seq = self._next_publication_seq()
        package["发布序号"] = seq
        package["曲线版本"] = f"curve-r{seq}"
        # 快照在聚合时已固化；发布只绑定序号，不再重算，保证序号↔曲线一一对应
        package["曲线快照"] = copy.deepcopy(package["曲线点"])
        for item in package["核对清单"]:
            item["发布序号"] = seq
        package["流转记录"].append(
            self._flow("序号绑定", f"发布序号#{seq}绑定校验和{package['校验和']}")
        )
        for item in package["核对清单"]:
            self._sync_scorecard_checklist(package, item, withdrawn=False)

    def _withdraw(self, package: dict[str, Any]) -> None:
        """撤下发布：评分卡保留被哪次发布使用的历史，仅追加撤下结论。"""
        for item in package["核对清单"]:
            self._sync_scorecard_checklist(package, item, withdrawn=True)

    def _sync_scorecard_checklist(
        self, package: dict[str, Any], item: dict[str, Any], *, withdrawn: bool,
    ) -> None:
        """趋势确认/撤下结论同步到评分卡的核对清单。"""
        score = store.find("dq_scorecard", int(item["评分id"]))
        if score is None:
            return
        seq = package["发布序号"]
        conclusion = "发布撤下" if withdrawn else item["核对结果"]
        score.setdefault("核对清单", []).append({
            "修订包": package["修订包编号"],
            "发布序号": seq,
            "核对结论": conclusion,
            "时间": self._now(),
        })
        if not withdrawn and item["核对结果"] == "已纳入":
            occupied = [part for part in str(score.get("发布占用", "")).split("；") if part]
            label = f"发布序号#{seq}"
            if label not in occupied:
                occupied.append(label)
            score["发布占用"] = "；".join(occupied)
        history = str(score.get("趋势核对结论", "")).strip("；")
        note = (
            f"{package['修订包编号']}/发布序号#{seq}：{conclusion}"
        )
        score["趋势核对结论"] = f"{history}；{note}".strip("；")

    def confirm_revision(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        return self.run_action(entry_id, "确认发布")

    def withdraw_revision(self, entry_id: int, reason: str = "") -> tuple[dict[str, Any] | None, str]:
        package = store.find(self.MODULE, entry_id)
        if package is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if package.get("status") in TERMINAL_STATUSES:
            return package, f"修订包当前状态为{package['status']}（终态），撤下请求被拒绝"
        if package.get("status") != "已发布":
            return package, f"修订包当前为{package['status']}，只有已发布曲线允许撤下"
        self._ensure_package_fields(package)
        self._transition(package, "撤下修订", reason=reason or "质量报告撤回该段曲线")
        return package, "趋势修订包已撤下"

    def obsolete_pending(self, entry_id: int, reason: str = "") -> tuple[dict[str, Any] | None, str]:
        package = store.find(self.MODULE, entry_id)
        if package is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if package.get("status") != "待确认":
            return package, f"修订包当前为{package['status']}，只有待确认包允许作废"
        self._ensure_package_fields(package)
        self._transition(package, "作废修订", reason=reason or "被重新聚合的修订包替代")
        return package, "待确认修订包已作废"

    # ------------------------------------------------------- 计算结束通知
    def receive_calc_finished(
        self, entry_id: int, calc_batch: str,
    ) -> dict[str, Any]:
        """接收计算结束通知。

        已撤下/已作废的修订包对通知一律拒收：旧通知不得恢复确认资格，
        也不产生任何状态变化，只留痕说明拒收原因。
        """
        package = store.find(self.MODULE, entry_id)
        if package is None:
            return {"ok": False, "受理": False, "原因": f"修订包{entry_id}不存在"}
        status = str(package.get("status"))
        package.setdefault("流转记录", [])
        if status in TERMINAL_STATUSES:
            package["流转记录"].append(
                self._flow("通知拒收", f"计算批次{calc_batch}在{status}后到达，不恢复确认资格")
            )
            return {
                "ok": False, "受理": False,
                "修订包": package["修订包编号"], "当前状态": status,
                "原因": f"修订包当前状态为{status}（终态），旧计算结束通知不能恢复确认资格",
            }
        matched = package.get("计算批次号") == calc_batch
        package["流转记录"].append(
            self._flow(
                "接收计算结束通知",
                f"计算批次{calc_batch}{'匹配' if matched else '不匹配(迟到批次)'}，当前状态{status}",
            )
        )
        return {
            "ok": True, "受理": True,
            "修订包": package["修订包编号"], "当前状态": status,
            "批次匹配": matched,
        }

    # ------------------------------------------------------------------ 复算
    def replay_curve(self, entry_id: int) -> dict[str, Any]:
        """按来源评分重新聚合曲线并比对校验和，验证发布序号对应的曲线可复算。"""
        package = store.find(self.MODULE, entry_id)
        if package is None:
            raise ValueError(f"修订包{entry_id}不存在")
        source_rows = [
            row for sid in package.get("来源评分", [])
            if (row := store.find("dq_scorecard", int(sid))) is not None
        ]
        points, _ = self._aggregate_points(source_rows)
        recomputed = self._checksum(points)
        original = str(package.get("校验和", ""))
        return {
            "修订包": package["修订包编号"],
            "发布序号": package.get("发布序号"),
            "原始校验和": original,
            "重算校验和": recomputed,
            "一致": original == recomputed,
            "可复算": original == recomputed,
        }

    # ---------------------------------------------------------- 报告取数
    def published_curve(self, table_name: str) -> dict[str, Any] | None:
        """返回表当前生效的已发布曲线（取发布序号最大的已发布包）。

        待确认/已撤下/已作废包都不会被质量报告取用。
        """
        candidates = [
            row for row in store.rows(self.MODULE)
            if row.get("目标表名") == table_name and row.get("status") == "已发布"
        ]
        if not candidates:
            return None
        latest = max(candidates, key=lambda r: int(r.get("发布序号") or 0))
        return {
            "修订包": latest["修订包编号"],
            "发布序号": latest["发布序号"],
            "曲线版本": latest["曲线版本"],
            "校验和": latest["校验和"],
            "曲线点": copy.deepcopy(latest.get("曲线快照") or latest.get("曲线点")),
        }

    def trace_curve_jump(
        self, table_name: str, jump_date: str, threshold: float = 5.0,
    ) -> dict[str, Any]:
        """从突变日期回查：定位发布曲线的环比突变，串联当日评分版本与修订流转记录。"""
        curve = self.published_curve(table_name)
        if curve is None:
            return {"目标表名": table_name, "突变日期": jump_date, "是否突变": False,
                    "原因": "没有可取用的已发布曲线"}
        points = sorted(curve["曲线点"], key=lambda p: p["日期"])
        current = next((p for p in points if p["日期"] == jump_date), None)
        previous = None
        if current is not None:
            idx = points.index(current)
            previous = points[idx - 1] if idx > 0 else None

        delta = None
        is_jump = False
        if current is not None and previous is not None:
            delta = round(float(current["综合质量"]) - float(previous["综合质量"]), 2)
            is_jump = abs(delta) >= threshold

        # 当日全部评分版本（含被替代的迟到评分）
        day_scores = [
            {
                "评分记录": r.get("记录编号"),
                "评分id": r.get("id"),
                "综合得分": r.get("综合得分"),
                "迟到标记": r.get("迟到标记", "首次评分"),
                "评分时间": r.get("评分时间"),
                "发布占用": r.get("发布占用", ""),
            }
            for r in store.rows("dq_scorecard")
            if r.get("目标表名") == table_name and self._score_date(r) == jump_date
        ]
        # 覆盖该日期的全部修订包及其流转痕迹
        packages = [
            {
                "修订包": r.get("修订包编号"),
                "状态": r.get("status"),
                "发布序号": r.get("发布序号"),
                "校验和": r.get("校验和"),
                "流转记录": copy.deepcopy(r.get("流转记录", [])),
            }
            for r in store.rows(self.MODULE)
            if r.get("目标表名") == table_name
            and r.get("起始日期") <= jump_date <= r.get("截止日期")
        ]
        return {
            "目标表名": table_name,
            "突变日期": jump_date,
            "阈值": threshold,
            "是否突变": is_jump,
            "发布序号": curve["发布序号"],
            "前一日值": previous["综合质量"] if previous else None,
            "当日值": current["综合质量"] if current else None,
            "变化幅度": delta,
            "当日评分版本": day_scores,
            "关联修订包": packages,
        }

    # ------------------------------------------------------------------ 工具
    def _next_publication_seq(self) -> int:
        existing = [
            int(r.get("发布序号") or 0)
            for r in store.rows(self.MODULE)
            if str(r.get("发布序号") or "").strip()
        ]
        return max(existing, default=0) + 1

    @staticmethod
    def _score_date(row: dict[str, Any]) -> str:
        value = row.get("评分日期") or str(row.get("评分时间", ""))[:10]
        return str(value)[:10]

    @staticmethod
    def _to_float(value: Any) -> float:
        try:
            return float(str(value).strip("%"))
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _checksum(points: list[dict[str, Any]]) -> str:
        payload = json.dumps(points, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _flow(action: str, detail: str) -> dict[str, str]:
        return {"时间": datetime.now(timezone.utc).isoformat(), "动作": action, "说明": detail}

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["operation"] = "聚合"
        snapshot["desc"] = "聚合评分卡日数据为趋势修订包并管理确认、发布与撤下"
        snapshot["artifact"] = "质量趋势修订包数据集"
        snapshot["artifact_desc"] = "绑定发布序号、曲线快照与核对清单的可复算趋势修订包"
        return snapshot
