/** @odoo-module **/
import { Component, onWillStart, onMounted, onWillUnmount, useRef, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { loadBundle } from "@web/core/assets";

const FOOTER = "Project manager  Eng  Ahmed Zahran";
const X = "alwabrn_warehouse.";
const KPI_ACTIONS = {
    materials: X + "action_materials", balance: X + "action_balance", in: X + "action_tx_in",
    out: X + "action_tx_out", custody: X + "action_custody", recon: X + "action_recon",
    diff: X + "action_recon_lines", batches: X + "action_batches", pending_tx: X + "action_tx_pending",
};
const QUICK = [
    { label: "إدخال رصيد", icon: "fa-pencil-square-o", action: X + "action_snapshot_new", write: true },
    { label: "استلام مواد", icon: "fa-arrow-down", action: X + "action_tx_new_receipt", write: true },
    { label: "صرف مواد", icon: "fa-arrow-up", action: X + "action_tx_new_issue", write: true },
    { label: "تصوير دفتر", icon: "fa-camera", action: X + "action_camera", write: true },
    { label: "لصق بيانات", icon: "fa-clipboard", action: X + "action_paste_wizard", write: true },
    { label: "رفع Excel", icon: "fa-file-excel-o", action: X + "action_upload_excel", write: true },
    { label: "مطابقة الرصيد", icon: "fa-balance-scale", action: X + "action_recon_new", write: true },
    { label: "استخراج تقرير", icon: "fa-file-text-o", action: X + "action_report_wizard", write: false },
    { label: "المساعد الذكي", icon: "fa-comments-o", action: X + "action_assistant", write: false },
];

class AlwBase extends Component {
    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        this.footer = FOOTER;
    }
    open(xmlid) {
        if (xmlid) {
            this.action.doAction(xmlid);
        }
    }
}

export class AlwDashboard extends AlwBase {
    static template = "alwabrn_warehouse.Dashboard";
    static props = ["*"];
    setup() {
        super.setup();
        this.state = useState({ data: null });
        this.quick = QUICK;
        onWillStart(() => this.load());
    }
    async load() {
        this.state.data = await this.orm.call("alwabrn.dashboard", "get_data", [{}]);
    }
    get quickActions() {
        return this.quick.filter((q) => !q.write || this.state.data.can_write);
    }
    openKpi(kpi) {
        this.open(KPI_ACTIONS[kpi.action]);
    }
}
registry.category("actions").add("alwabrn_dashboard", AlwDashboard);

const CHARTS = [
    { key: "daily", title: "الوارد مقابل المنصرف يومياً (عدد الحركات)", type: "bar" },
    { key: "monthly", title: "حركات المواد الشهرية حسب النوع (عدد الحركات)", type: "bar" },
    { key: "top", title: "المواد الأعلى حركة (عدد البنود)", type: "bar" },
    { key: "trend", title: "اتجاه فروقات الجرد (عدد البنود)", type: "line" },
    { key: "categories", title: "توزيع رصيد المستودع حسب الفئة (قيمة مرجعية ر.س)", type: "doughnut" },
    { key: "site_custody", title: "عهد المواقع (قيمة مرجعية ر.س)", type: "bar" },
    { key: "consumption", title: "استهلاك المواد حسب الموقع (عدد البنود)", type: "bar" },
    { key: "frequent", title: "مواد تتكرر بها الفروقات (عدد المرات)", type: "bar" },
];
const COLORS = ["#0B5345", "#C0392B", "#1F4E78", "#B7950B", "#6C3483", "#117A65", "#AF601A", "#2E4053",
    "#48C9B0", "#EC7063", "#5DADE2", "#F4D03F"];

export class AlwAnalytics extends AlwBase {
    static template = "alwabrn_warehouse.Analytics";
    static props = ["*"];
    setup() {
        super.setup();
        this.charts = CHARTS;
        this.root = useRef("root");
        this.instances = [];
        this.state = useState({
            data: null, options: { sites: [], projects: [], categories: [], tx_types: [] },
            f: { date_from: "", date_to: "", site_id: "", project_id: "", categ_id: "", tx_type: "", work_order: "", sec_code: "" },
        });
        onWillStart(async () => {
            await loadBundle("web.chartjs_lib");
            this.state.options = await this.orm.call("alwabrn.dashboard", "get_filter_options", []);
            await this.load();
        });
        onMounted(() => this.draw());
        onWillUnmount(() => this.destroy());
    }
    async filters() {
        const f = this.state.f;
        const out = {};
        for (const k of ["date_from", "date_to", "tx_type", "work_order"]) {
            if (f[k]) out[k] = f[k];
        }
        for (const k of ["site_id", "project_id", "categ_id"]) {
            if (f[k]) out[k] = parseInt(f[k]);
        }
        if (f.sec_code) {
            const ids = await this.orm.search("product.product", [["alw_sec_code", "=", f.sec_code.trim()]], { limit: 1 });
            if (!ids.length) {
                this.notification.add("كود SEC غير موجود: " + f.sec_code, { type: "warning" });
            } else {
                out.product_id = ids[0];
            }
        }
        return out;
    }
    async load() {
        this.state.data = await this.orm.call("alwabrn.dashboard", "get_data", [await this.filters()]);
        if (!this.state.f.date_from) {
            this.state.f.date_from = this.state.data.date_from;
            this.state.f.date_to = this.state.data.date_to;
        }
    }
    async apply() {
        await this.load();
        this.draw();
    }
    destroy() {
        this.instances.forEach((c) => c.destroy());
        this.instances = [];
    }
    draw() {
        this.destroy();
        const charts = this.state.data.charts;
        for (const def of CHARTS) {
            const canvas = this.root.el && this.root.el.querySelector(`canvas[data-chart="${def.key}"]`);
            if (!canvas) continue;
            const c = charts[def.key];
            let datasets;
            if (def.key === "daily") {
                datasets = [{ label: "وارد", data: c.in, backgroundColor: COLORS[0] },
                    { label: "منصرف", data: c.out, backgroundColor: COLORS[1] }];
            } else if (def.key === "monthly") {
                datasets = c.series.map((s, i) => ({ label: s.label, data: s.data, backgroundColor: COLORS[i % COLORS.length] }));
            } else {
                datasets = [{ label: def.title, data: c.data, borderColor: COLORS[2],
                    backgroundColor: def.type === "doughnut" ? COLORS : COLORS[2] }];
            }
            this.instances.push(new Chart(canvas, {
                type: def.type,
                data: { labels: c.labels, datasets },
                options: {
                    responsive: true, maintainAspectRatio: false,
                    plugins: { legend: { display: def.key === "daily" || def.key === "monthly" || def.type === "doughnut", position: "bottom" } },
                    scales: def.type === "doughnut" ? {} : { x: { stacked: def.key === "monthly" }, y: { stacked: def.key === "monthly", beginAtZero: true, ticks: { precision: 0 } } },
                },
            }));
        }
    }
    openKpi(kpi) {
        this.open(KPI_ACTIONS[kpi.action]);
    }
}
registry.category("actions").add("alwabrn_analytics", AlwAnalytics);

const REGISTERS = [
    { code: "incoming", label: "دفتر الوارد", type: "receipt" },
    { code: "outgoing", label: "دفتر المنصرف", type: "issue_site" },
    { code: "daily_balance", label: "كشف الرصيد اليومي", type: "book_balance" },
    { code: "physical", label: "كشف الجرد الفعلي", type: "physical_count" },
    { code: "returns", label: "دفتر المرتجعات", type: "return_in" },
];

export class AlwCamera extends AlwBase {
    static template = "alwabrn_warehouse.Camera";
    static props = ["*"];
    setup() {
        super.setup();
        this.registers = REGISTERS;
        this.state = useState({ register: "incoming", busy: false, files: [] });
        this.cameraInput = useRef("camera");
        this.fileInput = useRef("file");
    }
    pick(ref) {
        this[ref].el.click();
    }
    onFiles(ev) {
        for (const file of ev.target.files) {
            const reader = new FileReader();
            reader.onload = () => {
                this.state.files.push({ name: file.name || "photo.jpg", data: reader.result.split(",")[1],
                    preview: file.type.startsWith("image/") ? reader.result : null });
            };
            reader.readAsDataURL(file);
        }
        ev.target.value = "";
    }
    remove(index) {
        this.state.files.splice(index, 1);
    }
    async process() {
        if (!this.state.files.length) {
            this.notification.add("التقط صورة أو اختر ملفاً أولاً.", { type: "warning" });
            return;
        }
        this.state.busy = true;
        try {
            const reg = REGISTERS.find((r) => r.code === this.state.register);
            const [batchId] = await this.orm.create("alwabrn.import.batch", [{
                channel: "camera", content_type: reg.type, subject: "تصوير " + reg.label }]);
            for (const f of this.state.files) {
                const [docId] = await this.orm.create("alwabrn.source.document", [{
                    name: f.name, datas: f.data, channel: "camera", register_type: reg.code }]);
                await this.orm.call("alwabrn.import.batch", "load_document", [[batchId], docId]);
            }
            await this.orm.call("alwabrn.import.batch", "action_validate", [[batchId]]);
            this.state.files = [];
            this.action.doAction({ type: "ir.actions.act_window", res_model: "alwabrn.import.batch",
                res_id: batchId, views: [[false, "form"]] });
        } catch (e) {
            this.notification.add((e.data && e.data.message) || e.message || "تعذر معالجة الصورة", { type: "danger" });
        } finally {
            this.state.busy = false;
        }
    }
}
registry.category("actions").add("alwabrn_camera", AlwCamera);

const EXAMPLES = ["كم رصيد المادة 908111001؟", "ما المواد التي دخلت اليوم؟", "ما المواد التي خرجت اليوم؟",
    "ما المواد الموجودة بعهدة الموقع 105؟", "ما المواد التي بها فروقات؟", "قارن رصيد اليوم برصيد أمس.",
    "اعرض المواد التي لم تتم مطابقتها."];

export class AlwAssistant extends AlwBase {
    static template = "alwabrn_warehouse.Assistant";
    static props = ["*"];
    setup() {
        super.setup();
        this.examples = EXAMPLES;
        this.state = useState({ question: "", busy: false, messages: [] });
    }
    async ask(q) {
        const question = (q || this.state.question).trim();
        if (!question) return;
        this.state.busy = true;
        this.state.messages.push({ role: "user", text: question });
        this.state.question = "";
        try {
            const res = await this.orm.call("alwabrn.assistant", "ask", [question]);
            this.state.messages.push(Object.assign({ role: "bot" }, res));
        } catch (e) {
            this.state.messages.push({ role: "bot", text: "تعذر تنفيذ الاستعلام: " + ((e.data && e.data.message) || e.message), columns: [], rows: [] });
        } finally {
            this.state.busy = false;
        }
    }
    onKey(ev) {
        if (ev.key === "Enter") this.ask();
    }
}
registry.category("actions").add("alwabrn_assistant", AlwAssistant);
