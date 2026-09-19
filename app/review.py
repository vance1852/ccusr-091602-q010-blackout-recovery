"""复核：已执行动作不可删除，后续证据只生成复核结论。

每次新证据入库后调用 review_pass：对受影响单元重新裁决，与已执行
动作的冻结依据比较 —— 不一致则把动作置为 review_required 并生成
复核结论；一致则记录复核通过。原始执行记录永不修改。
"""
from __future__ import annotations

import uuid
from typing import Any

from .adjudication import adjudicate_package
from .evidence import build_package
from .store import Store, loads


def review_pass(store: Store, ns: str, unit_ids: list[str], at: str) -> list[dict]:
    """对指定单元执行复核，返回新生成的复核结论。"""
    created: list[dict] = []
    for uid in unit_ids:
        executed = store.query(
            "SELECT * FROM actions WHERE ns=? AND unit_id=?"
            " AND state IN ('completed','review_required')",
            (ns, uid),
        )
        if not executed:
            continue
        pkg = build_package(store, ns, uid)
        new = adjudicate_package(pkg)
        new_cp = new.checkpoint.checkpoint_id if new.checkpoint else None
        for action in executed:
            basis = loads(action["decision_basis_json"], {}) or {}
            old = (basis.get("decision"), basis.get("checkpoint_id"))
            new_key = (new.decision, new_cp)
            # 稳定指纹：同一动作、同一新旧结论对只生成一次复核。
            fingerprint = (
                f"{old[0]}@{old[1] or '-'}->{new_key[0]}@{new_key[1] or '-'}"
            )
            if new_key == old:
                conclusion = (
                    f"复核一致：新证据下裁决仍为 {new.decision}"
                    f"@{new_cp or '无检查点'}，与已执行动作依据相符"
                )
                consistent = True
            else:
                conclusion = (
                    f"复核不一致：已执行动作依据 {old[0]}@{old[1] or '无检查点'}，"
                    f"新证据下裁决为 {new.decision}@{new_cp or '无检查点'}"
                    f"（{new.rationale}）"
                )
                consistent = False
            dup = store.one(
                "SELECT review_id FROM reviews"
                " WHERE ns=? AND action_id=? AND fingerprint=?",
                (ns, action["action_id"], fingerprint),
            )
            if dup:
                continue
            review_id = f"REV-{uuid.uuid4().hex[:12]}"
            with store.tx():
                store.execute(
                    "INSERT INTO reviews"
                    "(ns, review_id, action_id, unit_id, fingerprint,"
                    " conclusion, created_at) VALUES(?,?,?,?,?,?,?)",
                    (ns, review_id, action["action_id"], uid, fingerprint,
                     conclusion, at),
                )
                if not consistent and action["state"] == "completed":
                    store.execute(
                        "UPDATE actions SET state='review_required'"
                        " WHERE ns=? AND action_id=? AND state='completed'",
                        (ns, action["action_id"]),
                    )
                store.emit(ns, "review_created",
                           {"review_id": review_id,
                            "action_id": action["action_id"],
                            "consistent": consistent}, at)
            created.append({"review_id": review_id,
                            "action_id": action["action_id"],
                            "consistent": consistent,
                            "conclusion": conclusion})
    return created
