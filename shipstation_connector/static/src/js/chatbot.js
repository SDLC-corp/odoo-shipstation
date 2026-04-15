/** @odoo-module **/

import { Component, onWillUnmount, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { rpc } from "@web/core/network/rpc";
import { useService } from "@web/core/utils/hooks";

export class ShipStationSimpleChatbot extends Component {
    setup() {
        this.menuService = useService("menu");
        this.state = useState({
            isVisible: this.isShipStationApp(),
            isOpen: false,
            isLoading: false,
            input: "",
            quickActions: [
                "Today's Shipments",
                "Recent Orders",
                "Pending Tracking",
                "Low Stock Inventory",
                "Sync Status",
            ],
            messages: [
                { role: "bot", text: "Hello. Ask me about shipments, tracking gaps, low stock, or sync status." },
            ],
        });

        this.onAppChanged = this.onAppChanged.bind(this);
        this.env.bus.addEventListener("MENUS:APP-CHANGED", this.onAppChanged);
        onWillUnmount(() => {
            this.env.bus.removeEventListener("MENUS:APP-CHANGED", this.onAppChanged);
        });
    }

    isShipStationApp() {
        const currentApp = this.menuService.getCurrentApp();
        return !!(currentApp && currentApp.name === "ShipStation");
    }

    onAppChanged() {
        const isVisible = this.isShipStationApp();
        this.state.isVisible = isVisible;
        if (!isVisible) {
            this.state.isOpen = false;
        }
    }

    toggleOpen() {
        if (this.state.isVisible) {
            this.state.isOpen = !this.state.isOpen;
        }
    }

    closePopup() {
        this.state.isOpen = false;
    }

    onInput(ev) {
        this.state.input = ev.target.value;
    }

    async sendQuickAction(ev) {
        this.state.input = ev.currentTarget.dataset.prompt || "";
        await this.sendMessage();
    }

    async sendMessage() {
        const message = (this.state.input || "").trim();
        if (!message || this.state.isLoading) {
            return;
        }

        this.state.messages.push({ role: "user", text: message });
        this.state.input = "";
        this.state.isLoading = true;
        try {
            const response = await rpc("/shipstation/ai/chatbot/message", { message });
            this.state.messages.push({
                role: "bot",
                text: (response && response.reply) || "I'm here to help with ShipStation connector questions.",
            });
        } catch (error) {
            console.error("ShipStation chatbot request failed", error);
            this.state.messages.push({
                role: "bot",
                text: "I could not process that right now. Please try again.",
            });
        } finally {
            this.state.isLoading = false;
        }
    }

    async onKeydown(ev) {
        if (ev.key === "Enter" && !ev.shiftKey) {
            ev.preventDefault();
            await this.sendMessage();
        }
    }
}

ShipStationSimpleChatbot.template = "shipstation_connector.SimpleChatbot";
registry.category("main_components").add("shipstation_simple_chatbot", { Component: ShipStationSimpleChatbot });
