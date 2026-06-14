import unittest

from audit_email_truth import collect_truth_table


class AuditEmailTruthContractTests(unittest.TestCase):
    def test_collect_truth_table_returns_startup_contract_without_secrets(self):
        report = collect_truth_table(
            "invoice-user@example.com",
            "fixture-auth-code",
            "2025-11-25",
            "2026-06-14",
        )

        report_text = str(report)
        self.assertEqual(report["status"], "skipped")
        self.assertEqual(report["email_domain"], "example.com")
        self.assertEqual(report["date_from"], "2025-11-25")
        self.assertEqual(report["date_to"], "2026-06-14")
        self.assertNotIn("fixture-auth-code", report_text)
        self.assertNotIn("invoice-user@example.com", report_text)


if __name__ == "__main__":
    unittest.main()
