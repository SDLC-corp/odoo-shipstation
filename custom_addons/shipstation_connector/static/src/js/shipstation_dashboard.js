/** @odoo-module **/

import { registry } from "@web/core/registry";
import { Component, onWillStart, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";


class ShipStationDashboard extends Component {
    setup() {
        this.orm = useService("orm");
        this.state = useState({
            summary: {
                instances: 0,
                active_instances: 0,
                orders: 0,
                shipments: 0,
                products: 0,
                customers: 0,
                inventory_items: 0,
                revenue: 0,
                failed_sync: 0,
                failed_sync_24h: 0,
                success_sync_24h: 0,
                success_rate_24h: 100,
                last_sync: "",
            },
            instances: [],
            recent_failures: [],
            recent_activity: [],
            low_stock_items: [],
            loading: true,
            error: "",
        });

        onWillStart(async () => {
            try {
                const data = await this.orm.call("shipstation.dashboard", "get_dashboard_data", []);
                this.state.summary = data.summary || this.state.summary;
                this.state.instances = data.instances || [];
                this.state.recent_failures = data.recent_failures || [];
                this.state.recent_activity = data.recent_activity || [];
                this.state.low_stock_items = data.low_stock_items || [];
            } catch (err) {
                this.state.error = (err && err.message) || "Failed to load ShipStation dashboard.";
            } finally {
                this.state.loading = false;
            }
        });
    }

    formatMoney(amount) {
        const value = Number(amount || 0);
        return value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }

    formatNumber(value) {
        return Number(value || 0).toLocaleString();
    }
}

ShipStationDashboard.template = "shipstation_dashboard_template";
registry.category("actions").add("shipstation_dashboard", ShipStationDashboard);
