"""证据采集与证据包组装。

四类证据：停电前快照、设备启动报告、物料扫描、命令日志。
每个在制单元汇成一个证据包，并标注跨源矛盾（如 MES 仍在执行、
控制器已待机、完成回执缓存未上传）。
"""
from __future__ import annotations

import uuid
from typing import Any, Optional

from .models import DEFAULT_ROUTE, EvidencePackage
from .store import Store, loads


def _eid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def register_unit(
    store: Store,
    ns: str,
    unit_id: str,
    kind: str,
    device_id: Optional[str] = None,
    line_id: Optional[str] = None,
    utility_id: Optional[str] = None,
    recipe_qty: float = 0.0,
    exposure_sensitive: bool = False,
    at: str = "",
) -> None:
    with store.tx():
        store.execute(
            "INSERT OR REPLACE INTO unit_registry"
            "(ns, unit_id, kind, device_id, line_id, utility_id,"
            " recipe_qty, exposure_sensitive) VALUES(?,?,?,?,?,?,?,?)",
            (
                ns,
                unit_id,
                kind,
                device_id,
                line_id,
                utility_id,
                recipe_qty,
                1 if exposure_sensitive else 0,
            ),
        )
        store.emit(ns, "unit_registered", {"unit_id": unit_id, "kind": kind}, at)


def define_route(
    store: Store,
    ns: str,
    device_id: str,
    operation_id: str,
    checkpoints: list[str],
    at: str = "",
) -> None:
    """定义设备+工序的检查点序列（检查点按设备与工序定义）。"""
    with store.tx():
        store.execute(
            "DELETE FROM routes WHERE ns=? AND device_id=? AND operation_id=?",
            (ns, device_id, operation_id),
        )
        for seq, cp in enumerate(checkpoints):
            store.execute(
                "INSERT INTO routes(ns, device_id, operation_id, seq, checkpoint_id)"
                " VALUES(?,?,?,?,?)",
                (ns, device_id, operation_id, seq, cp),
            )
        store.emit(
            ns,
            "route_defined",
            {"device_id": device_id, "operation_id": operation_id,
             "checkpoints": list(checkpoints)},
            at,
        )


def ingest_snapshot(
    store: Store,
    ns: str,
    unit_id: str,
    device_id: str,
    operation_id: str,
    checkpoint_id: str,
    state: str,
    expected_qty: float,
    recorded_at: str,
    meta: Optional[dict] = None,
    at: str = "",
) -> str:
    """登记停电前快照（每单元保留最新一份，历史进审计事件）。"""
    eid = _eid("SNAP")
    with store.tx():
        store.execute(
            "INSERT OR REPLACE INTO snapshots"
            "(ns, unit_id, evidence_id, device_id, operation_id, checkpoint_id,"
            " state, expected_qty, recorded_at, meta_json)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                ns, unit_id, eid, device_id, operation_id, checkpoint_id,
                state, expected_qty, recorded_at,
                _json(meta),
            ),
        )
        store.emit(ns, "evidence_ingested",
                   {"kind": "snapshot", "unit_id": unit_id, "evidence_id": eid}, at)
    return eid


def ingest_boot_report(
    store: Store,
    ns: str,
    device_id: str,
    boot_state: str,
    faults: Optional[list[str]] = None,
    excursion: bool = False,
    mid_operation: bool = False,
    restarted_at: str = "",
    at: str = "",
) -> str:
    """登记设备启动报告（每设备保留最新一份）。"""
    eid = _eid("BOOT")
    with store.tx():
        store.execute(
            "INSERT OR REPLACE INTO boot_reports"
            "(ns, device_id, evidence_id, boot_state, faults_json, excursion,"
            " mid_operation, restarted_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                ns, device_id, eid, boot_state, _json(faults or []),
                1 if excursion else 0, 1 if mid_operation else 0, restarted_at,
            ),
        )
        store.emit(ns, "evidence_ingested",
                   {"kind": "boot_report", "device_id": device_id,
                    "evidence_id": eid}, at)
    return eid


def ingest_material_scan(
    store: Store,
    ns: str,
    unit_id: str,
    scanned_qty: float,
    location: str = "",
    scanned_at: str = "",
    at: str = "",
) -> str:
    """登记物料扫描（每单元保留最新一份）。"""
    eid = _eid("SCAN")
    with store.tx():
        store.execute(
            "INSERT OR REPLACE INTO material_scans"
            "(ns, unit_id, evidence_id, scanned_qty, location, scanned_at)"
            " VALUES(?,?,?,?,?,?)",
            (ns, unit_id, eid, scanned_qty, location, scanned_at),
        )
        store.emit(ns, "evidence_ingested",
                   {"kind": "material_scan", "unit_id": unit_id,
                    "evidence_id": eid}, at)
    return eid


def ingest_command_log(
    store: Store,
    ns: str,
    business_key: str,
    unit_id: str,
    verb: str,
    payload: Optional[dict] = None,
    issued_at: str = "",
    status: str = "issued",
    at: str = "",
) -> str:
    """登记停电前命令日志（status: issued/acked/cached）。"""
    eid = _eid("CMDLOG")
    with store.tx():
        store.execute(
            "INSERT OR REPLACE INTO command_logs"
            "(ns, business_key, evidence_id, unit_id, verb, payload_json,"
            " issued_at, status) VALUES(?,?,?,?,?,?,?,?)",
            (ns, business_key, eid, unit_id, verb, _json(payload or {}),
             issued_at, status),
        )
        store.emit(ns, "evidence_ingested",
                   {"kind": "command_log", "business_key": business_key,
                    "evidence_id": eid}, at)
    return eid


def get_route(
    store: Store, ns: str, device_id: Optional[str], operation_id: Optional[str]
) -> list[str]:
    if not device_id or not operation_id:
        return list(DEFAULT_ROUTE)
    rows = store.query(
        "SELECT checkpoint_id FROM routes"
        " WHERE ns=? AND device_id=? AND operation_id=? ORDER BY seq",
        (ns, device_id, operation_id),
    )
    return [r["checkpoint_id"] for r in rows] or list(DEFAULT_ROUTE)


def build_package(store: Store, ns: str, unit_id: str) -> EvidencePackage:
    """汇集四类证据为单个在制单元的证据包，并标注跨源矛盾。"""
    unit = store.one(
        "SELECT * FROM unit_registry WHERE ns=? AND unit_id=?", (ns, unit_id)
    )
    if unit is None:
        raise KeyError(f"未登记的在制单元: {unit_id}")
    snapshot = store.one(
        "SELECT * FROM snapshots WHERE ns=? AND unit_id=?", (ns, unit_id)
    )
    device_id = (snapshot or {}).get("device_id") or unit.get("device_id")
    boot = (
        store.one(
            "SELECT * FROM boot_reports WHERE ns=? AND device_id=?",
            (ns, device_id),
        )
        if device_id
        else None
    )
    scan = store.one(
        "SELECT * FROM material_scans WHERE ns=? AND unit_id=?", (ns, unit_id)
    )
    commands = store.query(
        "SELECT * FROM command_logs WHERE ns=? AND unit_id=?"
        " ORDER BY issued_at, business_key",
        (ns, unit_id),
    )
    for c in commands:
        c["payload"] = loads(c.pop("payload_json"), {})
    if snapshot:
        snapshot["meta"] = loads(snapshot.pop("meta_json"), {})
    if boot:
        boot["faults"] = loads(boot.pop("faults_json"), [])

    operation_id = (snapshot or {}).get("operation_id")
    route = get_route(store, ns, device_id, operation_id)

    flags: dict[str, Any] = {}
    contradictions: list[str] = []

    mes_executing = bool(snapshot and snapshot["state"] == "in_progress")
    flags["mes_executing"] = mes_executing
    controller_standby = bool(boot and boot["boot_state"] == "standby")
    flags["controller_standby"] = controller_standby
    if mes_executing and controller_standby:
        contradictions.append(
            "MES 快照显示工序仍在执行，控制器重启后已回到待机"
        )

    receipt_pending = any(
        c["verb"] == "RECEIPT" and c["status"] == "cached" for c in commands
    )
    flags["receipt_pending"] = receipt_pending
    if receipt_pending:
        contradictions.append("完成回执缓存在控制器中，尚未上传 MES")

    mid_operation_conflict = bool(
        boot and boot["mid_operation"] and snapshot
        and snapshot["state"] == "completed"
    )
    flags["mid_operation_conflict"] = mid_operation_conflict
    if mid_operation_conflict:
        contradictions.append(
            "快照显示工序已完成，但启动报告表明断电时控制器仍在工序中段"
        )

    scan_diff: Optional[float] = None
    if snapshot and scan:
        scan_diff = round(scan["scanned_qty"] - snapshot["expected_qty"], 6)
        if scan_diff != 0:
            contradictions.append(
                f"物料账差 {scan_diff:+g}：扫描 {scan['scanned_qty']:g}"
                f" 与快照预期 {snapshot['expected_qty']:g} 不一致"
            )
    flags["scan_diff"] = scan_diff

    return EvidencePackage(
        unit_id=unit_id,
        unit=unit,
        snapshot=snapshot,
        boot_report=boot,
        scan=scan,
        commands=commands,
        route=route,
        flags=flags,
        contradictions=contradictions,
    )


def list_processing_units(store: Store, ns: str) -> list[dict[str, Any]]:
    return store.query(
        "SELECT * FROM unit_registry WHERE ns=? AND kind='processing'"
        " ORDER BY unit_id",
        (ns,),
    )


def _json(obj: Any) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False, sort_keys=True)
