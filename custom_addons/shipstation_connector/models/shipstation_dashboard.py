from odoo import api, models


class ShipStationDashboard(models.Model):
    _name = "shipstation.dashboard"
    _description = "ShipStation Dashboard"

    @api.model
    def get_dashboard_data(self):
        instances = self.env["shipstation.instance"].search([])
        return {
            "instances": [
                {
                    "id": inst.id,
                    "name": inst.name,
                    "total_orders": inst.total_orders,
                    "total_shipments": inst.total_shipments,
                    "total_revenue": inst.total_revenue,
                    "last_sync": inst.last_sync and inst.last_sync.strftime("%Y-%m-%d %H:%M:%S") or "",
                }
                for inst in instances
            ]
        }
