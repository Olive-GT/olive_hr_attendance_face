# -*- coding: utf-8 -*-
"""Incidencias del marcaje: todo lo que no salio normal, guardado y revisable.

**Por que existe este modelo.** La nomina de esta empresa asume que el empleado
asistio el 100% de las veces y se guia por los dias marcados como ausencia o
vacaciones. Las horas de `hr.attendance` no alimentan el pago. La consecuencia
es importante y va contra la intuicion:

    Una asistencia con la hora imprecisa NO es un error de nomina.
    Un dia de presencia perdido SI lo es.

Eso invierte el criterio con el que se diseno el doblado. La cautela original
—rechazar antes que adivinar— destruia la evidencia de que la persona estuvo
ahi, que es justamente el dato que importa. La politica correcta es la contraria:
**registrar siempre la asistencia, y guardar aparte todo lo que estuvo raro.**

Aqui se guarda ese "raro". Cada incidencia dice que se detecto, con que
marcajes, y que hizo el sistema al respecto. Nada se descarta en silencio.

Las incidencias abiertas son **derivadas**: si la jornada se vuelve a
reconstruir porque llego un marcaje tardio, se recalculan. Las que alguien ya
reviso se conservan como historial y no se vuelven a levantar — la decision de
una persona no se borra sola.
"""

from odoo import _, api, fields, models

# Orden de gravedad, de menor a mayor. Sirve para ordenar y para decidir el
# color en las vistas.
SEVERITY_BY_KIND = {
    "burst": "info",
    "repeated_punch": "info",
    "gray_band": "warning",
    "short_session": "warning",
    "long_session": "warning",
    "odd_count": "warning",
    "missing_out": "warning",
    "forced_close": "warning",
    "orphan_out": "warning",
    "immutable_conflict": "critical",
    "clock_unreliable": "critical",
    "unidentified": "critical",
}


class OliveAttendanceAnomaly(models.Model):
    _name = "olive.attendance.anomaly"
    _description = "Incidencia de marcaje"
    _inherit = ["mail.thread"]
    _order = "anomaly_date desc, severity_rank desc, id desc"

    employee_id = fields.Many2one(
        "hr.employee", index=True, ondelete="cascade",
        help="Vacio cuando el marcaje no se pudo identificar. Es justamente el "
             "caso mas grave: no se sabe de quien es esa presencia.",
    )
    company_id = fields.Many2one(
        "res.company", required=True, index=True,
        default=lambda self: self.env.company,
    )
    anomaly_date = fields.Date(
        required=True, index=True, string="Jornada",
        help="Jornada local a la que corresponde la incidencia.",
    )
    kind = fields.Selection(
        [
            ("repeated_punch", "Marcaje repetido"),
            ("burst", "Rafaga de marcajes"),
            ("missing_out", "Sin marcaje de salida"),
            ("forced_close", "Cerrada a la fuerza"),
            ("odd_count", "Numero impar de marcajes"),
            ("orphan_out", "Salida sin entrada"),
            ("short_session", "Jornada anormalmente corta"),
            ("long_session", "Jornada anormalmente larga"),
            ("immutable_conflict", "Choca con un registro manual"),
            ("clock_unreliable", "Reloj del equipo no confiable"),
            ("unidentified", "Marcaje sin identificar"),
            ("gray_band", "Identificacion en banda gris"),
        ],
        required=True, index=True, string="Tipo",
    )
    severity = fields.Selection(
        [("info", "Informativa"), ("warning", "Revisar"), ("critical", "Grave")],
        required=True, default="warning", index=True, string="Gravedad",
    )
    severity_rank = fields.Integer(
        compute="_compute_severity_rank", store=True,
        help="Solo para ordenar: 'critical' es alfabeticamente menor que "
             "'info', asi que ordenar por el campo de seleccion mentiria.",
    )
    detail = fields.Text(string="Que paso")
    resolution = fields.Text(
        string="Que hizo el sistema",
        help="Lo que el doblado decidio hacer. Importa tanto como la deteccion: "
             "sin esto no se sabe si el dato de asistencia es de fiar.",
    )

    punch_ids = fields.Many2many("olive.attendance.punch", string="Marcajes")
    attendance_id = fields.Many2one("hr.attendance", ondelete="set null")
    attendance_recorded = fields.Boolean(
        string="Asistencia registrada", default=True,
        help="Falso significa que ese dia NO quedo constancia de presencia. Es "
             "lo unico realmente grave: la nomina asume asistencia y descuenta "
             "por ausencias, asi que una presencia perdida si cuesta dinero.",
    )

    state = fields.Selection(
        [("open", "Abierta"), ("reviewed", "Revisada"), ("dismissed", "Descartada")],
        default="open", required=True, index=True, tracking=True,
    )
    reviewed_by_uid = fields.Many2one("res.users", readonly=True)
    reviewed_date = fields.Datetime(readonly=True)
    resolution_note = fields.Text(string="Nota de revision")

    # La huella hace idempotente el registro: reconstruir una jornada diez veces
    # no genera diez incidencias iguales.
    fingerprint = fields.Char(required=True, index=True, copy=False)

    _sql_constraints = [
        ("fingerprint_uniq", "unique(fingerprint)",
         "Esa incidencia ya estaba registrada."),
    ]

    @api.depends("severity")
    def _compute_severity_rank(self):
        ranks = {"info": 1, "warning": 2, "critical": 3}
        for record in self:
            record.severity_rank = ranks.get(record.severity, 0)

    @api.depends("employee_id", "kind", "anomaly_date")
    def _compute_display_name(self):
        labels = dict(self._fields["kind"].selection)
        for record in self:
            record.display_name = "%s · %s · %s" % (
                record.employee_id.display_name or "?",
                labels.get(record.kind, record.kind),
                record.anomaly_date or "",
            )

    # -- registro ---------------------------------------------------------

    @api.model
    def _record(self, employee, anomaly_date, kind, detail, resolution,
                punches=None, attendance=None, attendance_recorded=True, key=""):
        """Registra una incidencia si no estaba ya.

        `key` distingue dos incidencias del mismo tipo el mismo dia (por
        ejemplo dos jornadas cortas). Sin ella, la segunda se perderia.
        """
        fingerprint = "%s/%s/%s/%s" % (
            employee.id if employee else 0, anomaly_date, kind, key)
        existing = self.sudo().search([("fingerprint", "=", fingerprint)], limit=1)
        if existing:
            # Si ya la reviso una persona no se toca. Si sigue abierta se
            # refresca, porque la reconstruccion pudo cambiar los detalles.
            if existing.state == "open":
                existing.write({
                    "detail": detail, "resolution": resolution,
                    "attendance_id": attendance.id if attendance else False,
                    "attendance_recorded": attendance_recorded,
                    "punch_ids": [(6, 0, punches.ids if punches else [])],
                })
            return existing
        return self.sudo().create({
            "employee_id": employee.id if employee else False,
            "company_id": (employee.company_id.id if employee
                           else self.env.company.id),
            "anomaly_date": anomaly_date,
            "kind": kind,
            "severity": SEVERITY_BY_KIND.get(kind, "warning"),
            "detail": detail,
            "resolution": resolution,
            "attendance_id": attendance.id if attendance else False,
            "attendance_recorded": attendance_recorded,
            "punch_ids": [(6, 0, punches.ids if punches else [])],
            "fingerprint": fingerprint,
        })

    @api.model
    def _clear_open(self, employee, date_from, date_to):
        """Borra las incidencias abiertas de una jornada que se va a rehacer.

        Solo las abiertas: una incidencia ya revisada es una decision humana y
        se conserva aunque la jornada se reconstruya.
        """
        self.sudo().search([
            ("employee_id", "=", employee.id),
            ("anomaly_date", ">=", date_from),
            ("anomaly_date", "<=", date_to),
            ("state", "=", "open"),
        ]).unlink()

    # -- acciones ---------------------------------------------------------

    def action_review(self):
        self.write({
            "state": "reviewed",
            "reviewed_by_uid": self.env.user.id,
            "reviewed_date": fields.Datetime.now(),
        })

    def action_dismiss(self):
        self.write({
            "state": "dismissed",
            "reviewed_by_uid": self.env.user.id,
            "reviewed_date": fields.Datetime.now(),
        })

    def action_reopen(self):
        self.write({"state": "open", "reviewed_by_uid": False, "reviewed_date": False})

    def action_open_attendance(self):
        self.ensure_one()
        if not self.attendance_id:
            return False
        return {
            "type": "ir.actions.act_window",
            "res_model": "hr.attendance",
            "res_id": self.attendance_id.id,
            "view_mode": "form",
            "name": _("Asistencia"),
        }
