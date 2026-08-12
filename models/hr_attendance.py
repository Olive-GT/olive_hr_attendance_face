# -*- coding: utf-8 -*-
"""Trazabilidad de las asistencias que produjo el kiosco facial.

`olive_punch_ids` es lo que distingue una asistencia **reconstruible** (nacida
de marcajes del kiosco) de un **bloque inmutable** (creada a mano por RRHH,
importada, o hecha con el kiosco nativo de Odoo). El doblado de F2 solo puede
tocar las primeras: el trabajo manual de una persona nunca se pisa.
"""

from odoo import api, fields, models


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
