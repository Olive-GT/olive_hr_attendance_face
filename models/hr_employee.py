# -*- coding: utf-8 -*-

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class HrEmployee(models.Model):
    _inherit = "hr.employee"

    olive_face_template_ids = fields.One2many(
        "olive.attendance.face.template", "employee_id", string="Fotos de identificacion",
        groups="olive_hr_attendance_face.group_face_user",
    )
    olive_face_template_count = fields.Integer(
        compute="_compute_olive_face_template_count", store=True,
        string="Plantillas activas",
    )
    olive_consent_id = fields.Many2one(
        "olive.attendance.consent", compute="_compute_olive_consent", store=True,
    )
    olive_consent_state = fields.Selection(
        related="olive_consent_id.state", store=True, string="Consentimiento biometrico",
    )
    olive_face_enabled = fields.Boolean(
        compute="_compute_olive_face_enabled", store=True,
        string="Puede marcar por rostro",
        help="Requiere consentimiento otorgado y suficientes plantillas activas.",
    )

    @api.depends("olive_face_template_ids.state", "olive_face_template_ids.active",
                 "olive_face_template_ids.embedding")
    def _compute_olive_face_template_count(self):
        for employee in self:
            employee.olive_face_template_count = len(
                employee.sudo().olive_face_template_ids.filtered(
                    lambda t: t.state == "active" and t.active and t.embedding
                )
            )

    @api.depends("company_id")
    def _compute_olive_consent(self):
        consents = self.env["olive.attendance.consent"].sudo().search(
            [("employee_id", "in", self.ids)]
        )
        by_employee = {c.employee_id.id: c for c in consents}
        for employee in self:
            employee.olive_consent_id = by_employee.get(employee.id, False)

    @api.depends("olive_face_template_count", "olive_consent_state", "active",
                 "company_id.olive_face_min_templates")
    def _compute_olive_face_enabled(self):
        for employee in self:
            minimum = employee.company_id.olive_face_min_templates or 1
            employee.olive_face_enabled = bool(
                employee.active
                and employee.olive_consent_state == "granted"
                and employee.olive_face_template_count >= minimum
            )

    # -- fotos de identificacion -----------------------------------------

    def olive_seed_from_avatar(self):
        """Crea la foto de identificacion a partir del avatar de la ficha.

        Es lo que hace que enrolar a 100 personas no sea trabajo: si las fotos
        ya estan en Odoo, no hay que subir ni pedir nada.
        """
        Template = self.env["olive.attendance.face.template"]
        created = Template.browse()
        for employee in self:
            if not employee.image_1920:
                continue
            existing = Template.search([
                ("employee_id", "=", employee.id), ("source", "=", "avatar"),
            ], limit=1)
            if existing:
                # No se re-sincroniza: la copia se guarda redimensionada a
                # 1024 px, asi que jamas es byte a byte igual al avatar, y
                # compararlas reescribiria la foto en cada pasada dejando el
                # procesamiento siempre pendiente. Si cambia el avatar, se
                # borra esta foto y se vuelve a procesar.
                continue
            created |= Template.create({
                "employee_id": employee.id,
                "name": _("Foto de la ficha"),
                "image": employee.image_1920,
                "source": "avatar",
                "sequence": 1,
            })
        return created

    def action_olive_face_photos(self):
        """Abre las fotos de identificacion del empleado."""
        self.ensure_one()
        self.olive_seed_from_avatar()
        return {
            "type": "ir.actions.act_window",
            "name": _("Fotos de identificacion: %s", self.name),
            "res_model": "olive.attendance.face.template",
            "view_mode": "kanban,list,form",
            "domain": [("employee_id", "=", self.id)],
            "context": {"default_employee_id": self.id, "search_default_employee_id": self.id},
        }

    def action_olive_verify_face(self):
        """Abre la pantalla que comprueba si el sistema identifica a esta persona."""
        self.ensure_one()
        return {
            "type": "ir.actions.client",
            "tag": "olive_face_verify",
            "name": _("Verificar identificacion: %s", self.name),
            # El id va por las dos vias a proposito: `params` se pierde al
            # recargar la pagina (no se serializa en la URL) y sin el contexto
            # la pantalla se quedaria sin saber a quien verificar.
            "params": {"employee_id": self.id},
            "context": {"active_id": self.id, "active_model": "hr.employee"},
        }

    def olive_face_context(self):
        """Contexto para la pantalla de verificacion y de captura con camara."""
        if not self:
            raise UserError(_(
                "La pantalla se abrio sin saber a que empleado verificar. "
                "Entra desde el boton 'Verificar identificacion' de la ficha "
                "del empleado."
            ))
        self.ensure_one()
        company = self.company_id or self.env.company
        profile = company._olive_face_profile()
        if not profile:
            raise UserError(_(
                "No hay un perfil de modelos configurado. Se define en "
                "Ajustes -> Asistencias -> Reconocimiento Facial."
            ))
        consent = self.env["olive.attendance.consent"]._ensure_for_employee(self)
        templates = self.sudo().olive_face_template_ids.filtered(
            lambda t: t.active and t.embedding
        )
        return {
            "pipeline_version": self._olive_pipeline_version(),
            "employee": {"id": self.id, "name": self.name},
            "profile": profile._bootstrap_payload(),
            "settings": company._olive_face_client_settings(),
            "consent": {"state": consent.state, "id": consent.id},
            "min_templates": company.olive_face_min_templates,
            "templates": [{
                "id": t.id, "name": t.name, "source": t.source,
                "embedding": t.embedding, "state": t.state,
            } for t in templates],
        }

    def olive_add_camera_photo(self, image_b64, name=None):
        """Guarda una foto tomada con la camara como foto de identificacion.

        Se guarda la FOTO, no solo el vector: asi un cambio de modelo se resuelve
        reprocesando y no volviendo a llamar a la persona.
        """
        self.ensure_one()
        return self.env["olive.attendance.face.template"].create({
            "employee_id": self.id,
            "name": name or _("Captura con camara"),
            "image": image_b64,
            "source": "camera",
        }).id

    @api.model
    def _olive_pipeline_version(self):
        """Version del modulo, para versionar la URL del pipeline.

        pipeline.js vive fuera de los bundles de assets (a proposito: son ~50 MB
        que no deben cargarse para todo el mundo), asi que no recibe el
        versionado automatico de Odoo y el navegador se queda con la copia
        vieja. Colgar la version del modulo en la URL es lo que lo obliga a
        volver a bajarlo cuando cambia.
        """
        module = self.env["ir.module.module"].sudo().search(
            [("name", "=", "olive_hr_attendance_face")], limit=1)
        return module.latest_version or "0"

    @api.model
    def olive_pending_photos_context(self, employee_ids=None):
        """Todo lo pendiente de procesar, para la accion de procesar fotos.

        Una sola accion cubre los tres origenes: avatar de la ficha, foto subida
        y captura con camara. El navegador calcula los vectores; el servidor
        solo guarda el resultado.
        """
        company = self.env.company
        profile = company._olive_face_profile()
        if not profile:
            raise UserError(_(
                "No hay un perfil de modelos configurado. Se define en "
                "Ajustes -> Asistencias -> Reconocimiento Facial."
            ))
        domain = [("company_id", "=", company.id)]
        if employee_ids:
            domain.append(("id", "in", employee_ids))
        employees = self.search(domain)
        employees.olive_seed_from_avatar()

        Template = self.env["olive.attendance.face.template"]
        pending = Template.search([
            ("employee_id", "in", employees.ids),
            ("active", "=", True),
            "|", ("compute_state", "=", "pending"),
                 ("embedding_version", "!=", profile.embedding_version),
        ])
        without_photo = employees.filtered(lambda e: not e.image_1920) - \
            employees.filtered(lambda e: e.olive_face_template_ids)
        return {
            "pipeline_version": self._olive_pipeline_version(),
            "profile": profile._bootstrap_payload(),
            "settings": company._olive_face_client_settings(),
            "pending": [{
                "id": t.id,
                "employee_id": t.employee_id.id,
                "employee_name": t.employee_id.name,
                "name": t.name,
                "source": t.source,
            } for t in pending],
            "without_photo": [{"id": e.id, "name": e.name} for e in without_photo],
        }

    def action_olive_open_consent(self):
        """Abre (creando si hace falta) el consentimiento del empleado."""
        self.ensure_one()
        consent = self.env["olive.attendance.consent"]._ensure_for_employee(self)
        return {
            "type": "ir.actions.act_window",
            "name": _("Consentimiento biometrico"),
            "res_model": "olive.attendance.consent",
            "res_id": consent.id,
            "view_mode": "form",
            "target": "new",
        }

    def unlink(self):
        # Sin lapida, el kiosco seguiria reconociendo a alguien dado de baja.
        self.env["olive.attendance.tombstone"]._record("hr.employee", self.ids)
        return super().unlink()

    def write(self, vals):
        res = super().write(vals)
        # Archivar a alguien tambien tiene que llegar al kiosco.
        if "active" in vals and not vals["active"]:
            self.env["olive.attendance.tombstone"]._record("hr.employee", self.ids)
        return res

    def _olive_sync_payload(self):
        """Representacion minima para el kiosco: solo lo que necesita mostrar."""
        return [{
            "id": employee.id,
            "name": employee.name,
            "enabled": employee.olive_face_enabled,
        } for employee in self]
