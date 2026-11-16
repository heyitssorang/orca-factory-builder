import unittest

from helpers import make_env
from opsconsole.errors import NotFound


class RoleTests(unittest.TestCase):
    def test_members_and_tools(self):
        _, store, _, _ = make_env()
        store.create_role("reporters", tools=["user-report"])
        store.add_member("reporters", "alice")
        self.assertEqual(store.members("reporters"), ["alice"])
        self.assertEqual(store.tools_for("alice"), {"user-report"})
        self.assertEqual(store.tools_for("bob"), set())

    def test_unknown_role(self):
        _, store, _, _ = make_env()
        with self.assertRaises(NotFound):
            store.members("nope")


if __name__ == "__main__":
    unittest.main()
