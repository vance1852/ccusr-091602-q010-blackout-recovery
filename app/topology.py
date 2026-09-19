"""机组拓扑：登记单元类别与依赖关系，并校验层级顺序。

恢复依赖必须满足：公用工程 → 输送 → 加工单元，不允许越级依赖。
"""

from __future__ import annotations

import json
from typing import Any

from .models import CATEGORY_RANK, TopologyError, UnitCategory
from .store import Store


class TopologyRepository:
    def __init__(self, store: Store, clock) -> None:
        self._store = store
        self._clock = clock

    def register_unit(
        self,
        namespace: str,
        incident_id: str,
        unit_id: str,
        category: str,
        requires: list[str] | tuple[str, ...] = (),
        profile: dict[str, Any] | None = None,
    ) -> None:
        UnitCategory(category)  # 校验类别合法
        with self._store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO units (namespace, incident_id, unit_id, category, requires, profile, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (namespace, incident_id, unit_id) DO UPDATE SET
                  category = excluded.category,
                  requires = excluded.requires,
                  profile  = excluded.profile
                """,
                (
                    namespace,
                    incident_id,
                    unit_id,
                    UnitCategory(category).value,
                    json.dumps(list(requires), ensure_ascii=False),
                    json.dumps(profile or {}, ensure_ascii=False),
                    self._clock(),
                ),
            )

    def get(self, namespace: str, incident_id: str, unit_id: str) -> dict[str, Any] | None:
        row = self._store.query_one(
            "SELECT * FROM units WHERE namespace=? AND incident_id=? AND unit_id=?",
            (namespace, incident_id, unit_id),
        )
        return self._parse(row) if row else None

    def units(self, namespace: str, incident_id: str) -> list[dict[str, Any]]:
        rows = self._store.query(
            "SELECT * FROM units WHERE namespace=? AND incident_id=? ORDER BY unit_id",
            (namespace, incident_id),
        )
        return [self._parse(r) for r in rows]

    def validate(self, namespace: str, incident_id: str) -> None:
        """校验依赖指向已登记单元，且被依赖方层级严格更低。"""
        units = self.units(namespace, incident_id)
        by_id = {u["unit_id"]: u for u in units}
        problems: list[str] = []
        for unit in units:
            rank = CATEGORY_RANK[UnitCategory(unit["category"])]
            for dep in unit["requires"]:
                target = by_id.get(dep)
                if target is None:
                    problems.append(f"{unit['unit_id']} 依赖未登记单元 {dep}")
                    continue
                dep_rank = CATEGORY_RANK[UnitCategory(target["category"])]
                if dep_rank >= rank:
                    problems.append(
                        f"{unit['unit_id']}({unit['category']}) 越级依赖 "
                        f"{dep}({target['category']})"
                    )
        if problems:
            raise TopologyError("；".join(problems))

    @staticmethod
    def _parse(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "namespace": row["namespace"],
            "incident_id": row["incident_id"],
            "unit_id": row["unit_id"],
            "category": row["category"],
            "requires": json.loads(row["requires"]),
            "profile": json.loads(row["profile"]),
            "created_at": row["created_at"],
        }
