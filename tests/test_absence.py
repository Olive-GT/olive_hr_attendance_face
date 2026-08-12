# -*- coding: utf-8 -*-
"""Tests de la deteccion de ausencias — la parte que decide dinero.

El criterio que se prueba aqui una y otra vez es el mismo: **ante la duda, no
acusar**. No pagarle un dia a alguien que si trabajo es una falla grave; dejar
pasar una ausencia es un error menor. Los tests estan escritos para fallar si
alguien alguna vez invierte esa asimetria.
"""

from datetime import date, datetime, timedelta

from odoo import fields
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install", "olive_face")
class TestAbsence(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env["res.company"].create({"name": "Obra Test"})
        cls.company.partner_id.tz = "America/Guatemala"
        cls.company.olive_absence_min_confidence = 0.6
        cls.Absence = cls.env["olive.attendance.absence"]
        cls.Attendance = cls.env["hr.attendance"]

        # Una cuadrilla: hace falta grupo para que la senal "el grupo delata el
        # dia" tenga con quien comparar.
        cls.crew = cls.env["hr.employee"].create([
            {"name": "Obrero %s" % i, "company_id": cls.company.id,
             "tz": "America/Guatemala"}
            for i in range(1, 9)
        ])
        cls.target = cls.crew[0]

    # -- utilidades -------------------------------------------------------

    @classmethod
    def _day(cls, offset):
        """Un dia pasado, relativo a hoy: el barrido ignora hoy y el futuro."""
        return fields.Date.context_today(cls.env["olive.attendance.absence"]) \
            - timedelta(days=offset)

    def _worked(self, employee, day, hour=13):
        """Deja constancia de que la persona vino ese dia."""
        start = datetime.combine(day, datetime.min.time()) + timedelta(hours=hour)
        return self.Attendance.sudo().create({
            "employee_id": employee.id,
            "check_in": start,
            "check_out": start + timedelta(hours=8),
        })

    def _crew_worked(self, day, exclude=None):
        for employee in self.crew:
            if exclude and employee in exclude:
                continue
            self._worked(employee, day)

    def _week(self, missing=(), employee=None):
        """La cuadrilla trabaja del dia 7 al 3; el objetivo falta los indicados.

        Hace falta historial ALREDEDOR del dia en cuestion. Una ausencia se
        reconoce por contraste —con los companeros y con los dias vecinos— y una
        persona de la que no se sabe nada no se evalua, a proposito.
        """
        target = employee or self.target
        for offset in (7, 6, 5, 4, 3):
            for member in self.crew:
                if member == target and offset in missing:
                    continue
                self._worked(member, self._day(offset))

    def _scan(self, days_back=10):
        return self.Absence.sudo()._scan_period(
            self._day(days_back), self._day(1), company=self.company)

    def _absences(self, employee=None):
        return self.Absence.sudo().search([
            ("employee_id", "=", (employee or self.target).id)])

    # ==================================================================
    # La senal del grupo
    # ==================================================================

    def test_falta_mientras_todos_trabajan(self):
        """Siete compañeros vinieron y uno no, y el vino el dia antes y el de
        despues. Eso es una ausencia."""
        self._week(missing=(5,))

        self._scan()

        absence = self._absences().filtered(
            lambda a: a.absence_date == self._day(5))
        self.assertEqual(len(absence), 1, "No detecto una ausencia evidente")
        self.assertEqual(absence.state, "proposed", "Nunca debe auto-confirmar")
        self.assertGreater(absence.confidence, 0.8)
        self.assertTrue(absence.signals, "Toda propuesta debe venir explicada")

    def test_dia_sin_nadie_no_acusa_a_nadie(self):
        """Feriado o descanso colectivo: nadie vino, nadie falto.

        Es el caso que hace inutil cualquier calendario: el sistema lo deduce de
        que no vino NADIE, sin saber que dia era.
        """
        self._crew_worked(self._day(5))
        # El dia 4 no trabaja nadie.
        self._crew_worked(self._day(3))

        self._scan()

        acusados = self.Absence.sudo().search([
            ("company_id", "=", self.company.id),
            ("absence_date", "=", self._day(4)),
        ])
        self.assertFalse(
            acusados,
            "Un dia en que no vino nadie fue feriado, no 8 faltas simultaneas",
        )

    # ==================================================================
    # La senal del ritmo personal
    # ==================================================================

    def test_rotacion_3x3_no_genera_ausencias(self):
        """El caso que motivo todo el diseno.

        Una persona con turno 3 de trabajo y 3 de descanso, mientras el resto de
        la obra trabaja todos los dias. Sin esta defensa, sus tres dias de
        descanso serian tres faltas y se le descontarian tres dias de sueldo.
        """
        rest = self.crew[1]
        for offset in range(9, 0, -1):
            # Toda la cuadrilla trabaja siempre...
            self._crew_worked(self._day(offset), exclude=rest)
            # ...menos esta persona, que hace 3 y descansa 3.
            if (offset // 3) % 2 == 0:
                self._worked(rest, self._day(offset))

        self._scan()

        propuestas = self._absences(rest).filtered(lambda a: a.state == "proposed")
        self.assertFalse(
            propuestas,
            "Una rotacion 3x3 se propuso como ausencias: %s" % (
                propuestas.mapped("absence_date")),
        )

    def test_hueco_largo_no_se_propone(self):
        """Tres dias seguidos sin venir se parece a un descanso, no a faltar.

        Es el mismo caso que la rotacion 3x3, en chiquito: aunque toda la
        cuadrilla haya trabajado esos tres dias, un hueco largo no se acusa.
        """
        self._week(missing=(6, 5, 4))
        self._scan()

        propuestas = self._absences().filtered(lambda a: a.state == "proposed")
        self.assertFalse(
            propuestas,
            "Un hueco de tres dias se propuso como faltas: %s" % (
                propuestas.mapped("absence_date")),
        )

    # ==================================================================
    # Nada se descuenta solo
    # ==================================================================

    def test_nomina_solo_ve_lo_confirmado(self):
        """El contrato con el modulo de nomina.

        Una ausencia detectada pero no revisada NO puede llegar al pago. Es la
        regla que impide que un error del kiosco se convierta en un descuento.
        """
        self._week(missing=(5,))
        self._scan()

        detectadas = self._absences().filtered(lambda a: a.state == "proposed")
        self.assertTrue(detectadas, "Hacia falta al menos una detectada")

        datos = self.Absence.olive_confirmed_absences(
            self.target.id, self._day(10), self._day(1))
        self.assertEqual(
            datos["days"], 0,
            "Una ausencia sin revisar llego a la nomina. Eso descuenta dinero "
            "sin que nadie lo haya mirado.",
        )

        detectadas[0].action_confirm()
        datos = self.Absence.olive_confirmed_absences(
            self.target.id, self._day(10), self._day(1))
        self.assertEqual(datos["days"], 1)
        self.assertEqual(len(datos["dates"]), 1)

    def test_rechazada_no_se_descuenta(self):
        """Un dia marcado como "no era ausencia" vuelve a ser un dia cualquiera."""
        self._week(missing=(5,))
        self._scan()
        detectadas = self._absences().filtered(lambda a: a.state == "proposed")
        self.assertTrue(detectadas)
        detectadas[0].action_reject()

        datos = self.Absence.olive_confirmed_absences(
            self.target.id, self._day(10), self._day(1))
        self.assertEqual(datos["days"], 0)

    def test_marcaje_tardio_retira_la_propuesta(self):
        """El kiosco estuvo sin red; el marcaje llega despues.

        La ausencia propuesta tiene que desaparecer sola. Si no, se le
        descontaria un dia a alguien que si vino solo porque el internet fallo.
        """
        self._week(missing=(5,))
        self._scan()
        self.assertTrue(self._absences().filtered(lambda a: a.state == "proposed"))

        # Ahora si llega la constancia de que vino.
        self._worked(self.target, self._day(5))
        self._scan()

        self.assertFalse(
            self._absences().filtered(lambda a: a.absence_date == self._day(5)),
            "La propuesta sobrevivio a la prueba de que la persona si vino",
        )

    def test_revision_humana_sobrevive_al_barrido(self):
        """Una decision tomada por una persona no se borra sola."""
        self._week(missing=(5,))
        self._scan()
        proposed = self._absences().filtered(lambda a: a.state == "proposed")
        self.assertTrue(proposed)
        absence = proposed[0]
        absence.action_confirm()

        self._scan()
        self.assertTrue(absence.exists())
        self.assertEqual(absence.state, "confirmed")

    def test_hoy_no_se_evalua(self):
        """A media jornada todos parecen ausentes hasta que marcan."""
        today = fields.Date.context_today(self.Absence)
        self.Absence.sudo()._scan_period(today, today, company=self.company)
        self.assertFalse(self.Absence.sudo().search([
            ("company_id", "=", self.company.id), ("absence_date", "=", today)]))

    # ==================================================================
    # Quincena
    # ==================================================================

    def test_limites_de_quincena(self):
        bounds = self.Absence._quincena_bounds(date(2026, 8, 7))
        self.assertEqual(bounds, (date(2026, 8, 1), date(2026, 8, 15)))

        bounds = self.Absence._quincena_bounds(date(2026, 8, 20))
        self.assertEqual(bounds, (date(2026, 8, 16), date(2026, 8, 31)))

        # Febrero y fin de ano, que son donde fallan estas cuentas.
        bounds = self.Absence._quincena_bounds(date(2026, 2, 20))
        self.assertEqual(bounds, (date(2026, 2, 16), date(2026, 2, 28)))

        bounds = self.Absence._quincena_bounds(date(2026, 12, 20))
        self.assertEqual(bounds, (date(2026, 12, 16), date(2026, 12, 31)))
