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

import logging
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

_logger = logging.getLogger(__name__)

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

    # ==================================================================
    # Recepcion desde el acompanante
    # ==================================================================

    @api.model
    def olive_kiosk_context(self):
        """Todo lo que el acompanante necesita para reconocer sin red.

        Baja el indice completo de la compania de una sola vez. Con 100
        empleados y 3 fotos son ~205 KB: cabe en memoria y evita cualquier
        consulta al servidor durante el reconocimiento, que es la razon de que
        siga funcionando cuando se cae el internet.
        """
        company = self.env.company
        profile = company._olive_face_profile()
        if not profile:
            raise UserError(_(
                "No hay un perfil de modelos configurado. Se define en "
                "Ajustes -> Asistencias -> Reconocimiento Facial."
            ))
        device = self.env["olive.attendance.device"].sudo().search(
            [("company_id", "=", company.id), ("active", "=", True)], limit=1)
        if not device:
            device = self.env["olive.attendance.device"].sudo().create({
                "name": _("Acompanante"), "company_id": company.id, "state": "active",
            })
        templates = self.env["olive.attendance.face.template"].sudo().search([
            ("employee_id.company_id", "=", company.id),
            ("active", "=", True), ("state", "=", "active"),
            ("embedding", "!=", False),
            ("embedding_version", "=", profile.embedding_version),
        ])
        return {
            "pipeline_version": self.env["hr.employee"]._olive_pipeline_version(),
            "profile": profile._bootstrap_payload(),
            "settings": company._olive_face_client_settings(),
            "device_id": device.id,
            "server_time": fields.Datetime.to_string(fields.Datetime.now()),
            "embedding_version": profile.embedding_version,
            "people": [{
                "employee_id": t.employee_id.id,
                "name": t.employee_id.display_name,
                "template_id": t.id,
                "embedding": t.embedding,
            } for t in templates],
        }

    @api.model
    def olive_receive_punches(self, punches):
        """Recibe un lote de marcajes del acompanante.

        **Idempotente por UUID**, que es lo que permite al cliente reenviar su
        cola cuantas veces haga falta sin miedo: si la respuesta se pierde por
        un corte de red, el reenvio no duplica nada.

        Devuelve la lista de uuid aceptados o ya conocidos, que es la senal para
        que el cliente los borre de su cola local. Lo que no aparezca ahi se
        reintenta.
        """
        if not punches:
            return {"accepted": [], "duplicate": [], "rejected": []}

        incoming = {p["uuid"]: p for p in punches if p.get("uuid")}
        known = set(self.sudo().search([
            ("uuid", "in", list(incoming))]).mapped("uuid"))

        accepted, rejected = [], []
        values_list = []
        for uuid_key, payload in incoming.items():
            if uuid_key in known:
                continue
            try:
                values_list.append(self._kiosk_values(payload))
                accepted.append(uuid_key)
            except Exception as err:  # noqa: BLE001 - un marcaje malo no tumba el lote
                _logger.warning("Marcaje rechazado (%s): %s", uuid_key, err)
                rejected.append(uuid_key)

        created = self.sudo().create(values_list) if values_list else self.browse()

        batch = self.env["olive.attendance.sync.batch"].sudo().create({
            "device_id": created[:1].device_id.id or self.env[
                "olive.attendance.device"].sudo().search([], limit=1).id,
            "punch_count": len(incoming),
            "accepted_count": len(accepted),
            "duplicate_count": len(known),
            "rejected_count": len(rejected),
            "punch_ids": [(6, 0, created.ids)],
        })
        created.write({"batch_id": batch.id})

        # Doblado inmediato: con red, el marcaje aparece en las asistencias en
        # el momento en vez de esperar al cron.
        if created and self.env.company.olive_face_fold_inline:
            try:
                self._fold_pending(punch_ids=created.ids)
            except Exception:  # noqa: BLE001 - el marcaje ya esta guardado
                _logger.exception("El doblado en linea fallo; queda para el cron.")

        return {
            "accepted": accepted,
            "duplicate": sorted(known),
            "rejected": rejected,
        }

    @api.model
    def _kiosk_values(self, payload):
        """Traduce un marcaje del cliente a valores del modelo.

        La hora del dispositivo se conserva cruda y ademas se guarda corregida:
        si el reloj de la laptop esta mal, `device_time` es la evidencia y
        `punch_time` lo usable.
        """
        device_time = fields.Datetime.to_datetime(payload["device_time"])
        offset = float(payload.get("clock_offset_seconds") or 0.0)
        punch_time = device_time + timedelta(seconds=offset)

        company = self.env.company
        drift = abs(offset)
        if drift > (company.olive_face_reject_clock_drift_seconds or 3600):
            confidence = "unreliable"
        elif drift > (company.olive_face_max_clock_drift_seconds or 120):
            confidence = "drift"
        else:
            confidence = "good"

        return {
            "uuid": payload["uuid"],
            "device_id": payload["device_id"],
            "device_time": device_time,
            "punch_time": punch_time,
            "clock_offset_seconds": offset,
            "clock_confidence": confidence,
            "monotonic_ms": payload.get("monotonic_ms") or 0.0,
            "boot_id": payload.get("boot_id") or False,
            "employee_id": payload.get("employee_id") or False,
            "method": "face",
            "direction": "auto",
            "match_score": payload.get("match_score") or 0.0,
            "margin_score": payload.get("margin_score") or 0.0,
            "frames_agreed": payload.get("frames_agreed") or 0,
            "liveness_score": payload.get("liveness_score") or 0.0,
            "template_id": payload.get("template_id") or False,
            "runner_up_employee_id": payload.get("runner_up_employee_id") or False,
            "embedding_version": payload.get("embedding_version") or False,
            "review_state": "pending" if payload.get("needs_review") else "none",
        }
