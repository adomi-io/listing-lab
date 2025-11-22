/** @odoo-module **/

import { Component, useState, onWillStart, onWillUnmount, onWillUpdateProps } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { _t } from "@web/core/l10n/translation";
import { useService } from "@web/core/utils/hooks";

const fieldRegistry = registry.category("fields");

export class ListingFeatures extends Component {
    static template = "real_estate_listings.ListingFeatures";
    static props = standardFieldProps;

    setup() {
        this.orm = this.env.services.orm;
        this.action = this.env.services.action;
        this.bus = useService("bus_service");
        this.state = useState({ loading: true, groups: {} });

        // debug logger (kept lightweight)
        this._log = (...args) => console.log("[ListingFeatures]", ...args);

        // bus settings
        this.busSubscriptionType = "estate_property_update";
        this.currentChannel = null;
        this._onBusMessage = (payload) => this.onBusMessage(payload);

        // helper to compute and manage channel subscription
        this._channelName = (resId) => (resId ? `estate_property_${resId}` : null);
        this._updateChannelSubscription = (resId) => {
            const desired = this._channelName(resId);
            if (this.currentChannel && this.currentChannel !== desired) {
                this._log("deleteChannel", this.currentChannel);
                this.bus.deleteChannel(this.currentChannel);
                this.currentChannel = null;
            }
            if (desired && this.currentChannel !== desired) {
                this._log("addChannel", desired);
                this.bus.addChannel(desired);
                this.currentChannel = desired;
            }
        };

        onWillStart(async () => {
            // subscribe to notification type and channel (if resId already exists)
            try {
                this.bus.subscribe(this.busSubscriptionType, this._onBusMessage);
            } catch (_) {}
            const resId = this.props?.record?.resId;
            this._updateChannelSubscription(resId);

            // poller to catch first-save id appearance
            this._idPoller = setInterval(() => {
                try {
                    const currentResId = this.props?.record?.resId;
                    if (currentResId && this.currentChannel !== this._channelName(currentResId)) {
                        this._log("poller detected resId", currentResId);
                        this._updateChannelSubscription(currentResId);
                    }
                } catch (_) {}
            }, 1000);

            await this.loadFeatures();
        });

        onWillUnmount(() => {
            try {
                if (this.currentChannel) {
                    this.bus.deleteChannel(this.currentChannel);
                    this.currentChannel = null;
                }
                this.bus.unsubscribe(this.busSubscriptionType, this._onBusMessage);
            } catch (_) {}
            if (this._idPoller) {
                clearInterval(this._idPoller);
                this._idPoller = null;
            }
        });

        onWillUpdateProps((nextProps) => {
            const prevId = this.props?.record?.resId;
            const nextId = nextProps?.record?.resId;
            if (prevId !== nextId) {
                this._updateChannelSubscription(nextId);
            }
        });
    }

    get record() {
        return this.props.record?.data || {};
    }

    async loadFeatures() {
        const propertyId = this.record.id;
        if (!propertyId) {
            this.state.groups = {};
            this.state.loading = false;
            return;
        }

        const rows = await this.orm.searchRead(
            "real_estate.feature",
            [["property_id", "=", propertyId]],
            ["id", "parent_category", "category", "text_items", "display_text"]
        );

        const groups = {};
        for (const row of rows) {
            const parent = row.parent_category || "Other";
            const cat = row.category || "Details";
            if (!groups[parent]) groups[parent] = {};
            if (!groups[parent][cat]) groups[parent][cat] = [];

            // Prefer text_items (JSON list), fallback to display_text rendered HTML -> strip tags
            let items = [];
            if (row.text_items) {
                try {
                    const parsed = JSON.parse(row.text_items) || [];
                    items = parsed.filter(Boolean);
                } catch (_) {
                    // ignore JSON errors, fallback to display_text
                }
            }
            if (!items.length && row.display_text) {
                const tmp = document.createElement("div");
                tmp.innerHTML = row.display_text;
                items = Array.from(tmp.querySelectorAll("li")).map((li) => li.textContent.trim()).filter(Boolean);
            }
            if (!items.length) {
                continue;
            }
            groups[parent][cat].push({ id: row.id, items });
        }

        this.state.groups = groups;
        this.state.loading = false;
    }

    // React to bus notifications from sub-record updates and listing writes
    async onBusMessage(payload) {
        const resId = this.props?.record?.resId;
        if (!resId) return;
        if (!payload || payload.id !== resId) return; // other record
        // Any submodel update for this listing should refresh features from server
        this._log("bus refresh due to", payload?.source_model || payload?.model, payload?.event);
        await this.loadFeatures();
    }

    get parentCategories() {
        return Object.keys(this.state.groups).sort();
    }

    categoriesOf(parent) {
        return Object.keys(this.state.groups[parent] || {}).sort();
    }

    itemsOf(parent, category) {
        // merge all feature rows for the same category
        const lists = this.state.groups[parent]?.[category] || [];
        const acc = [];
        for (const row of lists) acc.push(...row.items);
        // Deduplicate while preserving order to avoid Owl duplicate t-key errors
        // Example duplicate observed: "Bedroom Level: Second"
        const seen = new Set();
        return acc.filter((val) => {
            const key = String(val);
            if (seen.has(key)) return false;
            seen.add(key);
            return true;
        });
    }

    async onAddFeature() {
        await this.action.doAction({
            type: "ir.actions.act_window",
            name: _t("Add Feature"),
            res_model: "real_estate.feature",
            view_mode: "form",
            views: [[false, "form"]],
            target: "new",
            context: {
                default_property_id: this.record.id,
            },
        });
        // Reload to include newly created item
        await this.loadFeatures();
    }
}

fieldRegistry.add("listing_features", {
    component: ListingFeatures,
    supportedTypes: ["one2many", "many2many", "char", "text"],
});
