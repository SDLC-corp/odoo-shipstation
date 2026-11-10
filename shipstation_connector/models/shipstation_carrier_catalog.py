
from odoo import fields, models


class ShipStationCarrierCatalog(models.Model):
    _name = "shipstation.carrier.catalog"
    _description = "Available ShipStation Carrier / Service / Package"
    _order = "instance_id, kind, carrier_code, name"

    instance_id = fields.Many2one("shipstation.instance", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="instance_id.company_id", store=True, index=True)
    kind = fields.Selection(
        [("carrier", "Carrier"), ("service", "Service"), ("package", "Package")],
        required=True, index=True,
    )
    carrier_code = fields.Char(required=True, index=True)
    code = fields.Char(required=True, index=True)
    name = fields.Char(required=True)
    last_seen_at = fields.Datetime(readonly=True)

    _sql_constraints = [
        (
            "unique_catalog",
            "unique(instance_id, kind, carrier_code, code)",
            "The same carrier/service/package cannot be duplicated within one instance.",
        ),
    ]
