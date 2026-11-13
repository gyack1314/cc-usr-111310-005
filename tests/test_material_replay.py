"""用冻结资料清单端到端验证回放层，并固定关键场景的期望口径。"""
import json
import unittest
from pathlib import Path

from warehouse_data.io import load
from warehouse_data.replay.material import (
    build_requests,
    build_store,
    replay_material,
)
from warehouse_data.replay.models import MissingReason, ResultStatus

DATA = Path(__file__).resolve().parent.parent / "data"


def by_key(rows):
    return {(r["request_id"], r["feature"]): r for r in rows}


class MaterialReplayTests(unittest.TestCase):
    def test_normal_expected_point_in_time_values(self):
        raw, _ = load(DATA / "normal.json")
        out = by_key(replay_material(raw))

        # q1(15/15)：t2 在 t=30 才到库，不能压掉 t1；事件 20/25 在未来
        q1_temp = out[("q1", "temperature")]
        self.assertEqual(q1_temp["status"], ResultStatus.VALUE.value)
        self.assertEqual(q1_temp["hit_record_id"], "t1")
        self.assertEqual(q1_temp["value"], 10)

        q1_state = out[("q1", "state")]
        self.assertEqual(q1_state["hit_record_id"], "s1")
        self.assertEqual(q1_state["value"], "ok")

        # q2(30/30)：最新事件 25 是 delete；temperature 回退到事件 20 的
        # 显式 null（命中缺测，不继续回退）；state 策略 missing 直接缺失
        q2_temp = out[("q2", "temperature")]
        self.assertEqual(q2_temp["status"], ResultStatus.VALUE.value)
        self.assertIsNone(q2_temp["value"])
        self.assertEqual(q2_temp["hit_record_id"], "t3")
        self.assertTrue(any(
            e["record_id"] == "t4" and e["reason"] == "delete_skipped"
            for e in q2_temp["exclusions"]
        ))

        q2_state = out[("q2", "state")]
        self.assertEqual(q2_state["status"], ResultStatus.MISSING.value)
        self.assertEqual(
            q2_state["missing_reason"],
            MissingReason.DELETE_POLICY_MISSING.value,
        )
        # 解释结构完整：TTL 判定与排除链都有内容
        self.assertTrue(q2_temp["ttl_judgments"])
        self.assertTrue(q2_state["exclusions"])

    def test_ttl_equality_fixture(self):
        raw, _ = load(DATA / "ttl-equality.json")
        out = by_key(replay_material(raw))
        temp = out[("eq", "temperature")]
        # knowledge_cutoff=15 挡住 t2；event10/cutoff30 age=20==ttl 恰可用
        self.assertEqual(temp["hit_record_id"], "t1")
        self.assertEqual(temp["ttl_judgments"][0]["age"], 20)
        self.assertTrue(temp["ttl_judgments"][0]["fresh"])
        state = out[("eq", "state")]
        self.assertEqual(state["hit_record_id"], "s1")

    def test_knowledge_cutoff_change_not_borrowed(self):
        raw, _ = load(DATA / "knowledge-cutoff-change.json")
        out = by_key(replay_material(raw))
        early = out[("early", "temperature")]
        later = out[("later-known", "temperature")]
        # 同 event_cutoff=15，knowledge_cutoff 不同：15 -> t1，35 -> t2 更正
        self.assertEqual(early["hit_record_id"], "t1")
        self.assertEqual(later["hit_record_id"], "t2")
        self.assertEqual(later["value"], 99)
        self.assertEqual(early["knowledge_cutoff"], 15)
        self.assertEqual(later["knowledge_cutoff"], 35)

    def test_store_replay_matches_material_helper(self):
        raw, _ = load(DATA / "normal.json")
        store = build_store(raw)
        results = store.replay_batch(build_requests(raw))
        self.assertEqual(
            [r.to_dict() for r in results], replay_material(raw)
        )
        # 每条请求-特征组合独立输出
        self.assertEqual(len(results), len(raw["requests"]) * len(raw["features"]))

    def test_invalid_config_rejected_at_commit(self):
        raw = json.loads((DATA / "invalid-config.json").read_text("utf8"))
        with self.assertRaises(ValueError):
            build_store(raw)


if __name__ == "__main__":
    unittest.main()
