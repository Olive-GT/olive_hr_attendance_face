# -*- coding: utf-8 -*-
"""Deteccion de ausencias sin saber el horario.

Este es el unico punto de contacto con la nomina. La nomina es quincenal, asume
que el empleado asistio, y descuenta por los dias en que no. Asi que de todo lo
que produce el kiosco, lo unico que necesita el pago es una lista de dias:
**cuando alguien no vino y tenia que venir.**

El criterio que gobierna todo el diseno
---------------------------------------
No pagarle un dia a alguien que si trabajo es una falla grave. Pagarle un dia a
alguien que falto es un error menor y recuperable. La asimetria es enorme, y de
ella se derivan tres reglas duras:

1. **Nada se descuenta automaticamente.** El sistema detecta; una persona
   confirma. Solo el estado `confirmed` llega a la nomina.
2. **Ante la duda, no se propone nada.** Es preferible dejar pasar una ausencia
   real que proponer una falsa.
3. **Toda propuesta viene con su explicacion.** El supervisor tiene que poder
   ver POR QUE el sistema sospecha, y contradecirlo con un clic.

El problema del horario, y por que no se usa el calendario
----------------------------------------------------------
En construccion los turnos son rotativos: tres dias de trabajo, tres de
descanso. Un ciclo de 6 dias no cabe en el calendario de Odoo, que repite cada 7
o cada 14. Forzarlo produciria dias "esperados" equivocados, y por tanto
ausencias falsas — exactamente en la direccion peligrosa.

Asi que no se pregunta ningun calendario. **Se deduce de los datos que el propio
sistema ya genero**, con dos senales que no necesitan configuracion:

* **El grupo delata el dia.** Si 34 de 39 companeros vinieron, era dia de
  trabajo. Si no vino casi nadie, era descanso o feriado y nadie falto.
* **El ritmo personal delata la ausencia.** Un hueco de un solo dia entre dos
  dias trabajados es una falta. Un hueco de tres dias seguidos, en alguien que
  despues vuelve, es una rotacion.

Ninguna de las dos sabe nada de horarios, y las dos mejoran solas a medida que
se acumula historial.
"""

import logging
from datetime import date as date_type, datetime, time, timedelta

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)

# Dias de historial que se miran alrededor del periodo para calcular huecos y
# vecinos. Con 7 alcanza para reconocer una rotacion de 3x3 completa.
CONTEXT_DAYS = 7


class OliveAttendanceAbsence(models.Model):
    _name = "olive.attendance.absence"
    _description = "Ausencia detectada"
    _inherit = ["mail.thread"]
    _order = "absence_date desc, employee_id"

    employee_id = fields.Many2one(
        "hr.employee", required=True, index=True, ondelete="cascade",
    )
    company_id = fields.Many2one(
        related="employee_id.company_id", store=True, index=True,
    )
    department_id = fields.Many2one(
        related="employee_id.department_id", store=True, index=True,
    )
    absence_date = fields.Date(required=True, index=True, string="Dia")

    state = fields.Selection(
        [
            ("proposed", "Propuesta por el sistema"),
            ("confirmed", "Ausencia confirmada"),
            ("rejected", "No era ausencia"),
        ],
        default="proposed", required=True, index=True, tracking=True,
        help="Solo 'confirmada' llega a la nomina, y solo la pone una persona.\n\n"
             "'No era ausencia' no es un estado con significado propio: equivale "
             "a no tener registro, y solo existe para que el barrido no vuelva a "
             "insistir con el mismo dia.",
    )
    reason = fields.Selection(
        [
            ("unjustified", "Falta injustificada"),
            ("rest_day", "Era su dia de descanso"),
            ("kiosk_failure", "Vino pero el kiosco no lo registro"),
            ("other", "Otro"),
        ],
        tracking=True,
        help="Vacaciones y permisos NO van aqui: se registran en Ausencias de "
             "Odoo (hr.leave), que es donde ya viven.",
    )
    note = fields.Text(string="Nota")

    # -- por que el sistema sospecha --------------------------------------
    confidence = fields.Float(
        digits=(3, 2), string="Confianza", readonly=True,
        help="Que tan seguro esta el sistema de que esto es una ausencia real. "
             "No decide nada: solo ordena la lista para revisar primero lo mas "
             "probable.",
    )
    signals = fields.Text(
        readonly=True, string="Por que se sospecha",
        help="La explicacion en palabras. Si el sistema no puede explicar por "
             "que sospecha, no deberia proponer nada.",
    )
    peers_present = fields.Integer(readonly=True, string="Companeros presentes")
    peers_total = fields.Integer(readonly=True, string="Companeros en total")
    gap_length = fields.Integer(
        readonly=True, string="Dias seguidos sin venir",
        help="Un hueco largo suele ser rotacion o vacaciones, no una falta.",
    )
    worked_before = fields.Boolean(readonly=True, string="Trabajo el dia anterior")
    worked_after = fields.Boolean(readonly=True, string="Trabajo el dia siguiente")

    reviewed_by_uid = fields.Many2one("res.users", readonly=True)
    reviewed_date = fields.Datetime(readonly=True)

    _sql_constraints = [
        ("employee_date_uniq", "unique(employee_id, absence_date)",
         "Ya existe un registro de ausencia para ese empleado ese dia."),
    ]

    @api.depends("employee_id", "absence_date")
    def _compute_display_name(self):
        for record in self:
            record.display_name = "%s · %s" % (
                record.employee_id.display_name or "?", record.absence_date or "")

    # ==================================================================
    # Deteccion
    # ==================================================================

    @api.model
    def _scan_period(self, date_from, date_to, company=None, employee_ids=None):
        """Busca ausencias en un periodo y propone candidatas.

        Nunca toca un registro que ya reviso una persona.
        """
        company = company or self.env.company
        date_from = fields.Date.to_date(date_from)
        date_to = fields.Date.to_date(date_to)
        # Hoy nunca se evalua: la jornada esta a medias y todos pareceran
        # ausentes hasta que marquen.
        today = fields.Date.context_today(self)
        date_to = min(date_to, today - timedelta(days=1))
        if date_from > date_to:
            return {"scanned": 0, "created": 0, "removed": 0}

        domain = [("company_id", "=", company.id)]
        if employee_ids:
            domain.append(("id", "in", employee_ids))
        employees = self.env["hr.employee"].sudo().search(domain)
        if not employees:
            return {"scanned": 0, "created": 0, "removed": 0}

        presence = self._presence_map(employees, date_from, date_to)
        excused = self._excused_map(employees, date_from, date_to)
        threshold = company.olive_absence_min_confidence or 0.6

        created = removed = scanned = 0
        existing = {
            (a.employee_id.id, a.absence_date): a
            for a in self.sudo().search([
                ("employee_id", "in", employees.ids),
                ("absence_date", ">=", date_from),
                ("absence_date", "<=", date_to),
            ])
        }

        for day in self._days_between(date_from, date_to):
            resolve_peers = self._peer_counts(employees, day, presence)
            for employee in employees:
                if not self._employed_on(employee, day):
                    continue
                scanned += 1
                record = existing.get((employee.id, day))

                if (employee.id, day) in presence:
                    # Llego un marcaje tardio y ahora si consta que vino: la
                    # candidata deja de tener sentido. Solo se retira si nadie
                    # la habia revisado.
                    if record and record.state == "proposed":
                        record.unlink()
                        removed += 1
                    continue

                if (employee.id, day) in excused:
                    if record and record.state == "proposed":
                        record.unlink()
                        removed += 1
                    continue

                evaluation = self._evaluate(
                    employee, day, presence, resolve_peers(employee))
                if evaluation["confidence"] < threshold:
                    # No se propone nada. Igual aparece en la cuadricula como
                    # "no vino", pero sin acusar a nadie.
                    if record and record.state == "proposed":
                        record.unlink()
                        removed += 1
                    continue

                values = {
                    "confidence": evaluation["confidence"],
                    "signals": evaluation["signals"],
                    "peers_present": evaluation["peers_present"],
                    "peers_total": evaluation["peers_total"],
                    "gap_length": evaluation["gap_length"],
                    "worked_before": evaluation["worked_before"],
                    "worked_after": evaluation["worked_after"],
                }
                if record:
                    # Un dia que ya reviso una persona no se vuelve a tocar,
                    # aunque el barrido cambie de opinion.
                    if record.state == "proposed":
                        record.write(values)
                else:
                    self.sudo().create(dict(
                        values, employee_id=employee.id, absence_date=day))
                    created += 1

        _logger.info(
            "Barrido de ausencias %s a %s: %s evaluados, %s propuestas, %s retiradas",
            date_from, date_to, scanned, created, removed,
        )
        return {"scanned": scanned, "created": created, "removed": removed}

    def _evaluate(self, employee, day, presence, peers):
        """Puntua que tan probable es que ese dia sea una ausencia real.

        Cada ajuste del puntaje se acompana de la frase que lo explica: si el
        sistema no puede decir por que sospecha, no tiene por que acusar.
        """
        present, total = peers
        ratio = (present / total) if total else 0.0
        reasons = []

        # Senal 1 — el grupo delata el dia. Es la base del puntaje: si ese dia
        # no vino casi nadie, no era dia de trabajo y nadie falto.
        confidence = ratio
        if total:
            reasons.append(_(
                "Ese dia vinieron %(present)s de %(total)s companeros (%(pct)s%%).",
                present=present, total=total, pct=round(ratio * 100),
            ))
        else:
            reasons.append(_("No hay companeros con quien comparar ese dia."))

        # Senal 2 — el ritmo personal. Un hueco de un dia entre dos trabajados
        # es lo mas parecido a una falta que existe sin saber el horario.
        before = (employee.id, day - timedelta(days=1)) in presence
        after = (employee.id, day + timedelta(days=1)) in presence
        gap = self._gap_length(employee, day, presence)

        if gap == 1 and before and after:
            confidence = min(1.0, confidence + 0.25)
            reasons.append(_(
                "Trabajo el dia anterior y el siguiente: es un hueco de un solo dia."))
        elif gap >= 3:
            # Tres dias seguidos sin venir se parece mucho mas a una rotacion o
            # a vacaciones que a faltar tres veces al trabajo.
            confidence *= 0.4
            reasons.append(_(
                "Lleva %s dias seguidos sin venir, lo que se parece mas a un "
                "descanso o una rotacion que a una falta.", gap))
        elif not before and not after:
            confidence *= 0.7
            reasons.append(_(
                "Tampoco vino el dia anterior ni el siguiente."))

        return {
            "confidence": round(min(max(confidence, 0.0), 1.0), 2),
            "signals": "\n".join(reasons),
            "peers_present": present, "peers_total": total,
            "gap_length": gap, "worked_before": before, "worked_after": after,
        }

    # -- datos de apoyo ---------------------------------------------------

    def _presence_map(self, employees, date_from, date_to):
        """Conjunto de (empleado, dia) con constancia de que vino.

        Basta UNA asistencia para que el dia cuente como trabajado. No importan
        las horas: la pregunta es binaria.
        """
        margin = timedelta(days=CONTEXT_DAYS)
        attendances = self.env["hr.attendance"].sudo().search_read(
            [
                ("employee_id", "in", employees.ids),
                ("check_in", ">=", self._start_of(date_from - margin)),
                ("check_in", "<=", self._end_of(date_to + margin)),
            ],
            ["employee_id", "check_in"],
        )
        tz_by_employee = {e.id: e.tz or self.env.company.partner_id.tz or "UTC"
                          for e in employees}
        presence = set()
        for row in attendances:
            employee_id = row["employee_id"][0]
            local = fields.Datetime.context_timestamp(
                self.with_context(tz=tz_by_employee.get(employee_id, "UTC")),
                row["check_in"],
            )
            presence.add((employee_id, local.date()))
        return presence

    def _excused_map(self, employees, date_from, date_to):
        """Dias con permiso o vacaciones aprobadas: no son ausencias.

        No es informacion de horario sino un hecho explicito que alguien ya
        aprobo, asi que aqui si corresponde usarlo.
        """
        excused = set()
        if "hr.leave" not in self.env:
            return excused
        try:
            leaves = self.env["hr.leave"].sudo().search([
                ("employee_id", "in", employees.ids),
                ("state", "=", "validate"),
                ("date_from", "<=", self._end_of(date_to)),
                ("date_to", ">=", self._start_of(date_from)),
            ])
        except Exception:  # noqa: BLE001 - la ausencia de permisos no es fatal
            _logger.warning("No se pudieron leer los permisos aprobados.")
            return excused
        for leave in leaves:
            start = max(leave.date_from.date(), date_from)
            end = min(leave.date_to.date(), date_to)
            for day in self._days_between(start, end):
                excused.add((leave.employee_id.id, day))
        return excused

    def _peer_counts(self, employees, day, presence):
        """Cuantos companeros vinieron ese dia, y de cuantos.

        El grupo de comparacion es el departamento si lo hay y tiene gente
        suficiente; si no, toda la compania. Un grupo de dos personas no dice
        nada estadisticamente.
        """
        counts = {}
        for employee in employees:
            if not self._employed_on(employee, day):
                continue
            key = employee.department_id.id or 0
            present, total = counts.get(key, (0, 0))
            counts[key] = (
                present + (1 if (employee.id, day) in presence else 0), total + 1)
        company_present = sum(c[0] for c in counts.values())
        company_total = sum(c[1] for c in counts.values())

        def resolve(employee):
            key = employee.department_id.id or 0
            present, total = counts.get(key, (0, 0))
            if total >= 3:
                return present, total
            return company_present, company_total

        return resolve

    def _gap_length(self, employee, day, presence):
        """Cuantos dias seguidos sin venir incluye este dia."""
        length = 1
        cursor = day - timedelta(days=1)
        for _step in range(CONTEXT_DAYS):
            if (employee.id, cursor) in presence:
                break
            length += 1
            cursor -= timedelta(days=1)
        cursor = day + timedelta(days=1)
        for _step in range(CONTEXT_DAYS):
            if (employee.id, cursor) in presence:
                break
            length += 1
            cursor += timedelta(days=1)
        return length

    def _employed_on(self, employee, day):
        """Si la persona ya estaba contratada ese dia y todavia lo estaba."""
        if employee.create_date and employee.create_date.date() > day:
            return False
        return True

    @staticmethod
    def _days_between(date_from, date_to):
        day = date_from
        while day <= date_to:
            yield day
            day += timedelta(days=1)

    @staticmethod
    def _start_of(day):
        return datetime.combine(day, time.min)

    @staticmethod
    def _end_of(day):
        return datetime.combine(day, time.max)

    # ==================================================================
    # Revision
    # ==================================================================

    def _mark(self, state, reason=None):
        values = {
            "state": state,
            "reviewed_by_uid": self.env.user.id,
            "reviewed_date": fields.Datetime.now(),
        }
        if reason:
            values["reason"] = reason
        return self.write(values)

    def action_confirm(self):
        """Confirmada = se descuenta. Es la unica accion que cuesta dinero."""
        return self._mark("confirmed", "unjustified")

    def action_reject(self):
        """No era ausencia. El dia vuelve a ser un dia cualquiera."""
        return self._mark("rejected")

    def action_reopen(self):
        return self.write({
            "state": "proposed", "reviewed_by_uid": False, "reviewed_date": False,
        })

    def action_open_leave(self):
        """Abre el registro de ausencias de Odoo para vacaciones o permisos.

        No se duplica: las vacaciones y los permisos ya son hr.leave y ahi se
        quedan. Desde aqui solo se salta a la pantalla que corresponde.
        """
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "res_model": "hr.leave",
            "view_mode": "form",
            "target": "new",
            "name": _("Permiso o vacaciones"),
            "context": {
                "default_employee_id": self.employee_id.id,
                "default_request_date_from": self.absence_date,
                "default_request_date_to": self.absence_date,
            },
        }

    # ==================================================================
    # El unico punto de contacto con la nomina
    # ==================================================================

    @api.model
    def olive_confirmed_absences(self, employee_id, date_from, date_to):
        """Dias de ausencia CONFIRMADOS de un empleado en un periodo.

        Esta es toda la interfaz con la nomina, a proposito. Los dos modulos son
        independientes: este detecta y registra ausencias, aquel calcula el
        pago. Ninguno importa al otro ni escribe en sus tablas.

        La nomina llama a esto cuando genera la quincena. **Solo devuelve lo
        confirmado por una persona**: lo detectado y no revisado nunca sale de
        aqui, porque descontar sin que nadie lo haya mirado es justo lo que no
        se quiere.

        Uso desde el modulo de nomina::

            datos = env["olive.attendance.absence"].olive_confirmed_absences(
                empleado.id, quincena_desde, quincena_hasta)
            dias_no_pagados = datos["days"]
        """
        records = self.sudo().search([
            ("employee_id", "=", employee_id),
            ("absence_date", ">=", date_from),
            ("absence_date", "<=", date_to),
            ("state", "=", "confirmed"),
        ])
        return {
            "days": len(records),
            "dates": [fields.Date.to_string(r.absence_date) for r in records],
            "employee_id": employee_id,
        }

    @api.model
    def olive_pending_review_count(self, date_from, date_to, company_id=None):
        """Cuantas ausencias detectadas siguen sin revisar en el periodo.

        La nomina deberia consultarlo ANTES de cerrar la quincena: cerrar con
        ausencias sin revisar significa pagar dias que quiza no se trabajaron, o
        peor, que el supervisor todavia no vio.
        """
        return self.sudo().search_count([
            ("company_id", "=", company_id or self.env.company.id),
            ("absence_date", ">=", date_from),
            ("absence_date", "<=", date_to),
            ("state", "=", "proposed"),
        ])

    # ==================================================================
    # Quincena y cron
    # ==================================================================

    @api.model
    def _quincena_bounds(self, reference=None):
        """Limites de la quincena que contiene la fecha dada."""
        reference = reference or fields.Date.context_today(self)
        if isinstance(reference, str):
            reference = fields.Date.to_date(reference)
        if reference.day <= 15:
            return reference.replace(day=1), reference.replace(day=15)
        if reference.month == 12:
            last = date_type(reference.year, 12, 31)
        else:
            last = date_type(reference.year, reference.month + 1, 1) - timedelta(days=1)
        return reference.replace(day=16), last

    @api.model
    def _cron_scan(self):
        """Barrido diario de la quincena en curso y de la anterior.

        Se repasa tambien la anterior porque un marcaje puede llegar tarde: el
        kiosco estuvo sin red, se sincroniza al tercer dia, y esa ausencia
        propuesta deja de serlo. Volver a barrer la retira sola.
        """
        today = fields.Date.context_today(self)
        start, end = self._quincena_bounds(today)
        previous_end = start - timedelta(days=1)
        previous_start, _prev = self._quincena_bounds(previous_end)
        for company in self.env["res.company"].sudo().search([]):
            self.with_company(company)._scan_period(
                previous_start, end, company=company)

    def action_scan_now(self):
        """Barrido manual de la quincena en curso."""
        start, end = self._quincena_bounds()
        result = self._scan_period(start, end)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "success",
                "title": _("Barrido terminado"),
                "message": _(
                    "%(scanned)s dias evaluados, %(created)s ausencias propuestas, "
                    "%(removed)s retiradas.", **result),
                "next": {"type": "ir.actions.act_window_close"},
            },
        }

    # ==================================================================
    # La cuadricula de la quincena
    # ==================================================================

    @api.model
    def olive_grid_payload(self, date_from=None, date_to=None, department_id=None):
        """Estado de cada empleado cada dia del periodo.

        Es la pantalla que resuelve el problema de los horarios rotativos sin
        modelarlos: el patron 3x3 se ve a simple vista en la fila de la persona,
        y el supervisor decide. Ninguna configuracion puede competir con eso.
        """
        company = self.env.company
        if not date_from or not date_to:
            date_from, date_to = self._quincena_bounds()
        date_from = fields.Date.to_date(date_from)
        date_to = fields.Date.to_date(date_to)

        domain = [("company_id", "=", company.id)]
        if department_id:
            domain.append(("department_id", "=", department_id))
        employees = self.env["hr.employee"].sudo().search(domain, order="name")
        if not employees:
            return {"dates": [], "rows": [], "date_from": str(date_from),
                    "date_to": str(date_to)}

        presence = self._presence_map(employees, date_from, date_to)
        excused = self._excused_map(employees, date_from, date_to)
        records = {
            (a.employee_id.id, a.absence_date): a
            for a in self.sudo().search([
                ("employee_id", "in", employees.ids),
                ("absence_date", ">=", date_from),
                ("absence_date", "<=", date_to),
            ])
        }
        days = list(self._days_between(date_from, date_to))
        today = fields.Date.context_today(self)

        # Cuanta gente vino cada dia. Es lo que permite ver de un vistazo que un
        # dia sin nadie fue feriado y no una ausencia colectiva.
        per_day_present = {
            day: sum(1 for e in employees if (e.id, day) in presence) for day in days
        }

        rows = []
        for employee in employees:
            cells = []
            worked = confirmed = pending = 0
            for day in days:
                record = records.get((employee.id, day))
                if (employee.id, day) in presence:
                    status = "worked"
                    worked += 1
                elif day >= today:
                    status = "future"
                elif (employee.id, day) in excused:
                    status = "excused"
                elif record and record.state != "rejected":
                    status = record.state
                    if record.state == "confirmed":
                        confirmed += 1
                    elif record.state == "proposed":
                        pending += 1
                else:
                    # No vino, pero el sistema no tiene motivos para acusarlo.
                    status = "quiet"
                cells.append({
                    "date": str(day),
                    "status": status,
                    "absence_id": record.id if record else None,
                    "confidence": record.confidence if record else 0.0,
                    "signals": record.signals if record else "",
                })
            rows.append({
                "employee_id": employee.id,
                "name": employee.display_name,
                "department": employee.department_id.display_name or "",
                "cells": cells,
                "worked": worked,
                "confirmed": confirmed,
                "pending": pending,
            })

        return {
            "date_from": str(date_from), "date_to": str(date_to),
            "dates": [{
                "date": str(day),
                "day": day.day,
                "weekday": day.weekday(),
                "present": per_day_present[day],
                "total": len(employees),
            } for day in days],
            "rows": rows,
            "employee_count": len(employees),
        }

    @api.model
    def olive_grid_set_state(self, employee_id, day, state, reason=None):
        """Marca el estado de un dia desde la cuadricula.

        Permite crear la ausencia aunque el sistema no la hubiera propuesto: el
        supervisor sabe cosas que el sistema no.
        """
        day = fields.Date.to_date(day)
        record = self.sudo().search([
            ("employee_id", "=", employee_id), ("absence_date", "=", day),
        ], limit=1)
        if not record:
            record = self.sudo().create({
                "employee_id": employee_id, "absence_date": day,
                "signals": _("Registrada a mano por un supervisor."),
            })
        if state == "proposed":
            record.action_reopen()
        else:
            record._mark(state, reason)
        return {"absence_id": record.id, "state": record.state}
