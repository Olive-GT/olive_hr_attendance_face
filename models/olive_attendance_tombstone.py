# -*- coding: utf-8 -*-
"""Lapidas de registros borrados, para que el sync incremental pueda propagar bajas.

Odoo no deja ningun rastro de un `unlink`. Sin este modelo, el sync incremental
—que pregunta "que cambio desde tal fecha"— jamas se enteraria de que un
empleado fue dado de baja o una plantilla borrada, y **el kiosco los seguiria
reconociendo para siempre**, incluso a alguien despedido.
"""

from odoo import api, fields, models

# Pasado este plazo, cualquier kiosco vivo ya sincronizo la baja. Un kiosco que
# lleve mas de medio ano sin conectarse necesita resincronizacion total de todos
# modos (action_force_full_resync).
RETENTION_DAYS = 180


class OliveAttendanceTombstone(models.Model):
    _name = "olive.attendance.tombstone"
    _description = "Registro borrado (para sincronizacion incremental)"
    _order = "id desc"

    model = fields.Char(required=True, index=True)
    res_id = fields.Integer(required=True, index=True)
    company_id = fields.Many2one("res.company", index=True)

    @api.model
    def _record(self, model, res_ids, company=None):
        """Deja constancia del borrado de esos registros."""
        if not res_ids:
            return self.browse()
        company_id = (company or self.env.company).id
        return self.sudo().create([
            {"model": model, "res_id": res_id, "company_id": company_id}
            for res_id in res_ids
        ])

    @api.model
    def _since(self, model, since, company_id):
        """IDs borrados desde `since`, para el payload de sync_down."""
        domain = [("model", "=", model), ("company_id", "=", company_id)]
        if since:
            domain.append(("create_date", ">", since))
        return self.sudo().search(domain).mapped("res_id")

    @api.model
    def _cron_vacuum(self):
        """Purga lapidas viejas."""
        cutoff = fields.Datetime.subtract(fields.Datetime.now(), days=RETENTION_DAYS)
        stale = self.sudo().search([("create_date", "<", cutoff)])
        count = len(stale)
        stale.unlink()
        return count
