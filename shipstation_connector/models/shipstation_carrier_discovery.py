
from odoo import fields, models, _
from odoo.exceptions import UserError


class ShipStationCarrierDiscovery(models.Model):
    _inherit = "shipstation.instance"

    carrier_catalog_ids = fields.One2many(
        "shipstation.carrier.catalog", "instance_id", string="Discovered Carrier Options"
    )
    package_preset_ids = fields.One2many(
        "shipstation.package.preset", "instance_id", string="Package Presets"
    )

    def action_discover_carrier_catalog(self):
        """Explicit V1 GET-only discovery. Never modify Odoo delivery carrier mappings."""
        self.ensure_one()
        self._require_shipstation_admin()
        self.check_access_rights("write")
        self.check_access_rule("write")
        if self.company_id not in self.env.companies:
            raise UserError(_("Switch to the ShipStation instance company first."))
        response = self._ss_request("GET", "/carriers")
        carriers = response if isinstance(response, list) else (
            response.get("carriers") if isinstance(response, dict) else None
        )
        if not isinstance(carriers, list) or len(carriers) > 40:
            raise UserError(_("Invalid or oversized ShipStation carrier list."))
        captured = []
        for carrier in carriers:
            if not isinstance(carrier, dict):
                raise UserError(_("Invalid carrier response."))
            code = str(carrier.get("code") or carrier.get("carrierCode") or "").strip()
            if not code or len(code) > 100:
                raise UserError(_("A carrier has no valid code."))
            captured.append(("carrier", code, code, str(carrier.get("name") or code)[:180]))
            for endpoint, kind in (("listservices", "service"), ("listpackages", "package")):
                result = self._ss_request("GET", "/carriers/" + endpoint, params={"carrierCode": code})
                options = result if isinstance(result, list) else None
                if options is None or len(options) > 150:
                    raise UserError(_("Invalid or oversized ShipStation %s response.") % kind)
                for item in options:
                    if not isinstance(item, dict):
                        raise UserError(_("Invalid ShipStation carrier option."))
                    item_code = str(item.get("code") or "").strip()
                    if item_code:
                        if len(item_code) > 100:
                            raise UserError(_("Invalid ShipStation carrier option code."))
                        captured.append((kind, code, item_code, str(item.get("name") or item_code)[:180]))
        model = self.env["shipstation.carrier.catalog"]
        now = fields.Datetime.now()
        # Validate the entire remote response before any local write.
        for kind, carrier, code, name in captured:
            existing = model.search([
                ("instance_id", "=", self.id), ("kind", "=", kind),
                ("carrier_code", "=", carrier), ("code", "=", code),
            ], limit=2)
            if len(existing) > 1:
                raise UserError(_("Duplicate local catalog entries detected; resolve them first."))
            vals = {"name": name, "last_seen_at": now}
            if existing:
                existing.write(vals)
            else:
                model.create(dict(vals, instance_id=self.id, kind=kind, carrier_code=carrier, code=code))
        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {"title": _("Carrier catalog refreshed"),
                       "message": _("Recorded %s carrier/service/package options. No shipping labels were created.") % len(captured),
                       "type": "success", "sticky": False},
        }
