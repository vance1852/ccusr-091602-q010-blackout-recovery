"""正式推进视图：阻塞链、物料账差、命令去重结果与各单元最终处置。"""
from __future__ import annotations

from typing import Any

from .models import LEVEL_PROCESSING
from .planning import blocking_chain, list_actions
from .store import Store, loads


def material_account(store: Store, ns: str) -> dict[str, dict[str, Any]]:
    """物料账：快照预期 vs 实物扫描 vs 恢复期间移动，给出账差。

    覆盖快照、扫描、移动任一来源出现的单元；缺快照时预期为 None，
    账差无法计算但实物与移动仍可见。
    """
    snaps = {
        r["unit_id"]: r
        for r in store.query("SELECT * FROM snapshots WHERE ns=?", (ns,))
    }
    scans = {
        r["unit_id"]: r
        for r in store.query("SELECT * FROM material_scans WHERE ns=?", (ns,))
    }
    moves: dict[str, float] = {}
    for m in store.query("SELECT * FROM material_movements WHERE ns=?", (ns,)):
        moves[m["unit_id"]] = moves.get(m["unit_id"], 0.0) + m["delta"]

    account: dict[str, dict[str, Any]] = {}
    for uid in sorted(set(snaps) | set(scans) | set(moves)):
        expected = snaps.get(uid, {}).get("expected_qty")
        scanned = scans.get(uid, {}).get("scanned_qty")
        moved = round(moves.get(uid, 0.0), 6)
        scan_diff = (
            round(scanned - expected, 6)
            if scanned is not None and expected is not None
            else None
        )
        current_diff = (
            round(scanned + moved - expected, 6)
            if scanned is not None and expected is not None
            else None
        )
        account[uid] = {
            "expected": expected,
            "scanned": scanned,
            "moved": moved,
            "scan_diff": scan_diff,
            "current_diff": current_diff,
        }
    return account


def command_summary(store: Store, ns: str) -> dict[str, Any]:
    """命令去重结果：四类结果计数 + 每次提交的明细（含被拒绝的）。"""
    rows = store.query(
        "SELECT business_key, unit_id, verb, result, detail, at"
        " FROM command_attempts WHERE ns=? ORDER BY seq",
        (ns,),
    )
    counts = {"accepted": 0, "duplicate": 0, "stale": 0, "dependency_blocked": 0}
    for r in rows:
        counts[r["result"]] = counts.get(r["result"], 0) + 1
    return {"counts": counts, "details": rows}


def _disposition(decision: str | None, verb: str | None, state: str | None) -> str:
    if decision is None:
        return "pending"
    if state == "completed":
        return {
            "resume_operation": "resumed",
            "upload_receipt": "receipt_uploaded",
            "scrap": "scrapped",
            "inspect": "inspected",
        }.get(verb or "", "completed")
    if state == "review_required":
        return "under_review"
    if verb == "hold":
        return "held_insufficient_evidence"
    return "pending"


def progression_report(store: Store, ns: str) -> dict[str, Any]:
    """恢复经理的正式推进视图。"""
    actions = list_actions(store, ns)
    decisions = {
        r["unit_id"]: r
        for r in store.query("SELECT * FROM decisions WHERE ns=?", (ns,))
    }

    units: dict[str, Any] = {}
    for uid, dec in decisions.items():
        act = next(
            (a for a in actions
             if a["unit_id"] == uid and a["level"] == LEVEL_PROCESSING),
            None,
        )
        cp = loads(dec["checkpoint_json"]) if dec["checkpoint_json"] else None
        units[uid] = {
            "decision": dec["decision"],
            "source": dec["source"],
            "checkpoint": cp["checkpoint_id"] if cp else None,
            "rationale": dec["rationale"],
            "action_id": act["action_id"] if act else None,
            "action_state": act["state"] if act else None,
            "disposition": _disposition(
                dec["decision"], act["verb"] if act else None,
                act["state"] if act else None,
            ),
        }

    chains = {}
    for a in actions:
        if a["state"] != "completed":
            chain = blocking_chain(store, ns, a["action_id"])
            if chain:
                chains[a["action_id"]] = chain

    reviews = store.query(
        "SELECT * FROM reviews WHERE ns=? ORDER BY created_at", (ns,)
    )
    outage = store.one(
        "SELECT value FROM meta WHERE ns=? AND key='outage_id'", (ns,)
    )
    return {
        "namespace": ns,
        "outage_id": outage["value"] if outage else None,
        "units": units,
        "blocking_chains": chains,
        "material_account": material_account(store, ns),
        "commands": command_summary(store, ns),
        "reviews": reviews,
        "actions": [
            {
                "action_id": a["action_id"],
                "verb": a["verb"],
                "level": a["level"],
                "state": a["state"],
                "blocked_reason": a["blocked_reason"],
            }
            for a in actions
        ],
    }
