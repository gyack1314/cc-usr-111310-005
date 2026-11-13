"""双截止时点回放层测试。

覆盖：未来到达旧事件、更正回溯、最新事件删除（回退/missing 两种策略）、
显式 null 不回退、TTL 等号与过期即停、重复请求独立、缓存键隔离、
版本不可变与事务原子性、索引与枚举参考等价、索引不做全库扫描。
"""
import random
import unittest
from typing import Any

from warehouse_data.replay import (
    Candidate,
    CommitError,
    FeatureIndex,
    FeaturePolicy,
    FeatureRequest,
    FeatureStore,
    MissingReason,
    Op,
    ResultStatus,
    resolve_by_enumeration,
)
from warehouse_data.replay.engine import resolve_with_index

FALLBACK = FeaturePolicy("temp", ttl=20, delete_policy="fallback")
MISSING_POLICY = FeaturePolicy("state", ttl=20, delete_policy="missing")
LONG = FeaturePolicy("long", ttl=10**9, delete_policy="fallback")


def rec(rid, entity, feature, et, at, rev=1, prio=1, op=Op.VALUE, value=None):
    if op is Op.DELETE:
        return Candidate(
            record_id=rid, entity=entity, feature=feature,
            event_time=et, available_time=at, revision=rev,
            source_priority=prio, op=op,
        )
    return Candidate(
        record_id=rid, entity=entity, feature=feature,
        event_time=et, available_time=at, revision=rev,
        source_priority=prio, op=op, value=value,
    )


def req(rid, entity, feature, ec, kc):
    return FeatureRequest(rid, entity, feature, ec, kc)


def build_store(records, policies=(), version="v1", policy_version="p1"):
    store = FeatureStore()
    store.commit(version, records, policies or (FALLBACK, MISSING_POLICY, LONG),
                 policy_version)
    return store


class CutoffSemanticsTests(unittest.TestCase):
    def setUp(self):
        # temp(d1): 事件 10 两条修订（r2 在 t=30 才到库）；事件 20 显式 null；
        # 事件 25 delete；另有一条事件 5 的迟到候选 t=100 才到库。
        self.records = [
            rec("a1", "d1", "temp", 10, 12, rev=1, value=10),
            rec("a2", "d1", "temp", 10, 30, rev=2, value=99),
            rec("a3", "d1", "temp", 20, 20, value=None),
            rec("a4", "d1", "temp", 25, 26, op=Op.DELETE),
            rec("a5", "d1", "temp", 5, 100, value=1),
            # state: 同事件布局但删除策略为 missing
            rec("b1", "d1", "state", 10, 10, value="ok"),
            rec("b2", "d1", "state", 25, 26, op=Op.DELETE),
            # long: 无 TTL 压力，用于多级 delete 回退
            rec("c1", "d1", "long", 30, 31, op=Op.DELETE),
            rec("c2", "d1", "long", 20, 21, op=Op.DELETE),
            rec("c3", "d1", "long", 10, 11, value=7),
        ]
        self.store = build_store(self.records)

    def test_future_arrival_to_old_event_not_visible(self):
        # cutoff 15/15：事件 5 的候选 t=100 才到库，不可见；命中事件 10 旧修订
        r = self.store.replay(req("q", "d1", "temp", 15, 15))
        self.assertEqual(r.status, ResultStatus.VALUE)
        self.assertEqual(r.hit_record_id, "a1")
        self.assertEqual(r.value, 10)
        self.assertTrue(all(
            e.event_time is None or e.event_time <= 15 for e in r.exclusions
        ))

    def test_correction_backfill_visible_after_available_time(self):
        # 同 event_cutoff=15，knowledge_cutoff 推进到 35：更正修订 a2 已可得并胜出
        early = self.store.replay(req("q1", "d1", "temp", 15, 15))
        later = self.store.replay(req("q2", "d1", "temp", 15, 35))
        self.assertEqual(early.value, 10)
        self.assertEqual(later.value, 99)
        self.assertEqual(later.hit_record_id, "a2")
        # 高修订在 knowledge_cutoff 之后不能压掉当时可得的旧修订
        blocked = self.store.replay(req("q3", "d1", "temp", 15, 29))
        self.assertEqual(blocked.hit_record_id, "a1")
        self.assertIn("a2", [e.record_id for e in blocked.exclusions])

    def test_explicit_null_is_hit_and_never_falls_back(self):
        # 事件 25 是 delete，回退到事件 20；事件 20 是显式 null，命中即停
        r = self.store.replay(req("q", "d1", "temp", 30, 30))
        self.assertEqual(r.status, ResultStatus.VALUE)
        self.assertIsNone(r.value)
        self.assertEqual(r.hit_record_id, "a3")
        chain = [e.record_id for e in r.exclusions]
        self.assertIn("a4", chain)  # delete 被跳过
        self.assertNotIn("a1", chain)  # 没有继续回退到事件 10

    def test_delete_policy_missing_vs_fallback(self):
        # state 策略 missing：事件 25 delete 直接 missing，不回退事件 10
        st = self.store.replay(req("q", "d1", "state", 30, 30))
        self.assertEqual(st.status, ResultStatus.MISSING)
        self.assertEqual(st.missing_reason, MissingReason.DELETE_POLICY_MISSING)
        # temp 策略 fallback：同样 delete 却继续回退（命中 null）
        tp = self.store.replay(req("q", "d1", "temp", 30, 30))
        self.assertEqual(tp.status, ResultStatus.VALUE)

    def test_multi_level_delete_fallback_chain(self):
        r = self.store.replay(req("q", "d1", "long", 40, 40))
        self.assertEqual(r.hit_record_id, "c3")
        self.assertEqual(r.value, 7)
        skipped = [e.record_id for e in r.exclusions
                   if e.reason.value == "delete_skipped"]
        self.assertEqual(skipped, ["c1", "c2"])  # 从新到旧依次回退

    def test_ttl_equality_boundary_is_usable(self):
        # event 10 + ttl 20，event_cutoff=30：age=20==ttl，等号可用
        r = self.store.replay(req("eq", "d1", "temp", 30, 15))
        self.assertEqual(r.status, ResultStatus.VALUE)
        self.assertEqual(r.hit_record_id, "a1")
        j = r.ttl_judgments[0]
        self.assertTrue(j.fresh)
        self.assertEqual((j.age, j.ttl), (20, 20))

    def test_ttl_expired_stops_and_never_borrows_older(self):
        # 专用夹具：事件 25 delete（新鲜、fallback 跳过），事件 10 的值
        # age=22>ttl=20 已过期。过期即停，不能借事件 10 声称新鲜。
        ttl_store = build_store([
            rec("d", "e1", "temp", 25, 26, op=Op.DELETE),
            rec("old", "e1", "temp", 10, 11, value=1),
        ])
        r = ttl_store.replay(req("q", "e1", "temp", 32, 35))
        self.assertEqual(r.status, ResultStatus.MISSING)
        self.assertEqual(r.missing_reason, MissingReason.TTL_EXPIRED)
        self.assertFalse(r.ttl_judgments[-1].fresh)
        self.assertEqual(r.ttl_judgments[-1].age, 22)
        self.assertIsNone(r.hit_record_id)

    def test_ttl_expired_on_newest_group_stops_immediately(self):
        # 最新可见赢家本身就过期：连回退扫描都不允许开始
        ttl_store = build_store([
            rec("old", "e1", "temp", 1, 2, value=1),
            rec("older", "e1", "temp", 0, 2, value=2),
        ])
        r = ttl_store.replay(req("q", "e1", "temp", 30, 30))
        self.assertEqual(r.missing_reason, MissingReason.TTL_EXPIRED)
        self.assertEqual(len(r.ttl_judgments), 1)  # 更老事件未被扫描
        self.assertIsNone(r.hit_record_id)

    def test_missing_reasons_are_distinguished(self):
        no_entity = self.store.replay(req("q", "ghost", "temp", 30, 30))
        self.assertEqual(no_entity.missing_reason, MissingReason.NO_ENTITY)

        no_candidate_store = build_store([
            rec("only", "d2", "state", 10, 10, value="x"),
        ])
        no_candidate = no_candidate_store.replay(
            req("q", "d2", "temp", 30, 30)
        )
        # d2 实体存在（state 有记录），但 temp 特征无任何候选
        self.assertEqual(
            no_candidate.missing_reason, MissingReason.NO_AVAILABLE_CANDIDATE
        )

        # 桶里有候选但全部晚于 knowledge_cutoff
        fresh_store = build_store([rec("x1", "d9", "temp", 10, 50, value=3)])
        r = fresh_store.replay(req("q", "d9", "temp", 30, 20))
        self.assertEqual(
            r.missing_reason, MissingReason.NO_AVAILABLE_CANDIDATE
        )

        unknown = self.store.replay(req("q", "d1", "nope", 30, 30))
        self.assertEqual(unknown.missing_reason, MissingReason.UNKNOWN_FEATURE)

    def test_duplicate_requests_are_independent_outputs(self):
        # 同 request_id 重复、同 entity 多请求：按 request_id 各自输出
        batch = [
            req("same", "d1", "temp", 15, 15),
            req("same", "d1", "temp", 15, 15),
            req("other", "d1", "temp", 15, 35),
        ]
        out = self.store.replay_batch(batch)
        self.assertEqual([r.request_id for r in out], ["same", "same", "other"])
        self.assertEqual([r.value for r in out], [10, 10, 99])
        self.assertEqual(len(out), 3)


class PrecedenceTests(unittest.TestCase):
    def test_revision_then_priority_then_record_id(self):
        records = [
            rec("r-low", "e", "f", 10, 10, rev=1, prio=9, value=1),
            rec("r-high", "e", "f", 10, 10, rev=2, prio=1, value=2),
            rec("r-mid", "e", "f", 10, 10, rev=1, prio=5, value=3),
        ]
        store = build_store(records, policies=(FeaturePolicy("f", 50, "fallback"),))
        r = store.replay(req("q", "e", "f", 20, 20))
        self.assertEqual(r.hit_record_id, "r-high")  # 高修订胜出，与优先级无关
        reasons = {e.record_id: e.reason.value for e in r.exclusions}
        self.assertEqual(reasons["r-low"], "lower_revision")
        self.assertEqual(reasons["r-mid"], "lower_revision")

    def test_same_revision_priority_and_record_id(self):
        records = [
            rec("z", "e", "f", 10, 10, rev=1, prio=5, value=1),
            rec("m", "e", "f", 10, 10, rev=1, prio=5, value=2),
            rec("p", "e", "f", 10, 10, rev=1, prio=5, value=3),
        ]
        store = build_store(records, policies=(FeaturePolicy("f", 50, "fallback"),))
        r = store.replay(req("q", "e", "f", 20, 20))
        # 修订与来源优先级全平，record_id Unicode 序决胜
        self.assertEqual(r.hit_record_id, "z")
        reasons = {e.record_id: e.reason.value for e in r.exclusions}
        self.assertEqual(reasons["m"], "lower_record_id")
        self.assertEqual(reasons["p"], "lower_record_id")
        # 冲突信息保留：败者 detail 写明败因键
        self.assertTrue(all(e.detail for e in r.exclusions))

    def test_last_arriving_does_not_win(self):
        # “最后到库”的更正若 revision 不更高，不得仅凭到库顺序胜出
        records = [
            rec("old", "e", "f", 10, 10, rev=2, value=1),
            rec("late", "e", "f", 10, 40, rev=1, value=2),
        ]
        store = build_store(records, policies=(FeaturePolicy("f", 50, "fallback"),))
        r = store.replay(req("q", "e", "f", 50, 50))
        self.assertEqual(r.hit_record_id, "old")


class VersionAndCacheTests(unittest.TestCase):
    def setUp(self):
        self.store = FeatureStore()
        # 缓存隔离测试需要同事件的更正修订在 knowledge_cutoff 之后才可得
        self.store.commit(
            "w1",
            [rec("v1r", "d1", "temp", 10, 12, rev=1, value=10),
             rec("v1b", "d1", "temp", 10, 30, rev=2, value=99)],
            (FALLBACK,), "p1",
        )

    def test_late_correction_goes_to_new_version_old_version_frozen(self):
        store = FeatureStore()
        store.commit(
            "w1",
            [rec("v1r", "d1", "temp", 10, 12, rev=1, value=10)],
            (FALLBACK,), "p1",
        )
        old = store.replay(req("q", "d1", "temp", 15, 35), "w1")
        self.assertEqual(old.hit_record_id, "v1r")

        store.commit(
            "w2",
            [rec("v2r", "d1", "temp", 10, 30, rev=2, value=99)],
            (FALLBACK,), "p1",
        )
        # 固定旧版本重放：候选集合不可变，结果一致
        pinned_old = store.replay(req("q", "d1", "temp", 15, 35), "w1")
        self.assertEqual(pinned_old.hit_record_id, "v1r")
        # 新版本能看到更正
        new = store.replay(req("q", "d1", "temp", 15, 35), "w2")
        self.assertEqual(new.hit_record_id, "v2r")
        self.assertEqual(store.versions(), ("w1", "w2"))
        self.assertEqual(store.head_version, "w2")

    def test_replay_same_double_cutoff_is_stable(self):
        r1 = self.store.replay(req("q", "d1", "temp", 15, 15), "w1")
        r2 = self.store.replay(req("q", "d1", "temp", 15, 15), "w1")
        self.assertEqual(r1.to_dict(), r2.to_dict())

    def test_cache_key_includes_both_cutoffs_and_policy_version(self):
        a = self.store.replay(req("q", "d1", "temp", 15, 15))
        a_again = self.store.replay(req("q", "d1", "temp", 15, 15))
        b = self.store.replay(req("q", "d1", "temp", 15, 35))
        self.assertIs(a, a_again)  # 同双截止命中缓存，复用同一结果
        self.assertIsNot(a, b)
        self.assertNotEqual(a.value, b.value)
        # 缓存条数区分双截止，不互借
        self.assertEqual(self.store.cache_size(), 2)

        # 策略版本变化也是不同缓存项
        self.store.commit("w3", [], (FALLBACK,), "p2")
        c = self.store.replay(req("q", "d1", "temp", 15, 15), "w3")
        self.assertEqual(c.policy_version, "p2")
        self.assertEqual(self.store.cache_size("w3"), 1)

    def test_commit_is_atomic_and_rejects_bad_batches(self):
        before = self.store.versions()
        with self.assertRaises(CommitError):
            self.store.commit(
                "bad",
                [rec("dup", "d1", "temp", 10, 12, value=1),
                 rec("dup", "d1", "temp", 11, 12, value=2)],
                (FALLBACK,), "p1",
            )
        with self.assertRaises(CommitError):
            self.store.commit("bad2", [rec("v1r", "d1", "temp", 1, 1)],
                              (FALLBACK,), "p1")  # record_id 跨版本复用
        with self.assertRaises(CommitError):
            self.store.commit("bad3", [rec("x", "d1", "ghost", 1, 1)],
                              (FALLBACK,), "p1")  # 未注册特征
        with self.assertRaises(CommitError):
            self.store.commit("w1", [], (FALLBACK,), "p1")  # 版本不可覆盖
        self.assertEqual(self.store.versions(), before)
        self.assertEqual(self.store.head_version, "w1")

    def test_policy_per_feature_independent_ttl_and_delete(self):
        records = [
            rec("t", "d1", "temp", 10, 10, value=1),
            rec("s", "d1", "state", 10, 10, value="x"),
        ]
        store = build_store(records)
        # temp ttl=20 等号可用；state ttl=20 同口径，验证策略对象互不串用
        r1 = store.replay(req("q", "d1", "temp", 30, 30))
        r2 = store.replay(req("q", "d1", "state", 30, 30))
        self.assertTrue(r1.ttl_judgments[0].fresh)
        self.assertTrue(r2.ttl_judgments[0].fresh)
        self.assertEqual(r1.ttl_judgments[0].ttl, 20)
        self.assertEqual(r2.ttl_judgments[0].ttl, 20)


class IndexVsEnumerationTests(unittest.TestCase):
    def test_equivalence_on_fixture_scenarios(self):
        records = [
            rec("a1", "d1", "temp", 10, 12, rev=1, value=10),
            rec("a2", "d1", "temp", 10, 30, rev=2, value=99),
            rec("a3", "d1", "temp", 20, 20, value=None),
            rec("a4", "d1", "temp", 25, 26, op=Op.DELETE),
            rec("a5", "d1", "temp", 5, 100, value=1),
            rec("b1", "d1", "state", 10, 10, value="ok"),
            rec("b2", "d1", "state", 25, 26, op=Op.DELETE),
            rec("z1", "d8", "temp", 0, 0, value=4),
        ]
        policies = (FALLBACK, MISSING_POLICY)
        index = FeatureIndex(records)
        cutoffs = [(ec, kc) for ec in range(0, 40, 4) for kc in range(0, 110, 9)]
        for feature, policy in (("temp", FALLBACK), ("state", MISSING_POLICY)):
            for ec, kc in cutoffs:
                q = req("q", "d1", feature, ec, kc)
                got = resolve_with_index(q, policy, index, "w", "p")
                ref = resolve_by_enumeration(q, policy, records, "w", "p")
                self._assert_core_equal(ref, got, (feature, ec, kc))

    def test_randomized_equivalence(self):
        rng = random.Random(20261113)
        features = ("temp", "state")
        records = []
        for i in range(180):
            records.append(rec(
                f"r{i:04d}",
                f"e{rng.randrange(8)}",
                rng.choice(features),
                rng.randrange(0, 100),
                rng.randrange(0, 120),
                rev=rng.randrange(1, 5),
                prio=rng.randrange(1, 4),
                op=Op.DELETE if rng.random() < 0.18 else Op.VALUE,
                value=None if rng.random() < 0.2 else rng.randrange(1000),
            ))
        index = FeatureIndex(records)
        for _ in range(400):
            q = req(
                "q", f"e{rng.randrange(10)}", rng.choice(features),
                rng.randrange(0, 130), rng.randrange(0, 130),
            )
            policy = FALLBACK if q.feature == "temp" else MISSING_POLICY
            got = resolve_with_index(q, policy, index, "w", "p")
            ref = resolve_by_enumeration(q, policy, records, "w", "p")
            self._assert_core_equal(ref, got, q)

    @staticmethod
    def _assert_core_equal(ref, got, tag):
        for attr in (
            "status", "value", "hit_record_id", "hit_event_time",
            "missing_reason",
        ):
            unittest.TestCase().assertEqual(
                getattr(ref, attr), getattr(got, attr),
                msg=f"{tag} 字段 {attr} 不一致",
            )
        unittest.TestCase().assertEqual(
            [(j.record_id, j.event_time, j.fresh, j.age)
             for j in ref.ttl_judgments],
            [(j.record_id, j.event_time, j.fresh, j.age)
             for j in got.ttl_judgments],
            msg=f"{tag} TTL 判定不一致",
        )


class IndexScanBoundedTests(unittest.TestCase):
    def test_no_full_scan_per_request(self):
        # 大量候选落在 event_cutoff 之后；正式索引绝不能逐请求访问它们
        records = [rec("hit", "d1", "temp", 5, 5, value=1)]
        records += [
            rec(f"future-{i}", "d1", "temp", 1000 + i, 1000 + i, value=i)
            for i in range(500)
        ]
        records += [
            rec(f"other-{i}", f"e{i}", "temp", 10, 10, value=i)
            for i in range(500)
        ]
        index = FeatureIndex(records)

        visited: list[int] = []
        original = index.scan_groups

        def guarded(entity, feature, event_cutoff, knowledge_cutoff):
            out = original(entity, feature, event_cutoff, knowledge_cutoff)
            for group, _, _ in out:
                visited.append(group.event_time)
                assert group.event_time <= event_cutoff
            return out

        index.scan_groups = guarded  # type: ignore[method-assign]
        policy = FeaturePolicy("temp", 10**9, "fallback")
        r = resolve_with_index(req("q", "d1", "temp", 100, 100),
                               policy, index, "w", "p")
        self.assertEqual(r.hit_record_id, "hit")
        self.assertTrue(visited)
        self.assertLess(
            len(visited), 10,
            "单请求只应触达 cutoff 内的少量事件组，而非全库",
        )


class ModelValidationTests(unittest.TestCase):
    def test_bad_shapes(self):
        with self.assertRaises(ValueError):
            rec("x", "e", "f", 1.0, 1)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            rec("x", "e", "f", True, 1)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            Candidate("x", "e", "f", 1, 1, -1, 1, Op.VALUE, 1)
        with self.assertRaises(ValueError):
            Candidate("x", "e", "f", 1, 1, 1, 1, Op.DELETE, "v")
        with self.assertRaises(ValueError):
            FeaturePolicy("f", -1, "fallback")
        with self.assertRaises(ValueError):
            FeaturePolicy("f", 1, "explode")
        with self.assertRaises(ValueError):
            req("", "e", "f", 1, 1)

    def test_value_type_consistency_enforced_per_feature(self):
        store = FeatureStore()
        with self.assertRaises(CommitError):
            store.commit("w", [
                rec("i", "e", "f", 1, 1, value=1),
                rec("s", "e", "f", 2, 2, value="x"),
            ], (FeaturePolicy("f", 10, "fallback"),), "p")


if __name__ == "__main__":
    unittest.main()
