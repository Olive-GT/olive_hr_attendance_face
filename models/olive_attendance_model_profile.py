# -*- coding: utf-8 -*-
"""Versiona el conjunto de modelos de IA que usa el kiosco.

La razon de existir de este modelo es una sola: **dos embeddings solo son
comparables si fueron producidos por el mismo modelo**. Cambiar de modelo
invalida todas las plantillas ya capturadas. Tener el perfil versionado
explicitamente convierte ese cambio en una migracion controlada (crear perfil
nuevo, re-enrolar, activar) en vez de una corrupcion silenciosa en la que el
kiosco deja de reconocer a nadie y nadie sabe por que.
"""

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

# Artefactos que el kiosco descarga. Cada uno lleva url, sha256, bytes y, si
# aplica, el tamano de entrada del modelo.
ARTIFACTS = ["detector", "embedder", "liveness"]


class OliveAttendanceModelProfile(models.Model):
    _name = "olive.attendance.model.profile"
    _description = "Perfil de modelos de reconocimiento facial"
    _order = "sequence, id"

    name = fields.Char(required=True)
    code = fields.Char(required=True, help="Identificador tecnico, p.ej. yunet_sface_v1.")
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    notes = fields.Text()

    embedding_version = fields.Char(
        required=True,
        help="Cadena que identifica el espacio de embeddings. Dos vectores solo "
             "son comparables si comparten este valor. NUNCA se reutiliza: si "
             "cambia el modelo, cambia esta cadena.",
    )
    embedding_dim = fields.Integer(required=True, default=128)

    detector_url = fields.Char()
    detector_sha256 = fields.Char()
    detector_bytes = fields.Integer()
    detector_input_size = fields.Integer(
        default=480,
        help="Lado del cuadrado de entrada del detector, en pixeles.",
    )

    embedder_url = fields.Char()
    embedder_sha256 = fields.Char()
    embedder_bytes = fields.Integer()
    embedder_input_size = fields.Integer(default=112)

    liveness_url = fields.Char()
    liveness_sha256 = fields.Char()
    liveness_bytes = fields.Integer()
    liveness_input_size = fields.Integer(default=80)
    liveness_enabled = fields.Boolean(default=False)

    ort_js_url = fields.Char(string="URL de ONNX Runtime (js)")
    ort_wasm_url = fields.Char(string="URL de ONNX Runtime (wasm)")

    _sql_constraints = [
        ("code_uniq", "unique(code)", "El codigo del perfil debe ser unico."),
        ("embedding_version_uniq", "unique(embedding_version)",
         "Ese embedding_version ya existe. Nunca se reutiliza: si cambia el "
         "modelo, tiene que cambiar la version."),
    ]

    @api.constrains("embedding_dim")
    def _check_embedding_dim(self):
        for profile in self:
            if profile.embedding_dim <= 0 or profile.embedding_dim > 4096:
                raise ValidationError(_("La dimension del embedding no es plausible."))

    @api.constrains("liveness_enabled", "liveness_url")
    def _check_liveness(self):
        for profile in self:
            if profile.liveness_enabled and not profile.liveness_url:
                raise ValidationError(_(
                    "No se puede exigir deteccion de vida sin haber configurado "
                    "el modelo correspondiente."
                ))

    def _bootstrap_payload(self):
        """Lo que el kiosco necesita para descargar y verificar sus modelos."""
        self.ensure_one()
        payload = {
            "code": self.code,
            "embedding_version": self.embedding_version,
            "embedding_dim": self.embedding_dim,
            "ort_js_url": self.ort_js_url,
            "ort_wasm_url": self.ort_wasm_url,
            "artifacts": {},
        }
        for name in ARTIFACTS:
            url = self[f"{name}_url"]
            if not url:
                continue
            payload["artifacts"][name] = {
                "url": url,
                "sha256": self[f"{name}_sha256"],
                "bytes": self[f"{name}_bytes"],
                "input_size": self[f"{name}_input_size"],
            }
        payload["liveness_enabled"] = self.liveness_enabled
        return payload
