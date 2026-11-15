"""调用配额控制业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from app.services.usage import UsageService
from app.store import store

MODULE = "quota"
REQUIRED_FIELDS = ["配置编号", "调用方名称", "每秒上限"]
STATUS_ORDER = ["草稿", "已生效", "已超额", "已过期"]
STATUS_FIELD = "配额状态"  # 列表页与详情页渲染的业务状态字段，必须与内部 status 同步
ACTION_RULES = {"生效配额": "已生效", "调整配额": "已生效", "取消配额": "已过期"}
ACTION_MESSAGES = {"生效配额": "配额配置已生效", "调整配额": "配额配置已调整", "取消配额": "配额配置已取消"}
NEGATIVE_ACTIONS = ["取消配额"]
AUDIT_EVENTS = ("配置保存", "动作回执", "详情回读")
WINDOW_SECONDS = 2  # 并发窗口覆盖的秒数：窗口容量 = 每秒上限 × 窗口秒数

usage_service = UsageService()


def _to_int(value: Any) -> int:
    """把配置值解析成整数；未配置或无法解析时按 0 处理。"""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


class QuotaService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("配置编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is not None:
            self._ensure_runtime_state(entry)
            self._record_audit(entry, "详情回读")
        return entry

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        # 登记什么就保存什么：业务字段（日调用上限、并发限制、超限策略、有效期至等）
        # 必须随配置一起落库，否则详情回读会丢字段。
        business = {key: value for key, value in values.items() if key != "action"}
        code = str(business.get("配置编号") or "").strip()
        # 同一配置编号只保留一条记录，重复登记视为更新，避免列表重复显示。
        existing = next(
            (row for row in rows if str(row.get("配置编号", "")).strip() == code),
            None,
        )
        if existing is not None:
            existing.update(business)
            self._sync_status_field(existing)
            self._ensure_runtime_state(existing)
            self._record_audit(existing, "配置保存")
            return existing, []
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update(business)
        entry["status"] = STATUS_ORDER[0]
        entry[STATUS_FIELD] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        self._ensure_runtime_state(entry)
        self._record_audit(entry, "配置保存")
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"配额配置 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于调用配额控制可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        self._ensure_runtime_state(entry)
        entry["status"] = target
        entry[STATUS_FIELD] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        if action == "生效配额":
            # 生效后开启新的并发窗口与当日计数，准入判断从干净的状态开始。
            entry["并发窗口"] = []
            entry["当日调用"] = 0
            entry["统计日期"] = date.today().isoformat()
        self._record_audit(entry, "动作回执")
        return entry, ACTION_MESSAGES.get(action, f"配额配置已{action}")

    def admit_call(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, bool, str]:
        """调用准入判断：按有效期、日调用上限与并发窗口容量决定放行或拒绝。

        每次调用在并发窗口里只留一份准入状态；超限且策略为拒绝时，配额转为
        已超额，同时把拒绝结果写进使用效果追踪，调用方看到的不是成功计数。
        """
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, False, f"配额配置 {entry_id} 不存在或已归档"
        self._ensure_runtime_state(entry)
        caller = str(values.get("调用方名称") or entry.get("调用方名称") or "").strip()
        call_id = str(values.get("调用编号") or "").strip()

        # 幂等去重：同一调用编号在并发窗口里只留严格的一份准入状态。
        if call_id:
            for state in entry["并发窗口"]:
                if state.get("调用编号") == call_id:
                    if state.get("结果") == "已拒绝":
                        return entry, False, str(state.get("拒绝原因") or "调用被拒绝")
                    return entry, True, "调用已放行（重复调用已去重）"

        status = str(entry.get("status") or "")
        if status == "已过期":
            return entry, False, self._reject(entry, caller, call_id, "配额已过期，调用被拒绝", in_window=False)
        if status == "已超额":
            return entry, False, self._reject(entry, caller, call_id, "配额已超额，调用被拒绝", in_window=False)
        if status != "已生效":
            return entry, False, self._reject(entry, caller, call_id, "配额未生效，调用被拒绝", in_window=False)

        deadline = str(entry.get("有效期至") or "").strip()
        today = date.today().isoformat()
        if deadline and deadline < today:
            entry["status"] = "已过期"
            entry[STATUS_FIELD] = "已过期"
            entry["pending"] = False
            return entry, False, self._reject(entry, caller, call_id, f"配额已过有效期（{deadline}），调用被拒绝", in_window=False)

        if entry.get("统计日期") != today:
            entry["统计日期"] = today
            entry["当日调用"] = 0
        daily_limit = _to_int(entry.get("日调用上限"))
        if daily_limit > 0 and int(entry.get("当日调用", 0)) >= daily_limit:
            reason = f"超出日调用上限（{daily_limit} 次/日），超限策略：拒绝"
            return entry, False, self._reject(entry, caller, call_id, reason)

        per_second = _to_int(entry.get("每秒上限"))
        capacity = per_second * WINDOW_SECONDS
        if capacity <= 0:
            return entry, False, self._reject(entry, caller, call_id, "每秒上限未配置，调用被拒绝", in_window=False)
        if len(entry["并发窗口"]) >= capacity and str(entry.get("超限策略") or "").strip() == "拒绝":
            reason = f"超出每秒上限（{per_second} 次/秒），并发窗口容量 {capacity} 已满，超限策略：拒绝"
            return entry, False, self._reject(entry, caller, call_id, reason)

        self._append_window_state(entry, caller, call_id, "成功", "")
        entry["当日调用"] = int(entry.get("当日调用", 0)) + 1
        usage_service.record_call(product=str(entry.get("配置编号") or ""), caller=caller, success=True)
        used = len(entry["并发窗口"])
        return entry, True, f"调用已放行（并发窗口 {used}/{capacity}）"

    def _reject(self, entry: dict[str, Any], caller: str, call_id: str, reason: str, *, in_window: bool = True) -> str:
        """登记一次拒绝：窗口留一份拒绝状态、配额转已超额、效果追踪记拒绝原因。"""
        if in_window:
            self._append_window_state(entry, caller, call_id, "已拒绝", reason)
            entry["status"] = "已超额"
            entry[STATUS_FIELD] = "已超额"
            entry["pending"] = True
            entry["abnormal"] = True
        usage_service.record_call(
            product=str(entry.get("配置编号") or ""),
            caller=caller,
            success=False,
            reason=reason,
        )
        return reason

    def _append_window_state(self, entry: dict[str, Any], caller: str, call_id: str, result: str, reason: str) -> None:
        window = entry["并发窗口"]
        if not call_id:
            call_id = f"CALL-{entry['id']:04d}-{len(window) + 1:04d}"
        window.append({
            "调用编号": call_id,
            "调用方名称": caller,
            "结果": result,
            "拒绝原因": reason,
            "时间": datetime.now().isoformat(timespec="seconds"),
        })

    def _ensure_runtime_state(self, entry: dict[str, Any]) -> None:
        entry.setdefault("并发窗口", [])
        entry.setdefault("当日调用", 0)
        entry.setdefault("统计日期", date.today().isoformat())
        entry.setdefault("审计轨迹", {})
        if not str(entry.get("审计单号") or "").strip():
            entry["审计单号"] = f"QAC-11{int(entry.get('id', 0)):02d}-05"

    def _record_audit(self, entry: dict[str, Any], event: str) -> None:
        if event not in AUDIT_EVENTS:
            return
        entry["审计轨迹"][event] = datetime.now().isoformat(timespec="seconds")

    def _sync_status_field(self, entry: dict[str, Any]) -> None:
        status = str(entry.get(STATUS_FIELD) or "").strip()
        if status in STATUS_ORDER:
            entry["status"] = status
        else:
            entry[STATUS_FIELD] = str(entry.get("status") or STATUS_ORDER[0])
