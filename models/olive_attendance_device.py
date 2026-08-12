# -*- coding: utf-8 -*-
"""El kiosco fisico: un dispositivo enlazado por token."""

import uuid

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from werkzeug.urls import url_join


class OliveAttendanceDevice(models.Model):
    _name = "olive.attendance.device"
    _description = "Kiosco de asistencia facial"
    _inherit = ["mail.thread"]
    _order = "name"

    name = fields.Char(required=True, tracking=True)
    company_id = fields.Many2one(
        "res.company", required=True, index=True,
        default=lambda self: self.env.company,
    )
    active = fields.Boolean(default=True)
    state = fields.Selection(
        [("draft", "Sin enlazar"), ("linked", "Enlazado"), ("blocked", "Bloqueado")],
        default="draft", required=True, tracking=True,
    )

    # -- secretos ---------------------------------------------------------
    # El token es el unico secreto de la URL del kiosco y el secret_key firma
    # los envios. Ambos restringidos al grupo de gestion: quien pueda leerlos
    # puede suplantar al kiosco entero.
    token = fields.Char(
        required=True, copy=False, index=True,
        default=lambda self: uuid.uuid4().hex,
        groups="olive_hr_attendance_face.group_face_manager",
    )
    secret_key = fields.Char(
        required=True, copy=False,
        default=lambda self: uuid.uuid4().hex,
        groups="olive_hr_attendance_face.group_face_manager",
        help="Clave con la que el kiosco firma sus envios (HMAC).",
    )
    kiosk_url = fields.Char(compute="_compute_kiosk_url")

    # -- configuracion ----------------------------------------------------
    model_profile_id = fields.Many2one(
        "olive.attendance.model.profile", string="Perfil de modelos",
        default=lambda self: self.env.company.olive_face_model_profile_id,
    )
    sync_epoch = fields.Integer(
        default=1, required=True,
        help="Incrementarlo obliga al kiosco a descartar su copia local y "
             "recargar todo en la siguiente sincronizacion.",
    )

    # -- telemetria -------------------------------------------------------
    last_seen = fields.Datetime(readonly=True)
    last_sync_down = fields.Datetime(readonly=True)
    last_sync_up = fields.Datetime(readonly=True)
    last_clock_offset_seconds = fields.Float(readonly=True, digits=(16, 3))
    last_ip = fields.Char(readonly=True)
    user_agent = fields.Char(readonly=True)
    app_version = fields.Char(readonly=True)

    # Resultado del autodiagnostico: permite ver el estado del equipo de un
    # cliente sin viajar hasta el sitio.
    last_selftest_date = fields.Datetime(readonly=True)
    last_selftest_result = fields.Selection(
        [("pass", "Adecuado"), ("warn", "Con advertencias"), ("fail", "Inadecuado")],
        readonly=True,
    )
    last_selftest_detail = fields.Text(readonly=True)
    benchmark_ms = fields.Float(
        readonly=True, digits=(16, 1),
        help="Tiempo medido hasta identificar, en milisegundos.",
    )

    queued_punch_count = fields.Integer(
        compute="_compute_queued_punch_count", string="Marcajes sin doblar",
    )

    _sql_constraints = [
        ("token_uniq", "unique(token)", "El token del dispositivo debe ser unico."),
    ]

    @api.depends("token")
    def _compute_kiosk_url(self):
        base = self.env["ir.config_parameter"].sudo().get_param("web.base.url")
        for device in self:
            device.kiosk_url = (
                url_join(base, "/olive_attendance/kiosk/%s" % device.token)
                if device.token else False
            )

    def _compute_queued_punch_count(self):
        # read_group en vez de search_count por dispositivo: una sola consulta.
        counts = dict(self.env["olive.attendance.punch"]._read_group(
            [("device_id", "in", self.ids), ("state", "=", "queued")],
            groupby=["device_id"], aggregates=["__count"],
        ))
        for device in self:
            device.queued_punch_count = counts.get(device, 0)

    # -- acciones ---------------------------------------------------------

    def action_regenerate_token(self):
        """Invalida la URL actual. El kiosco queda desconectado hasta reenlazarlo."""
        for device in self:
            device.write({
                "token": uuid.uuid4().hex,
                "secret_key": uuid.uuid4().hex,
                "state": "draft",
            })
            device.message_post(body=_("Token regenerado; hay que reenlazar el kiosco."))

    def action_force_full_resync(self):
        """Obliga al kiosco a descartar su copia local y bajarlo todo de nuevo."""
        for device in self:
            device.sync_epoch += 1
            device.message_post(body=_("Resincronizacion total solicitada (epoch %s).",
                                       device.sync_epoch))

    def action_block(self):
        self.write({"state": "blocked"})

    def action_unblock(self):
        self.write({"state": "linked"})

    # -- API interna ------------------------------------------------------

    @api.model
    def _resolve_token(self, token):
        """Devuelve el dispositivo activo del token, o lanza si no sirve.

        Se usa desde controllers publicos, asi que va con sudo() y devuelve
        siempre el mismo mensaje: no se le confirma a un desconocido si un
        token existe pero esta bloqueado.
        """
        if not token or not isinstance(token, str) or len(token) != 32:
            raise UserError(_("Kiosco no reconocido."))
        device = self.sudo().search([("token", "=", token)], limit=1)
        if not device or device.state == "blocked" or not device.active:
            raise UserError(_("Kiosco no reconocido."))
        return device

    def _touch(self, ip=None, user_agent=None, app_version=None):
        """Registra el contacto del kiosco. Alimenta el watchdog."""
        vals = {"last_seen": fields.Datetime.now()}
        if ip:
            vals["last_ip"] = ip
        if user_agent:
            vals["user_agent"] = user_agent[:200]
        if app_version:
            vals["app_version"] = app_version
        if self.state == "draft":
            vals["state"] = "linked"
        self.sudo().write(vals)
