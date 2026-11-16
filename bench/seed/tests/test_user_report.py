import unittest

from helpers import make_env
from opsconsole.errors import NotAuthorized
from opsconsole.user_report import user_report


class UserReportTests(unittest.TestCase):
    def test_report_and_audit(self):
        _, store, directory, audit = make_env()
        store.create_role("reporters", tools=["user-report"])
        store.add_member("reporters", "bob")
        r = user_report(store, directory, audit, "bob", "alice")
        self.assertEqual(r, {"user": "alice", "enabled": True, "groups": ["staff", "vpn"]})
        self.assertEqual(len(audit.query(actor="bob", action="user-report")), 1)

    def test_needs_role(self):
        _, store, directory, audit = make_env()
        with self.assertRaises(NotAuthorized):
            user_report(store, directory, audit, "bob", "alice")


if __name__ == "__main__":
    unittest.main()
