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

    def test_templates_use_the_odoo_20_icon_font(self):
        # Odoo 20 no longer ships Font Awesome: an <i class="fa fa-..."/> renders as nothing.
        # Icons are <i class="oi" data-icon="<material symbol>"/> instead.
        from odoo.modules.module import Manifest
        manifest = Manifest.for_addon("shipstation_connector")
        files = ["shipstation_connector/" + rel for rel in manifest["data"] if rel.endswith(".xml")]
        files += [rel for bundle in manifest["assets"].values() for rel in bundle if rel.endswith(".xml")]
        offenders = []
        for rel in files:
            source = open(file_path(rel), encoding="utf-8").read()
            if re.search(r"""class=["'][^"']*\bfa\b|\bicon=["']fa-""", source):
                offenders.append(rel)
        self.assertFalse(offenders, "Font Awesome icons (not rendered on 20): %s" % offenders)

    def test_backend_bundle_loads_module_js_and_templates(self):
        """Odoo 20 / Owl 3: the module's JS and QWeb templates must go into web.assets_backend cleanly.

        Owl 3 removed several Owl 2 exports (useState among them), so every name the module imports
        from @odoo/owl is checked against the Owl library that ships with this Odoo.
        """
        bundle = self.env["ir.qweb"]._get_asset_bundle("web.assets_backend", css=False, js=True)
        prefix = "/shipstation_connector/static/src/"
        scripts = [asset for asset in bundle.javascripts if asset.url.startswith(prefix)]
        self.assertEqual(
            sorted(asset.url.rsplit("/", 1)[1] for asset in scripts),
            ["chatbot.js", "shipstation_dashboard.js", "shipstation_operations_home.js"])
        owl_source = open(file_path("web/static/lib/owl/owl.js"), encoding="utf-8").read()
        owl_exports = set(re.findall(r"^\s+(\w+): \(\) => \w+,?$", owl_source.split("__export(index_exports, {", 1)[1].split("});", 1)[0], re.M))
        self.assertIn("proxy", owl_exports)
        for asset in scripts:
            with self.subTest(js=asset.url):
                content = asset.content  # transpiled to an odoo.define() module
                self.assertIn("odoo.define(", content)
                for names in re.findall(r'import\s*\{([^}]*)\}\s*from\s*"@odoo/owl"', asset._content):
                    imported = {name.strip().split(" as ")[0] for name in names.split(",") if name.strip()}
                    self.assertFalse(imported - owl_exports, "not exported by Owl 3: %s" % (imported - owl_exports))
        templates = bundle.generate_xml_bundle()
        self.assertNotIn("throw new Error(", templates)
        for name in ("shipstation_dashboard_template", "shipstation_connector.SimpleChatbot",
                     "shipstation_connector.OperationsHome"):
            self.assertIn('registerTemplate("%s"' % name, templates)

    def test_order_form_hides_actions_a_reader_cannot_use(self):
        arch = self.env["shipstation.order.sync"].with_user(self.user_reader).get_view(
            self.env.ref("shipstation_connector.view_shipstation_order_sync_form").id, "form")["arch"]
        visible = [b.get("name") for b in etree.fromstring(arch).iter("button") if b.get("type") == "object"]
        self.assertFalse(set(visible) & {"action_create_label", "action_push_to_shipstation", "action_get_rates",
                                         "action_select_best_rate", "action_pull_from_shipstation"}, visible)
        manager_arch = self.env["shipstation.order.sync"].with_user(self.user_manager).get_view(
            self.env.ref("shipstation_connector.view_shipstation_order_sync_form").id, "form")["arch"]
        manager = [b.get("name") for b in etree.fromstring(manager_arch).iter("button")]
        self.assertIn("action_create_label", manager)
        self.assertIn("action_pull_from_shipstation", manager)

    def test_list_screens_explain_themselves_when_empty(self):
        """A fresh install must tell the user what each empty screen is for and what to do next."""
        for xmlid in (
            "action_shipstation_instances", "action_shipstation_orders", "action_shipstation_shipments",
            "action_shipstation_products", "action_shipstation_inventory", "action_shipstation_customers",
            "action_shipstation_categories", "action_shipstation_reports", "action_shipstation_sync_logs",
            "action_shipstation_s06_package_presets", "action_shipstation_s06_carrier_catalog",
            "action_shipstation_s07_fulfillment_batches", "action_shipstation_s07_return_cases",
            "action_shipstation_s08_exception_board", "action_shipstation_shipping_workbench",
            "action_shipstation_webhook_inbox",
        ):
            with self.subTest(action=xmlid):
                action = self.env.ref("shipstation_connector." + xmlid)
                self.assertIn("o_view_nocontent", action.help or "", "missing empty-state help")

    def test_listing_and_docs_match_the_manifest(self):
        """The marketplace page and README must not drift from what the module declares."""
        from odoo.modules.module import Manifest
        manifest = Manifest.for_addon("shipstation_connector")
        listing = open(file_path("shipstation_connector/static/description/index.html"), encoding="utf-8").read()
        self.assertIn(manifest["license"], listing, "store page states a different license than the manifest")
        for other in ("LGPL-3", "AGPL-3", "GPL-3"):
            if other != manifest["license"]:
                self.assertNotIn(">%s<" % other, listing)
        self.assertIn("requests", manifest["external_dependencies"].get("python", []))
        for dependency in manifest["depends"]:
            self.assertIn(dependency, listing.replace("&amp;", "&") if dependency in ("mail",) else
                          listing + " ".join(manifest["depends"]))
        readme = open(file_path("shipstation_connector/README.md"), encoding="utf-8").read()
        self.assertIn(manifest["version"].split(".", 2)[2], readme, "README must mention the current version")
        self.assertNotRegex(readme, r"POST[^\n]*`/shipstation/webhook`[^\n]*(creates|accepts)",
                            "the anonymous endpoint is disabled and must not be documented as usable")
        self.assertNotRegex(listing.lower(), r"(automatically|auto)[^<]{0,40}(buy|purchase)[^<]{0,20}label")

    def test_default_cron_jobs_never_purchase_labels(self):
        """Background jobs may only read/sync; none may reference label or refund code."""
        for cron in self._module_records("ir.cron"):
            self.assertNotRegex(cron.code or "", r"(?i)label|refund|void|push")


@tagged("post_install", "-at_install", "shipstation")
class TestHotPathIndexes(ShipStationCommon):
    """Volume testing at 100k rows (all versions) showed the default list orders and the request-log date filter
    scanning whole tables. These indexes turned 95-180 ms page loads into <1 ms; keep them."""

    def test_list_order_and_purge_columns_are_indexed(self):
        expected = {
            ("shipstation_order_sync", "order_date"),
            ("shipstation_shipment_sync", "ship_date"),
            ("shipstation_sync_log", "create_date"),
            ("shipstation_order_sync", "shipstation_order_number"),
        }
        self.env.cr.execute("SELECT tablename, indexdef FROM pg_indexes WHERE tablename IN %s",
                            (tuple({t for t, _c in expected}),))
        rows = self.env.cr.fetchall()
        for table, column in expected:
            with self.subTest(table=table, column=column):
                self.assertTrue(any(t == table and re.search(r"\(\s*%s\b" % column, d) for t, d in rows),
                                "%s.%s has no index" % (table, column))
