/** @odoo-module **/

// El acompanante: reconoce y marca mientras la persona usa su computadora.
//
// No es "modo kiosco". Un modo kiosco ocupa la pantalla entera y bloquea el
// equipo, y aqui la laptop es la herramienta de trabajo de alguien. Esto es una
// ventana chica que se queda encima de todo lo demas y sigue funcionando
// mientras ella escribe en otra aplicacion.
//
// La restriccion que decide el diseno: **Chrome congela lo que no se ve**. En
// una pestana oculta requestAnimationFrame deja de dispararse por completo, asi
// que el reconocimiento no se ralentizaria, se detendria. Por eso existe el
// boton de ventana flotante (Document Picture-in-Picture): crea una ventana
// real siempre encima, que para el navegador SI esta visible.
//
// Se refuerza con dos relojes:
//   * requestAnimationFrame cuando la pagina es visible: fluido y barato.
//   * setTimeout cuando no lo es: Chrome lo limita a una vez por minuto, pero
//     eso mantiene vivo el bucle y permite avisar en pantalla que asi no sirve.

import { Component, onMounted, onWillUnmount, useEffect, useRef, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";

import * as queue from "./queue";

const PIPELINE_BASE = "/olive_hr_attendance_face/static/lib/pipeline/pipeline.js";
const pipelineUrl = (v) => `${PIPELINE_BASE}?v=${encodeURIComponent(v || "0")}`;

// Cada cuanto se intenta vaciar la cola cuando hay algo pendiente.
const FLUSH_MS = 30000;
// Cuanto se sostiene en pantalla el saludo a quien acaba de marcar.
const GREET_MS = 5000;

function uuid4() {
    if (crypto.randomUUID) {
        return crypto.randomUUID().replace(/-/g, "");
    }
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    return [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function errorText(err) {
    return err?.data?.message || err?.message || String(err);
}

export class FaceKiosk extends Component {
    static template = "olive_hr_attendance_face.FaceKiosk";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.notification = useService("notification");

        this.videoRef = useRef("video");
        this.frameCanvas = document.createElement("canvas");

        this.state = useState({
            phase: "loading",
            progress: _t("Iniciando..."),
            error: null,
            cameraError: false,
            peopleCount: 0,
            queued: 0,
            online: navigator.onLine,
            persisted: false,
            floating: false,
            hidden: false,
            detected: false,
            streak: 0,
            greeting: null,      // a quien se acaba de reconocer
            todayCount: 0,
            lastSync: null,
        });

        // Estado que no necesita re-render y no debe dispararlo.
        this.people = [];
        this.running = false;
        this.cooldown = new Map();     // employee_id -> hasta cuando no marcar
        this.streakId = null;
        this.streakLast = 0;
        this.shapes = [];
        this.sharpness = [];
        this.bootId = uuid4();
        this.clockOffset = 0;
        this.greetUntil = 0;

        this.onVisibility = () => {
            this.state.hidden = document.visibilityState === "hidden";
        };
        this.onOnline = () => {
            this.state.online = navigator.onLine;
            if (navigator.onLine) {
                this.flush();
            }
        };

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

    // ==================================================================
    // Arranque
    // ==================================================================

    async start() {
        try {
            document.addEventListener("visibilitychange", this.onVisibility);
            window.addEventListener("online", this.onOnline);
            window.addEventListener("offline", this.onOnline);

            this.state.persisted = await queue.requestPersistence();
            this.state.queued = await queue.count();

            this.ctx = await this.orm.call(
                "olive.attendance.punch", "olive_kiosk_context", []);
            this.syncClock();

            this.state.progress = _t("Descargando modelos (solo la primera vez)...");
            this.pipeline = await import(pipelineUrl(this.ctx.pipeline_version));
            await this.pipeline.init(this.ctx.profile, () => {});

            // Un empleado, muchas fotos: el puntaje de la persona es el maximo
            // entre las suyas. Basta parecerse a una para ser reconocido.
            const byEmployee = new Map();
            for (const row of this.ctx.people) {
                if (!byEmployee.has(row.employee_id)) {
                    byEmployee.set(row.employee_id, {
                        id: row.employee_id, name: row.name, vecs: [],
                    });
                }
                byEmployee.get(row.employee_id).vecs.push({
                    template_id: row.template_id,
                    vec: this.pipeline.decodeEmbedding(row.embedding),
                });
            }
            this.people = [...byEmployee.values()];
            this.state.peopleCount = this.people.length;

            if (!this.people.length) {
                throw new Error(_t(
                    "No hay ninguna persona habilitada para marcar por rostro. "
                    + "Hay que procesar sus fotos y activarlas antes de usar esta "
                    + "pantalla."
                ));
            }

            this.state.progress = _t("Abriendo la camara...");
            try {
                this.stream = await navigator.mediaDevices.getUserMedia({
                    video: { width: { ideal: 1280 }, height: { ideal: 720 },
                             facingMode: "user" },
                    audio: false,
                });
            } catch (camErr) {
                this.state.cameraError = true;
                throw new Error(this.cameraErrorText(camErr));
            }

            this.state.phase = "ready";
            this.flushTimer = setInterval(() => this.flush(), FLUSH_MS);
            this.flush();
        } catch (err) {
            this.state.phase = "error";
            this.state.error = errorText(err);
        }
    }

    /** Desfase entre el reloj del servidor y el de esta laptop. */
    syncClock() {
        if (!this.ctx?.server_time) {
            return;
        }
        const server = new Date(this.ctx.server_time.replace(" ", "T") + "Z");
        this.clockOffset = (server.getTime() - Date.now()) / 1000;
    }

    cameraErrorText(err) {
        const name = err?.name || "";
        if (name === "NotAllowedError") {
            return _t("Le negaste el permiso a la camara. Habilitalo en el candado "
                      + "de la barra de direcciones y recarga.");
        }
        if (name === "NotFoundError" || name === "DevicesNotFoundError") {
            return _t("No se encontro ninguna camara conectada.");
        }
        if (name === "NotReadableError") {
            return _t("Otra aplicacion esta usando la camara. Cerra Zoom, Meet o "
                      + "Teams y volve a intentar.");
        }
        if (!navigator.mediaDevices) {
            return _t("El navegador bloqueo la camara porque la conexion no es "
                      + "segura (no es HTTPS ni localhost).");
        }
        return err?.message || String(err);
    }

    async attachStream(el) {
        el.srcObject = this.stream;
        try {
            await el.play();
        } catch (err) {
            this.state.phase = "error";
            this.state.error = errorText(err);
            return;
        }
        if (!this.running) {
            this.running = true;
            this.loop();
        }
    }

    stop() {
        this.running = false;
        clearInterval(this.flushTimer);
        document.removeEventListener("visibilitychange", this.onVisibility);
        window.removeEventListener("online", this.onOnline);
        window.removeEventListener("offline", this.onOnline);
        this.stream?.getTracks().forEach((t) => t.stop());
        this.pipWindow?.close();
    }

    // ==================================================================
    // Ventana flotante
    // ==================================================================

    /**
     * Saca la ventanita a flotar encima de todo con Document Picture-in-Picture.
     *
     * Es lo que resuelve el problema de fondo: una pestana tapada se congela,
     * pero una ventana PiP esta siempre visible para el navegador, asi que el
     * bucle sigue corriendo mientras ella trabaja en cualquier otra aplicacion.
     */
    async floatWindow() {
        if (!window.documentPictureInPicture) {
            this.notification.add(
                _t("Este navegador no puede abrir la ventana flotante. Hace falta "
                   + "Chrome o Edge. Mientras tanto, deja esta pestana visible en "
                   + "un costado de la pantalla."),
                { type: "warning" });
            return;
        }
        try {
            const pip = await window.documentPictureInPicture.requestWindow({
                width: 320, height: 420,
            });
            // La ventana PiP es un documento aparte: no hereda los estilos.
            for (const sheet of document.styleSheets) {
                try {
                    const css = [...sheet.cssRules].map((r) => r.cssText).join("");
                    const style = pip.document.createElement("style");
                    style.textContent = css;
                    pip.document.head.appendChild(style);
                } catch {
                    // Hoja de otro origen: no se puede leer y no importa.
                }
            }
            const host = this.el || document.querySelector(".o_olive_kiosk");
            pip.document.body.append(host);
            pip.addEventListener("pagehide", () => {
                document.querySelector(".o_action_manager")?.append(host);
                this.state.floating = false;
            });
            this.pipWindow = pip;
            this.state.floating = true;
        } catch (err) {
            this.notification.add(errorText(err), { type: "danger" });
        }
    }

    // ==================================================================
    // Bucle de reconocimiento
    // ==================================================================

    async loop() {
        while (this.running) {
            try {
                await this.tick();
            } catch (err) {
                console.warn("olive_kiosk:", err);
            }
            // Oculta, Chrome no dispara requestAnimationFrame en absoluto: el
            // temporizador mantiene el bucle vivo aunque sea a paso de tortuga.
            if (document.visibilityState === "hidden") {
                await new Promise((r) => setTimeout(r, 1000));
            } else {
                await new Promise((r) => requestAnimationFrame(r));
            }
        }
    }

    async tick() {
        const video = this.videoRef.el;
        if (!video?.videoWidth) {
            return;
        }
        if (this.state.greeting && Date.now() < this.greetUntil) {
            return;
        }
        if (this.state.greeting) {
            this.state.greeting = null;
        }

        const frame = this.pipeline.grabFrame(video, this.frameCanvas);
        const faces = await this.pipeline.detect(frame, this.ctx.settings);
        const picked = this.pipeline.pickFace(faces, this.ctx.settings);
        this.state.detected = Boolean(picked.face);

        if (!picked.face) {
            this.resetStreak();
            return;
        }

        const result = await this.pipeline.embed(frame, picked.face);
        this.evaluate(result);
    }

    /** Puntua contra todos y aplica las guardas antes de marcar. */
    evaluate(result) {
        const scored = this.people.map((person) => {
            let best = -1;
            let templateId = null;
            for (const t of person.vecs) {
                const sim = this.pipeline.cosine(result.vector, t.vec);
                if (sim > best) {
                    best = sim;
                    templateId = t.template_id;
                }
            }
            return { id: person.id, name: person.name, score: best, templateId };
        });
        scored.sort((a, b) => b.score - a.score);
        const top1 = scored[0];
        const top2 = scored[1];
        const margin = top2 ? top1.score - top2.score : 1;
        const s = this.ctx.settings;

        // Guarda 1 — umbral. Ante duda no se marca.
        if (top1.score < s.match_threshold) {
            this.resetStreak();
            return;
        }
        // Guarda 3 — margen sobre el segundo candidato.
        if (margin < s.margin_min) {
            this.resetStreak();
            return;
        }
        // Guarda 4 — bloqueo por persona. Con una camara pasiva esto es lo que
        // evita cientos de registros de quien trabaja frente a ella.
        const until = this.cooldown.get(top1.id) || 0;
        if (Date.now() < until) {
            return;
        }
        // Guarda 2 — consistencia entre frames.
        const now = Date.now();
        if (this.streakId === top1.id && now - this.streakLast <= (s.frame_window_ms || 4000)) {
            this.state.streak += 1;
        } else {
            this.streakId = top1.id;
            this.state.streak = 1;
            this.shapes = [];
            this.sharpness = [];
        }
        this.streakLast = now;
        this.shapes.push(result.shape);
        this.sharpness.push(result.sharpness);

        if (this.state.streak < (s.frames_required || 5)) {
            return;
        }
        this.register(top1, top2, margin);
    }

    resetStreak() {
        this.streakId = null;
        this.state.streak = 0;
        this.shapes = [];
        this.sharpness = [];
    }

    /**
     * Senal de vida, sin modelo de por medio.
     *
     * Combina dos cosas que ya salen del pipeline: cuanto varia la geometria del
     * rostro entre frames (una foto es rigida) y cuanto detalle fino tiene el
     * recorte (una pantalla pierde textura). No es una prueba de vida
     * certificada y un video la enganaria; corta el ataque facil, que es
     * sostener una foto delante de la camara.
     */
    livenessScore() {
        const variation = this.pipeline.shapeVariation(this.shapes);
        const sharp = this.sharpness.length
            ? this.sharpness.reduce((a, b) => a + b, 0) / this.sharpness.length : 0;
        const movement = Math.min(1, variation / 0.004);
        const texture = Math.min(1, sharp / 120);
        return Math.round((0.6 * movement + 0.4 * texture) * 100) / 100;
    }

    async register(top1, top2, margin) {
        const s = this.ctx.settings;
        const liveness = this.livenessScore();
        const suspect = s.liveness_required && liveness < (s.liveness_threshold || 0.7);

        const punch = {
            uuid: uuid4(),
            device_id: this.ctx.device_id,
            device_time: new Date().toISOString().slice(0, 19).replace("T", " "),
            clock_offset_seconds: this.clockOffset,
            monotonic_ms: Math.round(performance.now()),
            boot_id: this.bootId,
            employee_id: top1.id,
            match_score: Number(top1.score.toFixed(4)),
            margin_score: Number(margin.toFixed(4)),
            frames_agreed: this.state.streak,
            liveness_score: liveness,
            template_id: top1.templateId,
            runner_up_employee_id: top2?.id || false,
            embedding_version: this.ctx.embedding_version,
            // Guarda 5 — banda gris, y ahora tambien la sospecha de foto: se
            // marca igual, pero queda senalado para que alguien lo mire.
            needs_review: top1.score < (s.review_threshold || 0.65) || suspect,
        };

        await queue.push(punch);
        this.state.queued = await queue.count();
        this.state.todayCount += 1;

        this.cooldown.set(top1.id, Date.now() + (s.cooldown_seconds || 900) * 1000);
        this.resetStreak();
        this.state.greeting = {
            name: top1.name,
            score: top1.score,
            suspect,
        };
        this.greetUntil = Date.now() + GREET_MS;

        this.flush();
    }

    // ==================================================================
    // Envio
    // ==================================================================

    async flush() {
        if (this.flushing || !navigator.onLine) {
            return;
        }
        this.flushing = true;
        try {
            const pending = await queue.all();
            if (!pending.length) {
                return;
            }
            const result = await this.orm.call(
                "olive.attendance.punch", "olive_receive_punches", [pending]);
            // Solo se borra lo que el servidor confirmo. Lo rechazado tambien:
            // reintentarlo para siempre no lo va a arreglar, y queda registrado
            // del lado del servidor.
            await queue.drop([
                ...result.accepted, ...result.duplicate, ...result.rejected,
            ]);
            this.state.queued = await queue.count();
            this.state.lastSync = new Date().toLocaleTimeString();
        } catch (err) {
            // Sin red no es un error: para eso existe la cola.
            console.warn("olive_kiosk: no se pudo sincronizar", err);
        } finally {
            this.flushing = false;
        }
    }

    // -- lectura para la plantilla ---------------------------------------

    get ctxFrames() {
        return this.ctx?.settings?.frames_required || 5;
    }

    get statusLabel() {
        if (!this.state.online) {
            return _t("Sin conexion — se guarda en la laptop");
        }
        if (this.state.queued) {
            return _t("Enviando %s pendiente(s)", this.state.queued);
        }
        return _t("Al dia");
    }

    get statusClass() {
        if (!this.state.online) {
            return "text-bg-warning";
        }
        return this.state.queued ? "text-bg-info" : "text-bg-success";
    }
}

registry.category("actions").add("olive_face_kiosk", FaceKiosk);
