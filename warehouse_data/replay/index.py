"""按 entity/feature/双时间建立的不可变索引。

设计目标：批请求不得为每条请求全扫全部候选。
- 第一层 dict 按 (entity, feature) 直接定位桶；
- 桶内 event_time 升序，bisect 截到 event_cutoff，未来事件根本不进扫描；
- 桶内另维护 available_time 升序数组，bisect 截到 knowledge_cutoff，
  每个事件时刻的修订组只在“当时已可得”的成员里决胜，
  knowledge_cutoff 之后到达的更高修订无法压掉旧修订。
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from typing import Optional

from .models import Candidate, Exclusion, ExclusionReason


@dataclass(frozen=True)
class _EventGroup:
    """同一 (entity, feature, event_time) 的全部修订，按可得时刻排序。"""

    event_time: int
    available_times: tuple[int, ...]      # 与 by_available 对齐，升序
    by_available: tuple[Candidate, ...]


@dataclass(frozen=True)
class _Bucket:
    event_times: tuple[int, ...]                  # 升序，与 groups 对齐
    groups: tuple[_EventGroup, ...]


class FeatureIndex:
    """不可变快照索引；同一版本候选集合只构建一次。"""

    def __init__(self, candidates: list[Candidate] | tuple[Candidate, ...]):
        raw_buckets: dict[tuple[str, str], dict[int, list[Candidate]]] = {}
        for c in candidates:
            raw_buckets.setdefault((c.entity, c.feature), {}).setdefault(
                c.event_time, []
            ).append(c)

        buckets: dict[tuple[str, str], _Bucket] = {}
        for key, by_event in raw_buckets.items():
            groups: list[_EventGroup] = []
            for event_time in sorted(by_event):
                members = tuple(by_event[event_time])
                avail_pairs = sorted(((c.available_time, c) for c in members),
                                     key=lambda p: (p[0], p[1].record_id))
                groups.append(
                    _EventGroup(
                        event_time=event_time,
                        available_times=tuple(t for t, _ in avail_pairs),
                        by_available=tuple(c for _, c in avail_pairs),
                    )
                )
            buckets[key] = _Bucket(
                event_times=tuple(g.event_time for g in groups),
                groups=tuple(groups),
            )
        self._buckets = buckets

    def has_entity(self, entity: str) -> bool:
        return any(k[0] == entity for k in self._buckets)

    def scan_groups(
        self,
        entity: str,
        feature: str,
        event_cutoff: int,
        knowledge_cutoff: int,
    ) -> list[tuple[_EventGroup, list[Candidate], list[Exclusion]]]:
        """返回事件时刻从新到旧的（修订组、已可得成员、排除链）。

        只访问 event_time<=event_cutoff 的组；组内用 available_time 二分，
        未可得成员按 after_knowledge_cutoff 入排除链，绝不参与决胜。
        """
        bucket = self._buckets.get((entity, feature))
        if bucket is None:
            return []
        upper = bisect_right(bucket.event_times, event_cutoff)
        out: list[tuple[_EventGroup, list[Candidate], list[Exclusion]]] = []
        for group in reversed(bucket.groups[:upper]):
            cut = bisect_right(group.available_times, knowledge_cutoff)
            available = list(group.by_available[:cut])
            unavailable = list(group.by_available[cut:])
            exclusions = [
                Exclusion(
                    record_id=c.record_id,
                    event_time=c.event_time,
                    reason=ExclusionReason.AFTER_KNOWLEDGE_CUTOFF,
                    detail=(
                        f"available_time={c.available_time}>"
                        f"knowledge_cutoff={knowledge_cutoff}"
                    ),
                )
                for c in unavailable
            ]
            out.append((group, available, exclusions))
        return out

    @staticmethod
    def pick_winner(
        available: list[Candidate],
    ) -> tuple[Optional[Candidate], list[Exclusion]]:
        """同事件时刻已可得成员按 revision→source_priority→record_id 决胜。

        败者保留具体败因键，冲突信息不合并、不吞掉。输入已按可得时刻排序，
        这里重按决胜键降序，保证“最后到库”不影响结果。
        """
        if not available:
            return None, []
        ordered = sorted(available, key=lambda c: c.precedence, reverse=True)
        winner = ordered[0]
        exclusions: list[Exclusion] = []
        for loser in ordered[1:]:
            if loser.revision != winner.revision:
                reason = ExclusionReason.LOWER_REVISION
                detail = f"revision {loser.revision}<{winner.revision}"
            elif loser.source_priority != winner.source_priority:
                reason = ExclusionReason.LOWER_SOURCE_PRIORITY
                detail = (
                    f"source_priority {loser.source_priority}"
                    f"<{winner.source_priority}"
                )
            else:
                reason = ExclusionReason.LOWER_RECORD_ID
                detail = f"record_id {loser.record_id!r}<{winner.record_id!r}"
            exclusions.append(
                Exclusion(
                    record_id=loser.record_id,
                    event_time=loser.event_time,
                    reason=reason,
                    detail=detail,
                )
            )
        return winner, exclusions
