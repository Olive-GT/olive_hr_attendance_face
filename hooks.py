# -*- coding: utf-8 -*-

import logging

_logger = logging.getLogger(__name__)


def post_init_hook(env):
    """Asigna el perfil de modelos a las companias que ya existian.

    El `default` de un campo solo alcanza a los registros creados DESPUES de
    instalar el modulo. Toda base real ya tiene sus companias creadas, asi que
    sin este hook el campo queda vacio y el modulo falla en la primera pantalla
    que se abra.
    """
    profile = env.ref(
        "olive_hr_attendance_face.model_profile_yunet_sface_v1",
        raise_if_not_found=False,
    )
    if not profile:
        _logger.warning("No se encontro el perfil de modelos semilla.")
        return
    companies = env["res.company"].search([("olive_face_model_profile_id", "=", False)])
    companies.write({"olive_face_model_profile_id": profile.id})
    _logger.info(
        "Perfil de reconocimiento facial asignado a %s companias.", len(companies)
    )
