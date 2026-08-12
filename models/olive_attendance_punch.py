# -*- coding: utf-8 -*-
"""Marcaje crudo tal como lo produjo el kiosco. Almacen append-only.

Este modelo es **la fuente de verdad** de todo lo que vino del kiosco.
`hr.attendance` es una proyeccion derivada y reconstruible a partir de aqui
(el doblado vive en olive_attendance_fold.py, fase F2).

Por que existe esta capa intermedia y no se escribe directo a hr.attendance:
`hr.attendance._check_validity` del core rechaza con ValidationError las
asistencias solapadas, una segunda asistencia abierta del mismo empleado, y las
asistencias intermedias. Una cola offline que llega tarde y desordenada choca
contra esas restricciones y se atora reintentando para siempre. Guardando
primero el hecho crudo, la insercion nunca falla y la reconciliacion se resuelve
despues, con toda la informacion sobre la mesa.
"""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

# Lo unico que puede cambiar despues de creado. Todo lo demas son *hechos*:
# quien marco, cuando, y con que confianza. Reescribir un hecho es falsificar
# evidencia, y estos registros alimentan la nomina.
MUTABLE_FIELDS = {
    "state", "attendance_id", "attendance_field", "error_message", "fold_attempts",
    "review_state", "reviewed_by_uid", "reviewed_date", "review_note",
    "snapshot", "batch_id",
    # campos tecnicos de mail.thread y del ORM
    "message_ids", "message_follower_ids", "activity_ids", "write_date", "write_uid",
}


class OliveAttendancePunch(models.Model):
    _name = "olive.attendance.punch"
    _description = "Marcaje crudo del kiosco facial"
    _inherit = ["mail.thread"]
    _order = "punch_time asc, id asc"

    # -- identidad e idempotencia ----------------------------------------
    # Esta restriccion es TODA la idempotencia del sistema: el kiosco puede
    # reenviar el mismo lote las veces que haga falta sin duplicar nada.
    uuid = fields.Char(required=True, index=True, copy=False, readonly=True)
    device_id = fields.Many2one(
        "olive.attendance.device", required=True, index=True,
        ondelete="restrict", readonly=True,
    )
    batch_id = fields.Many2one(
        "olive.attendance.sync.batch", ondelete="set null", index=True, readonly=True,
    )
    company_id = fields.Many2one(
        related="device_id.company_id", store=True, index=True,
    )

    # -- tiempo -----------------------------------------------------------
    device_time = fields.Datetime(
        required=True, readonly=True,
        help="Hora cruda del reloj del equipo. No se modifica NUNCA: es la "
             "evidencia de lo que el dispositivo creia que era la hora.",
    )
    punch_time = fields.Datetime(
        required=True, index=True, readonly=True,
        help="Hora corregida por el desfase de reloj. Es la que se dobla.",
    )
    monotonic_ms = fields.Float(
        readonly=True, digits=(16, 1),
        help="performance.now() en el instante del marcaje. Preserva el orden "
             "relativo aunque la hora absoluta sea dudosa.",
    )
    boot_id = fields.Char(
        index=True, readonly=True,
        help="Identificador del arranque de la aplicacion. monotonic_ms solo "
             "es comparable dentro del mismo boot_id.",
    )
    clock_offset_seconds = fields.Float(readonly=True, digits=(16, 3))
    clock_confidence = fields.Selection(
        [("good", "Confiable"), ("drift", "Con desvio"), ("unreliable", "No confiable")],
        default="good", required=True, index=True, readonly=True,
    )

    # -- identificacion ---------------------------------------------------
    employee_id = fields.Many2one(
        "hr.employee", index=True, ondelete="restrict",
        help="Puede quedar vacio a proposito: un marcaje sin empleado es la "
             "evidencia de una identificacion fallida, y se conserva.",
    )
    method = fields.Selection(
        [("face", "Rostro"), ("pin", "PIN (diagnostico)"),
         ("badge", "Credencial"), ("manual", "Manual")],
        default="face", required=True, readonly=True,
    )
    direction = fields.Selection(
        [("auto", "Automatica"), ("in", "Entrada"), ("out", "Salida")],
        default="auto", required=True, readonly=True,
    )

    match_score = fields.Float(digits=(4, 4), readonly=True)
    margin_score = fields.Float(
        digits=(4, 4), readonly=True,
        help="Diferencia entre el primer y el segundo candidato. Un margen "
             "pequeno significa que habia otra persona parecida.",
    )
    frames_agreed = fields.Integer(readonly=True)
    liveness_score = fields.Float(digits=(4, 4), readonly=True)
    template_id = fields.Many2one(
        "olive.attendance.face.template", ondelete="set null", readonly=True,
    )
    runner_up_employee_id = fields.Many2one(
        "hr.employee", ondelete="set null", readonly=True,
        string="Segundo candidato",
    )
    embedding_version = fields.Char(readonly=True)
    app_version = fields.Char(readonly=True)
    snapshot = fields.Binary(attachment=True, string="Foto de auditoria")
    latitude = fields.Float(digits=(10, 7), readonly=True)
    longitude = fields.Float(digits=(10, 7), readonly=True)

    # -- ciclo de vida ----------------------------------------------------
    state = fields.Selection(
        [("queued", "En cola"), ("applied", "Aplicado"), ("duplicate", "Duplicado"),
         ("rejected", "Rechazado"), ("error", "Error")],
        default="queued", required=True, index=True,
    )
    attendance_id = fields.Many2one(
        "hr.attendance", ondelete="set null", index=True, readonly=True,
    )
    attendance_field = fields.Selection(
        [("check_in", "Entrada"), ("check_out", "Salida")], readonly=True,
    )
    fold_attempts = fields.Integer(default=0, readonly=True)
    error_message = fields.Text(readonly=True)

    review_state = fields.Selection(
        [("none", "Sin revision"), ("pending", "Pendiente"),
         ("confirmed", "Confirmado"), ("rejected", "Rechazado")],
        default="none", required=True, index=True,
    )
    reviewed_by_uid = fields.Many2one("res.users", readonly=True)
    reviewed_date = fields.Datetime(readonly=True)
    review_note = fields.Text()

    _sql_constraints = [
        ("uuid_uniq", "unique(uuid)",
         "Ese marcaje ya fue recibido. La restriccion es intencional: es lo que "
         "hace idempotente el reenvio de la cola del kiosco."),
    ]

    @api.depends("employee_id", "punch_time")
    def _compute_display_name(self):
        # Odoo 18 removio name_get; el nombre se calcula asi.
        for punch in self:
            punch.display_name = "%s · %s" % (
                punch.employee_id.display_name or _("Sin identificar"),
                fields.Datetime.to_string(punch.punch_time) or "",
            )

    # -- append-only ------------------------------------------------------

    def write(self, vals):
        """Bloquea la reescritura de los hechos.

        Solo el gestor puede corregir `employee_id` (para asignar un marcaje que
        quedo sin identificar), y queda registrado en el chatter.
        """
        touched = set(vals) - MUTABLE_FIELDS
        if touched == {"employee_id"}:
            if not self.env.user.has_group("olive_hr_attendance_face.group_face_manager"):
                raise AccessError(_(
                    "Solo un gestor de asistencia facial puede reasignar el "
                    "empleado de un marcaje."
                ))
            for punch in self:
                before = punch.employee_id.display_name or _("sin identificar")
                punch.message_post(body=_(
                    "Empleado reasignado: %(before)s → %(after)s",
                    before=before,
                    after=self.env["hr.employee"].browse(vals["employee_id"]).display_name,
                ))
        elif touched:
            raise AccessError(_(
                "Los marcajes son evidencia y no se editan. Campos bloqueados: %s. "
                "Para corregir un error, rechaza el marcaje y registra la "
                "asistencia a mano.",
                ", ".join(sorted(touched)),
            ))
        return super().write(vals)

    def unlink(self):
        if not self.env.user.has_group("olive_hr_attendance_face.group_face_manager"):
            raise AccessError(_("Solo un gestor puede borrar marcajes."))
        return super().unlink()

    # -- acciones ---------------------------------------------------------

    def action_confirm_review(self):
        self.write({
            "review_state": "confirmed",
            "reviewed_by_uid": self.env.user.id,
            "reviewed_date": fields.Datetime.now(),
        })

    def action_reject(self):
        self.write({
            "state": "rejected",
            "review_state": "rejected",
            "reviewed_by_uid": self.env.user.id,
            "reviewed_date": fields.Datetime.now(),
        })

    def action_force_fold(self):
        """Reintenta el doblado de estos marcajes (implementado en F2)."""
        if not hasattr(self, "_fold_pending"):
            raise UserError(_("El doblado todavia no esta implementado (fase F2)."))
        return self._fold_pending(punch_ids=self.ids)
