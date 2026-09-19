"""断电后产线恢复裁决领域包。"""

from .models import OFFICIAL_NS
from .service import RecoveryService

PROJECT_NAME = "factory-blackout-recovery"

__all__ = ["PROJECT_NAME", "OFFICIAL_NS", "RecoveryService"]
