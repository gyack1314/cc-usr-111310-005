"""按请求时点回放的领域模型与校验。

时间口径见 docs/data-contract.md：所有时间/截止为有符号整数，TTL 为
event_cutoff-event_time，等号边界可用；显式 null 是命中但缺测，与 delete
墓碑是两类不同语义。本模块不读取任何墙钟时间。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class Op(str, Enum):
    """候选操作：完整值（含显式 null）或删除墓碑。"""

    VALUE = "value"
    DELETE = "delete"


class ResultStatus(str, Enum):
    """单特征回放终态。"""

    VALUE = "value"          # 命中有值记录，value 可以为 None（显式 null）
    MISSING = "missing"      # 按原始可得性口径无值，原因见 missing_reason


class MissingReason(str, Enum):
    """missing 的互斥原因，禁止用同一个兜底原因吞掉差异。"""

    NO_ENTITY = "no_entity"
    NO_AVAILABLE_CANDIDATE = "no_available_candidate"
    TTL_EXPIRED = "ttl_expired"
    DELETE_POLICY_MISSING = "delete_policy_missing"
    UNKNOWN_FEATURE = "unknown_feature"


class ExclusionReason(str, Enum):
    """赢家排除链中每条候选被排除的原因。"""

    AFTER_EVENT_CUTOFF = "after_event_cutoff"
    AFTER_KNOWLEDGE_CUTOFF = "after_knowledge_cutoff"
    TTL_EXPIRED = "ttl_expired"
    LOWER_REVISION = "lower_revision"
    LOWER_SOURCE_PRIORITY = "lower_source_priority"
    LOWER_RECORD_ID = "lower_record_id"
    DELETE_SKIPPED = "delete_skipped"
    NO_AVAILABLE_IN_GROUP = "no_available_in_group"


def signed_int(x: Any, name: str) -> int:
    """有符号整数校验：bool 不是整数。"""
    if type(x) is not int:
        raise ValueError(f"{name} 必须为整数")
    return x


def non_negative_int(x: Any, name: str) -> int:
    if type(x) is not int or x < 0:
        raise ValueError(f"{name} 必须为非负整数")
    return x


def value_type_name(value: Any) -> str:
    if value is None:
        return "null"
    if type(value) is int:
        return "int"
    if type(value) is str:
        return "string"
    raise ValueError("value 仅允许 int/string/null")


@dataclass(frozen=True)
class Candidate:
    """不可变候选记录；record_id 全仓唯一且不可复用。"""

    record_id: str
    entity: str
    feature: str
    event_time: int
    available_time: int
    revision: int
    source_priority: int
    op: Op
    value: Any = None

    def __post_init__(self) -> None:
        if not isinstance(self.record_id, str) or not self.record_id:
            raise ValueError("record_id 缺失")
        if not isinstance(self.entity, str) or not self.entity:
            raise ValueError("entity 缺失")
        if not isinstance(self.feature, str) or not self.feature:
            raise ValueError("feature 缺失")
        signed_int(self.event_time, "event_time")
        signed_int(self.available_time, "available_time")
        non_negative_int(self.revision, "revision")
        non_negative_int(self.source_priority, "source_priority")
        if not isinstance(self.op, Op):
            raise ValueError("op 非法")
        if self.op is Op.VALUE:
            value_type_name(self.value)  # 仅校验形状，None 合法
        elif self.value is not None:
            raise ValueError("delete 墓碑不能携带 value")

    @property
    def is_delete(self) -> bool:
        return self.op is Op.DELETE

    @classmethod
    def from_raw(cls, row: dict[str, Any]) -> "Candidate":
        op = Op(row["op"])
        return cls(
            record_id=row["record_id"],
            entity=row["entity"],
            feature=row["feature"],
            event_time=row["event_time"],
            available_time=row["available_time"],
            revision=row["revision"],
            source_priority=row["source_priority"],
            op=op,
            value=row.get("value"),
        )

    @property
    def precedence(self) -> tuple[int, int, str]:
        """同 event_time 修订决胜键：revision、source_priority、record_id。"""
        return self.revision, self.source_priority, self.record_id


@dataclass(frozen=True)
class FeaturePolicy:
    """单特征的独立 TTL 与删除策略，不允许跨特征共用默认值。"""

    name: str
    ttl: int
    delete_policy: str  # "fallback" 或 "missing"

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("特征名缺失")
        non_negative_int(self.ttl, "ttl")
        if self.delete_policy not in ("fallback", "missing"):
            raise ValueError("删除策略未知")

    @property
    def fallback_on_delete(self) -> bool:
        return self.delete_policy == "fallback"

    def fresh(self, event_time: int, event_cutoff: int) -> tuple[bool, int]:
        """TTL 判定：age=event_cutoff-event_time，age==ttl 仍可用。"""
        age = event_cutoff - event_time
        return age <= self.ttl, age


@dataclass(frozen=True)
class Exclusion:
    """赢家排除链的一条记录，冲突信息（败在哪个键）原样保留。"""

    record_id: Optional[str]
    event_time: Optional[int]
    reason: ExclusionReason
    detail: str = ""


@dataclass(frozen=True)
class TtlJudgment:
    """对每个被扫描事件时刻赢家的 TTL 判定。"""

    record_id: str
    event_time: int
    ttl: int
    age: int
    fresh: bool


@dataclass(frozen=True)
class FeatureRequest:
    """一次请求中的一个特征查询；request_id 稳定且重复请求各自独立。"""

    request_id: str
    entity: str
    feature: str
    event_cutoff: int
    knowledge_cutoff: int

    def __post_init__(self) -> None:
        for name in ("request_id", "entity", "feature"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name} 缺失")
        signed_int(self.event_cutoff, "event_cutoff")
        signed_int(self.knowledge_cutoff, "knowledge_cutoff")

    @classmethod
    def from_raw(cls, row: dict[str, Any], feature: str) -> "FeatureRequest":
        return cls(
            request_id=row["request_id"],
            entity=row["entity"],
            feature=feature,
            event_cutoff=row["event_cutoff"],
            knowledge_cutoff=row["knowledge_cutoff"],
        )


@dataclass(frozen=True)
class ReplayResult:
    """单请求单特征的回放结果与完整解释。"""

    request_id: str
    entity: str
    feature: str
    event_cutoff: int
    knowledge_cutoff: int
    warehouse_version: str
    policy_version: str
    status: ResultStatus
    value: Any = None
    hit_record_id: Optional[str] = None
    hit_event_time: Optional[int] = None
    missing_reason: Optional[MissingReason] = None
    exclusions: tuple[Exclusion, ...] = field(default_factory=tuple)
    ttl_judgments: tuple[TtlJudgment, ...] = field(default_factory=tuple)

    @property
    def is_hit(self) -> bool:
        return self.status is ResultStatus.VALUE

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "entity": self.entity,
            "feature": self.feature,
            "event_cutoff": self.event_cutoff,
            "knowledge_cutoff": self.knowledge_cutoff,
            "warehouse_version": self.warehouse_version,
            "policy_version": self.policy_version,
            "status": self.status.value,
            "value": self.value,
            "hit_record_id": self.hit_record_id,
            "hit_event_time": self.hit_event_time,
            "missing_reason": None if self.missing_reason is None else self.missing_reason.value,
            "exclusions": [
                {
                    "record_id": e.record_id,
                    "event_time": e.event_time,
                    "reason": e.reason.value,
                    "detail": e.detail,
                }
                for e in self.exclusions
            ],
            "ttl_judgments": [
                {
                    "record_id": j.record_id,
                    "event_time": j.event_time,
                    "ttl": j.ttl,
                    "age": j.age,
                    "fresh": j.fresh,
                }
                for j in self.ttl_judgments
            ],
        }
