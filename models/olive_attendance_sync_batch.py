# -*- coding: utf-8 -*-
"""Auditoria del buzon: que envio el kiosco y cuando.

Responde "¿que mando la laptop?" sin tener que leer logs del servidor, que en
un despliegue en la nube casi nunca estan a mano cuando se necesitan.
"""

from odoo import api, fields, models


class OliveAttendanceSyncBatch(models.Model):
    _name = "olive.attendance.sync.batch"
    _description = "Lote de sincronizacion del kiosco"
    _order = "received_date desc, id desc"

    name = fields.Char(compute="_compute_name")
    device_id = fields.Many2one(
        "olive.attendance.device", required=True, index=True, ondelete="cascade",
    )
    company_id = fields.Many2one(related="device_id.company_id", store=True, index=True)

    received_date = fields.Datetime(default=fields.Datetime.now, required=True, index=True)
    client_sent_date = fields.Datetime(
        help="Hora en la que el kiosco dice haber enviado el lote.",
    )
    client_batch_seq = fields.Integer(
        help="Contador incremental del kiosco. Un salto delata lotes perdidos.",
    )

    punch_count = fields.Integer(string="Recibidos")
    accepted_count = fields.Integer(string="Aceptados")
    duplicate_count = fields.Integer(string="Duplicados")
    rejected_count = fields.Integer(string="Rechazados")

    clock_offset_seconds = fields.Float(digits=(16, 3))
    clock_confidence = fields.Selection([
        ("good", "Confiable"), ("drift", "Con desvio"), ("unreliable", "No confiable"),
    ])
    queue_depth_after = fields.Integer(
        string="Cola restante",
        help="Marcajes que el kiosco dice tener aun pendientes tras este envio.",
    )
    payload_bytes = fields.Integer()
    notes = fields.Text()

    punch_ids = fields.One2many("olive.attendance.punch", "batch_id")

    @api.depends("device_id", "received_date")
    def _compute_name(self):
        for batch in self:
            batch.name = "%s · %s" % (
                batch.device_id.name or "?",
                fields.Datetime.to_string(batch.received_date),
            )
