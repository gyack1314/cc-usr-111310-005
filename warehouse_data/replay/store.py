"""版本化特征仓：不可变快照、事务提交与请求缓存。

一致性约束：
- 每个 warehouse_version 是不可变候选集合；迟到更正只追加新版本，
  固定旧版本 + 相同双截止 + 相同策略版本的重放永远一致；
- 索引构建、版本发布、缓存挂接在同一把锁内完成，对外表现为一个事务，
  提交失败不会留下半更新状态；
- 缓存键必须同时包含两个截止与策略版本，event_cutoff 相同但
  knowledge_cutoff 不同的结果绝不能互借；
- 全程只使用请求携带的截止时间，不读处理机墙钟。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Iterable, Optional

from .engine import resolve_with_index
from .index import FeatureIndex
from .models import (
    Candidate,
    FeaturePolicy,
    FeatureRequest,
    MissingReason,
    ReplayResult,
    ResultStatus,
    value_type_name,
)


@dataclass(frozen=True)
class _Snapshot:
    warehouse_version: str
    policy_version: str
    policies: dict[str, FeaturePolicy]
    index: FeatureIndex
    cache: dict[tuple, ReplayResult]


class CommitError(ValueError):
    """事务提交期校验失败；调用后仓库保持提交前状态。"""


class FeatureStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._snapshots: dict[str, _Snapshot] = {}
        self._head: Optional[_Snapshot] = None
        self._record_ids: set[str] = set()

    # ---------- 只读 ----------

    @property
    def head_version(self) -> Optional[str]:
        with self._lock:
            return None if self._head is None else self._head.warehouse_version

    def versions(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._snapshots)

    def policy_version_of(self, warehouse_version: str) -> str:
        with self._lock:
            return self._snapshot(warehouse_version).policy_version

    def _snapshot(self, warehouse_version: Optional[str]) -> _Snapshot:
        label = self.head_version if warehouse_version is None else warehouse_version
        if label is None or label not in self._snapshots:
            raise CommitError(f"未知仓库版本: {label!r}")
        return self._snapshots[label]

    # ---------- 事务提交 ----------

    def commit(
        self,
        warehouse_version: str,
        candidates: Iterable[Candidate],
        policies: Iterable[FeaturePolicy],
        policy_version: str,
    ) -> None:
        """原子提交一个新版本：校验 -> 建索引 -> 发布 -> 挂空缓存。"""
        if not isinstance(warehouse_version, str) or not warehouse_version:
            raise CommitError("warehouse_version 缺失")
        if not isinstance(policy_version, str) or not policy_version:
            raise CommitError("policy_version 缺失")
        rows = list(candidates)
        policy_map = {p.name: p for p in policies}
        if len(policy_map) != len(list(policies)):
            raise CommitError("特征配置重名")

        with self._lock:
            if warehouse_version in self._snapshots:
                raise CommitError(f"仓库版本已存在且不可变: {warehouse_version}")

            seen: set[str] = set()
            value_types: dict[str, str] = {}
            for c in rows:
                if c.record_id in seen:
                    raise CommitError(f"record_id 在批次内重复: {c.record_id}")
                if c.record_id in self._record_ids:
                    raise CommitError(
                        f"record_id 不可跨版本复用: {c.record_id}"
                    )
                if c.feature not in policy_map:
                    raise CommitError(f"候选引用了未注册特征: {c.feature}")
                if c.op.value == "value":
                    # 显式 null 是“有记录但缺测”，不参与固定类型约束；
                    # int/string 混用于同一 feature 才是形状错误。
                    if c.value is not None:
                        tname = value_type_name(c.value)
                        old = value_types.setdefault(c.feature, tname)
                        if old != tname:
                            raise CommitError(
                                f"特征 {c.feature} 值类型不一致: "
                                f"{old} vs {tname}"
                            )
                seen.add(c.record_id)

            # 先在锁内构建索引；构建抛错则本次提交无任何副作用
            new_index = FeatureIndex(rows)
            snapshot = _Snapshot(
                warehouse_version=warehouse_version,
                policy_version=policy_version,
                policies=policy_map,
                index=new_index,
                cache={},
            )
            # 以下赋值构成对外可见的原子发布点
            self._snapshots[warehouse_version] = snapshot
            self._record_ids |= seen
            self._head = snapshot

    # ---------- 回放 ----------

    @staticmethod
    def _cache_key(req: FeatureRequest, policy_version: str) -> tuple:
        return (
            req.request_id,
            req.entity,
            req.feature,
            req.event_cutoff,
            req.knowledge_cutoff,
            policy_version,
        )

    def replay(
        self,
        req: FeatureRequest,
        warehouse_version: Optional[str] = None,
        use_cache: bool = True,
    ) -> ReplayResult:
        """按请求时点回放；默认固定到当前 head，也可指定历史版本。"""
        with self._lock:
            snap = self._snapshot(warehouse_version)
            policy = snap.policies.get(req.feature)
            if policy is None:
                return ReplayResult(
                    request_id=req.request_id,
                    entity=req.entity,
                    feature=req.feature,
                    event_cutoff=req.event_cutoff,
                    knowledge_cutoff=req.knowledge_cutoff,
                    warehouse_version=snap.warehouse_version,
                    policy_version=snap.policy_version,
                    status=ResultStatus.MISSING,
                    missing_reason=MissingReason.UNKNOWN_FEATURE,
                )
            key = self._cache_key(req, snap.policy_version)
            if use_cache and key in snap.cache:
                return snap.cache[key]
            result = resolve_with_index(
                req, policy, snap.index,
                snap.warehouse_version, snap.policy_version,
            )
            if use_cache:
                snap.cache[key] = result
            return result

    def replay_batch(
        self,
        requests_: Iterable[FeatureRequest],
        warehouse_version: Optional[str] = None,
        use_cache: bool = True,
    ) -> list[ReplayResult]:
        """批请求：重复 request_id / 重复 entity 仍各自独立输出。"""
        return [
            self.replay(r, warehouse_version=warehouse_version, use_cache=use_cache)
            for r in requests_
        ]

    def cache_size(self, warehouse_version: Optional[str] = None) -> int:
        with self._lock:
            return len(self._snapshot(warehouse_version).cache)
