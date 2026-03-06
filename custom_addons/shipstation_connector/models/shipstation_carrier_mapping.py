from odoo import fields, models


class ShipStationCarrierMapping(models.Model):
    _name = "shipstation.carrier.mapping"
    _description = "ShipStation Carrier Mapping"
    _order = "instance_id, shipstation_carrier_code"

    instance_id = fields.Many2one("shipstation.instance", required=True, ondelete="cascade", index=True)
    shipstation_carrier_code = fields.Char(required=True, index=True)
    odoo_carrier_id = fields.Many2one("delivery.carrier", required=True, string="Odoo Carrier")
    active = fields.Boolean(default=True)

    _sql_constraints = [
        (
            "uniq_carrier_mapping",
            "unique(instance_id, shipstation_carrier_code)",
            "Carrier mapping must be unique per instance.",
        ),
    ]
