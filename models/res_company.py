# -*- coding: utf-8 -*-
"""Parametros del reconocimiento facial, por compania.

Los umbrales viven aqui y no en el codigo del kiosco a proposito: la
calibracion se hace **en remoto**, cambiando estos valores en Odoo, y baja sola
al kiosco en la siguiente sincronizacion. Es lo que permite prometer
"instalacion sin tecnico, puesta a punto remota".
"""

from odoo import fields, models


class ResCompany(models.Model):
    _inherit = "res.company"

    olive_face_model_profile_id = fields.Many2one(
        "olive.attendance.model.profile", string="Perfil de modelos",
        default=lambda self: self.env.ref(
            "olive_hr_attendance_face.model_profile_yunet_sface_v1",
            raise_if_not_found=False,
        ),
    )

    # -- las 5 guardas ----------------------------------------------------
    olive_face_match_threshold = fields.Float(
        default=0.55, digits=(3, 3), string="Umbral de coincidencia",
        help="Guarda 1. Por debajo de esto no se marca. Ante la duda, rechazar: "
             "el respaldo es el guardia, y molestarlo cuesta menos que atribuir "
             "un marcaje a la persona equivocada.",
    )
    olive_face_review_threshold = fields.Float(
        default=0.65, digits=(3, 3), string="Umbral de certeza",
        help="Guarda 5. Entre el umbral de coincidencia y este, el marcaje entra "
             "pero queda marcado para revision de un supervisor.",
    )
    olive_face_margin_min = fields.Float(
        default=0.06, digits=(3, 3), string="Margen minimo",
        help="Guarda 3. Diferencia minima entre el primer y el segundo candidato. "
             "Es lo que evita confundir a hermanos y parecidos.",
    )
    olive_face_frames_required = fields.Integer(
        default=5, string="Frames coincidentes",
        help="Guarda 2. La misma identidad tiene que ganar en tantos frames "
             "seguidos. Subido de 3 a 5 porque el presupuesto de tiempo lo "
             "permite y se prefiere exactitud sobre velocidad.",
    )
    olive_face_frame_window_ms = fields.Integer(
        default=4000, string="Ventana de frames (ms)",
    )
    olive_face_cooldown_seconds = fields.Integer(
        default=90, string="Bloqueo entre marcajes (s)",
        help="Guarda 4. Sin pantalla de confirmacion, la camara sigue viendo a la "
             "persona: sin este bloqueo generaria marcajes en rafaga.",
    )

    # -- calidad de la captura -------------------------------------------
    olive_face_detector_input_size = fields.Integer(
        default=480, string="Resolucion de deteccion (px)",
        help="Medido en F0: subir de 320 a 480 cuesta ~9 ms y da landmarks mas "
             "precisos. De la precision de los landmarks depende la alineacion, "
             "y de la alineacion depende todo lo demas.",
    )
    olive_face_min_face_px = fields.Integer(
        default=110, string="Tamano minimo del rostro (px)",
    )
    olive_face_min_templates = fields.Integer(
        default=3, string="Plantillas minimas por empleado",
    )
    olive_face_liveness_threshold = fields.Float(default=0.70, digits=(3, 3))
    olive_face_liveness_required = fields.Boolean(default=True)
    olive_face_ambiguous_size_ratio = fields.Float(
        default=0.8, digits=(3, 2), string="Proporcion de ambiguedad",
        help="Con dos rostros en cuadro se procesa el mas grande. Solo se "
             "rechaza si el segundo alcanza esta proporcion del primero, es "
             "decir cuando de verdad no se sabe quien esta al frente. Trabar "
             "el kiosco ante cualquier segunda cara lo bloquearia justo en el "
             "cambio de turno.",
    )

    # -- reloj ------------------------------------------------------------
    olive_face_max_clock_drift_seconds = fields.Integer(
        default=120, string="Desvio de reloj tolerable (s)",
    )
    olive_face_reject_clock_drift_seconds = fields.Integer(
        default=3600, string="Desvio de reloj inaceptable (s)",
        help="Por encima de esto el marcaje no se dobla y queda para revision: "
             "si murio la pila del reloj del equipo, sus horas son basura y no "
             "deben llegar a la nomina.",
    )

    # -- doblado ----------------------------------------------------------
    olive_face_toggle_gap_seconds = fields.Integer(
        default=60, string="Colapso de rafaga (s)",
    )
    olive_face_max_shift_hours = fields.Float(
        default=16.0, string="Jornada maxima (h)",
        help="Una asistencia abierta que supere esto se cierra a la fuerza. Es "
             "el punto exacto que impide que una asistencia abierta huerfana "
             "atore la cola para siempre.",
    )
    olive_face_day_cutoff_hour = fields.Float(
        default=0.0, string="Corte de jornada (h)",
        help="0 = el dia va de medianoche a medianoche. Ponerlo en 4.0 para "
             "turnos nocturnos que cruzan la medianoche.",
    )
    olive_face_protect_validated = fields.Boolean(
        default=True, string="Proteger horas extra aprobadas",
    )
    olive_face_fold_inline = fields.Boolean(
        default=True, string="Doblar al recibir",
        help="Ademas del cron. Hace que los marcajes aparezcan de inmediato "
             "cuando hay red.",
    )

    # -- privacidad -------------------------------------------------------
    olive_face_store_snapshot = fields.Selection(
        [("never", "Nunca"), ("review_only", "Solo los que requieren revision"),
         ("always", "Siempre")],
        default="review_only", string="Guardar foto de auditoria",
    )
    olive_face_snapshot_retention_days = fields.Integer(
        default=30, string="Retencion de fotos (dias)",
        help="Sin purga, las fotos hacen crecer la base sin control.",
    )

    def _olive_face_profile(self):
        """Perfil de modelos de la compania, con respaldo al perfil semilla.

        El `default` del campo solo alcanza a las companias creadas DESPUES de
        instalar el modulo. Las que ya existian quedan con el campo vacio, asi
        que sin este respaldo el modulo falla en toda base preexistente — que
        son todas las reales.
        """
        self.ensure_one()
        if self.olive_face_model_profile_id:
            return self.olive_face_model_profile_id
        return self.env.ref(
            "olive_hr_attendance_face.model_profile_yunet_sface_v1",
            raise_if_not_found=False,
        )

    def _olive_face_client_settings(self):
        """Parametros que baja el kiosco. Solo lo que necesita para decidir."""
        self.ensure_one()
        return {
            "match_threshold": self.olive_face_match_threshold,
            "review_threshold": self.olive_face_review_threshold,
            "margin_min": self.olive_face_margin_min,
            "frames_required": self.olive_face_frames_required,
            "frame_window_ms": self.olive_face_frame_window_ms,
            "cooldown_seconds": self.olive_face_cooldown_seconds,
            "detector_input_size": self.olive_face_detector_input_size,
            "min_face_px": self.olive_face_min_face_px,
            "liveness_threshold": self.olive_face_liveness_threshold,
            "liveness_required": self.olive_face_liveness_required,
            "ambiguous_size_ratio": self.olive_face_ambiguous_size_ratio,
            "max_clock_drift_seconds": self.olive_face_max_clock_drift_seconds,
            "reject_clock_drift_seconds": self.olive_face_reject_clock_drift_seconds,
            "store_snapshot": self.olive_face_store_snapshot,
        }
