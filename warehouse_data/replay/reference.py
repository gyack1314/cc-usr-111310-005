"""小规模枚举参考实现，故意不依赖 FeatureIndex。

用途：在测试中对小候选全集做朴素双截止过滤与排序，与正式索引引擎交叉
校验。生产批处理不得使用本模块——它每次请求都枚举给定候选全集。
"""
from __future__ import annotations

from typing import Iterable

from .models import (
    Candidate,
    Exclusion,
    ExclusionReason,
    FeaturePolicy,
    FeatureRequest,
    MissingReason,
    ReplayResult,
    ResultStatus,
    TtlJudgment,
)


def _result(req, warehouse_version, policy_version, status, **kw):
    kw.setdefault("value", None)
    return ReplayResult(
        request_id=req.request_id,
        entity=req.entity,
        feature=req.feature,
        event_cutoff=req.event_cutoff,
        knowledge_cutoff=req.knowledge_cutoff,
        warehouse_version=warehouse_version,
        policy_version=policy_version,
        status=status,
        exclusions=tuple(kw.pop("exclusions", ())),
        ttl_judgments=tuple(kw.pop("ttl_judgments", ())),
        **kw,
    )


def resolve_by_enumeration(
    req: FeatureRequest,
    policy: FeaturePolicy,
    candidates: Iterable[Candidate],
    warehouse_version: str,
    policy_version: str,
) -> ReplayResult:
    """朴素枚举：先逐条双截止过滤，再分组、排序、从新到旧扫描。"""
    rows = [
        c for c in candidates
        if c.entity == req.entity and c.feature == req.feature
    ]
    if not any(c.entity == req.entity for c in candidates):
        return _result(
            req, warehouse_version, policy_version, ResultStatus.MISSING,
            missing_reason=MissingReason.NO_ENTITY,
        )

    exclusions: list[Exclusion] = []
    ttl_judgments: list[TtlJudgment] = []
    visible: list[Candidate] = []
    for c in rows:
        if c.event_time > req.event_cutoff:
            exclusions.append(Exclusion(
                record_id=c.record_id, event_time=c.event_time,
                reason=ExclusionReason.AFTER_EVENT_CUTOFF,
                detail=f"event_time={c.event_time}>event_cutoff={req.event_cutoff}",
            ))
        elif c.available_time > req.knowledge_cutoff:
            exclusions.append(Exclusion(
                record_id=c.record_id, event_time=c.event_time,
                reason=ExclusionReason.AFTER_KNOWLEDGE_CUTOFF,
                detail=(
                    f"available_time={c.available_time}>"
                    f"knowledge_cutoff={req.knowledge_cutoff}"
                ),
            ))
        else:
            visible.append(c)

    groups: dict[int, list[Candidate]] = {}
    for c in visible:
        groups.setdefault(c.event_time, []).append(c)

    for event_time in sorted(groups, reverse=True):
        ordered = sorted(
            groups[event_time], key=lambda c: c.precedence, reverse=True
        )
        winner, losers = ordered[0], ordered[1:]
        for loser in losers:
            if loser.revision != winner.revision:
                reason, detail = (
                    ExclusionReason.LOWER_REVISION,
                    f"revision {loser.revision}<{winner.revision}",
                )
            elif loser.source_priority != winner.source_priority:
                reason, detail = (
                    ExclusionReason.LOWER_SOURCE_PRIORITY,
                    f"source_priority {loser.source_priority}<{winner.source_priority}",
                )
            else:
                reason, detail = (
                    ExclusionReason.LOWER_RECORD_ID,
                    f"record_id {loser.record_id!r}<{winner.record_id!r}",
                )
            exclusions.append(Exclusion(
                record_id=loser.record_id, event_time=loser.event_time,
                reason=reason, detail=detail,
            ))

        fresh, age = policy.fresh(event_time, req.event_cutoff)
        ttl_judgments.append(TtlJudgment(
            record_id=winner.record_id, event_time=event_time,
            ttl=policy.ttl, age=age, fresh=fresh,
        ))
        if not fresh:
            exclusions.append(Exclusion(
                record_id=winner.record_id, event_time=event_time,
                reason=ExclusionReason.TTL_EXPIRED,
                detail=f"age={age}>ttl={policy.ttl}，过期即停，不借更老事件",
            ))
            return _result(
                req, warehouse_version, policy_version, ResultStatus.MISSING,
                missing_reason=MissingReason.TTL_EXPIRED,
                exclusions=exclusions, ttl_judgments=ttl_judgments,
            )
        if winner.is_delete:
            if not policy.fallback_on_delete:
                exclusions.append(Exclusion(
                    record_id=winner.record_id, event_time=event_time,
                    reason=ExclusionReason.DELETE_SKIPPED,
                    detail="delete 墓碑且 delete_policy=missing",
                ))
                return _result(
                    req, warehouse_version, policy_version, ResultStatus.MISSING,
                    missing_reason=MissingReason.DELETE_POLICY_MISSING,
                    exclusions=exclusions, ttl_judgments=ttl_judgments,
                )
            exclusions.append(Exclusion(
                record_id=winner.record_id, event_time=event_time,
                reason=ExclusionReason.DELETE_SKIPPED,
                detail="delete 墓碑，按 fallback 向更早事件回退",
            ))
            continue
        return _result(
            req, warehouse_version, policy_version, ResultStatus.VALUE,
            value=winner.value, hit_record_id=winner.record_id,
            hit_event_time=event_time,
            exclusions=exclusions, ttl_judgments=ttl_judgments,
        )

    return _result(
        req, warehouse_version, policy_version, ResultStatus.MISSING,
        missing_reason=MissingReason.NO_AVAILABLE_CANDIDATE,
        exclusions=exclusions, ttl_judgments=ttl_judgments,
    )
