"""领域模型：裁决、动作状态、命令结果与检查点。

所有枚举取值与 ``domain_contract.json`` 保持一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class StrEnum(str, Enum):
    def __str__(self) -> str:  # pragma: no cover - 便于日志输出
        return self.value


class UnitCategory(StrEnum):
    """单元类别：公用工程 / 输送 / 加工单元。"""

    UTILITY = "utility"
    CONVEYOR = "conveyor"
    PROCESSING = "processing"


#: 依赖层级：低层级未就绪时高层级不得越级恢复。
CATEGORY_RANK = {
    UnitCategory.UTILITY: 0,
    UnitCategory.CONVEYOR: 1,
    UnitCategory.PROCESSING: 2,
}


class Decision(StrEnum):
    AUTO_RESUME = "auto_resume"
    INSPECTION_REQUIRED = "inspection_required"
    SCRAP = "scrap"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class ActionState(StrEnum):
    BLOCKED = "blocked"
    READY = "ready"
    EXECUTING = "executing"
    COMPLETED = "completed"
    REVIEW_REQUIRED = "review_required"


class CommandResult(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    STALE = "stale"
    DEPENDENCY_BLOCKED = "dependency_blocked"


class ReviewConclusion(StrEnum):
    CONFIRMED = "confirmed"
    CONTRADICTED = "contradicted"


#: 正式记录命名空间；演练使用 ``drill:<incident>:<n>``。
OFFICIAL_NAMESPACE = "official"
DRILL_PREFIX = "drill:"

#: 已经执行过的动作状态：不可删除、不可改写，只能追加复核结论。
EXECUTED_STATES = (
    ActionState.EXECUTING.value,
    ActionState.COMPLETED.value,
    ActionState.REVIEW_REQUIRED.value,
)


class DomainError(Exception):
    """领域错误基类。"""


class ImmutableRecordError(DomainError):
    """执行过的恢复动作不可删除、不可改写。"""


class ConfirmationRequiredError(DomainError):
    """人工选择与自动建议不一致，需双人确认后才能执行。"""


class DuplicateApprovalError(DomainError):
    """同一确认人不可重复确认。"""


class TopologyError(DomainError):
    """依赖拓扑违反层级顺序（公用工程 → 输送 → 加工）。"""


@dataclass(frozen=True)
class Checkpoint:
    """裁决所依据的最后可信检查点。"""

    seq: int
    checkpoint_id: str
    operation: str
    step: str
    state: str
    source: str  # snapshot | command_log | boot_report
    recorded_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "checkpoint_id": self.checkpoint_id,
            "operation": self.operation,
            "step": self.step,
            "state": self.state,
            "source": self.source,
            "recorded_at": self.recorded_at,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Checkpoint":
        return Checkpoint(
            seq=int(data["seq"]),
            checkpoint_id=data["checkpoint_id"],
            operation=data["operation"],
            step=data["step"],
            state=data["state"],
            source=data["source"],
            recorded_at=data["recorded_at"],
        )


@dataclass
class EvidencePackage:
    """单个在制单元的恢复证据包：四类来源汇聚后的只读视图。"""

    unit_id: str
    category: UnitCategory
    snapshot: dict[str, Any] | None
    boot_report: dict[str, Any] | None
    material_scan: dict[str, Any] | None
    command_logs: list[dict[str, Any]] = field(default_factory=list)
    inspections: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class DecisionRecord:
    """裁决结论：必须解释所采用的最后可信检查点。"""

    unit_id: str
    decision: Decision
    last_trusted_checkpoint: Checkpoint | None
    rationale: list[str]
    material_account: dict[str, Any]  # {"book": ..., "scanned": ..., "deltas": ...}
    missing_evidence: list[str]
    resume_point: dict[str, Any] | None  # {"operation": ..., "step": feed|process|done}
