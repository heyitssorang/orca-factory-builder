import unittest

from helpers import make_env


class AuditTests(unittest.TestCase):
    def test_record_and_query(self):
        _, _, _, audit = make_env()
        audit.record("a", "x", "t1")
        audit.record("b", "y", "t2")
        self.assertEqual([r["target"] for r in audit.query(actor="a")], ["t1"])
        self.assertEqual(len(audit.rows()), 2)


if __name__ == "__main__":
    unittest.main()
