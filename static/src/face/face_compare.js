/** @odoo-module **/

// Matriz de similitud entre fotos ya procesadas.
//
// Responde la pregunta que decide el proyecto —¿el sistema distingue a estas
// personas?— **sin necesitar camara ni HTTPS**, porque los vectores ya estan
// calculados y la similitud coseno entre vectores normalizados es un simple
// producto punto.
//
// Sirve para dos cosas distintas y complementarias:
//   * Fotos de la MISMA persona: cuanto rinde su foto de archivo. Si dos fotos
//     suyas puntuan bajo, esa persona no va a ser reconocida en la caseta.
//   * Fotos de personas DISTINTAS: el riesgo de confusion. Si dos personas
//     puntuan alto, el umbral las va a confundir. Es lo que mas importa.

import { Component, onMounted, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";

function decode(b64) {
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) {
        bytes[i] = bin.charCodeAt(i);
    }
    return new Float32Array(bytes.buffer);
}

function cosine(a, b) {
    let s = 0;
    for (let i = 0; i < a.length; i++) {
        s += a[i] * b[i];
    }
    return s;
}

export class FaceCompare extends Component {
    static template = "olive_hr_attendance_face.FaceCompare";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.state = useState({
            phase: "loading", error: null, items: [], matrix: [],
            settings: { match_threshold: 0.55, review_threshold: 0.65 },
            stats: { same: [], diff: [] },
        });
        onMounted(() => this.load());
    }

    async load() {
        try {
            const ids = this.props.action?.params?.template_ids || [];
            const data = await this.orm.call(
                "olive.attendance.face.template", "olive_compare_payload", [ids]);
            this.state.settings = data.settings;
            const items = data.items.map((it) => ({ ...it, vec: decode(it.embedding) }));
            this.state.items = items;

            const same = [];
            const diff = [];
            this.state.matrix = items.map((a, i) => items.map((b, j) => {
                if (i === j) {
                    return null;
                }
                const score = cosine(a.vec, b.vec);
                if (i < j) {
                    (a.employee_id === b.employee_id ? same : diff).push(score);
                }
                return score;
            }));
            this.state.stats = { same, diff };
            this.state.phase = "ready";
        } catch (err) {
            this.state.phase = "error";
            this.state.error = err?.data?.message || err?.message || String(err);
        }
    }

    cellClass(score, rowIdx, colIdx) {
        if (score === null) {
            return "table-secondary";
        }
        const sameEmployee =
            this.state.items[rowIdx].employee_id === this.state.items[colIdx].employee_id;
        const { match_threshold: match } = this.state.settings;
        // La lectura correcta depende de quien es quien: para la misma persona
        // un puntaje alto es bueno; para personas distintas es exactamente el
        // fallo que hay que evitar.
        if (sameEmployee) {
            return score >= match ? "table-success" : "table-danger";
        }
        return score >= match ? "table-danger" : "table-success";
    }

    fmt(v) {
        return v === null ? "—" : v.toFixed(3);
    }

    get sameMin() {
        return this.state.stats.same.length ? Math.min(...this.state.stats.same) : null;
    }

    get diffMax() {
        return this.state.stats.diff.length ? Math.max(...this.state.stats.diff) : null;
    }

    /** El hueco entre el peor caso propio y el mejor caso ajeno. */
    get margin() {
        return this.sameMin !== null && this.diffMax !== null
            ? this.sameMin - this.diffMax : null;
    }

    get verdict() {
        const m = this.margin;
        if (m === null) {
            return {
                cls: "alert-secondary",
                text: _t("Faltan datos: hacen falta al menos dos fotos de la misma "
                         + "persona y dos de personas distintas para poder juzgar."),
            };
        }
        if (m > 0.15) {
            return { cls: "alert-success", text: _t(
                "Separacion amplia. El umbral tiene margen de sobra y el "
                + "reconocimiento deberia ser fiable con estas fotos.") };
        }
        if (m > 0.05) {
            return { cls: "alert-warning", text: _t(
                "Separacion justa. Funciona, pero conviene agregar capturas con "
                + "camara en las condiciones reales del kiosco antes de confiar.") };
        }
        return { cls: "alert-danger", text: _t(
            "Separacion insuficiente: el mejor puntaje entre personas distintas "
            + "alcanza al peor puntaje de la misma persona. Con estas fotos habria "
            + "confusiones. Hay que enrolar con capturas en vivo.") };
    }
}

registry.category("actions").add("olive_face_compare", FaceCompare);
