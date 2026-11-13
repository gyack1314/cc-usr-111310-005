"""正式索引上的按请求时点回放引擎。

取值顺序（每特征独立）：
1. 双截止过滤：event_time<=event_cutoff 且 available_time<=knowledge_cutoff，
   两个截止独立，不使用处理机当前时间；
2. 同 entity/feature/event_time 的已可得修订，按 revision、source_priority、
   record_id 决出赢家，knowledge_cutoff 之后到达的修订无权参与；
3. 事件时刻从新到旧扫描，逐赢家判 TTL（等号可用）；
4. 最新有效事件赢家为 delete 时，按该特征 delete_policy 决定 missing 或
   继续向更早事件回退；显式 null 是命中值，不触发回退。
"""
from __future__ import annotations

from typing import Optional

from .index import FeatureIndex
from .models import (
    Exclusion,
    ExclusionReason,
    FeaturePolicy,
    FeatureRequest,
    MissingReason,
    ReplayResult,
    ResultStatus,
    TtlJudgment,
)


def _base_result(
    req: FeatureRequest,
    warehouse_version: str,
    policy_version: str,
    status: ResultStatus,
    *,
    value=None,
    hit_record_id: Optional[str] = None,
    hit_event_time: Optional[int] = None,
    missing_reason: Optional[MissingReason] = None,
    exclusions: Optional[list[Exclusion]] = None,
    ttl_judgments: Optional[list[TtlJudgment]] = None,
) -> ReplayResult:
    return ReplayResult(
        request_id=req.request_id,
        entity=req.entity,
        feature=req.feature,
        event_cutoff=req.event_cutoff,
        knowledge_cutoff=req.knowledge_cutoff,
        warehouse_version=warehouse_version,
        policy_version=policy_version,
        status=status,
        value=value,
        hit_record_id=hit_record_id,
        hit_event_time=hit_event_time,
        missing_reason=missing_reason,
        exclusions=tuple(exclusions or ()),
        ttl_judgments=tuple(ttl_judgments or ()),
    )


def resolve_with_index(
    req: FeatureRequest,
    policy: FeaturePolicy,
    index: FeatureIndex,
    warehouse_version: str,
    policy_version: str,
) -> ReplayResult:
    """走正式索引的单特征回放。"""
    exclusions: list[Exclusion] = []
    ttl_judgments: list[TtlJudgment] = []

    if not index.has_entity(req.entity):
        return _base_result(
            req, warehouse_version, policy_version, ResultStatus.MISSING,
            missing_reason=MissingReason.NO_ENTITY,
        )

    scanned = index.scan_groups(
        req.entity, req.feature, req.event_cutoff, req.knowledge_cutoff
    )

    for group, available, unavailable_excl in scanned:
        exclusions.extend(unavailable_excl)
        winner, loser_excl = index.pick_winner(available)
        exclusions.extend(loser_excl)
        if winner is None:
            exclusions.append(
                Exclusion(
                    record_id=None,
                    event_time=group.event_time,
                    reason=ExclusionReason.NO_AVAILABLE_IN_GROUP,
                    detail=(
                        f"event_time={group.event_time} 的修订均晚于 "
                        f"knowledge_cutoff={req.knowledge_cutoff}"
                    ),
                )
            )
            continue

        fresh, age = policy.fresh(winner.event_time, req.event_cutoff)
        ttl_judgments.append(
            TtlJudgment(
                record_id=winner.record_id,
                event_time=winner.event_time,
                ttl=policy.ttl,
                age=age,
                fresh=fresh,
            )
        )
        if not fresh:
            exclusions.append(
                Exclusion(
                    record_id=winner.record_id,
                    event_time=winner.event_time,
                    reason=ExclusionReason.TTL_EXPIRED,
                    detail=f"age={age}>ttl={policy.ttl}，过期即停，不借更老事件",
                )
            )
            # 契约：过期不能回退到更老事件并声称新鲜
            return _base_result(
                req, warehouse_version, policy_version, ResultStatus.MISSING,
                missing_reason=MissingReason.TTL_EXPIRED,
                exclusions=exclusions, ttl_judgments=ttl_judgments,
            )

        if winner.is_delete:
            if not policy.fallback_on_delete:
                exclusions.append(
                    Exclusion(
                        record_id=winner.record_id,
                        event_time=winner.event_time,
                        reason=ExclusionReason.DELETE_SKIPPED,
                        detail="delete 墓碑且 delete_policy=missing",
                    )
                )
                return _base_result(
                    req, warehouse_version, policy_version, ResultStatus.MISSING,
                    missing_reason=MissingReason.DELETE_POLICY_MISSING,
                    exclusions=exclusions, ttl_judgments=ttl_judgments,
                )
            exclusions.append(
                Exclusion(
                    record_id=winner.record_id,
                    event_time=winner.event_time,
                    reason=ExclusionReason.DELETE_SKIPPED,
                    detail="delete 墓碑，按 fallback 向更早事件回退",
                )
            )
            continue

        # 显式 null 也走这里：是命中有值（缺测），value=None 原样返回，不回退
        return _base_result(
            req, warehouse_version, policy_version, ResultStatus.VALUE,
            value=winner.value,
            hit_record_id=winner.record_id,
            hit_event_time=winner.event_time,
            exclusions=exclusions, ttl_judgments=ttl_judgments,
        )

    # 走到这里只有两种情况：没有任何已可得候选；或新鲜事件赢家全是 delete，
    # 按 fallback 一路回退耗尽。两者按原始可得性口径都记 no_available_candidate。
    return _base_result(
        req, warehouse_version, policy_version, ResultStatus.MISSING,
        missing_reason=MissingReason.NO_AVAILABLE_CANDIDATE,
        exclusions=exclusions, ttl_judgments=ttl_judgments,
    )
