"""Admin-only local configuration diagnostics for the ShipStation connector.

No HTTP calls, label purchases, webhook registration, or stock modifications.
Do not include credential values in field text or notifications.
"""
from odoo import api, fields, models, _
from odoo.exceptions import AccessError


class ShipStationSetupAssistant(models.Model):
    _inherit = "shipstation.instance"

    setup_state = fields.Selection(
        [("blocked", "Action Required"), ("attention", "Review Optional Settings"),
         ("ready", "Locally Ready")],
        string="Local Setup Status", compute="_compute_setup_status",
        groups="shipstation_connector.group_shipstation_admin",
    )
    setup_summary = fields.Char(
        string="Setup Summary", compute="_compute_setup_status",
        groups="shipstation_connector.group_shipstation_admin",
    )
    setup_checklist = fields.Text(
        string="Configuration Checklist", compute="_compute_setup_status",
        groups="shipstation_connector.group_shipstation_admin",
    )
    setup_next_step = fields.Char(
        string="Recommended Next Step", compute="_compute_setup_status",
        groups="shipstation_connector.group_shipstation_admin",
    )

    def _get_local_setup_diagnostic(self):
        """Diagnose a single record using configuration metadata only.

        Explicitly separate blocking requirements from optional conveniences;
        local readiness never implies working remote credentials.
        """
        self.ensure_one()
        self._require_shipstation_admin()
        blockers, warnings, passed = [], [], []
        if not self.active:
            blockers.append(_("Activate this connection before running synchronization."))
        else:
            passed.append(_("Connection is active."))

        if not self.company_id:
            blockers.append(_("Choose the Odoo company for this connection."))
        else:
            passed.append(_("Odoo company is selected."))

        if not (self.api_key and self.api_secret):
            blockers.append(_("Provide both V1 API key and secret for orders and shipments."))
        else:
            passed.append(_("V1 credential fields have values (not yet authenticated)."))

        if not (self.store_id or "").strip():
            warnings.append(_("Enter the ShipStation store ID to limit order and shipment matching."))
        else:
            passed.append(_("Store ID is configured."))

        if self.cron_sync_inventory and not self.v2_api_key:
            blockers.append(_("A separate V2 API key is required when automated inventory reconciliation is enabled."))
        elif not self.v2_api_key:
            warnings.append(_("Optional: add a separate V2 key for V2 product/inventory features."))
        else:
            passed.append(_("V2 credential field has a value (not yet authenticated)."))

        warehouse = self.inventory_warehouse_id
        if self.cron_sync_inventory and not warehouse:
            blockers.append(_("Select an inventory warehouse before enabling inventory reconciliation."))
        elif warehouse and warehouse.company_id and warehouse.company_id != self.company_id:
            blockers.append(_("Inventory warehouse belongs to a different company."))
        elif warehouse:
            passed.append(_("Inventory warehouse is selected for read-only reconciliation."))

        any_webhook = any((self.webhook_order_create, self.webhook_order_update,
                           self.webhook_shipment_update))
        if any_webhook:
            # A computed callback URL exists only with HTTPS, an instance ID and a token.
            if not self.webhook_target_url:
                warnings.append(_("For V1 webhook notifications, configure the Odoo HTTPS base URL and callback secret."))
            else:
                passed.append(_("V1 callback URL can be generated; registration is not verified."))
        else:
            warnings.append(_("Optional: enable and register V1 webhooks if push notifications are required."))

        state = "blocked" if blockers else ("attention" if warnings else "ready")
        findings = (
            ["ACTION REQUIRED: " + item for item in blockers] +
            ["REVIEW: " + item for item in warnings] +
            ["OK: " + item for item in passed]
        )
        return {
            "state": state,
            "summary": _("%(critical)s required item(s); %(optional)s optional reminder(s).") % {
                "critical": len(blockers), "optional": len(warnings),
            },
            "next_step": (blockers or warnings or
                          [_('Configuration fields are populated. Run V1/V2 connection tests before syncing.')])[0],
            "checklist": "\n".join(findings) + "\n" + _(
                "Local checks only: remote authentication, shipping rates and labels have not been tested."
            ),
        }

    @api.depends(
        "active", "company_id", "api_key", "api_secret", "v2_api_key", "store_id",
        "inventory_warehouse_id", "cron_sync_inventory", "webhook_order_create",
        "webhook_order_update", "webhook_shipment_update", "webhook_token",
    )
    def _compute_setup_status(self):
        for record in self:
            status = record._get_local_setup_diagnostic()
            record.setup_state = status["state"]
            record.setup_summary = status["summary"]
            record.setup_checklist = status["checklist"]
            record.setup_next_step = status["next_step"]

    def action_check_local_setup(self):
        self.ensure_one()
        self._require_shipstation_admin()
        status = self._get_local_setup_diagnostic()
        level = "success" if status["state"] == "ready" else "warning"
        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {
                "title": _("ShipStation Local Setup Check"),
                "message": status["next_step"],
                "type": level,
                "sticky": status["state"] == "blocked",
            },
        }
