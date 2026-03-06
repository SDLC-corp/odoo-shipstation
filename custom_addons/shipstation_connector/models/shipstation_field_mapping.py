from odoo import api, fields, models, _


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
            ("attribute", "Attribute"),
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
                "attribute": "product.attribute",
            }.get(rec.model)

    def action_test_mapping(self):
        self.ensure_one()
        model = self.env[self.odoo_model_name]
        record = model.search([], limit=1)
        value = ""
        warning = ""
        if record:
            value = getattr(record, self.odoo_field_id.name, "")

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

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Mapping OK"),
                "message": message,
                "sticky": False,
            },
        }
