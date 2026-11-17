/** @odoo-module **/
import { Component, onMounted, onWillStart, onWillUnmount, useRef, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { loadBundle } from "@web/core/assets";

const COLORS = ["#0B5345", "#C0392B", "#1F4E78", "#B7950B", "#6C3483", "#117A65", "#AF601A", "#2E4053", "#48C9B0", "#EC7063"];
const EMPTY = { date_from: "", date_to: "", sec_code: "", categ_id: "", wo_text: "", stage: "", site_id: "", tx_type: "", wo_search: "" };

export class AlwExplorer extends Component {
    static template = "alwabrn_warehouse.Explorer";
    static props = ["*"];
    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        this.root = useRef("root");
        this.charts = [];
        this.footer = "Project manager  Eng  Ahmed Zahran";
        this.state = useState({ f: { ...EMPTY }, data: null, opts: { categories: [], stages: [], sites: [], tx_types: [] },
                                busy: false, tab: "materials", matQuery: "" });
        onWillStart(async () => {
            await loadBundle("web.chartjs_lib");
            this.state.opts = await this.orm.call("alwabrn.explorer", "get_options", []);
            await this.load();
        });
        onMounted(() => this.draw());
        onWillUnmount(() => this.destroy());
    }
    payload() {
        const f = {};
        for (const [k, v] of Object.entries(this.state.f)) if (v !== "" && v !== null) f[k] = v;
        return f;
    }
    async load() {
        this.state.busy = true;
        try {
            this.state.data = await this.orm.call("alwabrn.explorer", "get_data", [this.payload()]);
            const df = this.state.data.filters;
            if (!this.state.f.date_from) { this.state.f.date_from = df.date_from; this.state.f.date_to = df.date_to; }
            if (df.not_found) this.notification.add(df.not_found, { type: "warning" });
        } finally {
            this.state.busy = false;
        }
    }
    async apply() { await this.load(); this.draw(); }
    async reset() { this.state.f = { ...EMPTY }; await this.apply(); }
    async pickMaterial(r) { this.state.f.sec_code = r.sec; this.state.tab = "work_orders"; await this.apply(); }
    async pickWO(w) { this.state.f.wo_text = w.name; this.state.tab = "materials"; await this.apply(); }
    openStockCard(r) {
        this.action.doAction({ type: "ir.actions.act_window", name: "كرت الصنف " + r.sec, res_model: "alwabrn.stock.card.line",
            views: [[false, "list"], [false, "pivot"]], domain: [["product_id", "=", r.id]] });
    }
    openWO(w) {
        this.action.doAction({ type: "ir.actions.act_window", res_model: "alwabrn.work.order", res_id: w.id, views: [[false, "form"]] });
    }
    async exportReport(code, fmt, extra) {
        const f = { ...this.payload(), ...(extra || {}) };
        this.state.busy = true;
        try {
            const urls = await this.orm.call("alwabrn.explorer", "export", [code, f, fmt]);
            for (const u of urls) window.open(u, "_blank");
        } finally {
            this.state.busy = false;
        }
    }
    get materials() {
        const q = (this.state.matQuery || "").trim().toLowerCase();
        const rows = this.state.data.materials;
        return q ? rows.filter((r) => (r.sec + " " + r.name).toLowerCase().includes(q)) : rows;
    }
    fmt(v) {
        if (v === null || v === undefined || v === 0) return v === 0 ? "0" : "";
        return Number(v).toLocaleString("en-US", { maximumFractionDigits: 3 });
    }
    destroy() { this.charts.forEach((c) => c.destroy()); this.charts = []; }
    draw() {
        this.destroy();
        if (!this.root.el || !this.state.data) return;
        const c = this.state.data.charts;
        const mk = (key, type, labels, datasets, opts = {}) => {
            const el = this.root.el.querySelector(`canvas[data-chart="${key}"]`);
            if (!el) return;
            this.charts.push(new Chart(el, { type, data: { labels, datasets }, options: Object.assign({
                responsive: true, maintainAspectRatio: false,
                plugins: { legend: { display: type === "doughnut" || datasets.length > 1, position: "bottom" } },
                scales: type === "doughnut" ? {} : { y: { beginAtZero: true } } }, opts) }));
        };
        mk("monthly", "bar", c.monthly.labels, c.monthly.series.map((s, i) => ({ label: s.label, data: s.data, backgroundColor: COLORS[i] })));
        mk("stages", "doughnut", c.stages.labels, [{ data: c.stages.data, backgroundColor: COLORS }]);
        mk("top_materials", "bar", c.top_materials.labels, [{ label: "بنود صرف", data: c.top_materials.data, backgroundColor: COLORS[2] }], { indexAxis: "y" });
        mk("top_wos", "bar", c.top_wos.labels, [{ label: "بنود صرف", data: c.top_wos.data, backgroundColor: COLORS[3] }], { indexAxis: "y" });
        mk("categories", "doughnut", c.categories.labels, [{ data: c.categories.data, backgroundColor: COLORS }]);
    }
}
registry.category("actions").add("alwabrn_explorer", AlwExplorer);
