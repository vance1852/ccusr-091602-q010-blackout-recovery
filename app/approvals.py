"""人工改判与双人确认。

人工选择与自动建议一致：直接生效（来源 manual_consistent）。
不一致：生成确认单，须由与发起人不同的第二人确认后才生效
（来源 manual_dual_confirmed）。已执行动作对应的裁决不可改判。
"""
from __future__ import annotations

import uuid
from typing import Any, Optional

from .adjudication import get_decision
from .models import (
    DECISIONS,
    SOURCE_MANUAL_CONSISTENT,
    SOURCE_MANUAL_DUAL,
    ApprovalError,
    DomainError,
)
from .store import Store, dumps, loads

# 双人确认：发起人计一人，另需一名不同人员确认。
REQUIRED_DISTINCT_PEOPLE = 2


def override_decision(
    store: Store,
    ns: str,
    unit_id: str,
    manual_decision: str,
    user: str,
    at: str,
) -> dict[str, Any]:
    """人工改判入口；返回 {"status": "applied"|"pending", ...}。"""
    if manual_decision not in DECISIONS:
        raise DomainError(f"非法裁决类别: {manual_decision}")
    _ensure_not_executed(store, ns, unit_id)
    current = get_decision(store, ns, unit_id)
    if current is None:
        raise DomainError(f"单元 {unit_id} 尚无自动裁决，无法改判")

    if manual_decision == current.decision:
        _apply_decision(store, ns, unit_id, manual_decision,
                        SOURCE_MANUAL_CONSISTENT, at)
        return {"status": "applied", "unit_id": unit_id,
                "decision": manual_decision, "source": SOURCE_MANUAL_CONSISTENT}

    approval_id = f"APR-{uuid.uuid4().hex[:12]}"
    with store.tx():
        store.execute(
            "INSERT INTO approvals"
            "(ns, approval_id, unit_id, suggested, manual, requester,"
            " confirmations_json, status, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (ns, approval_id, unit_id, current.decision, manual_decision,
             user, dumps([user]), "pending", at),
        )
        store.emit(ns, "override_requested",
                   {"approval_id": approval_id, "unit_id": unit_id,
                    "suggested": current.decision, "manual": manual_decision,
                    "requester": user}, at)
    return {"status": "pending", "approval_id": approval_id,
            "unit_id": unit_id, "required_distinct_people":
            REQUIRED_DISTINCT_PEOPLE}


def confirm_override(
    store: Store, ns: str, approval_id: str, user: str, at: str
) -> dict[str, Any]:
    """第二人确认；确认人与发起人必须不同（双人确认）。"""
    with store.tx():
        row = store.one(
            "SELECT * FROM approvals WHERE ns=? AND approval_id=?",
            (ns, approval_id),
        )
        if row is None:
            raise ApprovalError(f"未知确认单: {approval_id}")
        if row["status"] != "pending":
            raise ApprovalError(f"确认单 {approval_id} 已处理")
        confirmations = loads(row["confirmations_json"], [])
        if user in confirmations:
            raise ApprovalError(
                "双人确认要求由不同人员完成，该用户已签署过本确认单"
            )
        confirmations.append(user)
        if len(set(confirmations)) < REQUIRED_DISTINCT_PEOPLE:
            store.execute(
                "UPDATE approvals SET confirmations_json=?"
                " WHERE ns=? AND approval_id=?",
                (dumps(confirmations), ns, approval_id),
            )
            return {"status": "pending", "approval_id": approval_id}
        _ensure_not_executed(store, ns, row["unit_id"])
        store.execute(
            "UPDATE approvals SET confirmations_json=?, status='confirmed',"
            " resolved_at=? WHERE ns=? AND approval_id=?",
            (dumps(confirmations), at, ns, approval_id),
        )
        store.emit(ns, "override_confirmed",
                   {"approval_id": approval_id, "unit_id": row["unit_id"],
                    "confirmations": confirmations}, at)
    _apply_decision(store, ns, row["unit_id"], row["manual"],
                    SOURCE_MANUAL_DUAL, at)
    return {"status": "confirmed", "approval_id": approval_id,
            "unit_id": row["unit_id"], "decision": row["manual"]}


def _apply_decision(
    store: Store, ns: str, unit_id: str, decision: str, source: str, at: str
) -> None:
    """写回裁决并同步计划（计划构建只重建未开始的动作）。"""
    from .planning import build_plan

    row = store.one(
        "SELECT checkpoint_json, rationale FROM decisions"
        " WHERE ns=? AND unit_id=?",
        (ns, unit_id),
    )
    rationale = (row["rationale"] if row else "") + f"｜人工改判为 {decision}（{source}）"
    with store.tx():
        store.execute(
            "UPDATE decisions SET decision=?, source=?, rationale=?,"
            " created_at=? WHERE ns=? AND unit_id=?",
            (decision, source, rationale, at, ns, unit_id),
        )
        store.emit(ns, "decision_made",
                   {"unit_id": unit_id, "decision": decision,
                    "source": source}, at)
    build_plan(store, ns, at)


def _ensure_not_executed(store: Store, ns: str, unit_id: str) -> None:
    row = store.one(
        "SELECT action_id, state FROM actions"
        " WHERE ns=? AND unit_id=? AND state IN"
        " ('executing','completed','review_required') LIMIT 1",
        (ns, unit_id),
    )
    if row:
        raise DomainError(
            f"单元 {unit_id} 的动作 {row['action_id']} 已执行，"
            "裁决不可改判；后续证据只会生成复核结论"
        )


def list_approvals(store: Store, ns: str) -> list[dict[str, Any]]:
    rows = store.query(
        "SELECT * FROM approvals WHERE ns=? ORDER BY created_at", (ns,)
    )
    for r in rows:
        r["confirmations"] = loads(r.pop("confirmations_json"), [])
    return rows
