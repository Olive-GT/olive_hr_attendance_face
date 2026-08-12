# -*- coding: utf-8 -*-
"""Foto de identificacion de un empleado y el vector derivado de ella.

**Un registro = una foto + su vector.** La foto es la fuente de verdad; el
vector es un derivado que se puede recalcular.

Guardar la foto y no solo el vector tiene una consecuencia importante: cambiar
de modelo de reconocimiento **deja de exigir volver a enrolar a nadie**. Se
recalculan los vectores en lote desde las fotos ya guardadas. Con solo el
vector, un cambio de modelo obligaria a que las 100 personas volvieran a pasar
por la camara.

El precio es que ahora si se almacenan rostros. Se asume a conciencia: Odoo ya
guarda la foto del empleado en su ficha, asi que el añadido real es marginal, y
sigue sujeto a consentimiento.

El vector se guarda como base64 de un Float32Array little-endian normalizado L2:
unas 10 veces mas compacto que JSON de floats, y viaja tal cual en el sync hacia
el kiosco.

Dato biometrico: acceso restringido.
"""

import base64
import binascii
import math
import struct

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

# Tolerancia sobre la norma L2. El vector viaja normalizado desde el navegador;
# la holgura solo absorbe el redondeo de float32.
_NORM_TOLERANCE = 1e-3


class OliveAttendanceFaceTemplate(models.Model):
    _name = "olive.attendance.face.template"
    _description = "Foto de identificacion facial"
    _order = "employee_id, sequence, id"

    employee_id = fields.Many2one(
        "hr.employee", required=True, index=True, ondelete="cascade",
    )
    company_id = fields.Many2one(
        related="employee_id.company_id", store=True, index=True,
    )
    sequence = fields.Integer(default=10)
    name = fields.Char(
        required=True, default=lambda self: _("Foto"),
        help="Condicion de la foto: 'frontal', 'con casco', 'con lentes'...",
    )

    # -- la foto: fuente de verdad ---------------------------------------
    # fields.Image redimensiona solo. 1024 px alcanza de sobra para recalcular
    # el vector si algun dia cambia el modelo, sin inflar la base.
    image = fields.Image(
        max_width=1024, max_height=1024, required=True, string="Foto",
        help="La foto de la que se deriva el vector.",
    )
    image_128 = fields.Image(related="image", max_width=128, max_height=128, store=True)

    source = fields.Selection(
        [("avatar", "Foto de la ficha"), ("upload", "Foto subida"),
         ("camera", "Captura con camara"), ("auto", "Aprendida por el kiosco")],
        default="upload", required=True, index=True, string="Origen",
        help="Una captura con la camara en las condiciones reales del kiosco "
             "rinde mas que una foto de archivo.",
    )

    # -- el vector: derivado ---------------------------------------------
    embedding = fields.Char(
        help="Base64 de un Float32Array little-endian, normalizado L2. Se "
             "calcula en el navegador; el servidor nunca ejecuta inferencia.",
    )
    embedding_version = fields.Char(index=True)
    dim = fields.Integer()

    compute_state = fields.Selection(
        [("pending", "Sin procesar"), ("ok", "Procesada"),
         ("no_face", "Sin rostro detectable"), ("ambiguous", "Varias caras"),
         ("too_small", "Rostro muy pequeno"), ("error", "Error")],
        default="pending", required=True, index=True, string="Procesamiento",
    )
    compute_message = fields.Char(readonly=True)

    quality_score = fields.Float(digits=(3, 3), help="Confianza de la deteccion.")
    face_px = fields.Integer(help="Ancho del rostro en pixeles.")
    luminance = fields.Float(digits=(5, 1), help="Luminancia media del recorte.")

    state = fields.Selection(
        [("draft", "Borrador"), ("active", "Activa"), ("archived", "Archivada")],
        default="draft", required=True, index=True,
    )
    active = fields.Boolean(default=True)

    capture_date = fields.Datetime(default=fields.Datetime.now, readonly=True)
    captured_by_uid = fields.Many2one(
        "res.users", default=lambda self: self.env.user, readonly=True,
    )

    @api.depends("name", "employee_id")
    def _compute_display_name(self):
        for tpl in self:
            tpl.display_name = "%s · %s" % (tpl.employee_id.display_name or "?", tpl.name)

    # -- validacion del vector -------------------------------------------

    @staticmethod
    def _decode_embedding(encoded, dim):
        """Devuelve la lista de floats, o lanza ValueError si no cuadra."""
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) != dim * 4:
            raise ValueError(
                "se esperaban %d bytes para %d dimensiones y llegaron %d"
                % (dim * 4, dim, len(raw))
            )
        return struct.unpack("<%df" % dim, raw)

    @api.constrains("embedding", "dim")
    def _check_embedding_format(self):
        """El servidor no infiere, pero si valida el formato de lo que recibe.

        Un vector malformado que llegue al kiosco lo dejaria reconociendo mal
        sin que nada mas lo delate.
        """
        for tpl in self.filtered("embedding"):
            if tpl.dim <= 0 or tpl.dim > 4096:
                raise ValidationError(_("Dimension de vector no plausible: %s", tpl.dim))
            try:
                values = self._decode_embedding(tpl.embedding, tpl.dim)
            except (ValueError, TypeError, binascii.Error) as err:
                raise ValidationError(
                    _("Vector malformado en la foto de %(empl)s: %(err)s",
                      empl=tpl.employee_id.display_name, err=err)
                ) from err
            norm = math.sqrt(sum(v * v for v in values))
            if abs(norm - 1.0) > _NORM_TOLERANCE:
                raise ValidationError(_(
                    "El vector de %(empl)s no esta normalizado (norma %(norm).6f).",
                    empl=tpl.employee_id.display_name, norm=norm,
                ))

    @api.constrains("state")
    def _check_activation(self):
        """Solo se activa lo que esta procesado y consentido."""
        for tpl in self.filtered(lambda t: t.state == "active"):
            if tpl.compute_state != "ok" or not tpl.embedding:
                raise ValidationError(_(
                    "La foto de %s no se puede activar: todavia no fue procesada, "
                    "o no se detecto un rostro utilizable en ella.",
                    tpl.employee_id.display_name,
                ))
            if tpl.employee_id.olive_consent_state != "granted":
                raise ValidationError(_(
                    "No se puede activar la foto de %s sin consentimiento "
                    "biometrico registrado.",
                    tpl.employee_id.display_name,
                ))
            profile = tpl.company_id.olive_face_model_profile_id
            if profile and tpl.embedding_version != profile.embedding_version:
                raise ValidationError(_(
                    "La foto de %(empl)s tiene un vector del espacio '%(tpl)s' pero "
                    "la compania usa '%(cur)s'. Hay que reprocesar las fotos: "
                    "vectores de modelos distintos no son comparables.",
                    empl=tpl.employee_id.display_name,
                    tpl=tpl.embedding_version or "-", cur=profile.embedding_version,
                ))

    # -- ciclo de vida ----------------------------------------------------

    def write(self, vals):
        # Cambiar la foto invalida el vector. Recalcularlo pasa a ser obligatorio:
        # dejar el vector viejo junto a una foto nueva seria mentir sobre el dato.
        if "image" in vals:
            vals.setdefault("compute_state", "pending")
            vals.setdefault("embedding", False)
            vals.setdefault("state", "draft")
        return super().write(vals)

    def unlink(self):
        self.env["olive.attendance.tombstone"]._record(self._name, self.ids)
        return super().unlink()

    def action_activate(self):
        self.write({"state": "active"})

    def action_archive_template(self):
        self.write({"state": "archived", "active": False})

    def action_reprocess(self):
        """Marca las fotos para que se les recalcule el vector."""
        self.write({"compute_state": "pending", "embedding": False, "state": "draft"})

    # -- API para el navegador -------------------------------------------

    def olive_store_result(self, result):
        """Guarda el resultado del procesamiento hecho en el navegador."""
        self.ensure_one()
        profile = self.company_id.olive_face_model_profile_id
        vals = {
            "compute_state": result.get("state", "error"),
            "compute_message": result.get("message") or False,
        }
        if result.get("state") == "ok":
            vals.update({
                "embedding": result["embedding"],
                "dim": result["dim"],
                "embedding_version": profile.embedding_version,
                "quality_score": result.get("quality_score", 0.0),
                "face_px": int(result.get("face_px") or 0),
                "luminance": result.get("luminance", 0.0),
            })
        else:
            vals.update({"embedding": False, "state": "draft"})
        # Se llama a super().write para no disparar el reseteo a 'pending' de
        # arriba, que solo corresponde cuando cambia la foto.
        super().write(vals)
        return True

    def _sync_payload(self):
        """Representacion compacta para el sync hacia el kiosco."""
        return [{
            "id": tpl.id,
            "employee_id": tpl.employee_id.id,
            "embedding": tpl.embedding,
            "dim": tpl.dim,
        } for tpl in self if tpl.embedding]
