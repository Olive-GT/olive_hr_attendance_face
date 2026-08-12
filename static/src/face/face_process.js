/** @odoo-module **/

// Procesa las fotos de identificacion pendientes y calcula su vector.
//
// Una sola pantalla para los tres origenes: la foto de la ficha del empleado,
// una foto subida, o una captura con camara. No hay un "enrolamiento" aparte:
// se suben fotos como en cualquier ficha de Odoo, y esto las procesa.
//
// El vector se calcula AQUI, en el navegador. El servidor solo guarda el
// resultado: nunca ejecuta inferencia.

import { Component, onMounted, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";

// La version va en la URL: sin ella el navegador se queda con la copia
// cacheada del pipeline y los arreglos no llegan nunca al cliente.
const PIPELINE_BASE = "/olive_hr_attendance_face/static/lib/pipeline/pipeline.js";
const pipelineUrl = (v) => `${PIPELINE_BASE}?v=${encodeURIComponent(v || "0")}`;

// En un retrato el rostro ocupa muchos pixeles. El minimo de la compania esta
// pensado para alguien a metro y medio de la camara del kiosco, no para una foto.
const PHOTO_MIN_FACE_PX = 60;

/** Los errores de RPC traen el mensaje util en data.message; `message` es la
 *  envoltura generica ("Odoo Server Error") y no dice nada. */
function errorText(err) {
    return err?.data?.message || err?.message || String(err);
}

export class FaceProcess extends Component {
    static template = "olive_hr_attendance_face.FaceProcess";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.notification = useService("notification");

        this.state = useState({
            phase: "loading",       // loading | ready | running | done | error
            progress: _t("Cargando..."),
            error: null,
            insecure: false,
            rows: [],
            withoutPhoto: [],
            done: 0,
            summary: { ok: 0, no_face: 0, ambiguous: 0, too_small: 0, error: 0 },
        });

        onMounted(() => this.load());
    }

    async load() {
        try {
            const ids = this.props.action?.params?.employee_ids || null;
            this.ctx = await this.orm.call("hr.employee", "olive_pending_photos_context", [ids]);
            this.state.rows = this.ctx.pending.map((p) => ({ ...p, status: "pending", message: "" }));
            this.state.withoutPhoto = this.ctx.without_photo;

            this.state.progress = _t("Descargando modelos (solo la primera vez)...");
            this.pipeline = await import(pipelineUrl(this.ctx.pipeline_version));
            this.state.insecure = !this.pipeline.isSecureContext();
            await this.pipeline.init(this.ctx.profile, () => {});
            this.state.phase = "ready";
        } catch (err) {
            this.state.phase = "error";
            this.state.error = errorText(err);
        }
    }

    /** Carga la foto guardada en el registro. */
    async loadImage(templateId) {
        const img = new Image();
        img.crossOrigin = "anonymous";
        img.src = `/web/image/olive.attendance.face.template/${templateId}/image`;
        await img.decode();
        const canvas = document.createElement("canvas");
        canvas.width = img.naturalWidth;
        canvas.height = img.naturalHeight;
        canvas.getContext("2d", { willReadFrequently: true }).drawImage(img, 0, 0);
        return canvas;
    }

    async run() {
        this.state.phase = "running";
        this.state.done = 0;
        this.state.summary = { ok: 0, no_face: 0, ambiguous: 0, too_small: 0, error: 0 };
        const settings = { ...this.ctx.settings, min_face_px: PHOTO_MIN_FACE_PX };

        for (const row of this.state.rows) {
            let result;
            try {
                const canvas = await this.loadImage(row.id);
                const res = await this.pipeline.process(canvas, settings);
                if (res.ok && res.rotation) {
                    row.message = _t("Se corrigio la orientacion (%s grados)", res.rotation);
                }
                result = res.ok
                    ? {
                        state: "ok",
                        embedding: res.base64,
                        dim: res.dim,
                        quality_score: res.face.score,
                        face_px: res.face.w,
                        luminance: res.luminance,
                    }
                    : { state: res.reason, message: this.reasonText(res.reason) };
            } catch (err) {
                result = { state: "error", message: errorText(err) };
            }

            try {
                await this.orm.call("olive.attendance.face.template", "olive_store_result",
                                    [row.id, result]);
            } catch (err) {
                result = { state: "error", message: errorText(err) };
            }

            row.status = result.state;
            row.message = result.message || row.message || "";
            this.state.summary[result.state] = (this.state.summary[result.state] || 0) + 1;
            this.state.done += 1;
            await new Promise((r) => setTimeout(r, 0));   // deja avanzar la barra
        }
        this.state.phase = "done";
        this.notification.add(
            _t("%s fotos procesadas correctamente.", this.state.summary.ok),
            { type: "success" }
        );
    }

    reasonText(reason) {
        return {
            no_face: _t("No se detecto ningun rostro"),
            ambiguous: _t("Hay mas de una persona en la foto"),
            too_small: _t("El rostro sale demasiado pequeno"),
        }[reason] || _t("Foto no utilizable");
    }

    statusClass(row) {
        return {
            ok: "text-bg-success",
            pending: "text-bg-light text-dark",
            no_face: "text-bg-danger",
            ambiguous: "text-bg-warning",
            too_small: "text-bg-warning",
            error: "text-bg-danger",
        }[row.status] || "text-bg-secondary";
    }

    statusLabel(row) {
        return {
            ok: _t("Procesada"),
            pending: _t("Pendiente"),
            no_face: _t("Sin rostro"),
            ambiguous: _t("Varias caras"),
            too_small: _t("Muy pequeno"),
            error: _t("Error"),
        }[row.status] || row.status;
    }

    get hasFailures() {
        const s = this.state.summary;
        return s.no_face || s.ambiguous || s.too_small || s.error;
    }
}

registry.category("actions").add("olive_face_process", FaceProcess);
