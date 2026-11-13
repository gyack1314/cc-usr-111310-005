"""把冻结 JSON 资料适配为回放层对象，并驱动整份清单重放。"""
from __future__ import annotations

from typing import Any, Optional

from ..io import integer
from .models import Candidate, FeaturePolicy, FeatureRequest
from .store import FeatureStore


def build_policies(raw: dict[str, Any]) -> list[FeaturePolicy]:
    return [
        FeaturePolicy(
            name=f["name"],
            ttl=integer(f["ttl"]),
            delete_policy=f["delete_policy"],
        )
        for f in raw["features"]
    ]


def build_candidates(raw: dict[str, Any]) -> list[Candidate]:
    return [Candidate.from_raw(r) for r in raw["records"]]


def build_requests(raw: dict[str, Any]) -> list[FeatureRequest]:
    """清单请求本身不带 feature，按全部注册特征各展开一条。"""
    out: list[FeatureRequest] = []
    for q in raw["requests"]:
        for f in raw["features"]:
            out.append(FeatureRequest.from_raw(q, f["name"]))
    return out


def build_store(raw: dict[str, Any]) -> FeatureStore:
    """从一份不可变清单构建单版本仓。"""
    store = FeatureStore()
    store.commit(
        warehouse_version=raw["warehouse_version"],
        candidates=build_candidates(raw),
        policies=build_policies(raw),
        policy_version=raw["policy_version"],
    )
    return store


def replay_material(raw: dict[str, Any],
                    warehouse_version: Optional[str] = None) -> list[dict[str, Any]]:
    """便利函数：装仓、展开请求、回放并返回可 JSON 序列化的解释结果。"""
    store = build_store(raw)
    results = store.replay_batch(build_requests(raw), warehouse_version)
    return [r.to_dict() for r in results]
