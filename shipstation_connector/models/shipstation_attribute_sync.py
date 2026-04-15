import json

from odoo import fields, models, _
from odoo.exceptions import UserError


class ShipStationAttributeSync(models.Model):
    _name = "shipstation.attribute.sync"
    _description = "ShipStation Attribute Sync"
    _order = "name"

    instance_id = fields.Many2one("shipstation.instance", ondelete="cascade", required=True, index=True)
    company_id = fields.Many2one(related="instance_id.company_id", store=True, index=True)
    odoo_attribute_id = fields.Many2one("product.attribute", string="Odoo Attribute")
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

    def _get_attribute_key_candidates(self):
        self.ensure_one()
        mapping_keys = self.instance_id._get_field_mappings("attribute").filtered(
            lambda m: m.odoo_field_id.name == "name"
        ).mapped("shipstation_field_key.name")
        fallback = ["attributes", "attribute", "attribute.name"]
        return [key for key in mapping_keys + fallback if key]

    def _extract_attribute_names(self, product_data):
        names = []
        for key in self._get_attribute_key_candidates():
            value = self._payload_get(product_data, key)
            if value in (None, ""):
                continue
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        name = item.get("name")
                    else:
                        name = item
                    if name:
                        names.append(str(name).strip())
            else:
                for item in str(value).split(","):
                    cleaned = item.strip()
                    if cleaned:
                        names.append(cleaned)
        return [name for name in names if name]

    def action_pull_from_shipstation(self):
        self.ensure_one()
        if not self.instance_id:
            raise UserError(_("Missing ShipStation instance."))

        target_name = (self.name or self.odoo_attribute_id.name or "").strip()
        if not target_name:
            raise UserError(_("Set Attribute Name (or Odoo Attribute) before pulling."))

        page = 1
        page_size = 100
        found_payload = {}
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
                names = [name.lower() for name in self._extract_attribute_names(product_data)]
                if target_name.lower() in names:
                    found_payload = product_data
                    break

            if found_payload or len(products) < page_size:
                break
            page += 1

        if not found_payload:
            raise UserError(_("No attribute value found in ShipStation products for '%s'.") % target_name)

        attribute = self.odoo_attribute_id
        if not attribute:
            attribute = self.env["product.attribute"].search([("name", "=", target_name)], limit=1)
        if not attribute:
            attribute = self.env["product.attribute"].create({"name": target_name})

        self.write(
            {
                "odoo_attribute_id": attribute.id,
                "name": attribute.name,
                "synced_on": fields.Datetime.now(),
                "payload": self._json_dump(found_payload),
            }
        )

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Attribute Pulled"),
                "message": _("Attribute refreshed from ShipStation product payload."),
                "type": "success",
                "sticky": False,
            },
        }

    def action_push_to_shipstation(self):
        self.ensure_one()
        if not self.instance_id:
            raise UserError(_("Missing ShipStation instance."))

        attribute = self.odoo_attribute_id
        if not attribute and self.name:
            attribute = self.env["product.attribute"].search([("name", "=", self.name)], limit=1)
        if not attribute:
            raise UserError(_("Select an Odoo Attribute before pushing."))

        ProductTemplate = self.env["product.template"].with_company(self.instance_id.company_id)
        products = ProductTemplate.search([("attribute_line_ids.attribute_id", "=", attribute.id)])
        if not products:
            raise UserError(_("No products found for this attribute."))

        pushed = 0
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

        self.write(
            {
                "odoo_attribute_id": attribute.id,
                "name": attribute.name,
                "synced_on": fields.Datetime.now(),
                "payload": self._json_dump({"pushed_products": pushed, "attribute": attribute.name}),
            }
        )

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Attribute Pushed"),
                "message": _("%s products pushed with attribute/field mappings.") % pushed,
                "type": "success",
                "sticky": False,
            },
        }
