# -*- coding: utf-8 -*-
"""Trazabilidad de las asistencias que produjo el kiosco facial.

`olive_punch_ids` es lo que distingue una asistencia **reconstruible** (nacida
de marcajes del kiosco) de un **bloque inmutable** (creada a mano por RRHH,
importada, o hecha con el kiosco nativo de Odoo). El doblado de F2 solo puede
tocar las primeras: el trabajo manual de una persona nunca se pisa.
"""

import statistics
from datetime import timedelta

from pytz import timezone, utc

from odoo import _, api, fields, models


class HrAttendance(models.Model):
    _inherit = "hr.attendance"

    olive_punch_ids = fields.One2many(
        "olive.attendance.punch", "attendance_id", string="Marcajes del kiosco",
    )
    olive_is_managed = fields.Boolean(
        compute="_compute_olive_is_managed", store=True,
        string="Reconstruible por el kiosco",
        help="Falso = asistencia creada fuera del kiosco facial. El doblado la "
             "trata como bloque inmutable y jamas la modifica.",
    )
    olive_needs_review = fields.Boolean(
        compute="_compute_olive_needs_review", store=True, string="Requiere revision",
    )
    olive_rebuilt_count = fields.Integer(
        default=0, readonly=True, string="Veces reconstruida",
        help="Cuantas veces la reconstruyo el doblado al llegar marcajes tardios.",
    )
    olive_anomaly = fields.Selection(
        [("missing_out", "Sin marcaje de salida"),
         ("forced_close", "Cierre forzado por jornada maxima"),
         ("clock_unreliable", "Reloj del equipo no confiable")],
        string="Anomalia", readonly=True, index=True,
    )

    # -- analisis de comportamiento ---------------------------------------
    #
    # Odoo sabe agrupar asistencias por fecha, pero no sabe promediar una HORA
    # DEL DIA: para el ORM `check_in` es un instante, no un momento del reloj.
    # Guardando la hora local como numero, las tablas dinamicas y los graficos
    # que Odoo ya trae pueden responder "¿a que hora entra en promedio?" sin
    # escribir un solo informe a medida.
    olive_local_date = fields.Date(
        compute="_compute_olive_local", store=True, index=True,
        string="Dia (local)",
        help="La fecha en la zona horaria del empleado. check_in esta en UTC, "
             "asi que agrupar por el directamente parte los turnos nocturnos.",
    )
    olive_check_in_hour = fields.Float(
        compute="_compute_olive_local", store=True, aggregator="avg",
        string="Hora de entrada", digits=(4, 2),
    )
    olive_check_out_hour = fields.Float(
        compute="_compute_olive_local", store=True, aggregator="avg",
        string="Hora del ultimo avistamiento", digits=(4, 2),
        help="Con una camara pasiva es la ultima vez que se vio a la persona, "
             "que no es lo mismo que la hora en que se fue: si se retira sin "
             "volver a pasar frente a la camara, este numero queda corto.",
    )
    olive_weekday = fields.Selection(
        [("0", "Lunes"), ("1", "Martes"), ("2", "Miercoles"), ("3", "Jueves"),
         ("4", "Viernes"), ("5", "Sabado"), ("6", "Domingo")],
        compute="_compute_olive_local", store=True, string="Dia de la semana",
    )

    @api.depends("check_in", "check_out", "employee_id")
    def _compute_olive_local(self):
        for attendance in self:
            tz = timezone(
                attendance.employee_id.tz
                or attendance.employee_id.company_id.partner_id.tz
                or "UTC"
            )
            if not attendance.check_in:
                attendance.olive_local_date = False
                attendance.olive_check_in_hour = 0.0
                attendance.olive_check_out_hour = 0.0
                attendance.olive_weekday = False
                continue
            local_in = utc.localize(attendance.check_in).astimezone(tz)
            attendance.olive_local_date = local_in.date()
            attendance.olive_weekday = str(local_in.weekday())
            attendance.olive_check_in_hour = local_in.hour + local_in.minute / 60.0
            if attendance.check_out:
                local_out = utc.localize(attendance.check_out).astimezone(tz)
                attendance.olive_check_out_hour = (
                    local_out.hour + local_out.minute / 60.0)
            else:
                attendance.olive_check_out_hour = 0.0

    @api.depends("olive_punch_ids")
    def _compute_olive_is_managed(self):
        for attendance in self:
            attendance.olive_is_managed = bool(attendance.olive_punch_ids)

    @api.depends("olive_punch_ids.review_state", "olive_anomaly")
    def _compute_olive_needs_review(self):
        for attendance in self:
            attendance.olive_needs_review = bool(
                attendance.olive_anomaly
                or any(p.review_state == "pending" for p in attendance.olive_punch_ids)
            )

    # ==================================================================
    # Resumen de comportamiento
    # ==================================================================

    @api.model
    def olive_behaviour_summary(self, date_from=None, date_to=None,
                                department_id=None):
        """Como se comporto cada empleado en el periodo.

        Esto NO alimenta la nomina. Es para entender el sitio: quien llega
        temprano, quien llega irregular, a que hora arranca de verdad la obra.

        La medida mas util no es la hora promedio sino **la regularidad**: dos
        personas pueden entrar en promedio a las 7:00 y una hacerlo siempre a
        las 7:00 y la otra alternar entre las 6:00 y las 8:00. La segunda es un
        problema de operacion aunque su promedio se vea perfecto.

        Y la comparacion se hace contra la propia cuadrilla, no contra un
        horario configurado: si toda la obra entra a las 6:30, llegar a las 7:00
        es tarde aunque ningun calendario lo diga.
        """
        if not date_from or not date_to:
            date_from, date_to = self.env["olive.attendance.absence"]._quincena_bounds()
        date_from = fields.Date.to_date(date_from)
        date_to = fields.Date.to_date(date_to)
        domain = [
            ("olive_local_date", ">=", date_from),
            ("olive_local_date", "<=", date_to),
            ("employee_id.company_id", "=", self.env.company.id),
        ]
        if department_id:
            domain.append(("employee_id.department_id", "=", department_id))
        attendances = self.sudo().search(domain, order="check_in asc")

        # Mediana de entrada de la obra cada dia: el patron de referencia sale
        # de la gente, no de una configuracion.
        by_day = {}
        for attendance in attendances:
            by_day.setdefault(attendance.olive_local_date, []).append(
                attendance.olive_check_in_hour)
        day_median = {
            day: statistics.median(hours) for day, hours in by_day.items() if hours
        }

        per_employee = {}
        for attendance in attendances:
            employee = attendance.employee_id
            bucket = per_employee.setdefault(employee.id, {
                "employee_id": employee.id,
                "name": employee.display_name,
                "department": employee.department_id.display_name or "",
                "days": set(), "in_hours": [], "out_hours": [],
                "worked_hours": 0.0, "vs_crew": [],
            })
            bucket["days"].add(attendance.olive_local_date)
            bucket["in_hours"].append(attendance.olive_check_in_hour)
            if attendance.check_out:
                bucket["out_hours"].append(attendance.olive_check_out_hour)
                bucket["worked_hours"] += attendance.worked_hours
            median = day_median.get(attendance.olive_local_date)
            if median is not None:
                bucket["vs_crew"].append(attendance.olive_check_in_hour - median)

        absences = dict(self.env["olive.attendance.absence"].sudo()._read_group(
            [
                ("absence_date", ">=", date_from), ("absence_date", "<=", date_to),
                ("state", "=", "confirmed"),
                ("employee_id", "in", list(per_employee)),
            ],
            ["employee_id"], ["__count"],
        ))

        rows = []
        for data in per_employee.values():
            in_hours = data["in_hours"]
            out_hours = data["out_hours"]
            days = len(data["days"])
            employee = self.env["hr.employee"].browse(data["employee_id"])
            rows.append({
                "employee_id": data["employee_id"],
                "name": data["name"],
                "department": data["department"],
                "days": days,
                "avg_in": round(statistics.mean(in_hours), 2) if in_hours else 0,
                "earliest_in": round(min(in_hours), 2) if in_hours else 0,
                "latest_in": round(max(in_hours), 2) if in_hours else 0,
                # Desviacion tipica en MINUTOS: es la cifra de regularidad, y se
                # da en minutos porque "0.35 horas" no le dice nada a nadie.
                "spread_minutes": round(
                    statistics.pstdev(in_hours) * 60, 0) if len(in_hours) > 1 else 0,
                "avg_out": round(statistics.mean(out_hours), 2) if out_hours else 0,
                "avg_hours": round(
                    data["worked_hours"] / days, 2) if days else 0,
                "total_hours": round(data["worked_hours"], 1),
                "vs_crew_minutes": round(
                    statistics.mean(data["vs_crew"]) * 60, 0) if data["vs_crew"] else 0,
                "absences": absences.get(employee, 0),
            })
        rows.sort(key=lambda r: r["name"])

        all_in = [h for data in per_employee.values() for h in data["in_hours"]]
        return {
            "date_from": str(date_from), "date_to": str(date_to),
            "rows": rows,
            "site": {
                "employees": len(rows),
                "attendances": len(attendances),
                "median_in": round(statistics.median(all_in), 2) if all_in else 0,
                "days": len(by_day),
            },
        }
