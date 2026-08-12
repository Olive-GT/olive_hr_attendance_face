/** @odoo-module **/

// La cuadricula de la quincena: quien vino cada dia.
//
// Resuelve el problema de los horarios rotativos sin modelarlos. Un patron 3x3
// se reconoce a simple vista en la fila de la persona —tres llenos, tres
// vacios— y ninguna configuracion puede competir con eso. El supervisor decide;
// el sistema solo senala lo que le parece sospechoso y dice por que.
//
// Regla de la pantalla: nada de lo que se ve aqui descuenta dinero por si solo.
// Solo el estado "Confirmada" llega a la nomina, y solo lo pone una persona.

import { Component, onMounted, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";

const STATUS = {
    worked: { cls: "o_olive_worked", label: "Vino", icon: "✓" },
    confirmed: { cls: "o_olive_confirmed", label: "Ausencia confirmada", icon: "✕" },
    proposed: { cls: "o_olive_candidate", label: "Posible ausencia", icon: "!" },
    excused: { cls: "o_olive_excused", label: "Permiso o vacaciones", icon: "P" },
    // Ni asistencia ni ausencia = descanso. No es un vacio de informacion: es
    // la respuesta, y por eso no lleva color ni pide nada.
    quiet: { cls: "o_olive_quiet", label: "Descanso (sin marcar)", icon: "·" },
    future: { cls: "o_olive_future", label: "", icon: "" },
};

const WEEKDAYS = ["L", "M", "X", "J", "V", "S", "D"];

function errorText(err) {
    return err?.data?.message || err?.message || String(err);
}

export class AbsenceGrid extends Component {
    static template = "olive_hr_attendance_face.AbsenceGrid";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.notification = useService("notification");
        this.action = useService("action");

        this.state = useState({
            phase: "loading",
            error: null,
            dateFrom: "",
            dateTo: "",
            dates: [],
            rows: [],
            selected: null,   // celda abierta para revisar
            busy: false,
        });
        this.STATUS = STATUS;
        this.WEEKDAYS = WEEKDAYS;

        onMounted(() => this.load());
    }

    async load(dateFrom = null, dateTo = null) {
        this.state.phase = "loading";
        try {
            const data = await this.orm.call(
                "olive.attendance.absence", "olive_grid_payload",
                [dateFrom, dateTo]);
            Object.assign(this.state, {
                dateFrom: data.date_from, dateTo: data.date_to,
                dates: data.dates, rows: data.rows, phase: "ready",
            });
        } catch (err) {
            this.state.phase = "error";
            this.state.error = errorText(err);
        }
    }

    async reload() {
        await this.load(this.state.dateFrom, this.state.dateTo);
    }

    /** Mueve el periodo una quincena hacia atras o hacia adelante. */
    async shift(direction) {
        const from = new Date(this.state.dateFrom + "T00:00:00");
        const to = new Date(this.state.dateTo + "T00:00:00");
        const span = Math.round((to - from) / 86400000) + 1;
        from.setDate(from.getDate() + direction * span);
        to.setDate(to.getDate() + direction * span);
        const fmt = (d) => d.toISOString().slice(0, 10);
        await this.load(fmt(from), fmt(to));
    }

    async runScan() {
        this.state.busy = true;
        try {
            await this.orm.call("olive.attendance.absence", "_scan_period",
                                [this.state.dateFrom, this.state.dateTo]);
            await this.reload();
            this.notification.add(
                _t("Barrido terminado. Nada se descuenta hasta que lo confirmes."),
                { type: "success" });
        } catch (err) {
            this.notification.add(errorText(err), { type: "danger" });
        } finally {
            this.state.busy = false;
        }
    }

    select(row, cell) {
        if (cell.status === "future") {
            return;
        }
        this.state.selected = { row, cell };
    }

    close() {
        this.state.selected = null;
    }

    /** Vacaciones y permisos viven en Ausencias de Odoo, no aqui. */
    async openLeave() {
        const { row, cell } = this.state.selected || {};
        if (!row) {
            return;
        }
        this.state.selected = null;
        await this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "hr.leave",
            view_mode: "form",
            views: [[false, "form"]],
            target: "new",
            name: _t("Permiso o vacaciones"),
            context: {
                default_employee_id: row.employee_id,
                default_request_date_from: cell.date,
                default_request_date_to: cell.date,
            },
        });
    }

    async setState(newState, reason = null) {
        const { row, cell } = this.state.selected || {};
        if (!row) {
            return;
        }
        this.state.busy = true;
        try {
            await this.orm.call("olive.attendance.absence", "olive_grid_set_state",
                                [row.employee_id, cell.date, newState, reason]);
            this.state.selected = null;
            await this.reload();
        } catch (err) {
            this.notification.add(errorText(err), { type: "danger" });
        } finally {
            this.state.busy = false;
        }
    }

    statusOf(cell) {
        return STATUS[cell.status] || STATUS.quiet;
    }

    /** Fondo de la columna: un dia en que no vino casi nadie fue feriado. */
    dayClass(day) {
        if (!day.total) {
            return "";
        }
        const ratio = day.present / day.total;
        if (ratio === 0) {
            return "o_olive_col_empty";
        }
        return ratio < 0.25 ? "o_olive_col_low" : "";
    }

    get totals() {
        return this.state.rows.reduce((acc, row) => ({
            confirmed: acc.confirmed + row.confirmed,
            pending: acc.pending + row.pending,
        }), { confirmed: 0, pending: 0 });
    }
}

registry.category("actions").add("olive_absence_grid", AbsenceGrid);
