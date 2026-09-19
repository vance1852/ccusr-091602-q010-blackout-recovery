"""裁决规则：基于证据包给出每个在制单元的处置结论。

规则是纯函数：相同证据包必然得到相同结论。每条结论都必须解释
所采用的最后可信检查点（last trusted checkpoint）及采信理由，
使恢复经理能审计“为什么从这里续跑”。
"""

from __future__ import annotations

from typing import Any

from .models import (
    Checkpoint,
    Decision,
    DecisionRecord,
    EvidencePackage,
    UnitCategory,
)


def _normalize(materials: dict[str, Any] | None) -> dict[str, float]:
    return {m: q for m, q in (materials or {}).items() if q}


def _materials_match(actual: dict[str, Any] | None, book: dict[str, Any] | None) -> bool:
    return _normalize(actual) == _normalize(book)


def _deltas(scanned: dict[str, Any] | None, book: dict[str, Any] | None) -> dict[str, float]:
    scanned_n, book_n = _normalize(scanned), _normalize(book)
    keys = set(scanned_n) | set(book_n)
    return {
        k: scanned_n.get(k, 0) - book_n.get(k, 0)
        for k in sorted(keys)
        if scanned_n.get(k, 0) != book_n.get(k, 0)
    }


def _account(book: dict | None, scanned: dict | None) -> dict[str, Any]:
    return {
        "book": _normalize(book),
        "scanned": _normalize(scanned),
        "deltas": _deltas(scanned, book),
    }


def _snapshot_checkpoint(snapshot: dict[str, Any]) -> Checkpoint:
    return Checkpoint(
        seq=int(snapshot.get("checkpoint_seq", 0)),
        checkpoint_id=snapshot.get("checkpoint_id", "snapshot"),
        operation=snapshot.get("operation", ""),
        step=snapshot.get("step", ""),
        state=snapshot.get("state", ""),
        source="snapshot",
        recorded_at=snapshot.get("captured_at", ""),
    )


def _next_operation(profile: dict[str, Any], operation: str) -> str | None:
    order = (profile or {}).get("operation_order", [])
    if operation in order:
        idx = order.index(operation)
        if idx + 1 < len(order):
            return order[idx + 1]
    return None


def adjudicate_unit(pkg: EvidencePackage, profile: dict[str, Any] | None = None) -> DecisionRecord:
    """对单个单元的证据包作出裁决。"""
    if pkg.category in (UnitCategory.UTILITY, UnitCategory.CONVEYOR):
        return _adjudicate_infrastructure(pkg)
    return _adjudicate_processing(pkg, profile or {})


def _adjudicate_infrastructure(pkg: EvidencePackage) -> DecisionRecord:
    """公用工程 / 输送单元：无物料账，依据快照与启动报告裁决。"""
    unit = pkg.unit_id
    missing = []
    if pkg.snapshot is None:
        missing.append("snapshot")
    if pkg.boot_report is None:
        missing.append("boot_report")
    checkpoint = _snapshot_checkpoint(pkg.snapshot) if pkg.snapshot else None
    if missing:
        return DecisionRecord(
            unit,
            Decision.INSUFFICIENT_EVIDENCE,
            checkpoint,
            [f"缺少证据来源: {', '.join(missing)}，无法裁决"],
            _account(None, None),
            missing,
            None,
        )
    boot = pkg.boot_report
    if boot.get("physical_damage"):
        return DecisionRecord(
            unit,
            Decision.INSPECTION_REQUIRED,
            checkpoint,
            ["启动报告标记物理损伤，需检修评估后才能恢复"],
            _account(None, None),
            [],
            None,
        )
    if boot.get("controller_state") == "standby" and not boot.get("errors"):
        return DecisionRecord(
            unit,
            Decision.AUTO_RESUME,
            checkpoint,
            [
                "控制器正常回到待机，与快照状态一致，可自动恢复",
                f"最后可信检查点: {checkpoint.checkpoint_id} (snapshot)",
            ],
            _account(None, None),
            [],
            None,
        )
    return DecisionRecord(
        unit,
        Decision.INSPECTION_REQUIRED,
        checkpoint,
        ["控制器重启后状态异常，需人工检查"],
        _account(None, None),
        [],
        None,
    )


def _adjudicate_processing(pkg: EvidencePackage, profile: dict[str, Any]) -> DecisionRecord:
    """加工单元：对账快照、缓存完成回执与物料扫描后裁决。"""
    unit = pkg.unit_id
    missing = []
    if pkg.snapshot is None:
        missing.append("snapshot")
    if pkg.boot_report is None:
        missing.append("boot_report")
    if pkg.material_scan is None:
        missing.append("material_scan")
    checkpoint = _snapshot_checkpoint(pkg.snapshot) if pkg.snapshot else None
    scanned = (pkg.material_scan or {}).get("materials")
    if missing:
        return DecisionRecord(
            unit,
            Decision.INSUFFICIENT_EVIDENCE,
            checkpoint,
            [f"缺少证据来源: {', '.join(missing)}，无法裁决"],
            _account(None, scanned),
            missing,
            None,
        )

    snap, boot = pkg.snapshot, pkg.boot_report
    scanned = pkg.material_scan.get("materials", {})
    S = _snapshot_checkpoint(snap)
    book_S = snap.get("expected_materials", {})

    if boot.get("physical_damage"):
        return DecisionRecord(
            unit,
            Decision.SCRAP,
            S,
            [
                "启动报告标记物理损伤，在制品必须报废",
                f"最后可信检查点: {S.checkpoint_id} (snapshot)",
            ],
            _account(book_S, scanned),
            [],
            None,
        )

    # 人工检查结论（恢复过程中补充的证据）优先于自动推断。
    if pkg.inspections:
        latest = pkg.inspections[-1]
        if latest.get("result") == "pass":
            step = "process" if S.state in ("fed", "executing") else "feed"
            return DecisionRecord(
                unit,
                Decision.AUTO_RESUME,
                S,
                [
                    "人工检查通过，按检查结论从最后可信检查点续作",
                    f"最后可信检查点: {S.checkpoint_id} (snapshot)",
                ],
                _account(book_S, scanned),
                [],
                {"operation": S.operation, "step": step},
            )
        if latest.get("result") == "fail":
            return DecisionRecord(
                unit,
                Decision.SCRAP,
                S,
                [
                    "人工检查不通过，按检查结论报废",
                    f"最后可信检查点: {S.checkpoint_id} (snapshot)",
                ],
                _account(book_S, scanned),
                [],
                None,
            )

    # 缓存的完成回执：断电前已本地完成、尚未上传 MES。
    receipts = [
        r
        for r in pkg.command_logs
        if r.get("command_type") == "completion_receipt"
        and r.get("operation") == S.operation
        and int(r.get("checkpoint_seq", 0)) > S.seq
    ]
    receipt = max(receipts, key=lambda r: int(r.get("checkpoint_seq", 0))) if receipts else None

    prefix: list[str] = []
    trusted = S
    book = book_S
    operation_completed = S.state == "completed"

    if receipt is not None:
        C = Checkpoint(
            seq=int(receipt["checkpoint_seq"]),
            checkpoint_id=receipt.get("business_key", "completion-receipt"),
            operation=S.operation,
            step="completed",
            state="completed",
            source="command_log",
            recorded_at=receipt.get("completed_at") or receipt.get("issued_at", ""),
        )
        consumed = receipt.get("consumed", {})
        book_C = {
            m: book_S.get(m, 0) - consumed.get(m, 0) for m in set(book_S) | set(consumed)
        }
        if _materials_match(scanned, book_C):
            # 回执经物料账证实：工序其实在断电前已完成。
            trusted, book, operation_completed = C, book_C, True
            prefix.append("缓存完成回执与物料账一致，采信回执作为最后可信检查点")
        elif _materials_match(scanned, book_S):
            # 物料未消耗：回执是幻影，回退到快照检查点。
            prefix.append("缓存完成回执与物料账矛盾（物料未消耗），丢弃回执并回退到快照检查点")
        else:
            return DecisionRecord(
                unit,
                Decision.INSPECTION_REQUIRED,
                S,
                [
                    "物料账与快照、完成回执均不一致，存在账差，需人工检查",
                    f"最后可信检查点: {S.checkpoint_id} (snapshot)",
                ],
                _account(book_S, scanned),
                [],
                None,
            )

    op_profile = (profile.get("operations") or {}).get(S.operation, {})
    outage_s = int(boot.get("outage_duration_s", 0))
    max_interruption = int(op_profile.get("max_interruption_s", 0))

    if (
        not operation_completed
        and op_profile.get("interruption_critical")
        and outage_s > max_interruption
    ):
        return DecisionRecord(
            unit,
            Decision.SCRAP,
            trusted,
            prefix
            + [
                f"断电 {outage_s}s 超过工序允许中断 {max_interruption}s，在制品降级，必须报废",
                f"最后可信检查点: {trusted.checkpoint_id} ({trusted.source})",
            ],
            _account(book, scanned),
            [],
            None,
        )

    if not _materials_match(scanned, book):
        return DecisionRecord(
            unit,
            Decision.INSPECTION_REQUIRED,
            trusted,
            prefix
            + [
                "物料账与最后可信检查点不一致，存在账差，需人工检查",
                f"最后可信检查点: {trusted.checkpoint_id} ({trusted.source})",
            ],
            _account(book, scanned),
            [],
            None,
        )

    if operation_completed:
        next_op = _next_operation(profile, S.operation)
        resume = (
            {"operation": next_op, "step": "feed"}
            if next_op
            else {"operation": None, "step": "done"}
        )
        return DecisionRecord(
            unit,
            Decision.AUTO_RESUME,
            trusted,
            prefix
            + [
                "工序在断电前已完成且经物料账确认，从下一工序续作",
                f"最后可信检查点: {trusted.checkpoint_id} ({trusted.source})",
            ],
            _account(book, scanned),
            [],
            resume,
        )

    if op_profile.get("quality_critical") and S.state == "executing":
        return DecisionRecord(
            unit,
            Decision.INSPECTION_REQUIRED,
            trusted,
            prefix
            + [
                "质量关键工序在步骤执行中被中断，需人工检查",
                f"最后可信检查点: {trusted.checkpoint_id} ({trusted.source})",
            ],
            _account(book, scanned),
            [],
            None,
        )

    if boot.get("retained_operation") and boot["retained_operation"] != S.operation:
        return DecisionRecord(
            unit,
            Decision.INSPECTION_REQUIRED,
            trusted,
            prefix
            + [
                f"控制器保留状态({boot['retained_operation']})与快照({S.operation})冲突，需人工检查",
                f"最后可信检查点: {trusted.checkpoint_id} ({trusted.source})",
            ],
            _account(book, scanned),
            [],
            None,
        )

    step = "process" if S.state in ("fed", "executing") else "feed"
    return DecisionRecord(
        unit,
        Decision.AUTO_RESUME,
        trusted,
        prefix
        + [
            "快照、启动报告与物料账一致，可从最后可信检查点自动续作",
            f"最后可信检查点: {trusted.checkpoint_id} ({trusted.source})",
        ],
        _account(book, scanned),
        [],
        {"operation": S.operation, "step": step},
    )
