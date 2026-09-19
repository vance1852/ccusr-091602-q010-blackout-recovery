"""恢复计划：按依赖层级（公用工程 -> 输送 -> 加工）生成动作并维护阻塞链。

计划构建是幂等的：已执行/执行中的动作不可触碰；未开始的动作可随
裁决更新（如双人确认后的改判）重建。
"""
from __future__ import annotations

from typing import Any, Optional

from .models import (
    BLOCKED_DEPENDENCY,
    BLOCKED_EVIDENCE,
    LEVEL_CONVEYING,
    LEVEL_PROCESSING,
    LEVEL_UTILITY,
)
from .store import Store, dumps, loads

# 裁决 -> 恢复动作动词。
DECISION_VERBS = {
    "auto_resume": None,  # 依快照状态细分
    "inspection_required": "inspect",
    "scrap": "scrap",
    "insufficient_evidence": "hold",
}

TERMINAL_STATES = ("executing", "completed", "review_required")


def utility_action_id(utility_id: str) -> str:
    return f"act-restore_utility-{utility_id}"


def conveying_action_id(line_id: str) -> str:
    return f"act-restore_conveying-{line_id}"


def unit_action_id(verb: str, unit_id: str) -> str:
    return f"act-{verb}-{unit_id}"


def _processing_verb(decision: str, snapshot_state: Optional[str]) -> str:
    if decision == "auto_resume":
        return "upload_receipt" if snapshot_state == "completed" else "resume_operation"
    return DECISION_VERBS[decision]


def build_plan(store: Store, ns: str, at: str) -> list[dict[str, Any]]:
    """根据当前裁决构建/更新恢复计划，返回全部动作。"""
    with store.tx():
        _ensure_infra_actions(store, ns, at)
        _ensure_processing_actions(store, ns, at)
        refresh_states(store, ns, at)
        store.emit(ns, "plan_built", {"actions": _count_actions(store, ns)}, at)
    return list_actions(store, ns)


def _count_actions(store: Store, ns: str) -> int:
    return store.one("SELECT COUNT(*) AS n FROM actions WHERE ns=?", (ns,))["n"]


def _insert_action(
    store: Store,
    ns: str,
    action_id: str,
    unit_id: Optional[str],
    level: int,
    verb: str,
    state: str,
    blocked_reason: Optional[str],
    depends: list[str],
    basis: Optional[dict],
    at: str,
) -> None:
    store.execute(
        "INSERT OR IGNORE INTO actions"
        "(ns, action_id, unit_id, level, verb, state, blocked_reason,"
        " depends_json, decision_basis_json, created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            ns, action_id, unit_id, level, verb, state, blocked_reason,
            dumps(depends), dumps(basis) if basis else None, at,
        ),
    )


def _ensure_infra_actions(store: Store, ns: str, at: str) -> None:
    utilities = store.query(
        "SELECT * FROM unit_registry WHERE ns=? AND kind='utility' ORDER BY unit_id",
        (ns,),
    )
    for u in utilities:
        _insert_action(
            store, ns, utility_action_id(u["unit_id"]), u["unit_id"],
            LEVEL_UTILITY, "restore_utility", "ready", None, [], None, at,
        )
    lines = store.query(
        "SELECT * FROM unit_registry WHERE ns=? AND kind='conveying'"
        " ORDER BY unit_id",
        (ns,),
    )
    for line in lines:
        deps = [utility_action_id(line["utility_id"])] if line["utility_id"] else []
        _insert_action(
            store, ns, conveying_action_id(line["unit_id"]), line["unit_id"],
            LEVEL_CONVEYING, "restore_conveying",
            "blocked" if deps else "ready",
            BLOCKED_DEPENDENCY if deps else None,
            deps, None, at,
        )


def _ensure_processing_actions(store: Store, ns: str, at: str) -> None:
    from .evidence import build_package, list_processing_units

    for unit in list_processing_units(store, ns):
        uid = unit["unit_id"]
        dec_row = store.one(
            "SELECT * FROM decisions WHERE ns=? AND unit_id=?", (ns, uid)
        )
        if dec_row is None:
            continue
        pkg = build_package(store, ns, uid)
        snapshot_state = pkg.snapshot["state"] if pkg.snapshot else None
        verb = _processing_verb(dec_row["decision"], snapshot_state)
        action_id = unit_action_id(verb, uid)

        # 裁决变化时，仅重建尚未开始的动作；已执行/执行中的保持不动。
        stale = store.query(
            "SELECT action_id, state FROM actions"
            " WHERE ns=? AND unit_id=? AND level=? AND action_id!=?",
            (ns, uid, LEVEL_PROCESSING, action_id),
        )
        for row in stale:
            if row["state"] in TERMINAL_STATES:
                continue
            store.execute(
                "DELETE FROM actions WHERE ns=? AND action_id=?",
                (ns, row["action_id"]),
            )
            store.emit(
                ns, "action_replaced",
                {"action_id": row["action_id"], "reason": "decision_updated"}, at,
            )

        deps: list[str] = []
        if unit["utility_id"]:
            deps.append(utility_action_id(unit["utility_id"]))
        if unit["line_id"]:
            deps.append(conveying_action_id(unit["line_id"]))

        if verb == "hold":
            state, reason = "blocked", BLOCKED_EVIDENCE
        elif deps:
            state, reason = "blocked", BLOCKED_DEPENDENCY
        else:
            state, reason = "ready", None

        basis = {
            "decision": dec_row["decision"],
            "checkpoint_id": _basis_checkpoint(dec_row["checkpoint_json"]),
            "source": dec_row["source"],
        }
        _insert_action(
            store, ns, action_id, uid, LEVEL_PROCESSING, verb,
            state, reason, deps, basis, at,
        )
        # 已存在的未开始动作：同步最新裁决依据（如双人确认后的来源）。
        store.execute(
            "UPDATE actions SET decision_basis_json=?"
            " WHERE ns=? AND action_id=? AND state NOT IN"
            " ('executing','completed','review_required')",
            (dumps(basis), ns, action_id),
        )


def _basis_checkpoint(checkpoint_json: Optional[str]) -> Optional[str]:
    cp = loads(checkpoint_json)
    return cp["checkpoint_id"] if cp else None


def refresh_states(store: Store, ns: str, at: str) -> None:
    """依赖全部完成的动作从 blocked 转为 ready（证据阻塞除外）。"""
    rows = store.query(
        "SELECT action_id, depends_json FROM actions"
        " WHERE ns=? AND state='blocked' AND blocked_reason=?",
        (ns, BLOCKED_DEPENDENCY),
    )
    for row in rows:
        deps = loads(row["depends_json"], [])
        done = all(
            (store.one(
                "SELECT state FROM actions WHERE ns=? AND action_id=?",
                (ns, dep),
            ) or {}).get("state")
            == "completed"
            for dep in deps
        )
        if done:
            store.execute(
                "UPDATE actions SET state='ready', blocked_reason=NULL"
                " WHERE ns=? AND action_id=?",
                (ns, row["action_id"]),
            )
            store.emit(
                ns, "action_state",
                {"action_id": row["action_id"], "state": "ready"}, at,
            )


def get_action(store: Store, ns: str, action_id: str) -> Optional[dict[str, Any]]:
    row = store.one(
        "SELECT * FROM actions WHERE ns=? AND action_id=?", (ns, action_id)
    )
    if row:
        row["depends"] = loads(row.pop("depends_json"), [])
        row["decision_basis"] = loads(row.pop("decision_basis_json"), None)
    return row


def list_actions(store: Store, ns: str) -> list[dict[str, Any]]:
    rows = store.query(
        "SELECT * FROM actions WHERE ns=? ORDER BY level, action_id", (ns,)
    )
    for r in rows:
        r["depends"] = loads(r.pop("depends_json"), [])
        r["decision_basis"] = loads(r.pop("decision_basis_json"), None)
    return rows


def blocking_chain(store: Store, ns: str, action_id: str) -> list[dict[str, Any]]:
    """返回该动作所有未完成的传递依赖（拓扑序），即阻塞链。"""
    chain: list[dict[str, Any]] = []
    seen: set[str] = set()

    def walk(aid: str) -> None:
        if aid in seen:
            return
        seen.add(aid)
        row = get_action(store, ns, aid)
        if row is None or row["state"] == "completed":
            return
        for dep in row["depends"]:
            walk(dep)
        chain.append(
            {
                "action_id": aid,
                "verb": row["verb"],
                "state": row["state"],
                "blocked_reason": row["blocked_reason"],
            }
        )

    row = get_action(store, ns, action_id)
    if row is None:
        return []
    for dep in row["depends"]:
        walk(dep)
    if row["state"] == "blocked" and row["blocked_reason"] == BLOCKED_EVIDENCE:
        chain.append(
            {
                "action_id": action_id,
                "verb": row["verb"],
                "state": row["state"],
                "blocked_reason": row["blocked_reason"],
            }
        )
    return chain
