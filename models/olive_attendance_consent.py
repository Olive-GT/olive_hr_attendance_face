# -*- coding: utf-8 -*-
"""Consentimiento del empleado para el tratamiento de su dato biometrico.

Los embeddings faciales son dato biometrico. Se registra el consentimiento
ahora, no cuando alguien pregunte: sin consentimiento vigente no se puede
activar ninguna plantilla, y revocarlo archiva todas las del empleado en el
acto.
"""

from odoo import _, api, fields, models


class OliveAttendanceConsent(models.Model):
    _name = "olive.attendance.consent"
    _description = "Consentimiento biometrico"
    _inherit = ["mail.thread"]
    _order = "employee_id, id desc"

    employee_id = fields.Many2one(
        "hr.employee", required=True, index=True, ondelete="cascade", tracking=True,
    )
    company_id = fields.Many2one(related="employee_id.company_id", store=True, index=True)
    state = fields.Selection(
        [("pending", "Pendiente"), ("granted", "Otorgado"), ("revoked", "Revocado")],
        default="pending", required=True, index=True, tracking=True,
    )
    granted_date = fields.Datetime(readonly=True, tracking=True)
    revoked_date = fields.Datetime(readonly=True, tracking=True)
    method = fields.Selection(
        [("paper", "Firma en papel"), ("digital", "Firma digital")],
        default="paper",
    )
    document = fields.Binary(attachment=True, string="Documento firmado")
    document_filename = fields.Char()

    # Permisos granulares: el consentimiento para reconocer no implica
    # consentimiento para almacenar fotografias.
    allow_snapshot = fields.Boolean(
        string="Permite foto de auditoria", default=True,
        help="Guardar la fotografia del momento del marcaje como evidencia.",
    )
    allow_thumbnail = fields.Boolean(
        string="Permite miniatura de enrolamiento", default=False,
    )
    notes = fields.Text()

    _sql_constraints = [
        ("employee_uniq", "unique(employee_id)",
         "Ya existe un registro de consentimiento para ese empleado."),
    ]

    def action_grant(self):
        self.write({"state": "granted", "granted_date": fields.Datetime.now()})
        for consent in self:
            consent.message_post(body=_("Consentimiento biometrico otorgado."))

    def action_revoke(self):
        """Revoca y archiva de inmediato todas las plantillas del empleado."""
        self.write({"state": "revoked", "revoked_date": fields.Datetime.now()})
        for consent in self:
            templates = consent.employee_id.olive_face_template_ids
            templates.write({"state": "archived", "active": False})
            consent.message_post(body=_(
                "Consentimiento revocado. Se archivaron %s plantillas faciales.",
                len(templates),
            ))

    @api.model
    def _ensure_for_employee(self, employee):
        """Devuelve el consentimiento del empleado, creandolo en pendiente."""
        consent = self.search([("employee_id", "=", employee.id)], limit=1)
        if not consent:
            consent = self.create({"employee_id": employee.id})
        return consent
