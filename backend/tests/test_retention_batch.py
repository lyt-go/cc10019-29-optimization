"""留存批次明细服务测试：合并扫描、续做、复用、补报与 LTV 校准。"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.store import store
from app.services.retention_batch import (
    BATCH_CONFIRMED,
    DONE,
    PENDING,
    RECALC,
    confirm_retention_batch,
    get_confirmed_retention_curve,
    get_retention_batch,
    register_retention_batch,
    report_cohort_supplement,
    resume_retention_batch,
    retry_retention_batch,
    submit_retention_batch,
)
from app.services.lifetime_value import ltv_with_retention, predict_ltv


class RetentionBatchTestBase(unittest.TestCase):
    def setUp(self) -> None:
        store._artifacts.clear()
        store._compute_cache.clear()
        store._snapshots.clear()
        store._tables["retention_batch"] = []
        store._tables["retention_queue"] = []
        store._next_ids["retention_batch"] = 1
        store._next_ids["retention_queue"] = 1


class TestBatchMergeAndResume(RetentionBatchTestBase):
    def test_same_cohort_reads_input_once(self) -> None:
        """同一时段、同一分段维度的多组只读取并聚合一次事件。"""
        # RC-001 与 RC-002 都从 2026-11-01 起算，但窗口/维度不同，按输入指纹复用。
        batch = submit_retention_batch(["RC-001", "RC-001"])
        self.assertEqual(batch["group_count"], 1)  # 重复配置去重

        batch = register_retention_batch(["RC-001", "RC-002"])
        # 两组窗口不同（7 / 30），事件输入指纹不同，聚合各建一次。
        batch = submit_retention_batch(["RC-001", "RC-002"])
        self.assertEqual(batch["status"], "待确认")
        self.assertGreaterEqual(batch["input_scans"], 1)

    def test_resume_only_computes_pending(self) -> None:
        """续做只补算未完成组，已算组保留原版本。"""
        batch = submit_retention_batch(["RC-001", "RC-002"], compute_limit=1)
        first = batch["groups"][0]
        second = batch["groups"][1]
        self.assertEqual(first["status"], DONE)
        self.assertEqual(second["status"], PENDING)
        first_version = first["result_version"]
        first_rates = list(first["retention_rates"]["2026-11-01"])

        resumed = resume_retention_batch(batch["batch_id"])
        self.assertEqual(resumed["groups"][0]["result_version"], first_version)
        self.assertEqual(resumed["groups"][0]["retention_rates"]["2026-11-01"], first_rates)
        self.assertEqual(resumed["groups"][1]["status"], DONE)
        self.assertEqual(resumed["pending_count"], 0)

    def test_confirmed_result_is_reused(self) -> None:
        """确认后的同条件批次直接复用结果，不再重算。"""
        first = submit_retention_batch(["RC-001"])
        confirm_retention_batch(first["batch_id"])

        second = submit_retention_batch(["RC-001"])
        group = second["groups"][0]
        self.assertTrue(group["reused"])
        self.assertEqual(group["reused_from"]["batch_id"], first["batch_id"])
        self.assertEqual(second["input_scans"], 0)

        detail = get_retention_batch(second["batch_id"])
        reused_groups = [g for g in detail["groups"] if g["reused"]]
        self.assertEqual(len(reused_groups), 1)


class TestSupplementAndRetry(RetentionBatchTestBase):
    def test_supplement_only_corrects_completed_group(self) -> None:
        """同批还有一组待算时，补报只更正已算完的首组。"""
        batch = submit_retention_batch(["RC-001", "RC-002"], compute_limit=1)
        outcome = report_cohort_supplement(
            batch["batch_id"],
            corrections=[
                {"config_id": "RC-001", "cohort_denominator": 999},
                {"config_id": "RC-002", "cohort_denominator": 500},
            ],
        )
        self.assertEqual(len(outcome["applied"]), 1)
        self.assertEqual(outcome["applied"][0]["config_id"], "RC-001")
        self.assertEqual(len(outcome["skipped"]), 1)
        self.assertEqual(outcome["skipped"][0]["config_id"], "RC-002")

        detail = get_retention_batch(batch["batch_id"])
        by_config = {g["config_id"]: g for g in detail["groups"]}
        self.assertEqual(by_config["RC-001"]["status"], RECALC)
        self.assertEqual(by_config["RC-001"]["cohort_denominator"], 999)
        self.assertEqual(by_config["RC-002"]["status"], PENDING)  # 没被顺带算掉

    def test_supplement_rejects_pending_group(self) -> None:
        batch = submit_retention_batch(["RC-001", "RC-002"], compute_limit=1)
        with self.assertRaises(ValueError):
            report_cohort_supplement(
                batch["batch_id"], config_id="RC-002", cohort_denominator=800
            )

    def test_retry_summary_explains_affected_calcs(self) -> None:
        """重试要说明哪些计算受此次更正影响。"""
        batch = submit_retention_batch(["RC-001", "RC-002"], compute_limit=1)
        report_cohort_supplement(
            batch["batch_id"], config_id="RC-001", cohort_denominator=400, remark="补报渠道漏记"
        )
        retried = retry_retention_batch(batch["batch_id"])
        summary = retried["retry_summary"]

        affected_ids = [item["config_id"] for item in summary["affected_by_correction"]]
        self.assertIn("RC-001", affected_ids)
        self.assertNotIn("RC-002", affected_ids)
        self.assertIn("RC-001", summary["message"])

        by_config = {g["config_id"]: g for g in retried["groups"]}
        self.assertEqual(by_config["RC-001"]["status"], DONE)
        self.assertEqual(by_config["RC-001"]["cohort_denominator"], 400)
        # 分母变了，0 日留存率应正好是 1.0。
        self.assertEqual(by_config["RC-001"]["retention_rates"]["2026-11-01"][0], 1.0)
        self.assertEqual(by_config["RC-001"]["result_version"], 2)
        self.assertEqual(
            by_config["RC-001"]["correction"]["resolved_version"], 2
        )

    def test_corrected_version_is_not_reused(self) -> None:
        """补报后旧版本失效，新的同条件批次必须重算。"""
        first = submit_retention_batch(["RC-001"])
        confirm_retention_batch(first["batch_id"])
        report_cohort_supplement(
            first["batch_id"], config_id="RC-001", cohort_denominator=300
        )

        second = submit_retention_batch(["RC-001"])
        self.assertFalse(second["groups"][0]["reused"])
        self.assertEqual(second["groups"][0]["cohort_denominator"], 300)


class TestConfirmedCurveForLtv(RetentionBatchTestBase):
    def test_unconfirmed_batch_is_rejected(self) -> None:
        """预测端不能把尚未确认的入组数据与已有曲线混着用。"""
        batch = submit_retention_batch(["RC-001"])
        predict_ltv("LTV-001")
        with self.assertRaises(ValueError) as ctx:
            ltv_with_retention("LTV-001", "RC-001")
        self.assertIn("尚未确认", str(ctx.exception))

        # 显式指定该批次也一样拒绝。
        with self.assertRaises(ValueError):
            ltv_with_retention("LTV-001", "RC-001", batch["batch_id"])

    def test_unresolved_correction_blocks_calibration(self) -> None:
        batch = submit_retention_batch(["RC-001"])
        confirm_retention_batch(batch["batch_id"])
        report_cohort_supplement(
            batch["batch_id"], config_id="RC-001", cohort_denominator=320
        )
        with self.assertRaises(ValueError):
            ltv_with_retention("LTV-001", "RC-001")

    def test_confirmed_curve_drives_valuation_and_is_auditable(self) -> None:
        batch = submit_retention_batch(["RC-001"])
        confirm_retention_batch(batch["batch_id"])
        predict_ltv("LTV-001")

        calibrated = ltv_with_retention("LTV-001", "RC-001")
        self.assertEqual(calibrated["batch_id"], batch["batch_id"])
        self.assertEqual(calibrated["result_version"], 1)
        self.assertEqual(len(calibrated["calibration_curve"]), 7)
        self.assertIn("avg_retention_rate", calibrated)

        used = calibrated["groups_used"][0]
        self.assertEqual(used["config_id"], "RC-001")
        self.assertEqual(used["result_version"], 1)
        self.assertIn("cohort_denominator", used)

    def test_reused_curve_reports_source(self) -> None:
        first = submit_retention_batch(["RC-001"])
        confirm_retention_batch(first["batch_id"])
        second = submit_retention_batch(["RC-001"])
        confirm_retention_batch(second["batch_id"])

        curve = get_confirmed_retention_curve("RC-001")
        self.assertEqual(curve["batch_id"], second["batch_id"])
        self.assertTrue(curve["reused"])
        self.assertEqual(curve["reused_from"]["batch_id"], first["batch_id"])

    def test_calibration_records_stale_after_correction(self) -> None:
        """重新计算后还能核对估值用的具体组别。"""
        batch = submit_retention_batch(["RC-001"])
        confirm_retention_batch(batch["batch_id"])
        predict_ltv("LTV-001")
        ltv_with_retention("LTV-001", "RC-001")

        report_cohort_supplement(
            batch["batch_id"], config_id="RC-001", cohort_denominator=360
        )
        retried = retry_retention_batch(batch["batch_id"])
        self.assertEqual(retried["retry_summary"]["affected_by_correction"][0]["config_id"], "RC-001")
        stale = retried["retry_summary"]["stale_valuations"]
        self.assertEqual(stale[0]["model_id"], "LTV-001")
        self.assertEqual(stale[0]["used_result_version"], 1)
        self.assertEqual(stale[0]["current_result_version"], 2)

        confirm_retention_batch(batch["batch_id"])
        recalibrated = ltv_with_retention("LTV-001", "RC-001")
        self.assertEqual(recalibrated["result_version"], 2)
        self.assertEqual(
            recalibrated["cohort_denominator"],
            retried["groups"][0]["cohort_denominator"],
        )
        self.assertEqual(recalibrated["groups_used"][0]["batch_id"], batch["batch_id"])


class TestBatchDetailAudit(RetentionBatchTestBase):
    def test_batch_detail_lists_versions_and_windows(self) -> None:
        batch = submit_retention_batch(["RC-001", "RC-002"])
        detail = get_retention_batch(batch["batch_id"])
        for group in detail["groups"]:
            self.assertIn("cohort_denominator", group)
            self.assertIn("return_window", group)
            self.assertIn("result_version", group)
            self.assertEqual(group["status"], DONE)
        self.assertEqual(detail["status"], "待确认")


if __name__ == "__main__":
    unittest.main(verbosity=2)
