/** @odoo-module **/

import { Component, onWillStart, proxy, useProps } from "@odoo/owl";
import { standardActionServiceProps } from "@web/webclient/actions/action_plugin";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";

/** Read-only operational overview; navigation only (no shipping side effects). */
export class ShipStationOperationsHome extends Component {
    static template = "shipstation_connector.OperationsHome";
    props = useProps({ ...standardActionServiceProps });

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.state = proxy({
            data: null,
            selectedInstanceId: false,
            loading: true,
            error: "",
            refreshedAt: "",
        });
        onWillStart(() => this.refresh());
    }

    async refresh() {
        this.state.loading = true;
        this.state.error = "";
        try {
            const requested = this.state.selectedInstanceId || false;
            const result = await this.orm.call(
                "shipstation.instance", "get_operations_overview", [requested]
            );
            this.state.data = result;
            this.state.selectedInstanceId = result.instance_id || false;
            this.state.refreshedAt = new Date().toLocaleTimeString();
        } catch (_error) {
            // Never show raw server exception text or request payloads.
            this.state.error = _t("Unable to refresh shipping operations. Previously loaded figures may be outdated. Check your access and retry.");
        } finally {
            this.state.loading = false;
        }
    }

    async onInstanceChange(event) {
        const value = event.target.value;
        this.state.selectedInstanceId = value ? Number(value) : false;
        await this.refresh();
    }

    /** Each navigation target is explicitly allowlisted and company-scoped. */
    async openQueue(kind) {
        const data = this.state.data;
        if (!data) return;
        const companyDomain = [["company_id", "=", data.company_id]];
        const instanceDomain = data.instance_id ? [["instance_id", "=", data.instance_id]] : [];
        const scoped = [...companyDomain, ...instanceDomain];
        const open = ["link_state", "in", ["pending", "review"]];
        let model, name, domain;
        switch (kind) {
            case "review":
                model = "shipstation.shipment.sync"; name = _t("Shipments needing review");
                domain = [...scoped, ["link_state", "=", "review"]]; break;
            case "pending":
                model = "shipstation.shipment.sync"; name = _t("Pending delivery links");
                domain = [...scoped, ["link_state", "=", "pending"]]; break;
            case "linked":
                model = "shipstation.shipment.sync"; name = _t("Linked shipments");
                domain = [...scoped, ["link_state", "=", "linked"]]; break;
            case "mine":
                model = "shipstation.shipment.sync"; name = _t("My shipping queue");
                domain = [...scoped, open, ["review_owner_id", "=", data.user_id]]; break;
            case "unassigned":
                model = "shipstation.shipment.sync"; name = _t("Unassigned shipping exceptions");
                domain = [...scoped, open, ["review_owner_id", "=", false]]; break;
            case "batches":
                model = "shipstation.fulfillment.batch"; name = _t("Active fulfillment batches");
                domain = [...scoped, ["state", "!=", "closed"]]; break;
            case "returns":
                model = "shipstation.return.case"; name = _t("Returns awaiting approval");
                domain = [...scoped, ["state", "=", "review"]]; break;
            case "all_returns":
                model = "shipstation.return.case"; name = _t("Return cases");
                domain = scoped; break;
            case "orders":
                model = "shipstation.order.sync"; name = _t("Synchronized orders");
                domain = scoped; break;
            case "connections":
                if (!data.can_admin) return;
                model = "shipstation.instance"; name = _t("ShipStation connections and setup");
                // Connections are the instance records themselves: never filter by a nonexistent instance_id field.
                domain = [...companyDomain];
                if (data.instance_id) domain.push(["id", "=", data.instance_id]);
                break;
            case "carriers":
                if (!data.can_manage) return;
                model = "shipstation.carrier.catalog"; name = _t("Carrier catalog");
                domain = scoped; break;
            case "presets":
                if (!data.can_manage) return;
                model = "shipstation.package.preset"; name = _t("Package presets");
                domain = scoped; break;
            case "logs":
                if (!data.can_manage) return;
                model = "shipstation.sync.log"; name = _t("Failed sync requests");
                domain = [["instance_id.company_id", "=", data.company_id], ["status", "=", "failed"]];
                if (data.instance_id) domain.push(["instance_id", "=", data.instance_id]);
                break;
            default:
                return;
        }
        await this.action.doAction({
            type: "ir.actions.act_window", name, res_model: model,
            views: [[false, "list"], [false, "form"]], domain,
            target: "current",
        });
    }

    async openRecord(kind, id) {
        const data = this.state.data;
        if (!data || !Number.isInteger(id) || id <= 0) return;
        const known = {
            shipment: ["shipstation.shipment.sync", data.shipments],
            returns: ["shipstation.return.case", data.returns],
            batch: ["shipstation.fulfillment.batch", data.batches],
        };
        const target = known[kind];
        if (!target || !target[1].some((item) => item.id === id)) return;
        await this.action.doAction({
            type: "ir.actions.act_window", res_model: target[0],
            views: [[false, "form"]], res_id: id, target: "current",
        });
    }
}

registry.category("actions").add("shipstation_connector.operations_home", ShipStationOperationsHome);
