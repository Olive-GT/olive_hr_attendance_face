# -*- coding: utf-8 -*-
"""Tests del doblado — la pieza de mayor riesgo del proyecto.

Cada test de aqui corresponde a algo que pasa de verdad en una planta con un
kiosco offline. Ninguno es teorico: el kiosco se queda sin red, alguien olvida
marcar la salida, RRHH registra una entrada a mano, se corta la luz a mitad de
turno. Si el doblado no aguanta estos casos, la nomina sale mal.

Correr con:  odoo -d BASE --test-tags olive_face --stop-after-init
"""

import uuid
from datetime import datetime, timedelta

from odoo import fields
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install", "olive_face")
class TestFold(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env["res.company"].create({"name": "Planta Test"})
        # Zona fija y sin horario de verano: las horas de los tests son
        # predecibles y no dependen de donde corra la suite.
        cls.company.partner_id.tz = "America/Guatemala"
        cls.company.write({
            "olive_face_toggle_gap_seconds": 60,
            "olive_face_min_session_minutes": 15,
            "olive_face_presence_first": True,
            "olive_face_expected_min_hours": 4.0,
            "olive_face_expected_max_hours": 12.0,
            "olive_face_max_shift_hours": 16.0,
            "olive_face_day_cutoff_hour": 0.0,
            "olive_face_protect_validated": True,
            "olive_face_pairing_mode": "alternate",
        })
        cls.employee = cls.env["hr.employee"].create({
            "name": "Empleado Prueba", "company_id": cls.company.id,
            "tz": "America/Guatemala",
        })
        cls.device = cls.env["olive.attendance.device"].sudo().create({
            "name": "Kiosco Test", "company_id": cls.company.id,
        })
        cls.Punch = cls.env["olive.attendance.punch"]
        cls.Attendance = cls.env["hr.attendance"]

    # -- utilidades -------------------------------------------------------

    def _punch(self, when, direction="auto", employee=None, **extra):
        """Crea un marcaje crudo. `when` es UTC ingenuo, como lo guarda Odoo."""
        values = {
            "uuid": uuid.uuid4().hex,
            "device_id": self.device.id,
            "device_time": when,
            "punch_time": when,
            "employee_id": (employee or self.employee).id,
            "direction": direction,
        }
        values.update(extra)
        return self.Punch.sudo().create(values)

    def _fold(self):
        return self.Punch.sudo()._fold_pending()

    def _our_punches(self):
        """Solo los marcajes de este test: la base puede tener otros."""
        return self.Punch.sudo().search([("device_id", "=", self.device.id)])

    def _anomalies(self, kind=None, employee=None):
        domain = [("employee_id", "=", (employee or self.employee).id)]
        if kind:
            domain.append(("kind", "=", kind))
        return self.env["olive.attendance.anomaly"].sudo().search(domain)

    def _attendances(self, employee=None):
        return self.Attendance.sudo().search(
            [("employee_id", "=", (employee or self.employee).id)],
            order="check_in asc",
        )

    @staticmethod
    def _at(day, hour, minute=0):
        """Un instante UTC ingenuo del dia indicado."""
        return datetime(2026, 3, day, hour, minute, 0)

    # ==================================================================
    # El camino feliz y su idempotencia
    # ==================================================================

    def test_par_simple(self):
        """Entrada y salida producen una asistencia cerrada."""
        self._punch(self._at(10, 13))   # 07:00 local
        self._punch(self._at(10, 23))   # 17:00 local
        self._fold()

        attendances = self._attendances()
        self.assertEqual(len(attendances), 1)
        self.assertEqual(attendances.check_in, self._at(10, 13))
        self.assertEqual(attendances.check_out, self._at(10, 23))
        self.assertTrue(attendances.olive_is_managed)
        self.assertEqual(set(self._our_punches().mapped("state")), {"applied"})

    def test_doblar_dos_veces_no_cambia_nada(self):
        """Idempotencia: la segunda pasada no debe reescribir ni duplicar.

        Si esto falla, el cron reescribiria las asistencias cada 10 minutos y
        cualquier referencia externa a su id quedaria rota sin aviso.
        """
        self._punch(self._at(10, 13))
        self._punch(self._at(10, 23))
        self._fold()
        first = self._attendances()
        first_id = first.id

        self.Punch.sudo()._fold_pending(punch_ids=self._our_punches().ids)
        again = self._attendances()
        self.assertEqual(len(again), 1)
        self.assertEqual(again.id, first_id, "La asistencia se reconstruyo sin necesidad")
        self.assertEqual(again.olive_rebuilt_count, 0)

    def test_uuid_duplicado_no_entra(self):
        """La restriccion de UUID es TODA la idempotencia del reenvio."""
        from psycopg2 import IntegrityError

        from odoo.tools import mute_logger

        shared = uuid.uuid4().hex
        self._punch(self._at(10, 13), uuid=shared)
        with self.assertRaises(IntegrityError), mute_logger("odoo.sql_db"):
            with self.env.cr.savepoint():
                self._punch(self._at(10, 14), uuid=shared)
                self.env.flush_all()

    # ==================================================================
    # Lo que hace falta el diseno entero: llegada tardia y desordenada
    # ==================================================================

    def test_llegada_desordenada(self):
        """Creados al reves, pero con su hora real: el resultado es el mismo.

        El kiosco puede vaciar la cola en cualquier orden. El doblado ordena por
        `punch_time`, no por orden de llegada.
        """
        self._punch(self._at(10, 23))   # la salida se crea primero
        self._punch(self._at(10, 13))
        self._fold()

        attendances = self._attendances()
        self.assertEqual(len(attendances), 1)
        self.assertEqual(attendances.check_in, self._at(10, 13))
        self.assertEqual(attendances.check_out, self._at(10, 23))

    def test_llegada_tardia_entre_asistencias_existentes(self):
        """El caso que rompe la insercion directa contra _check_validity.

        El kiosco estuvo sin red el dia 10. Mientras tanto se doblaron los dias
        11 y 12. Ahora llegan los marcajes viejos, que caen ENTRE asistencias ya
        escritas: insercion 'intermedia', prohibida por el core. Reconstruyendo
        la ventana no hay conflicto, porque el dia 10 se construye entero y
        aparte.
        """
        for day in (11, 12):
            self._punch(self._at(day, 13))
            self._punch(self._at(day, 23))
        self._fold()
        self.assertEqual(len(self._attendances()), 2)

        # Ahora si, los marcajes atrasados del dia 10.
        self._punch(self._at(10, 13))
        self._punch(self._at(10, 23))
        self._fold()

        attendances = self._attendances()
        self.assertEqual(len(attendances), 3)
        self.assertEqual(attendances[0].check_in, self._at(10, 13))
        self.assertEqual(
            set(self._our_punches().mapped("state")), {"applied"},
            "Algun marcaje quedo sin aplicar",
        )

    def test_marcaje_tardio_reconstruye_su_dia(self):
        """Un marcaje viejo que cambia un dia ya doblado lo reconstruye.

        Llega la salida que faltaba de un dia cerrado a la fuerza. La asistencia
        se rehace con la hora correcta y queda constancia de la reconstruccion.
        """
        # Horas recientes a proposito: una entrada vieja se cerraria a la fuerza
        # por jornada maxima y el test estaria midiendo otra cosa.
        entrada_time = fields.Datetime.now() - timedelta(hours=3)
        salida_time = fields.Datetime.now() - timedelta(hours=1)

        entrada = self._punch(entrada_time)
        self._fold()
        self.assertEqual(len(self._attendances()), 1)
        self.assertFalse(self._attendances().check_out, "Se cerro sin marcaje de salida")

        self._punch(salida_time)
        self._fold()

        attendances = self._attendances()
        self.assertEqual(len(attendances), 1)
        self.assertEqual(attendances.check_out, salida_time)
        self.assertEqual(attendances.olive_rebuilt_count, 1)
        self.assertEqual(entrada.state, "applied")

    # ==================================================================
    # Casos degenerados
    # ==================================================================

    def test_rafaga_se_colapsa(self):
        """Dos marcajes a segundos de distancia son uno solo.

        Red de seguridad del servidor por si falla el cooldown del kiosco.
        """
        self._punch(self._at(10, 13, 0))
        self._punch(self._at(10, 13, 0) + timedelta(seconds=20))
        self._punch(self._at(10, 23))
        self._fold()

        self.assertEqual(len(self._attendances()), 1)
        self.assertEqual(
            len(self._our_punches().filtered(lambda p: p.state == "duplicate")), 1)

    def test_marcaje_repetido_no_parte_la_jornada(self):
        """El error mas caro del sistema, y el mas facil de cometer.

        Marca a las 07:00, no ve confirmacion, vuelve a marcar a las 07:03. Por
        pura alternancia ese segundo marcaje seria la SALIDA: tres minutos
        trabajados, y la salida real de las 17:00 quedaria como una entrada
        nueva. Un dia entero destruido por un doble toque.

        Tres minutos superan el colapso de rafaga de 60 s, asi que la unica
        defensa es la duracion minima de jornada.
        """
        self._punch(self._at(10, 13, 0))
        self._punch(self._at(10, 13, 3))
        self._punch(self._at(10, 23))
        self._fold()

        attendances = self._attendances()
        self.assertEqual(len(attendances), 1, "La jornada se partio en dos")
        self.assertEqual(attendances.check_in, self._at(10, 13, 0))
        self.assertEqual(attendances.check_out, self._at(10, 23))
        self.assertEqual(
            len(self._our_punches().filtered(lambda p: p.state == "duplicate")), 1)

    def test_dia_con_almuerzo(self):
        """Cuatro marcajes legitimos son dos asistencias, no una."""
        self._punch(self._at(10, 13))   # entra 07:00
        self._punch(self._at(10, 18))   # sale 12:00
        self._punch(self._at(10, 19))   # vuelve 13:00
        self._punch(self._at(10, 23))   # sale 17:00
        self._fold()

        attendances = self._attendances()
        self.assertEqual(len(attendances), 2)
        self.assertEqual(attendances[0].check_out, self._at(10, 18))
        self.assertEqual(attendances[1].check_in, self._at(10, 19))

    def test_numero_impar_queda_senalado(self):
        """Tres marcajes son ambiguos por naturaleza: los revisa una persona.

        Entra, sale, y vuelve a entrar sin marcar la salida. No hay forma de
        adivinar la hora real de salida, asi que se cierra a la fuerza y —lo
        importante— queda marcado para revision en vez de pasar por bueno.
        """
        self._punch(self._at(10, 13))
        self._punch(self._at(10, 18))
        self._punch(self._at(10, 20))
        self._fold()

        attendances = self._attendances()
        self.assertEqual(len(attendances), 2)
        self.assertEqual(attendances[1].olive_anomaly, "forced_close")
        self.assertTrue(
            attendances[1].olive_needs_review,
            "Una jornada cerrada a la fuerza tiene que llegar a un supervisor",
        )

    def test_entrada_sobre_entrada(self):
        """Olvido marcar la salida y al dia siguiente vuelve a entrar.

        La primera se cierra sin solaparse con la segunda y queda marcada como
        anomalia. Lo que NO puede pasar es que se quede abierta: dos abiertas
        del mismo empleado son exactamente lo que el core prohibe.
        """
        self._punch(self._at(10, 13))
        self._punch(self._at(11, 13))
        self._fold()

        attendances = self._attendances()
        self.assertEqual(len(attendances), 2)
        self.assertTrue(attendances[0].check_out, "Quedo abierta y va a atorar la cola")
        self.assertEqual(attendances[0].olive_anomaly, "missing_out")
        self.assertLessEqual(attendances[0].check_out, attendances[1].check_in)

    def test_salida_huerfana_deja_constancia_de_presencia(self):
        """La hora se desconoce, pero la presencia es un hecho.

        La nomina asume asistencia y descuenta por ausencias, asi que una hora
        imprecisa no cuesta dinero pero un dia de presencia perdido si. Por eso
        se registra la asistencia igual y se levanta una incidencia, en vez de
        descartar el marcaje.
        """
        salida = self._punch(self._at(10, 23), direction="out")
        self._fold()

        self.assertEqual(len(self._attendances()), 1, "Se perdio la presencia")
        self.assertEqual(salida.state, "applied")
        incidencia = self._anomalies("orphan_out")
        self.assertEqual(len(incidencia), 1)
        self.assertTrue(incidencia.attendance_recorded)

    # ==================================================================
    # Incidencias: nada raro pasa inadvertido
    # ==================================================================

    def test_incidencia_por_marcaje_repetido(self):
        self._punch(self._at(10, 13, 0))
        self._punch(self._at(10, 13, 3))
        self._punch(self._at(10, 23))
        self._fold()

        incidencia = self._anomalies("repeated_punch")
        self.assertEqual(len(incidencia), 1)
        self.assertEqual(incidencia.severity, "info")
        self.assertTrue(incidencia.detail)
        self.assertTrue(incidencia.resolution, "Hay que decir que hizo el sistema")

    def test_incidencia_por_jornada_corta(self):
        """Una jornada de 2 h con la esperada en 4 h se marca, pero no se toca."""
        self._punch(self._at(10, 13))
        self._punch(self._at(10, 15))
        self._fold()

        self.assertEqual(len(self._attendances()), 1, "No debe bloquear nada")
        self.assertEqual(len(self._anomalies("short_session")), 1)

    def test_incidencia_por_numero_impar(self):
        self._punch(self._at(10, 13))
        self._punch(self._at(10, 18))
        self._punch(self._at(10, 20))
        self._fold()

        self.assertEqual(len(self._anomalies("odd_count")), 1)

    def test_marcaje_sin_identificar_es_grave(self):
        """El unico caso donde de verdad se pierde una presencia."""
        self.Punch.sudo().create({
            "uuid": uuid.uuid4().hex, "device_id": self.device.id,
            "device_time": self._at(10, 13), "punch_time": self._at(10, 13),
            "employee_id": False,
        })
        self._fold()

        incidencia = self.env["olive.attendance.anomaly"].sudo().search([
            ("kind", "=", "unidentified"), ("company_id", "=", self.company.id),
        ])
        self.assertEqual(len(incidencia), 1)
        self.assertEqual(incidencia.severity, "critical")
        self.assertFalse(
            incidencia.attendance_recorded,
            "Sin empleado no hay presencia registrada, y eso si cuesta dinero",
        )

    def test_incidencias_no_se_duplican_al_rehacer(self):
        """Reconstruir diez veces la jornada no genera diez incidencias."""
        self._punch(self._at(10, 13))
        self._punch(self._at(10, 15))
        self._fold()
        self.assertEqual(len(self._anomalies("short_session")), 1)

        self.Punch.sudo()._fold_pending(punch_ids=self._our_punches().ids)
        self.assertEqual(len(self._anomalies("short_session")), 1)

    def test_incidencia_revisada_sobrevive_la_reconstruccion(self):
        """Una decision humana no se borra sola al rehacer la jornada."""
        self._punch(self._at(10, 13))
        self._punch(self._at(10, 15))
        self._fold()
        incidencia = self._anomalies("short_session")
        incidencia.action_review()

        self.Punch.sudo()._fold_pending(punch_ids=self._our_punches().ids)
        self.assertTrue(incidencia.exists(), "Se borro una incidencia ya revisada")
        self.assertEqual(incidencia.state, "reviewed")

    def test_turno_cruzando_medianoche(self):
        """22:00 a 06:00 es UN turno, no dos jornadas partidas.

        Es lo que justifica expandir la ventana +-jornada maxima.
        """
        self._punch(self._at(11, 4))     # 10-mar 22:00 local
        self._punch(self._at(11, 12))    # 11-mar 06:00 local
        self._fold()

        attendances = self._attendances()
        self.assertEqual(len(attendances), 1)
        self.assertEqual(attendances.check_in, self._at(11, 4))
        self.assertEqual(attendances.check_out, self._at(11, 12))

    def test_cierre_forzado_por_jornada_maxima(self):
        """Una abierta muy vieja se cierra a la fuerza.

        Es el punto exacto que impide que una asistencia abierta huerfana
        bloquee para siempre todos los marcajes futuros de esa persona.
        """
        hace_mucho = fields.Datetime.now() - timedelta(hours=40)
        self._punch(hace_mucho)
        self._fold()

        attendances = self._attendances()
        self.assertEqual(len(attendances), 1)
        self.assertTrue(attendances.check_out)
        self.assertEqual(attendances.olive_anomaly, "forced_close")
        self.assertEqual(
            attendances.check_out, attendances.check_in + timedelta(hours=16))

    def test_abierta_reciente_se_respeta(self):
        """Quien entro hace dos horas sigue adentro: no se cierra nada."""
        self._punch(fields.Datetime.now() - timedelta(hours=2))
        self._fold()

        attendances = self._attendances()
        self.assertEqual(len(attendances), 1)
        self.assertFalse(attendances.check_out)
        self.assertFalse(attendances.olive_anomaly)

    # ==================================================================
    # Lo que el doblado no puede tocar
    # ==================================================================

    def test_bloque_inmutable_no_se_pisa(self):
        """Una asistencia registrada a mano gana siempre.

        Es el respaldo del kiosco: cuando no reconoce a alguien, el guardia lo
        registra a mano. Si el doblado pisara ese registro, el respaldo no
        serviria de nada.
        """
        manual = self.Attendance.sudo().create({
            "employee_id": self.employee.id,
            "check_in": self._at(10, 13),
            "check_out": self._at(10, 23),
        })
        self._punch(self._at(10, 14))
        self._punch(self._at(10, 22))
        self._fold()

        self.assertTrue(manual.exists(), "Se borro una asistencia manual")
        self.assertEqual(manual.check_in, self._at(10, 13))
        self.assertEqual(len(self._attendances()), 1)
        rechazados = self._our_punches().filtered(lambda p: p.state == "rejected")
        self.assertEqual(len(rechazados), 2)
        self.assertEqual(set(rechazados.mapped("review_state")), {"pending"})

    def test_marcaje_sin_empleado_no_se_dobla(self):
        """Evidencia de identificacion fallida: se conserva, no se aplica."""
        huerfano = self.Punch.sudo().create({
            "uuid": uuid.uuid4().hex,
            "device_id": self.device.id,
            "device_time": self._at(10, 13),
            "punch_time": self._at(10, 13),
            "employee_id": False,
        })
        self._fold()

        self.assertEqual(len(self._attendances()), 0)
        self.assertEqual(huerfano.state, "rejected")
        self.assertEqual(huerfano.review_state, "pending")

    def test_reloj_no_confiable_no_llega_a_nomina(self):
        """Si murio la pila del CMOS, esa hora es basura."""
        malo = self._punch(self._at(10, 13), clock_confidence="unreliable")
        self._fold()

        self.assertEqual(len(self._attendances()), 0)
        self.assertEqual(malo.state, "rejected")
        self.assertEqual(malo.review_state, "pending")

    def test_empleados_distintos_no_se_mezclan(self):
        """Cada empleado se reconstruye por separado."""
        otro = self.env["hr.employee"].create({
            "name": "Otro Empleado", "company_id": self.company.id,
            "tz": "America/Guatemala",
        })
        self._punch(self._at(10, 13))
        self._punch(self._at(10, 23))
        self._punch(self._at(10, 14), employee=otro)
        self._punch(self._at(10, 22), employee=otro)
        self._fold()

        self.assertEqual(len(self._attendances()), 1)
        self.assertEqual(len(self._attendances(otro)), 1)
        self.assertEqual(self._attendances(otro).check_in, self._at(10, 14))


@tagged("post_install", "-at_install", "olive_face")
class TestFoldFirstLast(TestFold):
    """El mismo doblado, pero con una camara pasiva que ve a la gente muchas veces.

    Es el modo del despliegue real: la camara esta en el escritorio de la
    asistente y ve pasar a todo el mundo, varias veces al dia, sin que nadie
    marque deliberadamente nada.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company.olive_face_pairing_mode = "first_last"

    def test_muchos_avistamientos_una_sola_jornada(self):
        """El caso que motiva el modo.

        Alternando entrada y salida, ocho avistamientos serian cuatro jornadas
        cortadas. Con primer y ultimo es una sola jornada correcta.
        """
        for hour in range(13, 24, 2):        # 07:00 a 17:00 local, cada 2 h
            self._punch(self._at(10, hour))
        self._fold()

        attendances = self._attendances()
        self.assertEqual(len(attendances), 1, "La jornada se partio en pedazos")
        self.assertEqual(attendances.check_in, self._at(10, 13))
        self.assertEqual(attendances.check_out, self._at(10, 23))

    def test_avistamientos_intermedios_quedan_como_evidencia(self):
        """Lo del medio no se tira: prueba que la persona estuvo todo el dia."""
        for hour in (13, 17, 20, 23):
            self._punch(self._at(10, hour))
        self._fold()

        attendance = self._attendances()
        intermedios = self._our_punches().filtered(
            lambda p: p.state == "applied" and not p.attendance_field)
        self.assertEqual(len(intermedios), 2)
        self.assertEqual(
            set(intermedios.mapped("attendance_id")), {attendance},
            "Los avistamientos intermedios tienen que quedar enlazados al dia")

    def test_un_solo_avistamiento_deja_constancia(self):
        """Se le vio una vez: consta que vino, aunque no cuanto se quedo."""
        self._punch(self._at(10, 13))
        self._fold()

        attendances = self._attendances()
        self.assertEqual(len(attendances), 1, "Se perdio la presencia")
        self.assertEqual(attendances.olive_anomaly, "missing_out")
        self.assertEqual(len(self._anomalies("missing_out")), 1)

    def test_dias_distintos_no_se_mezclan(self):
        """Primer y ultimo es POR DIA, no del periodo entero."""
        for day in (10, 11):
            self._punch(self._at(day, 13))
            self._punch(self._at(day, 18))
            self._punch(self._at(day, 23))
        self._fold()

        attendances = self._attendances()
        self.assertEqual(len(attendances), 2)
        self.assertEqual(attendances[0].check_in, self._at(10, 13))
        self.assertEqual(attendances[0].check_out, self._at(10, 23))
        self.assertEqual(attendances[1].check_in, self._at(11, 13))
