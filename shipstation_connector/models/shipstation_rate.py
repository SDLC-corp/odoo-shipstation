from odoo import fields, models, _
from odoo.exceptions import UserError


class ShipStationRate(models.Model):
    _name = "shipstation.rate"
    _description = "ShipStation Rate"
    _order = "shipment_cost asc, id"  # id breaks price ties so lists are stable

    order_id = fields.Many2one("shipstation.order.sync", ondelete="cascade", required=True)
    carrier_code = fields.Char()
    service_code = fields.Char()
    package_code = fields.Char()
    confirmation = fields.Char()
    carrier_friendly_name = fields.Char()
    service_friendly_name = fields.Char()
    package_friendly_name = fields.Char()
    shipment_cost = fields.Float()
    other_cost = fields.Float()
    tax_amount = fields.Float()
    delivery_days = fields.Integer()
    estimated_delivery_date = fields.Datetime()
    payload = fields.Text()

    def action_apply_rate(self):
        """Apply this *existing* quote locally; never create a label or call an API."""
        self.ensure_one()
        order = self.order_id
        order._check_rate_operator()
        order._ensure_fresh_rate_quote()
        if self not in order.rate_ids:
            raise UserError(_("The chosen rate does not belong to this order."))
        # Selecting a quote changes service/package parameters. Without rebasing the
        # fingerprint, the selected quote instantly appears stale in bulk readiness.
        # Keep all other quote inputs (address, weight and dimensions) unchanged.
        selected_request = order._build_v1_rate_request()
        selected_request["carrierCode"] = self.carrier_code
        for key, value in (
            ("serviceCode", self.service_code),
            ("packageCode", self.package_code),
            ("confirmation", self.confirmation),
        ):
            if value:
                selected_request[key] = value
            else:
                selected_request.pop(key, None)
        order.write({
            "carrier_code": self.carrier_code,
            "service_code": self.service_code,
            "package_code": self.package_code,
            "confirmation": self.confirmation,
            "rate_request_hash": order._rate_fingerprint(selected_request),
        })
        return order._rate_notice(_("Rate selected"), _("Shipping service selected. No label has been purchased."))
