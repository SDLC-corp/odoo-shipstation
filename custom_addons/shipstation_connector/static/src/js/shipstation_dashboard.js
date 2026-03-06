/** @odoo-module **/

import { registry } from "@web/core/registry";
import { Component, onWillStart, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";


class ShipStationDashboard extends Component {
    setup() {
        this.orm = useService("orm");
        this.state = useState({ instances: [] });

        onWillStart(async () => {
            const data = await this.orm.call("shipstation.dashboard", "get_dashboard_data", []);
            this.state.instances = data.instances || [];
        });
    }
}

ShipStationDashboard.template = "shipstation_dashboard_template";
registry.category("actions").add("shipstation_dashboard", ShipStationDashboard);
