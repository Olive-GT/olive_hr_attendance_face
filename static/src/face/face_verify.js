/** @odoo-module **/

// Comprueba si el sistema puede identificar a esta persona con las fotos que
// tiene guardadas, y permite agregar una captura con camara si no alcanzan.
//
// Es la respuesta directa a "esa foto que se use para verificar si el sistema
// puede identificar al usuario": aqui se ve el numero, en vivo, antes de
// depender del kiosco.

import { Component, onMounted, onWillUnmount, useRef, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";

const PIPELINE_URL = "/olive_hr_attendance_face/static/lib/pipeline/pipeline.js";

const CONDITIONS = [
    _t("Frontal"),
    _t("Con casco"),
    _t("Con lentes de seguridad"),
    _t("Con casco y lentes"),
];

export class FaceVerify extends Component {
    static template = "olive_hr_attendance_face.FaceVerify";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.notification = useService("notification");
        this.action = useService("action");

        this.videoRef = useRef("video");
        this.overlayRef = useRef("overlay");
        this.frameCanvas = document.createElement("canvas");

        this.state = useState({
            phase: "loading",
            progress: _t("Iniciando..."),
            error: null,
            employeeName: "",
            consentState: null,
            templates: [],
            live: { detected: false, facePx: 0, reason: null },
            score: 0,
            bestName: "",
            condition: CONDITIONS[0],
            busy: false,
        });

        this.employeeId = this.props.action?.params?.employee_id;
        this.conditions = CONDITIONS;
        this.running = false;

        onMounted(() => this.start());
        onWillUnmount(() => this.stop());
    }

    async start() {
        try {
            this.ctx = await this.orm.call("hr.employee", "olive_face_context", [this.employeeId]);
            this.state.employeeName = this.ctx.employee.name;
            this.state.consentState = this.ctx.consent.state;
            this.state.templates = this.ctx.templates;
            this.decoded = null;

            this.state.progress = _t("Descargando modelos (solo la primera vez)...");
            this.pipeline = await import(PIPELINE_URL);
            await this.pipeline.init(this.ctx.profile, () => {});
            this.decoded = this.ctx.templates.map((t) => ({
                name: t.name, vec: this.pipeline.decodeEmbedding(t.embedding),
            }));

            this.state.progress = _t("Abriendo la camara...");
            this.stream = await navigator.mediaDevices.getUserMedia({
                video: { width: { ideal: 1280 }, height: { ideal: 720 }, facingMode: "user" },
                audio: false,
            });
            this.videoRef.el.srcObject = this.stream;
            await this.videoRef.el.play();

            this.state.phase = "ready";
            this.running = true;
            this.loop();
        } catch (err) {
            this.state.phase = "error";
            this.state.error = err.message || String(err);
        }
    }

    stop() {
        this.running = false;
        this.stream?.getTracks().forEach((t) => t.stop());
    }

    async loop() {
        while (this.running) {
            try {
                await this.tick();
            } catch (err) {
                console.warn("olive_face:", err);
            }
            await new Promise((r) => requestAnimationFrame(r));
        }
    }

    async tick() {
        const video = this.videoRef.el;
        if (!video?.videoWidth || this.state.busy) {
            return;
        }
        const frame = this.pipeline.grabFrame(video, this.frameCanvas);
        const faces = await this.pipeline.detect(frame, this.ctx.settings);
        const picked = this.pipeline.pickFace(faces, this.ctx.settings);
        this.drawOverlay(picked.faces || faces, picked.face);

        this.state.live.detected = Boolean(picked.face);
        this.state.live.reason = picked.reason || null;
        this.state.live.facePx = Math.round(picked.face?.w || picked.face_px || 0);

        if (picked.face && this.decoded?.length) {
            const res = await this.pipeline.embed(frame, picked.face);
            let best = -1;
            let bestName = "";
            for (const t of this.decoded) {
                const sim = this.pipeline.cosine(res.vector, t.vec);
                if (sim > best) {
                    best = sim;
                    bestName = t.name;
                }
            }
            this.state.score = best;
            this.state.bestName = bestName;
        }
    }

    drawOverlay(faces, chosen) {
        const canvas = this.overlayRef.el;
        const video = this.videoRef.el;
        if (!canvas || !video.videoWidth) {
            return;
        }
        canvas.width = video.videoWidth;
        canvas.height = video.videoHeight;
        const ctx = canvas.getContext("2d");
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        for (const f of faces || []) {
            const isChosen = chosen && f === chosen;
            ctx.strokeStyle = isChosen ? "#00c853" : "#ff9100";
            ctx.lineWidth = isChosen ? 3 : 2;
            ctx.strokeRect(f.x, f.y, f.w, f.h);
            if (isChosen) {
                ctx.fillStyle = "#ff1744";
                for (let p = 0; p < 5; p++) {
                    ctx.beginPath();
                    ctx.arc(f.landmarks[2 * p], f.landmarks[2 * p + 1], 3, 0, 6.283);
                    ctx.fill();
                }
            }
        }
    }

    /** Guarda el frame actual como una foto mas del empleado, y la procesa. */
    async addPhoto() {
        if (!this.state.live.detected || this.state.busy) {
            return;
        }
        this.state.busy = true;
        try {
            const frame = this.pipeline.grabFrame(this.videoRef.el, this.frameCanvas);
            const b64 = frame.toDataURL("image/jpeg", 0.92).split(",")[1];
            const id = await this.orm.call("hr.employee", "olive_add_camera_photo",
                                           [this.employeeId, b64, this.state.condition]);

            const res = await this.pipeline.process(frame, this.ctx.settings);
            const result = res.ok
                ? {
                    state: "ok", embedding: res.base64, dim: res.dim,
                    quality_score: res.face.score, face_px: res.face.w,
                    luminance: res.luminance,
                }
                : { state: res.reason, message: _t("Captura no utilizable") };
            await this.orm.call("olive.attendance.face.template", "olive_store_result",
                                [id, result]);

            if (res.ok) {
                this.state.templates.push({ id, name: this.state.condition, embedding: res.base64 });
                this.decoded.push({ name: this.state.condition, vec: res.vector });
                this.notification.add(
                    _t("Foto agregada: %s. Una captura en las condiciones reales del "
                       + "kiosco vale mas que una foto de archivo.", this.state.condition),
                    { type: "success" }
                );
            } else {
                this.notification.add(_t("La captura no sirvio, se guardo para revision."),
                                      { type: "warning" });
            }
        } catch (err) {
            this.notification.add(err.message || String(err), { type: "danger" });
        } finally {
            this.state.busy = false;
        }
    }

    openPhotos() {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "olive.attendance.face.template",
            name: _t("Fotos de identificacion"),
            view_mode: "kanban,list,form",
            views: [[false, "kanban"], [false, "list"], [false, "form"]],
            domain: [["employee_id", "=", this.employeeId]],
            context: { default_employee_id: this.employeeId },
        });
    }

    get liveMessage() {
        return {
            no_face: _t("No veo ningun rostro"),
            ambiguous: _t("Hay dos personas en cuadro"),
            too_small: _t("Acercate mas a la camara"),
        }[this.state.live.reason] || _t("Rostro detectado");
    }

    get verdictClass() {
        if (!this.state.templates.length) return "text-bg-secondary";
        const s = this.state.score;
        if (s >= (this.ctx?.settings?.review_threshold ?? 0.65)) return "text-bg-success";
        if (s >= (this.ctx?.settings?.match_threshold ?? 0.55)) return "text-bg-warning";
        return "text-bg-danger";
    }

    get verdictText() {
        if (!this.state.templates.length) {
            return _t("No hay fotos procesadas para comparar");
        }
        const s = this.state.score;
        if (s >= (this.ctx?.settings?.review_threshold ?? 0.65)) {
            return _t("Identificado con certeza");
        }
        if (s >= (this.ctx?.settings?.match_threshold ?? 0.55)) {
            return _t("Identificado, pero quedaria para revision");
        }
        return _t("No identificado");
    }
}

registry.category("actions").add("olive_face_verify", FaceVerify);
