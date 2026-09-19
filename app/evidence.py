"""证据汇聚：把四类来源汇成每个在制单元的证据包。

来源：停电前快照（snapshot）、设备启动报告（boot_report）、
物料扫描（material_scan）、命令日志（command_log，含缓存的完成回执），
以及恢复过程中补充的人工检查结论（inspection）。
证据只追加、不修改；证据包取各来源最新一条。
"""

from __future__ import annotations

import json
from typing import Any

from .models import EvidencePackage, UnitCategory
from .store import Store

SOURCE_SNAPSHOT = "snapshot"
SOURCE_BOOT_REPORT = "boot_report"
SOURCE_MATERIAL_SCAN = "material_scan"
SOURCE_COMMAND_LOG = "command_log"
SOURCE_INSPECTION = "inspection"


class EvidenceRepository:
    def __init__(self, store: Store, clock) -> None:
        self._store = store
        self._clock = clock

    def record(
        self,
        namespace: str,
        incident_id: str,
        unit_id: str,
        source: str,
        payload: dict[str, Any],
    ) -> int:
        with self._store.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO evidence (namespace, incident_id, unit_id, source, payload, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    namespace,
                    incident_id,
                    unit_id,
                    source,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    self._clock(),
                ),
            )
        return int(cur.lastrowid)

    def package(
        self,
        namespace: str,
        incident_id: str,
        unit_id: str,
        category: str,
    ) -> EvidencePackage:
        rows = self._store.query(
            """
            SELECT source, payload FROM evidence
            WHERE namespace=? AND incident_id=? AND unit_id=?
            ORDER BY id
            """,
            (namespace, incident_id, unit_id),
        )
        snapshot = boot_report = material_scan = None
        command_logs: list[dict[str, Any]] = []
        inspections: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(row["payload"])
            source = row["source"]
            if source == SOURCE_SNAPSHOT:
                snapshot = payload
            elif source == SOURCE_BOOT_REPORT:
                boot_report = payload
            elif source == SOURCE_MATERIAL_SCAN:
                material_scan = payload
            elif source == SOURCE_COMMAND_LOG:
                command_logs.append(payload)
            elif source == SOURCE_INSPECTION:
                inspections.append(payload)
        command_logs.sort(key=lambda e: (e.get("issued_at", ""), e.get("business_key", "")))
        return EvidencePackage(
            unit_id=unit_id,
            category=UnitCategory(category),
            snapshot=snapshot,
            boot_report=boot_report,
            material_scan=material_scan,
            command_logs=command_logs,
            inspections=inspections,
        )
