import json

from odoo import fields, models, _
from odoo.exceptions import UserError


class ShipStationCategorySync(models.Model):
    _name = "shipstation.category.sync"
    _description = "ShipStation Category Sync"
    _order = "name"

    instance_id = fields.Many2one("shipstation.instance", ondelete="cascade", required=True, index=True)
    company_id = fields.Many2one(related="instance_id.company_id", store=True, index=True)
    odoo_category_id = fields.Many2one("product.category", string="Odoo Category")
    shipstation_category_id = fields.Char(index=True)
    name = fields.Char(index=True)
    synced_on = fields.Datetime(default=fields.Datetime.now)
    payload = fields.Text()

    def _json_dump(self, obj):
        try:
            return json.dumps(obj, ensure_ascii=False, indent=2, default=str)
        except Exception:
            return str(obj)

    def _payload_get(self, payload, key, default=None):
        if not isinstance(payload, dict) or not key:
            return default
        current = payload
        for part in str(key).split("."):
            if not isinstance(current, dict) or part not in current:
                return default
            current = current.get(part)
        return current

    def _get_category_key_candidates(self):
        self.ensure_one()
        mapping_keys = self.instance_id._get_field_mappings("category").filtered(
            lambda m: m.odoo_field_id.name == "name"
        ).mapped("shipstation_field_key.name")
        fallback = ["category", "productCategory", "category.name"]
        return [key for key in mapping_keys + fallback if key]

    def _extract_category_name(self, product_data):
        for key in self._get_category_key_candidates():
            value = self._payload_get(product_data, key)
            if value in (None, ""):
                continue
            if isinstance(value, str):
                value = value.strip()
            if value:
                return str(value)
        return ""

    def _extract_category_id(self, category_payload):
        if not isinstance(category_payload, dict):
            return ""
        category_id = (
            category_payload.get("categoryId")
            or category_payload.get("id")
            or category_payload.get("productCategoryId")
        )
        return str(category_id).strip() if category_id not in (None, "") else ""

    def _is_not_found_endpoint_error(self, message):
        text = str(message or "").lower()
        return (
            "error 404" in text
            or "no http resource was found" in text
            or "no action was found on the controller" in text
        )

    def _fetch_categories_direct(self):
        self.ensure_one()
        endpoints = [
            ("/products/categories", "categories"),
            ("/products/categories", "results"),
            ("/products/categories", None),
        ]
        for endpoint, key in endpoints:
            try:
                data = self.instance_id._ss_request("GET", endpoint)
            except UserError as exc:
                if self._is_not_found_endpoint_error(exc):
                    continue
                raise
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                if key and isinstance(data.get(key), list):
                    return data.get(key)
                for value in data.values():
                    if isinstance(value, list):
                        return value
        return []

    def _push_category_direct(self, category_name):
        self.ensure_one()
        payload = {"name": category_name}
        if self.shipstation_category_id and str(self.shipstation_category_id).isdigit():
            payload["categoryId"] = int(self.shipstation_category_id)

        calls = []
        if self.shipstation_category_id and str(self.shipstation_category_id).isdigit():
            calls.append(("PUT", f"/products/categories/{int(self.shipstation_category_id)}"))
        calls.extend(
            [
                ("POST", "/products/categories"),
                ("POST", "/products/createcategory"),
                ("POST", "/products/updatecategory"),
            ]
        )

        last_error = None
        for method, endpoint in calls:
            try:
                return self.instance_id._ss_request(method, endpoint, data=payload)
            except UserError as exc:
                last_error = exc
                if self._is_not_found_endpoint_error(exc):
                    continue
                raise
        if last_error:
            raise last_error
        return {}

    def action_pull_from_shipstation(self):
        self.ensure_one()
        if not self.instance_id:
            raise UserError(_("Missing ShipStation instance."))

        target_name = (self.name or self.odoo_category_id.name or "").strip()
        if not target_name:
            return self.action_pull_all_from_shipstation()

        found_payload = {}
        categories = self._fetch_categories_direct()
        for category_data in categories:
            name = str((category_data or {}).get("name") or "").strip()
            if name and name.lower() == target_name.lower():
                found_payload = category_data
                target_name = name
                break

        if not found_payload:
            page = 1
            page_size = 100
            while True:
                data = self.instance_id._ss_request(
                    "GET",
                    "/products",
                    params={"page": page, "pageSize": page_size},
                )
                products = (data or {}).get("products") or []
                if not products:
                    break

                for product_data in products:
                    category_name = self._extract_category_name(product_data)
                    if not category_name:
                        continue
                    if category_name.strip().lower() != target_name.lower():
                        continue
                    found_payload = product_data
                    target_name = category_name
                    break

                if found_payload or len(products) < page_size:
                    break
                page += 1

        if not found_payload:
            raise UserError(_("No category value found in ShipStation products for '%s'.") % target_name)

        category = self.odoo_category_id
        if not category:
            category = self.env["product.category"].search([("name", "=", target_name)], limit=1)
        if not category:
            category = self.env["product.category"].create({"name": target_name})

        self.write(
            {
                "odoo_category_id": category.id,
                "shipstation_category_id": self._extract_category_id(found_payload) or self.shipstation_category_id,
                "name": target_name,
                "synced_on": fields.Datetime.now(),
                "payload": self._json_dump(found_payload),
            }
        )

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Category Pulled"),
                "message": _("Category refreshed from ShipStation product payload."),
                "type": "success",
                "sticky": False,
            },
        }

    def action_pull_all_from_shipstation(self):
        self.ensure_one()
        if not self.instance_id:
            raise UserError(_("Missing ShipStation instance."))

        before_count = self.search_count([("instance_id", "=", self.instance_id.id)])
        self.instance_id._sync_categories(mode="manual")
        after_count = self.search_count([("instance_id", "=", self.instance_id.id)])
        added = max(after_count - before_count, 0)

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("All Categories Pulled"),
                "message": _("Category list refreshed. Total: %s, New: %s") % (after_count, added),
                "type": "success",
                "sticky": False,
            },
        }

    def action_push_to_shipstation(self):
        self.ensure_one()
        if not self.instance_id:
            raise UserError(_("Missing ShipStation instance."))

        category = self.odoo_category_id
        if not category and self.name:
            category = self.env["product.category"].search([("name", "=", self.name)], limit=1)
        if not category:
            raise UserError(_("Select an Odoo Category before pushing."))

        direct_response = {}
        direct_error = None
        try:
            direct_response = self._push_category_direct(category.name)
        except UserError as exc:
            direct_error = exc

        pushed = 0
        ProductTemplate = self.env["product.template"].with_company(self.instance_id.company_id)
        products = ProductTemplate.search([("categ_id", "child_of", category.id)])
        if products:
            ProductSync = self.env["shipstation.product.sync"]
            for product in products:
                sku = product.default_code or product.product_variant_id.default_code
                if not sku:
                    continue
                sync_rec = ProductSync.search(
                    [("instance_id", "=", self.instance_id.id), ("sku", "=", sku)],
                    limit=1,
                )
                if not sync_rec:
                    sync_rec = ProductSync.create(
                        {
                            "instance_id": self.instance_id.id,
                            "sku": sku,
                            "name": product.name,
                            "price": product.list_price,
                            "weight": product.weight,
                        }
                    )
                sync_rec.action_push_to_shipstation()
                pushed += 1

        if not direct_response and direct_error and pushed == 0:
            raise direct_error

        self.write(
            {
                "odoo_category_id": category.id,
                "shipstation_category_id": self._extract_category_id(direct_response) or self.shipstation_category_id,
                "name": category.name,
                "synced_on": fields.Datetime.now(),
                "payload": self._json_dump(
                    {
                        "category": category.name,
                        "shipstation_category_response": direct_response,
                        "pushed_products": pushed,
                        "direct_error": str(direct_error) if direct_error else False,
                    }
                ),
            }
        )

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Category Pushed"),
                "message": _(
                    "Category synced. Products pushed with category/field mappings: %s"
                )
                % pushed,
                "type": "success",
                "sticky": False,
            },
        }
