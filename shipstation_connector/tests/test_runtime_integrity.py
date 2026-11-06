"""Runtime (database-backed) integrity checks that source-text tests cannot give.

These load the real registry, render every view through the ORM, and verify that
every button, menu, action, cron job and asset the module declares actually
resolves to something that exists.
"""
import re

from lxml import etree

from odoo.tests.common import tagged
from odoo.tools import file_path

from .common import ShipStationCommon

VIEW_TYPES_WITH_SUBVIEW = {"list", "form", "kanban", "tree"}


@tagged("post_install", "-at_install", "shipstation")
class TestRuntimeIntegrity(ShipStationCommon):

    def _models(self):
        names = self.env["ir.model"].search([("model", "=like", "shipstation.%")]).mapped("model")
        return [name for name in names if name in self.env and not self.env[name]._abstract]

    def test_every_model_has_admin_acl(self):
        missing = [
            name for name in self._models()
            if not self.env[name].with_user(self.user_admin).has_access("read")
        ]
        self.assertFalse(missing, "Models the ShipStation admin cannot read: %s" % missing)

    def test_every_model_is_blocked_for_unprivileged_users(self):
        leaked = []
        for user in (self.user_plain, self.user_portal):
            for name in self._models():
                for op in ("read", "write", "create", "unlink"):
                    if self.env[name].with_user(user).has_access(op):
                        leaked.append((user.login, name, op))
        self.assertFalse(leaked, "Models reachable without a ShipStation group: %s" % leaked)

    def test_all_module_views_render_for_admin(self):
        views = self.env["ir.ui.view"].search([
            ("model", "=like", "shipstation.%"), ("inherit_id", "=", False),
        ])
        self.assertTrue(views)
        failures = []
        for view in views:
            try:
                self.env[view.model].with_user(self.user_admin).get_view(view.id, view.type)
            except Exception as exc:  # pragma: no cover - reported below
                failures.append((view.xml_id or view.name, str(exc)[:200]))
        self.assertFalse(failures, "Views that fail to render: %s" % failures)

    def _walk_buttons(self, node, model_name, found):
        for child in node:
            if not isinstance(child.tag, str):
                continue
            if child.tag == "button" and child.get("type") == "object":
                found.append((model_name, child.get("name")))
            elif child.tag == "field" and any(c.tag in VIEW_TYPES_WITH_SUBVIEW for c in child):
                field = self.env[model_name]._fields.get(child.get("name"))
                if field and field.comodel_name:
                    self._walk_buttons(child, field.comodel_name, found)
                    continue
            self._walk_buttons(child, model_name, found)

    def test_every_object_button_calls_an_existing_method(self):
        views = self.env["ir.ui.view"].search([
            ("model", "=like", "shipstation.%"), ("inherit_id", "=", False),
            ("type", "in", ("form", "list", "kanban")),
        ])
        broken = []
        for view in views:
            arch = self.env[view.model].with_user(self.user_admin).get_view(view.id, view.type)["arch"]
            found = []
            self._walk_buttons(etree.fromstring(arch), view.model, found)
            for model_name, method in found:
                if not callable(getattr(self.env[model_name], method or "", None)):
                    broken.append((view.xml_id or view.name, model_name, method))
        self.assertFalse(broken, "Buttons without a backing method: %s" % broken)

    def _module_records(self, model):
        data = self.env["ir.model.data"].search([
            ("module", "=", "shipstation_connector"), ("model", "=", model),
        ])
        return self.env[model].browse(data.mapped("res_id")).exists()

    def test_menus_resolve_to_valid_actions(self):
        menus = self._module_records("ir.ui.menu")
        self.assertTrue(menus)
        problems = []
        for menu in menus:
            action = menu.action
            if not action:
                continue
            if action._name == "ir.actions.act_window":
                if action.res_model not in self.env:
                    problems.append((menu.complete_name, "unknown model " + action.res_model))
            elif action._name == "ir.actions.client" and not action.tag:
                problems.append((menu.complete_name, "client action without tag"))
        self.assertFalse(problems, problems)

    def test_cron_jobs_call_existing_methods(self):
        crons = self._module_records("ir.cron")
        self.assertTrue(crons)
        for cron in crons:
            match = re.search(r"model\.(\w+)\(", cron.code or "")
            self.assertTrue(match, "Cron %s has unparsable code %r" % (cron.name, cron.code))
            self.assertTrue(
                callable(getattr(self.env[cron.model_name], match.group(1), None)),
                "Cron %s calls missing method %s" % (cron.name, match.group(1)),
            )

    def test_manifest_assets_and_data_files_exist(self):
        from odoo.modules.module import Manifest
        manifest = Manifest.for_addon("shipstation_connector")
        missing = []
        for rel in manifest["data"]:
            try:
                file_path("shipstation_connector/" + rel)
            except FileNotFoundError:
                missing.append(rel)
        for bundle, files in manifest["assets"].items():
            for rel in files:
                try:
                    file_path(rel)
                except FileNotFoundError:
                    missing.append(rel)
        self.assertFalse(missing, "Declared but missing files: %s" % missing)

    def test_default_cron_jobs_never_purchase_labels(self):
        """Background jobs may only read/sync; none may reference label or refund code."""
        for cron in self._module_records("ir.cron"):
            self.assertNotRegex(cron.code or "", r"(?i)label|refund|void|push")
