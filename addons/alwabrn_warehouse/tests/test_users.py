from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase, new_test_user


@tagged("alwabrn", "post_install", "-at_install")
class TestUserAdmin(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.sysadmin = new_test_user(cls.env, "alw_sysadmin", groups="alwabrn_warehouse.group_alw_admin")
        cls.manager = new_test_user(cls.env, "alw_mgr2", groups="alwabrn_warehouse.group_alw_manager")
        cls.Users = cls.env["res.users"].with_user(cls.sysadmin)

    def _login_ok(self, user, password):
        try:
            user.with_user(user)._check_credentials(
                {"login": user.login, "password": password, "type": "password"}, {"interactive": False})
            return True
        except Exception:
            return False

    def test_01_admin_creates_user_with_role_and_password(self):
        u = self.Users.create({"name": "موظف جديد", "login": "new_emp",
                               "alw_role": "employee", "alw_new_password": "Najran2026"})
        self.assertEqual(u.alw_role, "employee")
        self.assertTrue(u.has_group("alwabrn_warehouse.group_alw_employee"))
        self.assertFalse(u.has_group("alwabrn_warehouse.group_alw_manager"))
        self.assertTrue(self._login_ok(u, "Najran2026"))
        self.assertFalse(self._login_ok(u, "wrong-pass1"))
        # change role: old group removed, new one added
        u.alw_role = "auditor"
        self.assertTrue(u.has_group("alwabrn_warehouse.group_alw_auditor"))
        self.assertFalse(u.has_group("alwabrn_warehouse.group_alw_employee"))
        u.alw_role = "none"
        self.assertEqual(u.alw_role, "none")
        logs = self.env["alwabrn.approval"].search([("res_model", "=", "res.users"), ("res_id", "=", u.id)])
        self.assertTrue(logs)
        self.assertNotIn("Najran2026", " ".join((l.note or "") + (l.details or "") for l in logs))

    def test_02_weak_password_rejected(self):
        u = self.Users.create({"name": "س", "login": "weak_user", "alw_role": "auditor",
                               "alw_new_password": "Strong123"})
        for bad in ("short1", "onlyletters", "12345678", "weak_user"):
            with self.assertRaises(UserError):
                u.alw_set_password(bad)
        wiz = self.env["alwabrn.user.password.wizard"].with_user(self.sysadmin).create(
            {"user_id": u.id, "new_password": "Another123", "confirm_password": "Mismatch123"})
        with self.assertRaises(UserError):
            wiz.action_apply()
        wiz.confirm_password = "Another123"
        wiz.action_apply()
        self.assertFalse(wiz.new_password)
        self.assertTrue(self._login_ok(u, "Another123"))

    def test_03_non_admin_cannot_manage_users(self):
        target = new_test_user(self.env, "alw_target", groups="alwabrn_warehouse.group_alw_employee")
        with self.assertRaises(AccessError):
            target.with_user(self.manager).alw_set_password("Najran2026")
        with self.assertRaises(AccessError):
            target.with_user(self.manager).write({"alw_role": "admin"})

    def test_04_admin_cannot_lock_self_out(self):
        me = self.sysadmin.with_user(self.sysadmin)
        with self.assertRaises(UserError):
            me.alw_role = "employee"
        with self.assertRaises(UserError):
            me.action_alw_archive()

    def test_05_supervisor_sites_and_archive(self):
        site = self.env["alwabrn.site"].create({"name": "موقع ص", "code": "777"})
        u = self.Users.create({"name": "مشرف", "login": "sup_x", "alw_role": "supervisor",
                               "alw_new_password": "Super1234", "alw_site_ids": [(6, 0, [site.id])]})
        self.assertEqual(site.supervisor_id, u)
        u.alw_site_ids = [(5, 0, 0)]
        self.assertFalse(site.supervisor_id)
        u.action_alw_archive()
        self.assertFalse(u.active)
        self.assertFalse(self._login_ok(u, "Super1234") and u.active)
        u.action_alw_restore()
        self.assertTrue(u.active)
        self.assertIn(u, self.Users.search([("alw_role", "=", "supervisor")]))
