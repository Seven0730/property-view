import copy
import csv
import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

import property_view as pv


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.policy = pv.load_policy(pv.ROOT / "risk_policy.json")
        self.base = {
            "viewing_id": "x", "viewing_time": "2026-09-05T10:00:00+08:00",
            "status_synced_at": "2026-09-05T01:40:00Z",
            "region_name": "华东大区", "property_category": "测试",
            "available_units": "8", "recent_inquiries": "50", "is_successful": "0",
        }

    def file(self, rows, name="input.csv"):
        path = self.root / name
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        return path

    def record(self, **changes):
        row = dict(self.base, **changes)
        return pv.load_records(self.file([row]))[0][0]

    def upcoming(self):
        row = dict(self.base, decision_time="2026-09-05T09:00:00+08:00",
                   planned_departure_time="2026-09-05T09:30:00+08:00",
                   status_synced_at="2026-09-05T00:40:00Z")
        row.pop("is_successful")
        return row

    def test_timezone_arithmetic(self):
        self.assertEqual(self.record()["age_minutes"], 20)
        self.assertEqual(self.record(status_synced_at="2026-09-05T09:40:00+08:00")["age_minutes"], 20)

    def test_duplicates_and_conflicting_id(self):
        _, quality = pv.load_records(self.file([self.base, self.base]))
        self.assertEqual(quality["duplicate_rows_removed"], 1)
        with self.assertRaisesRegex(ValueError, "内容冲突"):
            pv.load_records(self.file([self.base, dict(self.base, is_successful="1")]))

    def test_missing_outcome_not_success(self):
        result = pv.evaluate([self.record(is_successful="")], self.policy)
        self.assertEqual(result["unknown"], 1)
        self.assertEqual(result["known"], 0)
        self.assertIsNone(result["baseline_failure_rate_known"])
        self.assertEqual(result["expected_net_minutes_saved"], -20)
        self.assertFalse(result["simulation_target_checks"]["failure_reduction"])

    def test_invalid_result_not_silently_accepted(self):
        with self.assertRaisesRegex(ValueError, "is_successful"):
            self.record(is_successful="2")

    def test_invalid_inventory_and_inquiry(self):
        for column, value in [("available_units", "-1"), ("available_units", "nan"),
                              ("available_units", "2.5"), ("recent_inquiries", "inf")]:
            with self.subTest(column=column, value=value), self.assertRaises(ValueError):
                self.record(**{column: value})

    def test_timezone_required(self):
        with self.assertRaisesRegex(ValueError, "必须包含时区"):
            self.record(status_synced_at="2026-09-05T09:40:00")

    def test_future_snapshot_rejected_in_history(self):
        with self.assertRaisesRegex(ValueError, "未来信息"):
            self.record(status_synced_at="2026-09-05T10:01:00+08:00")

    def test_no_outcome_leakage(self):
        failed = self.record()
        changed = dict(failed, is_successful=1, viewing_id="another", recent_inquiries=99999)
        self.assertEqual(pv.risk_reasons(failed, self.policy), pv.risk_reasons(changed, self.policy))

    def test_threshold_and_region_boundaries(self):
        record = self.record()
        self.assertFalse(pv.risk_reasons(dict(record, age_minutes=10), self.policy))
        self.assertEqual(pv.risk_reasons(dict(record, age_minutes=10.01), self.policy), ["STALE_STATUS"])
        self.assertFalse(pv.risk_reasons(dict(record, region_name="华南大区", available_units=4), self.policy))
        self.assertEqual(pv.risk_reasons(dict(record, region_name="华南大区", available_units=3, age_minutes=0), self.policy), ["LOW_STOCK"])
        self.assertEqual(pv.risk_reasons(dict(record, region_name="新大区"), self.policy), ["UNSUPPORTED_REGION"])

    def test_zero_inventory_preserved_with_success(self):
        record = self.record(available_units="0", is_successful="1", region_name="华南大区")
        self.assertEqual(record["is_successful"], 1)
        result = pv.evaluate([record], self.policy)
        self.assertEqual(result["targeted_successes"], 1)
        self.assertEqual(result["targeted_failures"], 0)

    def test_economics_includes_unknown_cost_and_fixed_denominator(self):
        record = self.record()
        # Two failures: one selected, one missed. One healthy selected, one unknown selected.
        rows = [record, dict(record, age_minutes=0),
                dict(record, is_successful=1), dict(record, is_successful=None)]
        result = pv.evaluate(rows, self.policy, preventable=.5, false_unavailable=.1)
        self.assertEqual(result["review_count"], 3)
        self.assertEqual(result["expected_avoided_failures"], .5)
        self.assertEqual(result["expected_remaining_failures"], 1.5)
        self.assertEqual(result["expected_failure_rate_original_known_denominator"], .5)
        self.assertEqual(result["expected_disrupted_successes"], .1)
        self.assertEqual(result["expected_net_minutes_saved"], 0)
        zero = pv.evaluate(rows, self.policy, preventable=0)
        self.assertEqual(zero["expected_net_minutes_saved"], -60)
        self.assertEqual(zero["expected_remaining_failures"], 2)

    def test_predeparture_requires_real_snapshot(self):
        with self.assertRaisesRegex(ValueError, "缺少字段"):
            pv.load_records(self.file([self.base]), mode="predeparture")
        row = self.upcoming()
        records, _ = pv.load_records(self.file([row]), mode="predeparture")
        self.assertEqual(records[0]["age_minutes"], 20)
        with self.assertRaisesRegex(ValueError, "未来信息"):
            pv.load_records(self.file([dict(row, status_synced_at="2026-09-05T09:01:00+08:00")]), mode="predeparture")
        with self.assertRaisesRegex(ValueError, "planned_departure_time"):
            pv.load_records(self.file([dict(row, planned_departure_time=row["decision_time"])]), mode="predeparture")

    def test_live_outcome_column_ignored(self):
        row = dict(self.upcoming(), is_successful="this must not affect scoring")
        records, _ = pv.load_records(self.file([row]), mode="predeparture")
        self.assertIsNone(records[0]["is_successful"])
        self.assertEqual(pv.risk_reasons(records[0], self.policy), ["STALE_STATUS"])

    def resolve_one(self, confirmation="unknown", at="2026-09-05T09:10:00+08:00", property_id="p1", mode="predeparture"):
        records, _ = pv.load_records(self.file([self.upcoming()]), mode="predeparture")
        queue = self.root / "queue.csv"
        pv.write_csv(queue, pv.QUEUE_FIELDS, pv.queue_rows(records, self.policy, mode))
        conf = self.file([dict(viewing_id="x", confirmation=confirmation, confirmed_at=at,
                              confirmed_by="test", property_id=property_id)], "confirm.csv")
        output = self.root / "resolved.csv"
        pv.resolve(Namespace(queue=queue, confirmations=conf, output=output))
        return pv.read_csv(output)[1][0]

    def test_risk_alone_never_cancels(self):
        record = self.record()
        row = next(pv.queue_rows([record], self.policy, "retrospective"))
        self.assertEqual(row["workflow_status"], "pending_review")
        self.assertNotIn("is_successful", row)

    def test_unknown_confirmation_stays_pending(self):
        self.assertEqual(self.resolve_one()["workflow_status"], "pending_review")

    def test_available_and_unavailable_require_specific_property(self):
        self.assertEqual(self.resolve_one("available")["workflow_status"], "confirmed_available")
        self.assertEqual(self.resolve_one("unavailable")["workflow_status"], "confirmed_unavailable")
        for state in ("available", "unavailable"):
            with self.subTest(state=state), self.assertRaisesRegex(ValueError, "具体房源"):
                self.resolve_one(state, property_id="")

    def test_confirmation_after_departure_is_late_even_before_arrival(self):
        self.assertEqual(self.resolve_one("unavailable", at="2026-09-05T09:30:00+08:00")["workflow_status"], "late_confirmation")
        self.assertEqual(self.resolve_one("unavailable", at="2026-09-05T09:45:00+08:00")["workflow_status"], "late_confirmation")

    def test_confirmation_before_decision_rejected(self):
        with self.assertRaisesRegex(ValueError, "早于"):
            self.resolve_one(at="2026-09-05T08:59:00+08:00")

    def test_historical_queue_not_executable(self):
        with self.assertRaisesRegex(ValueError, "历史模拟队列不可执行"):
            self.resolve_one(mode="retrospective")

    def test_invalid_policy_rejected(self):
        changed = copy.deepcopy(self.policy)
        changed["assumptions"]["preventable_fraction"] = 1.1
        path = self.root / "policy.json"
        path.write_text(json.dumps(changed), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "0 到 1"):
            pv.load_policy(path)

    def test_simulation_is_deterministic_and_does_not_change_records(self):
        records = [self.record()]
        before = copy.deepcopy(records)
        self.assertEqual(pv.evaluate(records, self.policy), pv.evaluate(records, self.policy))
        self.assertEqual(records, before)

    def test_missing_queue_confirmations_remain_pending(self):
        records, _ = pv.load_records(self.file([self.upcoming()]), mode="predeparture")
        queue = self.root / "queue.csv"
        pv.write_csv(queue, pv.QUEUE_FIELDS, pv.queue_rows(records, self.policy, "predeparture"))
        conf = self.root / "confirm.csv"
        pv.write_csv(conf, ["viewing_id", "confirmation", "confirmed_at", "confirmed_by", "property_id"], [])
        output = self.root / "resolved.csv"
        pv.resolve(Namespace(queue=queue, confirmations=conf, output=output))
        self.assertEqual(pv.read_csv(output)[1][0]["workflow_status"], "pending_review")


if __name__ == "__main__":
    unittest.main()
