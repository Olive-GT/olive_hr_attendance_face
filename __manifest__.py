# -*- coding: utf-8 -*-
{
    "name": "Asistencias por Reconocimiento Facial (Kiosco PWA)",
    "version": "18.0.1.1.0",
    "category": "Human Resources/Attendances",
    "summary": "Kiosco de ingresos y egresos por rostro, con reconocimiento en el navegador y tolerancia a cortes de red y energia",
    "author": "URBOP / OliveGT",
    "license": "LGPL-3",
    "depends": [
        "hr_attendance",
        "mail",
    ],
    "data": [
        "security/olive_hr_attendance_face_security.xml",
        "security/ir.model.access.csv",
        "data/olive_attendance_model_profile_data.xml",
        "data/ir_cron_data.xml",
        "views/olive_attendance_device_views.xml",
        "views/olive_attendance_punch_views.xml",
        "views/olive_attendance_biometric_views.xml",
        "views/olive_attendance_config_views.xml",
        "views/hr_views.xml",
        "views/menus.xml",
    ],
    "assets": {
        # Solo la interfaz. El pipeline de inferencia y los ~50 MB de modelos
        # viven en static/lib/ FUERA de todo bundle, y se cargan bajo demanda:
        # meterlos aqui le arruinaria el tiempo de carga a toda la base de datos.
        "web.assets_backend": [
            "olive_hr_attendance_face/static/src/face/face_process.js",
            "olive_hr_attendance_face/static/src/face/face_verify.js",
            "olive_hr_attendance_face/static/src/face/face_compare.js",
            "olive_hr_attendance_face/static/src/face/face_templates.xml",
            "olive_hr_attendance_face/static/src/face/face.scss",
        ],
    },
    "post_init_hook": "post_init_hook",
    "installable": True,
    "application": False,
}
