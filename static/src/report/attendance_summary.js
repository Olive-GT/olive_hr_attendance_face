/** @odoo-module **/

// Resumen de comportamiento de asistencia.
//
// No tiene nada que ver con la nomina: eso se resuelve en la cuadricula de
// ausencias. Esto responde otras preguntas, las de operacion — a que hora
// arranca de verdad la obra, quien llega irregular, quien se queda de mas.
//
// La cifra que mas informa no es la hora promedio sino la REGULARIDAD. Dos
// personas pueden promediar las 7:00 y una entrar siempre a las 7:00 mientras
// la otra alterna entre las 6:00 y las 8:00. La segunda es un problema de
// operacion aunque su promedio se vea impecable, y solo la dispersion lo
// delata.

import { Component, onMounted, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

/** 7.25 -> "07:15". Una hora decimal no se lee; una hora de reloj si. */
function asClock(value) {
    if (!value) {
        return "—";
    }
    const hours = Math.floor(value);
    const minutes = Math.round((value - hours) * 60);
    return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}`;
}

function signedMinutes(value) {
    if (!value) {
        return "0";
    }
    return `${value > 0 ? "+" : ""}${Math.round(value)}`;
}

export class AttendanceSummary extends Component {
    static template = "olive_hr_attendance_face.AttendanceSummary";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.state = useState({
            phase: "loading", error: null,
            dateFrom: "", dateTo: "", rows: [], site: {},
            sortBy: "name",
        });
        this.asClock = asClock;
        this.signedMinutes = signedMinutes;
        onMounted(() => this.load());
    }

    async load(dateFrom = null, dateTo = null) {
        this.state.phase = "loading";
        try {
            // Sin fechas, el servidor resuelve la quincena en curso.
            const data = await this.orm.call(
                "hr.attendance", "olive_behaviour_summary", [dateFrom, dateTo]);
            Object.assign(this.state, {
                dateFrom: data.date_from, dateTo: data.date_to,
                rows: data.rows, site: data.site, phase: "ready",
            });
        } catch (err) {
            this.state.phase = "error";
            this.state.error = err?.data?.message || err?.message || String(err);
        }
    }

    async shift(direction) {
        const from = new Date(this.state.dateFrom + "T00:00:00");
        const to = new Date(this.state.dateTo + "T00:00:00");
        const span = Math.round((to - from) / 86400000) + 1;
        from.setDate(from.getDate() + direction * span);
        to.setDate(to.getDate() + direction * span);
        const fmt = (d) => d.toISOString().slice(0, 10);
        await this.load(fmt(from), fmt(to));
    }

    sortBy(field) {
        this.state.sortBy = field;
        const numeric = field !== "name";
        this.state.rows = [...this.state.rows].sort((a, b) =>
            numeric ? b[field] - a[field] : String(a[field]).localeCompare(b[field]));
    }

    /** Mas de media hora de dispersion ya es un horario impredecible. */
    spreadClass(row) {
        if (row.spread_minutes >= 60) {
            return "text-danger fw-bold";
        }
        return row.spread_minutes >= 30 ? "text-warning fw-bold" : "";
    }

    lateClass(row) {
        if (row.vs_crew_minutes >= 30) {
            return "text-danger fw-bold";
        }
        return row.vs_crew_minutes >= 15 ? "text-warning" : "";
    }
}

registry.category("actions").add("olive_attendance_summary", AttendanceSummary);
