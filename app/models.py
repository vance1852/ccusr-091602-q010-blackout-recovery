"""领域模型、常量与异常。

所有标识符使用英文，面向恢复经理的解释性文本使用中文。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# 正式命名空间；演练使用 drill-<id> 命名空间，二者完全隔离。
OFFICIAL_NS = "official"

# 与 domain_contract.json 对齐的枚举。
DECISIONS = ("auto_resume", "inspection_required", "scrap", "insufficient_evidence")
ACTION_STATES = ("blocked", "ready", "executing", "completed", "review_required")
COMMAND_RESULTS = ("accepted", "duplicate", "stale", "dependency_blocked")

# 恢复计划的依赖层级：公用工程 -> 输送 -> 加工，未就绪不得越级。
LEVEL_UTILITY = 0
LEVEL_CONVEYING = 1
LEVEL_PROCESSING = 2

KIND_UTILITY = "utility"
KIND_CONVEYING = "conveying"
KIND_PROCESSING = "processing"

# 投料检查点名称：续作时若最后可信检查点早于它，恢复动作才需要补投料。
FEED_CHECKPOINT = "feed"
DEFAULT_ROUTE = ["enqueue", "feed", "process", "inspect", "complete"]

# 出现这些故障码时单元必须报废。
SCRAP_FAULTS = frozenset({"CONTAMINATION", "SAFETY"})

# 动作阻塞原因。
BLOCKED_DEPENDENCY = "dependency"
BLOCKED_EVIDENCE = "evidence"

# 裁决来源。
SOURCE_AUTO = "auto"
SOURCE_MANUAL_CONSISTENT = "manual_consistent"
SOURCE_MANUAL_DUAL = "manual_dual_confirmed"


class DomainError(Exception):
    """领域错误基类。"""


class ImmutableError(DomainError):
    """执行过的恢复动作不可删除/篡改。"""


class ApprovalError(DomainError):
    """双人确认流程错误。"""


class BlockedError(DomainError):
    """动作被依赖或证据阻塞，不能越级执行。"""

    def __init__(self, action_id: str, chain: list[dict[str, Any]]):
        self.action_id = action_id
        self.chain = chain
        desc = " -> ".join(
            f"{c['action_id']}[{c['state']}]" for c in chain
        ) or "<none>"
        super().__init__(f"动作 {action_id} 被阻塞，阻塞链: {desc}")


class SimulatedCrash(Exception):
    """演练/测试中注入的进程崩溃。"""


@dataclass
class TrustedCheckpoint:
    """最后可信检查点：裁决结论必须解释它。"""

    device_id: str
    operation_id: str
    checkpoint_id: str
    seq: int
    confirmed_by: list[str] = field(default_factory=list)
    capped_by: Optional[str] = None  # 压低可信度的证据 id
    material_verified: bool = True
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "operation_id": self.operation_id,
            "checkpoint_id": self.checkpoint_id,
            "seq": self.seq,
            "confirmed_by": list(self.confirmed_by),
            "capped_by": self.capped_by,
            "material_verified": self.material_verified,
            "notes": list(self.notes),
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "TrustedCheckpoint":
        return TrustedCheckpoint(
            device_id=d["device_id"],
            operation_id=d["operation_id"],
            checkpoint_id=d["checkpoint_id"],
            seq=d["seq"],
            confirmed_by=list(d.get("confirmed_by", [])),
            capped_by=d.get("capped_by"),
            material_verified=d.get("material_verified", True),
            notes=list(d.get("notes", [])),
        )


@dataclass
class Decision:
    """裁决结论：四分类 + 最后可信检查点 + 中文解释。"""

    unit_id: str
    decision: str
    checkpoint: Optional[TrustedCheckpoint]
    rationale: str
    evidence_ids: list[str] = field(default_factory=list)
    source: str = SOURCE_AUTO

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "decision": self.decision,
            "checkpoint": self.checkpoint.to_dict() if self.checkpoint else None,
            "rationale": self.rationale,
            "evidence_ids": list(self.evidence_ids),
            "source": self.source,
        }


@dataclass
class EvidencePackage:
    """在制单元证据包：快照 + 启动报告 + 物料扫描 + 命令日志。"""

    unit_id: str
    unit: dict[str, Any]
    snapshot: Optional[dict[str, Any]]
    boot_report: Optional[dict[str, Any]]
    scan: Optional[dict[str, Any]]
    commands: list[dict[str, Any]] = field(default_factory=list)
    route: list[str] = field(default_factory=list)
    flags: dict[str, Any] = field(default_factory=dict)
    contradictions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "unit": self.unit,
            "snapshot": self.snapshot,
            "boot_report": self.boot_report,
            "scan": self.scan,
            "commands": list(self.commands),
            "route": list(self.route),
            "flags": dict(self.flags),
            "contradictions": list(self.contradictions),
        }
