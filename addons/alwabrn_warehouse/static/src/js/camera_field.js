/** @odoo-module **/
// حقل «تصوير بالكاميرا»: يفتح الكاميرا الخلفية مباشرة على الجوال (capture=environment)، وعلى الكمبيوتر يفتح اختيار ملف صورة.
import { Component, useRef, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";

export class AlwCameraField extends Component {
    static template = "alwabrn_warehouse.CameraField";
    static props = { ...standardFieldProps };
    setup() {
        this.input = useRef("input");
        this.state = useState({ preview: null, name: "" });
    }
    open() {
        this.input.el.click();
    }
    onChange(ev) {
        const file = ev.target.files && ev.target.files[0];
        if (!file) return;
        const reader = new FileReader();
        reader.onload = async () => {
            const data = reader.result.split(",")[1];
            this.state.preview = reader.result;
            this.state.name = file.name || "camera.jpg";
            const changes = { [this.props.name]: data };
            if ("filename" in this.props.record.fields) changes.filename = this.state.name;
            await this.props.record.update(changes);
        };
        reader.readAsDataURL(file);
        ev.target.value = "";
    }
}
registry.category("fields").add("alw_camera", { component: AlwCameraField, supportedTypes: ["binary"] });
