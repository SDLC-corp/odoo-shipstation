from odoo import api, fields, models, _
from odoo.exceptions import UserError


class ShipStationFieldMapping(models.Model):
    _name = "shipstation.field.mapping"
    _description = "ShipStation Field Mapping"
    _rec_name = "odoo_field_id"

    instance_id = fields.Many2one(
        "shipstation.instance",
        string="ShipStation Instance",
        required=True,
        ondelete="cascade",
    )

    model = fields.Selection(
        [
            ("product", "Product"),
            ("order", "Order"),
            ("customer", "Customer"),
            ("category", "Category"),
        ],
        required=True,
        default="product",
    )

    active = fields.Boolean(default=True)

    odoo_field_id = fields.Many2one(
        "ir.model.fields",
        string="Odoo Field",
        required=True,
        ondelete="cascade",
        domain="[('model', '=', odoo_model_name), ('store', '=', True)]",
    )

    odoo_model_name = fields.Char(
        compute="_compute_odoo_model",
        store=True,
    )

    shipstation_field_key = fields.Many2one(
        "shipstation.field",
        string="ShipStation Field",
        required=True,
        domain="[('instance_id', '=', instance_id), ('model', '=', model), ('active', '=', True)]",
    )
    shipstation_field_description = fields.Char(
        related="shipstation_field_key.description",
        string="Expected Data",
        readonly=True,
    )

    @api.depends("model")
    def _compute_odoo_model(self):
        for rec in self:
            rec.odoo_model_name = {
                "product": "product.template",
                "order": "sale.order",
                "customer": "res.partner",
                "category": "product.category",
            }.get(rec.model)

    def action_test_mapping(self):
        self.ensure_one()
        if not self.odoo_model_name:
            raise UserError(
                _("This mapping model is no longer supported. Please create a new mapping for Product/Order/Customer/Category.")
            )
        model = self.env[self.odoo_model_name]
        record = model.search([], limit=1)
        value = ""
        warning = ""
        if record:
            try:
                value = getattr(record, self.odoo_field_id.name, "")
            except Exception:
                value = ""

        numeric_shipstation_keys = {"price", "weight", "stockLevel", "orderTotal", "shippingAmount", "taxAmount"}
        key_name = self.shipstation_field_key.name or ""
        if key_name in numeric_shipstation_keys and value not in (False, None, ""):
            try:
                float(value)
            except (TypeError, ValueError):
                warning = _(
                    "Warning: Selected ShipStation key '%s' expects a number, but mapped Odoo field value is text."
                ) % key_name

        message = (
            f"Model: {self.model}\n"
            f"ShipStation key: {self.shipstation_field_key.name}\n"
            f"Expected data: {self.shipstation_field_description or '-'}\n"
            f"Odoo value: {value}"
        )
        if warning:
            message = f"{message}\n{warning}"

        refreshed = self._reapply_mapping_to_synced_records()
        if refreshed:
            message = f"{message}\nRefreshed synced rows: {refreshed}"

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Mapping OK"),
                "message": message,
                "sticky": False,
            },
        }

    def _parse_payload_text(self, payload_text):
        if not payload_text:
            return {}
        text = str(payload_text).strip()
        if not text:
            return {}
        try:
            import json

            payload = json.loads(text)
            if isinstance(payload, dict):
                if isinstance(payload.get("response"), dict):
                    return payload.get("response")
                return payload
        except Exception:
            pass
        try:
            import ast

            payload = ast.literal_eval(text)
            if isinstance(payload, dict) and isinstance(payload.get("response"), dict):
                return payload.get("response")
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    def _reapply_mapping_to_synced_records(self):
        """Refresh existing sync rows from saved payload after mapping changes."""
        self.ensure_one()
        if not self.instance_id:
            return 0

        refreshed = 0
        instance = self.instance_id

        model_map = {
            "product": "shipstation.product.sync",
            "order": "shipstation.order.sync",
            "customer": "shipstation.customer.sync",
        }
        sync_model_name = model_map.get(self.model)
        if not sync_model_name:
            return 0

        sync_model = self.env[sync_model_name]
        records = sync_model.search([("instance_id", "=", instance.id)])
        for rec in records:
            payload = self._parse_payload_text(rec.payload)
            if not payload:
                continue
            updated = sync_model._upsert_from_payload(instance, payload)
            if updated:
                refreshed += 1
                if updated.id != rec.id:
                    rec.write(
                        {
                            key: updated[key]
                            for key in rec._fields
                            if key in updated._fields and key not in ("id", "create_uid", "create_date", "write_uid", "write_date")
                        }
                    )
        return refreshed
