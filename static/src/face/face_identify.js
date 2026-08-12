/** @odoo-module **/

// Identificacion 1:N — la prueba que de verdad importa.
//
// La pantalla de verificacion responde "¿esta camara se parece a las fotos de
// Juan Carlos?", que es una pregunta facil: ya sabemos quien es. El kiosco tiene
// que responder otra mucho mas dificil, sin que nadie le diga nada de antemano:
// "¿a quien de TODOS se parece mas, y estoy lo bastante seguro como para
// registrarlo?". Ahi es donde aparecen las confusiones entre personas parecidas
// y donde el umbral se gana o se pierde.
//
// Esta pantalla corre exactamente las guardas del kiosco, pero no marca nada:
// solo muestra la decision que se habria tomado y por que. Es el instrumento de
// medicion del proyecto — el que dice si el sistema separa o confunde.

import { Component, onMounted, onWillUnmount, useEffect, useRef, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";

const PIPELINE_BASE = "/olive_hr_attendance_face/static/lib/pipeline/pipeline.js";
const pipelineUrl = (v) => `${PIPELINE_BASE}?v=${encodeURIComponent(v || "0")}`;

// Cuanto se sostiene en pantalla una decision antes de volver a mirar. El
// kiosco usa el cooldown de la compania (90 s) para no marcar dos veces a la
// misma persona; aqui se quiere probar muchas veces seguidas, asi que se
// sostiene solo lo justo para poder leerla.
const HOLD_MS = 4000;

function errorText(err) {
    return err?.data?.message || err?.message || String(err);
}

export class FaceIdentify extends Component {
    static template = "olive_hr_attendance_face.FaceIdentify";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.notification = useService("notification");

        this.videoRef = useRef("video");
        this.overlayRef = useRef("overlay");
        this.frameCanvas = document.createElement("canvas");

        this.state = useState({
            phase: "loading",
            progress: _t("Iniciando..."),
            error: null,
            cameraError: false,
            peopleCount: 0,
            templateCount: 0,
            settings: {},
            live: { detected: false, facePx: 0, reason: null },
            ranking: [],          // los candidatos mas parecidos, en vivo
            streak: 0,            // frames seguidos de acuerdo
            decision: null,       // la decision sostenida
            log: [],              // historial de la sesion, para sacar conclusiones
        });

        this.people = [];
        this.running = false;
        this.streakId = null;
        this.streakLast = 0;
        this.holdUntil = 0;

        useEffect(
            (el) => {
                if (el && this.stream && el.srcObject !== this.stream) {
                    this.attachStream(el);
                }
            },
            () => [this.videoRef.el]
        );

        onMounted(() => this.start());
        onWillUnmount(() => this.stop());
    }

    async start() {
        try {
            this.ctx = await this.orm.call(
                "olive.attendance.face.template", "olive_identify_payload", []);
            this.state.settings = this.ctx.settings;

            // Un empleado, muchas fotos. El puntaje de la persona es el MAXIMO
            // entre sus fotos, no el promedio: basta con parecerse a una de
            // ellas, y promediar castigaria justamente a quien tiene fotos
            // variadas, que es lo que queremos fomentar.
            const byEmployee = new Map();
            for (const t of this.ctx.templates) {
                if (!byEmployee.has(t.employee_id)) {
                    byEmployee.set(t.employee_id, {
                        id: t.employee_id, name: t.employee, templates: [],
                    });
                }
                byEmployee.get(t.employee_id).templates.push(t);
            }
            this.state.peopleCount = byEmployee.size;
            this.state.templateCount = this.ctx.templates.length;

            if (!byEmployee.size) {
                throw new Error(_t(
                    "No hay ninguna foto procesada todavia. Entra en 'Procesar fotos' "
                    + "y procesa las fotos de las fichas antes de probar aqui."
                ));
            }

            this.state.progress = _t("Descargando modelos (solo la primera vez)...");
            this.pipeline = await import(pipelineUrl(this.ctx.pipeline_version));
            await this.pipeline.init(this.ctx.profile, () => {});

            this.people = [...byEmployee.values()].map((p) => ({
                ...p,
                vecs: p.templates.map((t) => ({
                    name: t.name, vec: this.pipeline.decodeEmbedding(t.embedding),
                })),
            }));

            this.state.progress = _t("Abriendo la camara...");
            try {
                this.stream = await navigator.mediaDevices.getUserMedia({
                    video: { width: { ideal: 1280 }, height: { ideal: 720 }, facingMode: "user" },
                    audio: false,
                });
            } catch (camErr) {
                this.state.cameraError = true;
                throw new Error(this.cameraErrorText(camErr));
            }

            this.state.phase = "ready";
        } catch (err) {
            this.state.phase = "error";
            this.state.error = errorText(err);
        }
    }

    cameraErrorText(err) {
        const name = err?.name || "";
        if (name === "NotAllowedError") {
            return _t("Le negaste el permiso a la camara. Habilitalo en el candado de "
                      + "la barra de direcciones y recarga la pagina.");
        }
        if (name === "NotFoundError" || name === "DevicesNotFoundError") {
            return _t("No se encontro ninguna camara conectada a este equipo.");
        }
        if (name === "NotReadableError") {
            return _t("La camara existe pero otra aplicacion la esta usando.");
        }
        if (!navigator.mediaDevices) {
            return _t("El navegador bloqueo la camara porque la conexion no es segura "
                      + "(no es HTTPS ni localhost).");
        }
        return err?.message || String(err);
    }

    async attachStream(el) {
        el.srcObject = this.stream;
        try {
            await el.play();
        } catch (err) {
            this.state.phase = "error";
            this.state.error = _t("La camara abrio pero el video no arranco: %s",
                                  errorText(err));
            return;
        }
        if (!this.running) {
            this.running = true;
            this.loop();
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
        if (!video?.videoWidth) {
            return;
        }
        // Con una decision sostenida se deja de procesar: da tiempo a leerla y
        // evita que el resultado parpadee mientras la persona se va.
        if (this.state.decision && Date.now() < this.holdUntil) {
            return;
        }
        if (this.state.decision) {
            this.clearDecision();
        }

        const frame = this.pipeline.grabFrame(video, this.frameCanvas);
        const faces = await this.pipeline.detect(frame, this.ctx.settings);
        const picked = this.pipeline.pickFace(faces, this.ctx.settings);
        this.drawOverlay(faces, picked.face);

        this.state.live.detected = Boolean(picked.face);
        this.state.live.reason = picked.reason || null;
        this.state.live.facePx = Math.round(picked.face?.w || 0);

        if (!picked.face) {
            this.resetStreak();
            this.state.ranking = [];
            return;
        }

        const res = await this.pipeline.embed(frame, picked.face);
        this.rank(res.vector);
    }

    /** Puntua a cada persona y aplica las guardas del kiosco. */
    rank(vector) {
        const scored = this.people.map((p) => {
            let best = -1;
            let bestTemplate = "";
            for (const t of p.vecs) {
                const sim = this.pipeline.cosine(vector, t.vec);
                if (sim > best) {
                    best = sim;
                    bestTemplate = t.name;
                }
            }
            return { id: p.id, name: p.name, score: best, template: bestTemplate };
        });
        scored.sort((a, b) => b.score - a.score);
        this.state.ranking = scored.slice(0, 5);

        const top1 = scored[0];
        const top2 = scored[1];
        const margin = top2 ? top1.score - top2.score : 1;
        const s = this.ctx.settings;

        // Guarda 1 — umbral. Ante duda no se marca: el respaldo es el guardia.
        if (top1.score < s.match_threshold) {
            this.resetStreak();
            return;
        }
        // Guarda 3 — margen sobre el segundo. Un puntaje alto no basta si otra
        // persona puntua casi igual: eso es precisamente una confusion.
        if (margin < s.margin_min) {
            this.resetStreak();
            this.decide({
                status: "ambiguous", top1, top2, margin,
                reason: _t("Dos personas puntuan casi igual. El kiosco NO marcaria."),
            });
            return;
        }
        // Guarda 2 — consistencia entre frames. Una coincidencia por casualidad
        // no se repite; una identificacion real si.
        const now = Date.now();
        const withinWindow = now - this.streakLast <= (s.frame_window_ms || 2000);
        if (this.streakId === top1.id && withinWindow) {
            this.state.streak += 1;
        } else {
            this.streakId = top1.id;
            this.state.streak = 1;
        }
        this.streakLast = now;

        if (this.state.streak < (s.frames_required || 5)) {
            return;
        }
        // Guarda 5 — banda gris: marca, pero queda para revision del supervisor.
        this.decide({
            status: top1.score >= s.review_threshold ? "ok" : "review",
            top1, top2, margin,
            reason: top1.score >= s.review_threshold
                ? _t("Identificado con certeza. El kiosco marcaria.")
                : _t("Marcaria, pero dejandolo para revision del supervisor."),
        });
    }

    decide(payload) {
        this.state.decision = payload;
        this.holdUntil = Date.now() + HOLD_MS;
        this.state.log.unshift({
            key: `${Date.now()}`,
            status: payload.status,
            name: payload.top1.name,
            score: payload.top1.score,
            runnerUp: payload.top2?.name || "—",
            runnerUpScore: payload.top2?.score ?? null,
            margin: payload.margin,
        });
        this.state.log = this.state.log.slice(0, 20);
        this.resetStreak();
    }

    clearDecision() {
        this.state.decision = null;
        this.resetStreak();
    }

    resetStreak() {
        this.streakId = null;
        this.state.streak = 0;
    }

    /** Descarta la decision sostenida y vuelve a mirar de inmediato. */
    retry() {
        this.holdUntil = 0;
        this.clearDecision();
    }

    drawOverlay(faces, chosen) {
        const canvas = this.overlayRef.el;
        const video = this.videoRef.el;
        if (!canvas || !video?.videoWidth) {
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
        }
    }

    // -- lectura para la plantilla ---------------------------------------

    fmt(v) {
        return v === null || v === undefined ? "—" : v.toFixed(3);
    }

    get liveMessage() {
        return {
            no_face: _t("No veo ningun rostro"),
            ambiguous: _t("Hay dos personas en cuadro"),
            too_small: _t("Acercate mas a la camara"),
        }[this.state.live.reason] || _t("Rostro detectado");
    }

    get decisionClass() {
        return {
            ok: "alert-success", review: "alert-warning", ambiguous: "alert-danger",
        }[this.state.decision?.status] || "alert-secondary";
    }

    get decisionTitle() {
        return {
            ok: _t("IDENTIFICADO"),
            review: _t("IDENTIFICADO (para revisar)"),
            ambiguous: _t("NO SE MARCA — ambiguo"),
        }[this.state.decision?.status] || "";
    }

    rowClass(row) {
        if (row.score >= (this.ctx?.settings?.review_threshold ?? 0.65)) {
            return "table-success";
        }
        if (row.score >= (this.ctx?.settings?.match_threshold ?? 0.55)) {
            return "table-warning";
        }
        return "";
    }

    logClass(entry) {
        return { ok: "text-bg-success", review: "text-bg-warning",
                 ambiguous: "text-bg-danger" }[entry.status] || "text-bg-secondary";
    }
}

registry.category("actions").add("olive_face_identify", FaceIdentify);
