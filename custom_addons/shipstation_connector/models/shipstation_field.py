from odoo import api, fields, models


class ShipStationField(models.Model):
    _name = "shipstation.field"
    _description = "ShipStation Field"
    _rec_name = "name"
    _order = "name"

    instance_id = fields.Many2one(
        "shipstation.instance",
        string="ShipStation Instance",
        required=True,
        ondelete="cascade",
        index=True,
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

    name = fields.Char(
        string="ShipStation Field Key",
        required=True,
        index=True,
    )

    description = fields.Char(
        string="Description",
    )

    active = fields.Boolean(default=True)

    _sql_constraints = [
        (
            "uniq_field_per_instance",
            "unique(instance_id, model, name)",
            "ShipStation field must be unique per instance and model.",
        )
    ]

    @classmethod
    def _default_field_catalog(cls):
        return {
            "product": [
                ("sku", "Product SKU (text)."),
                ("name", "Product name/title (text)."),
                ("price", "Product sale price (number)."),
                ("weight", "Product weight (number)."),
                ("stockLevel", "Available stock quantity (number)."),
            ],
            "order": [
                ("orderNumber", "Unique order reference/number (text)."),
                ("orderDate", "Order datetime in ISO format."),
                ("orderStatus", "Order status, e.g. awaiting_shipment."),
                ("customerEmail", "Customer email address."),
                ("customerName", "Customer full name."),
                ("orderTotal", "Order total amount (number)."),
                ("shippingAmount", "Shipping amount (number)."),
                ("billTo.name", "Billing contact name."),
                ("shipTo.name", "Shipping contact name."),
                ("shipTo.street1", "Shipping address line 1."),
                ("shipTo.city", "Shipping city."),
                ("shipTo.state", "Shipping state code/name."),
                ("shipTo.postalCode", "Shipping postal/zip code."),
                ("shipTo.country", "Shipping country code (2 letters)."),
            ],
            "customer": [
                ("name", "Customer full name."),
                ("email", "Customer email address."),
                ("phone", "Customer phone number."),
                ("street1", "Address line 1."),
                ("street2", "Address line 2."),
                ("city", "City."),
                ("state", "State code/name."),
                ("postalCode", "Postal/zip code."),
                ("country", "Country code (2 letters)."),
            ],
            "category": [
                ("name", "Category name."),
            ],
        }

    @api.model
    def ensure_default_fields_for_instance(self, instance):
        if not instance:
            return
        catalog = self._default_field_catalog()
        for model_name, entries in catalog.items():
            for key, description in entries:
                existing = self.search(
                    [
                        ("instance_id", "=", instance.id),
                        ("model", "=", model_name),
                        ("name", "=", key),
                    ],
                    limit=1,
                )
                if existing:
                    if not existing.description and description:
                        existing.description = description
                    continue
                self.create(
                    {
                        "instance_id": instance.id,
                        "model": model_name,
                        "name": key,
                        "description": description,
                        "active": True,
                    }
                )
