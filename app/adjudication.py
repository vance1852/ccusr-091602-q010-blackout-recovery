"""裁决规则引擎。

先计算"最后可信检查点"（快照给出基准，启动报告/物料扫描可压低或
标记不可信），再按有序规则得出四分类结论。每条结论都带中文解释，
说明采信了哪个检查点、被哪些证据确认或压低。
"""
from __future__ import annotations

from typing import Optional

from .models import (
    SCRAP_FAULTS,
    Decision,
    EvidencePackage,
    TrustedCheckpoint,
)
from .store import Store, dumps, loads


def last_trusted_checkpoint(pkg: EvidencePackage) -> Optional[TrustedCheckpoint]:
    """计算最后可信检查点。

    基准来自停电前快照；启动报告有故障或"断电时仍在工序中段"矛盾时，
    可信度被压低一个检查点；物料账差不压低检查点，但标记物料未核实。
    """
    snap = pkg.snapshot
    if snap is None:
        return None
    route = pkg.route or []
    cp_id = snap["checkpoint_id"]
    if cp_id in route:
        base_seq = route.index(cp_id)
    else:
        base_seq = 0
    confirmed_by = [snap["evidence_id"]]
    notes: list[str] = []
    capped_by: Optional[str] = None
    seq = base_seq

    boot = pkg.boot_report
    if boot is None:
        # 缺少启动报告：只采信路线起点，等待更多证据。
        seq = 0
        notes.append("缺少设备启动报告，可信度压至路线起点")
    else:
        confirmed_by.append(boot["evidence_id"])
        faults = set(boot.get("faults") or [])
        if faults and seq > 0:
            seq -= 1
            capped_by = boot["evidence_id"]
            notes.append(
                f"启动报告存在故障 {sorted(faults)}，最后一段设备动作未核实，"
                "可信度压低一个检查点"
            )
        elif pkg.flags.get("mid_operation_conflict") and seq > 0:
            seq -= 1
            capped_by = boot["evidence_id"]
            notes.append(
                "快照称工序已完成但启动报告称断电时仍在工序中段，"
                "可信度压低一个检查点"
            )

    material_verified = True
    scan = pkg.scan
    if scan is not None:
        confirmed_by.append(scan["evidence_id"])
        if pkg.flags.get("scan_diff"):
            material_verified = False
            notes.append("物料扫描与快照账存不一致，物料状态未核实")

    cp_id = route[seq] if route else snap["checkpoint_id"]
    return TrustedCheckpoint(
        device_id=snap["device_id"],
        operation_id=snap["operation_id"],
        checkpoint_id=cp_id,
        seq=seq,
        confirmed_by=confirmed_by,
        capped_by=capped_by,
        material_verified=material_verified,
        notes=notes,
    )


def _cp_text(cp: Optional[TrustedCheckpoint]) -> str:
    if cp is None:
        return "无可用检查点（缺少停电前快照）"
    src = "、".join(cp.confirmed_by) or "无"
    text = (
        f"最后可信检查点 {cp.checkpoint_id}"
        f"（设备 {cp.device_id}/工序 {cp.operation_id}，证据 {src}）"
    )
    if cp.capped_by:
        text += f"，可信度被 {cp.capped_by} 压低"
    return text


def adjudicate_package(pkg: EvidencePackage) -> Decision:
    """对单个证据包运行有序规则，产出裁决结论。"""
    uid = pkg.unit_id
    cp = last_trusted_checkpoint(pkg)
    evidence_ids = list(cp.confirmed_by) if cp else []
    cp_text = _cp_text(cp)

    def decide(decision: str, reason: str) -> Decision:
        return Decision(
            unit_id=uid,
            decision=decision,
            checkpoint=cp,
            rationale=f"{reason}；{cp_text}",
            evidence_ids=evidence_ids,
        )

    snap, boot, scan = pkg.snapshot, pkg.boot_report, pkg.scan

    # 1. 证据完整性：缺任一类关键证据都无法自动定论。
    if snap is None:
        return decide("insufficient_evidence", "缺少停电前快照，无法确定工序位置")
    if boot is None:
        return decide("insufficient_evidence", "缺少设备启动报告，控制器状态未知")
    if scan is None:
        return decide("insufficient_evidence", "缺少物料扫描，账存无法核实")

    faults = set(boot.get("faults") or [])

    # 2. 报废：暴露超限（敏感物料）或污染/安全类故障。
    if boot["excursion"] and pkg.unit.get("exposure_sensitive"):
        return decide(
            "scrap",
            "断电期间环境暴露超限且单元为暴露敏感物料，继续加工存在质量风险",
        )
    fatal = sorted(faults & SCRAP_FAULTS)
    if fatal:
        return decide("scrap", f"启动报告含必须报废的故障 {fatal}")

    # 3. 需要检查：账差不符、非致命故障、暴露超限（非敏感物料）。
    diff = pkg.flags.get("scan_diff")
    if diff:
        return decide(
            "inspection_required",
            f"物料账差 {diff:+g}，实物与账存不一致，须人工清点核对",
        )
    if boot["excursion"]:
        return decide(
            "inspection_required",
            "断电期间出现环境暴露超限（非敏感物料），须检查确认质量",
        )
    if faults:
        return decide(
            "inspection_required",
            f"启动报告含非致命故障 {sorted(faults)}，须检查设备与在制品",
        )

    # 4. 可自动续作：状态一致且物料账实相符。
    if snap["state"] == "completed":
        receipt = "完成回执缓存未上传，续作动作仅需补传回执" if pkg.flags.get(
            "receipt_pending"
        ) else "工序已完成"
        return decide("auto_resume", f"{receipt}，账实相符")
    if (
        snap["state"] == "in_progress"
        and boot["boot_state"] == "standby"
        and not pkg.flags.get("mid_operation_conflict")
    ):
        return decide(
            "auto_resume",
            "MES 工序中断点、控制器待机状态与物料扫描三方一致，"
            "可自最后可信检查点续作",
        )

    # 5. 其余组合无法采信。
    return decide(
        "insufficient_evidence",
        f"快照状态 {snap['state']} 与控制器状态 {boot['boot_state']} "
        "的组合无法采信",
    )


def adjudicate_all(store: Store, ns: str, at: str) -> dict[str, Decision]:
    """对全部在制单元裁决并落库（保留人工裁决的来源标记）。"""
    from .evidence import build_package, list_processing_units

    results: dict[str, Decision] = {}
    units = list_processing_units(store, ns)
    with store.tx():
        for unit in units:
            pkg = build_package(store, ns, unit["unit_id"])
            dec = adjudicate_package(pkg)
            old = store.one(
                "SELECT source FROM decisions WHERE ns=? AND unit_id=?",
                (ns, dec.unit_id),
            )
            source = old["source"] if old and old["source"] != "auto" else dec.source
            store.execute(
                "INSERT OR REPLACE INTO decisions"
                "(ns, unit_id, decision, checkpoint_json, rationale, source,"
                " created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    ns,
                    dec.unit_id,
                    dec.decision,
                    dumps(dec.checkpoint.to_dict() if dec.checkpoint else None),
                    dec.rationale,
                    source,
                    at,
                ),
            )
            store.emit(
                ns,
                "decision_made",
                {
                    "unit_id": dec.unit_id,
                    "decision": dec.decision,
                    "checkpoint": dec.checkpoint.checkpoint_id
                    if dec.checkpoint
                    else None,
                    "source": source,
                },
                at,
            )
            dec.source = source
            results[dec.unit_id] = dec
    return results


def get_decision(store: Store, ns: str, unit_id: str) -> Optional[Decision]:
    row = store.one(
        "SELECT * FROM decisions WHERE ns=? AND unit_id=?", (ns, unit_id)
    )
    if row is None:
        return None
    cp_raw = loads(row["checkpoint_json"])
    return Decision(
        unit_id=unit_id,
        decision=row["decision"],
        checkpoint=TrustedCheckpoint.from_dict(cp_raw) if cp_raw else None,
        rationale=row["rationale"],
        source=row["source"],
    )
