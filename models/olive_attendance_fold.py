# -*- coding: utf-8 -*-
"""El doblado: de marcajes crudos a `hr.attendance`.

**La pieza de mayor riesgo del proyecto.** Aqui es donde una cola offline que
llega tarde y desordenada se convierte en asistencias validas sin romper nada.

El problema que resuelve
------------------------
`hr.attendance._check_validity` rechaza con ValidationError tres cosas:
asistencias solapadas, una segunda asistencia abierta del mismo empleado, y
asistencias intermedias. Insertar marcaje a marcaje contra esas restricciones
funciona mientras todo llegue en orden y a tiempo — es decir, mientras no pase
justo lo que este sistema esta disenado para soportar. Un kiosco que estuvo tres
dias sin red manda marcajes viejos que caen ENTRE asistencias ya escritas, y esa
insercion falla siempre, reintenta siempre, y la cola se atora para siempre.

La solucion: **no se inserta marcaje a marcaje. Se reconstruye por completo la
ventana (empleado, jornada) afectada.**

`olive.attendance.punch` es la verdad; `hr.attendance` es una proyeccion
derivada y reconstruible. Llega un marcaje tardio de hace tres dias, se tira
todo lo que el kiosco habia escrito de ese dia y se vuelve a construir desde
cero con la informacion completa. El resultado no depende del orden de llegada,
que es exactamente la propiedad que hace falta.

Lo que NUNCA se toca
--------------------
Toda `hr.attendance` que no provenga de un marcaje es un **bloque inmutable**:
la registro RRHH a mano, la importo alguien, o salio del kiosco nativo de Odoo.
El doblado jamas la modifica ni la borra. Si un marcaje contradice un bloque
inmutable, el marcaje se rechaza y se escala a una persona — **el trabajo manual
nunca se pisa y la contradiccion no se resuelve en silencio.**

Esto importa mas de lo que parece: el respaldo del kiosco ante un no
reconocimiento es que el guardia registre la entrada a mano. Ese registro manual
tiene que sobrevivir a cualquier doblado posterior, o el respaldo no sirve.
"""

import logging
from datetime import datetime, time, timedelta

from pytz import timezone, utc

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)

# Espacio de nombres del bloqueo de Postgres. Un entero cualquiera, pero fijo:
# lo unico que importa es que no colisione con otro modulo que use bloqueos de
# aviso sobre el mismo id de empleado.
ADVISORY_LOCK_NS = 831171

# Estados de marcaje que participan en una reconstruccion. Los `applied` entran
# a proposito: sin ellos no se puede reconstruir la secuencia completa del dia,
# solo el pedazo que acaba de llegar.
FOLDABLE_STATES = ("queued", "applied")


class OliveAttendancePunch(models.Model):
    _inherit = "olive.attendance.punch"

    # ==================================================================
    # Entrada
    # ==================================================================

    @api.model
    def _cron_fold_pending(self, limit=500):
        """Punto de entrada del cron."""
        return self._fold_pending(limit=limit)

    @api.model
    def _fold_pending(self, punch_ids=None, limit=500):
        """Doblado de los marcajes indicados, o de todo lo pendiente.

        Devuelve un resumen con lo que hizo. Cada ambito se procesa en su propia
        transaccion (savepoint): un dia que falle no debe arrastrarse al resto.
        """
        if punch_ids:
            punches = self.browse(punch_ids).exists()
        else:
            punches = self.search(
                [("state", "=", "queued")], limit=limit, order="punch_time asc, id asc"
            )
        if not punches:
            return {"scopes": 0, "applied": 0, "rejected": 0}

        summary = {"scopes": 0, "applied": 0, "rejected": 0, "errors": 0}

        # Los que ni siquiera son candidatos se apartan antes de armar ambitos.
        summary["rejected"] += len(self._quarantine_unfoldable(punches))

        scopes = self._build_scopes(punches.filtered(lambda p: p.state == "queued"))
        for (employee, dt_from, dt_to) in scopes:
            summary["scopes"] += 1
            try:
                with self.env.cr.savepoint():
                    result = self._fold_scope(employee, dt_from, dt_to)
                summary["applied"] += result["applied"]
                summary["rejected"] += result["rejected"]
            except Exception:  # noqa: BLE001 - un ambito roto no frena los demas
                summary["errors"] += 1
                _logger.exception(
                    "Fallo el doblado de %s entre %s y %s", employee.display_name,
                    dt_from, dt_to,
                )
                self._mark_scope_error(employee, dt_from, dt_to)
        _logger.info("Doblado: %s", summary)
        return summary

    def _quarantine_unfoldable(self, punches):
        """Aparta lo que no se puede doblar, con el motivo escrito.

        No son errores del sistema sino hechos que necesitan a una persona.
        Dejarlos en la cola los haria reintentar para siempre.
        """
        quarantined = self.browse()

        # Sin empleado: la identificacion fallo. El marcaje se conserva como
        # evidencia y un gestor decide a quien corresponde, si es que a alguien.
        orphans = punches.filtered(lambda p: p.state == "queued" and not p.employee_id)
        if orphans:
            orphans.write({
                "state": "rejected", "review_state": "pending",
                "error_message": _("Marcaje sin empleado identificado."),
            })
            quarantined |= orphans
            for punch in orphans:
                # `attendance_recorded=False`: es el unico caso donde de verdad
                # se pierde una presencia, porque no se sabe de quien es. Por eso
                # entra como grave.
                self._record_punch_anomaly(
                    punch, "unidentified",
                    _("El kiosco no logro identificar a la persona."),
                    _("No se registro ninguna asistencia: no se sabe a quien "
                      "corresponde. Hay que asignarle un empleado a mano."),
                    attendance_recorded=False,
                )

        # Reloj inservible: si murio la pila del CMOS, la hora es basura y no
        # puede llegar a la nomina disfrazada de dato.
        broken_clock = punches.filtered(
            lambda p: p.state == "queued" and p.clock_confidence == "unreliable"
        )
        if broken_clock:
            broken_clock.write({
                "state": "rejected", "review_state": "pending",
                "error_message": _(
                    "El reloj del equipo no era confiable en este marcaje; la "
                    "hora no se puede usar para calcular asistencia."
                ),
            })
            quarantined |= broken_clock
            for punch in broken_clock:
                self._record_punch_anomaly(
                    punch, "clock_unreliable",
                    _("El reloj del equipo estaba mal cuando se marco."),
                    _("No se registro asistencia: no se sabe ni siquiera de que "
                      "dia es. La hora cruda queda guardada como evidencia."),
                    attendance_recorded=False,
                )
        return quarantined

    def _record_punch_anomaly(self, punch, kind, detail, resolution,
                              attendance_recorded=True):
        """Incidencia atada a un marcaje suelto, sin jornada reconstruida."""
        company = punch.company_id or self.env.company
        tz = timezone(
            (punch.employee_id and punch.employee_id.tz)
            or company.partner_id.tz or "UTC"
        )
        cutoff = company.olive_face_day_cutoff_hour or 0.0
        self.env["olive.attendance.anomaly"]._record(
            punch.employee_id or None,
            self._jornada_date(punch.punch_time, tz, cutoff),
            kind, detail, resolution,
            punches=punch, key=punch.uuid,
            attendance_recorded=attendance_recorded,
        )

    # ==================================================================
    # Paso 0 — ambitos
    # ==================================================================

    @api.model
    def _build_scopes(self, punches):
        """Ventanas (empleado, desde, hasta) que hay que reconstruir.

        La jornada se calcula en la zona horaria local y desplazada por el corte
        de jornada, y despues se expande +-`max_shift_hours`. Sin esa expansion
        un turno de 22:00 a 06:00 quedaria partido en dos jornadas y se
        reconstruiria mal cada vez.
        """
        raw = []
        for employee, group in self._group_by_employee(punches).items():
            params = self._fold_params(employee)
            tz = self._employee_tz(employee)
            margin = timedelta(hours=params["max_shift_hours"])
            for punch in group:
                start, end = self._day_window(punch.punch_time, tz, params["cutoff_hour"])
                raw.append((employee, start - margin, end + margin))

        # Ambitos solapados del mismo empleado se fusionan: reconstruir dos
        # veces la misma franja es trabajo doble, y peor, la segunda pasada
        # veria el resultado de la primera.
        merged = []
        for employee in {r[0] for r in raw}:
            windows = sorted([(r[1], r[2]) for r in raw if r[0] == employee])
            current_start, current_end = windows[0]
            for start, end in windows[1:]:
                if start <= current_end:
                    current_end = max(current_end, end)
                else:
                    merged.append((employee, current_start, current_end))
                    current_start, current_end = start, end
            merged.append((employee, current_start, current_end))
        return merged

    def _group_by_employee(self, punches):
        grouped = {}
        for punch in punches:
            grouped.setdefault(punch.employee_id, self.browse())
            grouped[punch.employee_id] |= punch
        return grouped

    def _day_window(self, moment, tz, cutoff_hour):
        """Inicio y fin (UTC ingenuo) de la jornada local que contiene `moment`."""
        local = utc.localize(moment).astimezone(tz)
        shifted = local - timedelta(hours=cutoff_hour)
        start_local = tz.localize(
            datetime.combine(shifted.date(), time.min)
        ) + timedelta(hours=cutoff_hour)
        end_local = start_local + timedelta(days=1)
        return (
            start_local.astimezone(utc).replace(tzinfo=None),
            end_local.astimezone(utc).replace(tzinfo=None),
        )

    def _employee_tz(self, employee):
        name = employee.tz or employee.company_id.partner_id.tz or "UTC"
        return timezone(name)

    def _fold_params(self, employee):
        company = employee.company_id or self.env.company
        return {
            "toggle_gap": timedelta(seconds=company.olive_face_toggle_gap_seconds or 0),
            "min_session": timedelta(
                minutes=company.olive_face_min_session_minutes or 0),
            "max_shift_hours": company.olive_face_max_shift_hours or 16.0,
            "max_shift": timedelta(hours=company.olive_face_max_shift_hours or 16.0),
            "cutoff_hour": company.olive_face_day_cutoff_hour or 0.0,
            "protect_validated": company.olive_face_protect_validated,
            "presence_first": company.olive_face_presence_first,
            "expected_min": timedelta(hours=company.olive_face_expected_min_hours or 0),
            "expected_max": timedelta(
                hours=company.olive_face_expected_max_hours or 24),
        }

    def _jornada_date(self, moment, tz, cutoff_hour):
        """Fecha local de la jornada a la que pertenece un instante."""
        local = utc.localize(moment).astimezone(tz)
        return (local - timedelta(hours=cutoff_hour)).date()

    # ==================================================================
    # Reconstruccion de un ambito
    # ==================================================================

    def _fold_scope(self, employee, dt_from, dt_to):
        """Reconstruye por completo una ventana (empleado, jornada)."""
        # Paso 1 — bloqueo. El cron y el doblado en linea pueden coincidir sobre
        # el mismo empleado, y _check_validity lee y valida pero no bloquea: sin
        # esto, dos procesos concurrentes ven cada uno un mundo consistente y
        # juntos escriben uno que no lo es.
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)", (ADVISORY_LOCK_NS, employee.id)
        )

        params = self._fold_params(employee)
        tz = self._employee_tz(employee)
        punches, managed, immutable = self._collect_events(
            employee, dt_from, dt_to, params)

        # Las incidencias abiertas son derivadas, igual que las asistencias: si
        # la jornada se rehace, se recalculan. Las ya revisadas se conservan —
        # son decisiones humanas y no se borran solas.
        Anomaly = self.env["olive.attendance.anomaly"]
        Anomaly._clear_open(
            employee,
            self._jornada_date(dt_from, tz, params["cutoff_hour"]),
            self._jornada_date(dt_to, tz, params["cutoff_hour"]),
        )

        pairs, rejected, notes = self._normalize_sequence(punches, params)
        result = self._apply_pairs(employee, pairs, managed, immutable, params)

        # Cada incidencia queda enlazada a la asistencia que produjo, para que
        # desde el portal se salte directo al registro afectado.
        by_uuid = {
            pair["in_punch"].uuid: pair.get("attendance")
            for pair in pairs if pair.get("in_punch")
        }
        for note in notes:
            Anomaly._record(
                employee,
                self._jornada_date(note["moment"], tz, params["cutoff_hour"]),
                note["kind"], note["detail"], note["resolution"],
                punches=note.get("punches"), key=note.get("key", ""),
                attendance=by_uuid.get(note.get("key")),
            )

        if rejected:
            rejected.write({
                "state": "rejected", "review_state": "pending",
                "error_message": _(
                    "Salida sin entrada previa. No se inventa una entrada: "
                    "hace falta que alguien decida la hora real."
                ),
            })
            result["rejected"] += len(rejected)
        return result

    def _collect_events(self, employee, dt_from, dt_to, params):
        """Los tres conjuntos que deciden todo: marcajes, propias, ajenas."""
        dt_from, dt_to = self._widen_to_whole_attendances(employee, dt_from, dt_to)

        punches = self.sudo().search([
            ("employee_id", "=", employee.id),
            ("punch_time", ">=", dt_from),
            ("punch_time", "<", dt_to),
            ("state", "in", FOLDABLE_STATES),
            ("clock_confidence", "!=", "unreliable"),
        ], order="punch_time asc, id asc")

        attendances = self.env["hr.attendance"].sudo().search([
            ("employee_id", "=", employee.id),
            ("check_in", "<", dt_to),
            "|", ("check_out", "=", False), ("check_out", ">", dt_from),
        ])
        managed = attendances.filtered(lambda a: a.olive_punch_ids)
        immutable = attendances - managed

        # Las horas extra aprobadas se protegen aunque hayan nacido del kiosco:
        # alguien ya las reviso y las valido, y reconstruirlas borraria esa
        # decision humana.
        if params["protect_validated"]:
            validated = managed.filtered(lambda a: self._is_validated(a))
            if validated:
                managed -= validated
                immutable |= validated
        return punches, managed, immutable

    def _widen_to_whole_attendances(self, employee, dt_from, dt_to):
        """Amplia la ventana hasta contener ENTERA toda asistencia propia que cruce.

        Sin esto hay un fallo silencioso y grave. La ventana se calcula como
        jornada +-jornada maxima, asi que su borde cae en un punto arbitrario y
        puede partir por la mitad una asistencia de otro dia: entraria su
        marcaje de entrada pero no el de salida, y esa asistencia se
        reconstruiria como si la persona nunca hubiera salido.

        Se amplia solo hasta cubrir las asistencias que cruzan el borde, no una
        jornada mas: las asistencias no se encadenan entre si, asi que esto
        converge en una o dos vueltas en vez de arrastrar el historial entero.
        """
        Attendance = self.env["hr.attendance"].sudo()
        second = timedelta(seconds=1)
        for _round in range(5):
            straddling = Attendance.search([
                ("employee_id", "=", employee.id),
                ("check_in", "<", dt_to),
                "|", ("check_out", "=", False), ("check_out", ">", dt_from),
            ]).filtered(lambda a: a.olive_punch_ids)
            new_from = min([dt_from] + [a.check_in for a in straddling])
            new_to = max(
                [dt_to] + [a.check_out + second for a in straddling if a.check_out]
            )
            if new_from >= dt_from and new_to <= dt_to:
                return dt_from, dt_to
            dt_from, dt_to = min(dt_from, new_from), max(dt_to, new_to)
        _logger.warning(
            "La ventana de doblado de %s no convergio; se usa la ultima.",
            employee.display_name,
        )
        return dt_from, dt_to

    def _is_validated(self, attendance):
        """Si la asistencia tiene horas extra ya aprobadas.

        El campo cambia entre versiones y ediciones de Odoo, asi que se consulta
        con cuidado en vez de asumir que existe.
        """
        for field_name in ("validated_overtime_hours", "overtime_status"):
            if field_name in attendance._fields:
                value = attendance[field_name]
                if field_name == "overtime_status":
                    return value == "approved"
                return bool(value)
        return False

    # ==================================================================
    # Paso 3 — normalizar la secuencia
    # ==================================================================

    def _normalize_sequence(self, punches, params):
        """Convierte una lista de marcajes en pares entrada/salida.

        Es donde se tratan los casos degenerados. Ninguno es teorico: todos
        pasan en una planta real en la primera semana.
        """
        notes = []
        events = []
        last_kept = None
        bursts = self.browse()
        for punch in punches:
            # Colapso de rafaga: red de seguridad del lado servidor para la
            # guarda de cooldown del kiosco. Si el kiosco fallo, o si dos
            # kioscos vieran a la misma persona, aqui se corrige.
            if last_kept and (punch.punch_time - last_kept) < params["toggle_gap"]:
                bursts |= punch
                continue
            last_kept = punch.punch_time
            events.append({
                "punch": punch,
                "time": punch.punch_time,
                "direction": punch.direction,
            })
        if bursts:
            bursts.write({
                "state": "duplicate",
                "error_message": _("Colapsado por cercania con el marcaje anterior."),
            })
            notes.append({
                "kind": "burst", "moment": bursts[0].punch_time, "punches": bursts,
                "detail": _("%s marcaje(s) a segundos del anterior.", len(bursts)),
                "resolution": _("Se descartaron por repetidos. No afectan la jornada."),
            })

        pairs = []
        rejected = self.browse()
        repeats = self.browse()
        open_pair = None
        for event in events:
            direction = event["direction"]
            if direction == "auto":
                # Por alternancia: quien esta adentro sale, quien esta afuera entra.
                direction = "out" if open_pair else "in"

            if direction == "in":
                if open_pair:
                    # Entrada sobre entrada: se olvido de marcar la salida. Se
                    # cierra la anterior lo mas tarde posible sin solaparse con
                    # esta, y queda marcada como anomalia para que alguien la vea.
                    limit = open_pair["check_in"] + params["max_shift"]
                    open_pair["check_out"] = min(limit, event["time"])
                    open_pair["anomaly"] = "missing_out"
                    notes.append({
                        "kind": "missing_out", "moment": open_pair["check_in"],
                        "punches": open_pair["in_punch"],
                        "key": open_pair["in_punch"].uuid,
                        "detail": _(
                            "Entro y volvio a entrar sin marcar la salida "
                            "intermedia."),
                        "resolution": _(
                            "Se registro la presencia y se cerro al comenzar la "
                            "siguiente. La hora de salida real se desconoce."),
                    })
                    pairs.append(open_pair)
                open_pair = {
                    "check_in": event["time"], "in_punch": event["punch"],
                    "check_out": None, "out_punch": None, "anomaly": None,
                }
            else:
                if not open_pair:
                    # Salida sin entrada. Alguien marco al salir pero su entrada
                    # nunca llego —el kiosco no lo reconocio al entrar, o su
                    # marcaje se perdio—.
                    #
                    # La hora de entrada es imposible de saber, pero la
                    # PRESENCIA es un hecho: esa persona estuvo ahi. Y la
                    # presencia es justamente lo que importa, porque la nomina
                    # asume asistencia y descuenta por ausencias. Descartar el
                    # marcaje por no conocer una hora seria tirar el dato
                    # valioso por no tener el accesorio.
                    if not params["presence_first"]:
                        rejected |= event["punch"]
                        continue
                    pairs.append({
                        "check_in": event["time"],
                        "check_out": event["time"] + timedelta(minutes=1),
                        "in_punch": event["punch"], "out_punch": None,
                        "anomaly": "missing_out", "synthetic": True,
                    })
                    notes.append({
                        "kind": "orphan_out", "moment": event["time"],
                        "punches": event["punch"], "key": event["punch"].uuid,
                        "detail": _(
                            "Marco la salida sin que existiera una entrada previa."),
                        "resolution": _(
                            "Se dejo constancia de la presencia con duracion "
                            "simbolica. La hora de entrada real se desconoce y "
                            "hay que corregirla a mano si importa."),
                    })
                    continue

                # Nadie entra y sale en diez minutos. Un par asi no es una
                # jornada: es alguien que marco, no vio confirmacion, y volvio a
                # marcar. Tratarlo como salida es el error MAS CARO del sistema
                # —convierte un dia entero en tres minutos trabajados— asi que
                # el segundo marcaje se colapsa y la entrada sigue abierta,
                # esperando la salida de verdad.
                if event["time"] - open_pair["check_in"] < params["min_session"]:
                    repeats |= event["punch"]
                    notes.append({
                        "kind": "repeated_punch", "moment": event["time"],
                        "punches": event["punch"], "key": event["punch"].uuid,
                        "detail": _(
                            "Volvio a marcar a los %s minutos de haber marcado.",
                            int((event["time"] - open_pair["check_in"])
                                .total_seconds() // 60)),
                        "resolution": _(
                            "Se tomo como repeticion, no como salida. La jornada "
                            "sigue abierta esperando la salida real."),
                    })
                    continue

                open_pair["check_out"] = event["time"]
                open_pair["out_punch"] = event["punch"]
                pairs.append(open_pair)
                open_pair = None

        if open_pair:
            # Una asistencia abierta es legitima: la persona sigue adentro. Pero
            # si lleva mas que la jornada maxima, se cierra a la fuerza.
            #
            # ESTE es el punto exacto que impide que el caso (b) de
            # _check_validity —una segunda asistencia abierta— atore la cola
            # para siempre. Sin cierre forzado, un turno que nunca marco salida
            # bloquea todos los marcajes futuros de esa persona.
            elapsed = fields.Datetime.now() - open_pair["check_in"]
            if elapsed > params["max_shift"]:
                open_pair["check_out"] = open_pair["check_in"] + params["max_shift"]
                open_pair["anomaly"] = "forced_close"
                notes.append({
                    "kind": "forced_close", "moment": open_pair["check_in"],
                    "punches": open_pair["in_punch"],
                    "key": open_pair["in_punch"].uuid,
                    "detail": _("Entro y nunca marco la salida."),
                    "resolution": _(
                        "Se cerro a la fuerza a las %s horas. La presencia queda "
                        "registrada; la hora de salida es inventada y no debe "
                        "usarse para calcular horas.",
                        params["max_shift"].total_seconds() / 3600),
                })
            pairs.append(open_pair)

        if repeats:
            repeats.write({
                "state": "duplicate",
                "error_message": _(
                    "Marcaje repetido: la persona ya habia marcado hace muy poco. "
                    "Se descarta para no partir la jornada en dos."
                ),
            })

        # Un numero impar de marcajes significa que falta uno. Es ambiguo por
        # naturaleza —no hay forma de saber cual falta— asi que no se resuelve,
        # se senala.
        if len(events) % 2 == 1:
            notes.append({
                "kind": "odd_count", "moment": events[0]["time"],
                "punches": self.browse([e["punch"].id for e in events]),
                "detail": _("%s marcajes en la jornada: falta uno.", len(events)),
                "resolution": _(
                    "La asistencia quedo registrada, pero alguna de sus horas "
                    "es una suposicion del sistema."),
            })

        # Jornadas de duracion rara. No bloquean nada; solo se marcan.
        for pair in pairs:
            if pair.get("synthetic") or not pair["check_out"]:
                continue
            duration = pair["check_out"] - pair["check_in"]
            kind = None
            if duration < params["expected_min"]:
                kind = "short_session"
            elif duration > params["expected_max"]:
                kind = "long_session"
            if kind:
                notes.append({
                    "kind": kind, "moment": pair["check_in"],
                    "punches": pair["in_punch"], "key": pair["in_punch"].uuid,
                    "detail": _(
                        "Jornada de %.1f horas.",
                        duration.total_seconds() / 3600),
                    "resolution": _("Se registro tal cual. Solo se marca por rara."),
                })

        return pairs, rejected, notes

    # ==================================================================
    # Paso 4 — escribir (el orden importa)
    # ==================================================================

    def _apply_pairs(self, employee, pairs, managed, immutable, params):
        """Lleva los pares deseados a `hr.attendance`.

        El orden de las operaciones no es una preferencia de estilo: es lo que
        satisface las tres restricciones del core a la vez.
        """
        Attendance = self.env["hr.attendance"].sudo()
        result = {"applied": 0, "rejected": 0}

        # Emparejar deseado contra actual POR UUID del marcaje de entrada, no
        # por posicion. Por posicion, un marcaje tardio que se inserta al
        # principio desplazaria todo y forzaria a reescribir el dia entero.
        existing = {}
        for attendance in managed:
            in_punch = attendance.olive_punch_ids.filtered(
                lambda p: p.attendance_field == "check_in"
            )[:1]
            if in_punch:
                existing[in_punch.uuid] = attendance

        unchanged = self.env["hr.attendance"].sudo().browse()
        to_create = []
        for pair in pairs:
            attendance = existing.get(pair["in_punch"].uuid)
            # Odoo devuelve False para un datetime vacio y el par usa None: sin
            # normalizar, una asistencia abierta jamas se reconoceria como igual
            # y se reescribiria en cada pasada.
            same_out = (attendance.check_out or None) == (pair["check_out"] or None) \
                if attendance else False
            if attendance and attendance.check_in == pair["check_in"] and same_out:
                unchanged |= attendance
                pair["attendance"] = attendance
                continue
            pair["previous"] = attendance
            to_create.append(pair)

        # Conflicto con bloque inmutable: no se escribe nada de ese par. El
        # trabajo manual gana siempre y la contradiccion se escala.
        blocked = [p for p in to_create if self._conflicts(p, immutable)]
        if blocked:
            result["rejected"] += self._escalate_conflict(employee, blocked)
            # Por identidad, no por igualdad: son diccionarios, y dos pares
            # distintos con los mismos valores compararian iguales.
            blocked_ids = {id(p) for p in blocked}
            to_create = [p for p in to_create if id(p) not in blocked_ids]

        # PRIMERO borrar, DESPUES crear. Al reves, la creacion chocaria contra
        # las asistencias que estan a punto de desaparecer.
        obsolete = managed - unchanged
        if obsolete:
            obsolete.olive_punch_ids.sudo().write({
                "attendance_id": False, "attendance_field": False,
            })
            obsolete.unlink()

        # Y crear en orden cronologico ascendente, con check_out ya incluido en
        # el mismo create. Crear abierta y cerrarla con un write posterior es
        # exactamente el patron que dispara la restriccion de "segunda
        # asistencia abierta". Como solo el ultimo par puede quedar abierto, el
        # orden ascendente garantiza que nunca haya dos abiertas a la vez.
        for pair in sorted(to_create, key=lambda p: p["check_in"]):
            values = {
                "employee_id": employee.id,
                "check_in": pair["check_in"],
                "check_out": pair["check_out"],
                "olive_anomaly": pair["anomaly"],
            }
            if pair.get("previous"):
                values["olive_rebuilt_count"] = pair["previous"].olive_rebuilt_count + 1
            attendance = Attendance.create(values)
            pair["attendance"] = attendance

            pair["in_punch"].sudo().write({
                "state": "applied", "attendance_id": attendance.id,
                "attendance_field": "check_in", "error_message": False,
                "fold_attempts": pair["in_punch"].fold_attempts + 1,
            })
            if pair["out_punch"]:
                pair["out_punch"].sudo().write({
                    "state": "applied", "attendance_id": attendance.id,
                    "attendance_field": "check_out", "error_message": False,
                    "fold_attempts": pair["out_punch"].fold_attempts + 1,
                })
            result["applied"] += 1
        return result

    def _conflicts(self, pair, immutable):
        """Si el par pisaria una asistencia que el doblado no puede tocar."""
        far_future = datetime.max
        start = pair["check_in"]
        end = pair["check_out"] or far_future
        for attendance in immutable:
            other_start = attendance.check_in
            other_end = attendance.check_out or far_future
            if start < other_end and other_start < end:
                return True
        return False

    def _escalate_conflict(self, employee, blocked):
        """Rechaza los marcajes en conflicto y le avisa a una persona.

        Devuelve cuantos marcajes quedaron rechazados.
        """
        punches = self.browse()
        for pair in blocked:
            punches |= pair["in_punch"]
            if pair["out_punch"]:
                punches |= pair["out_punch"]
        punches.sudo().write({
            "state": "rejected", "review_state": "pending",
            "error_message": _(
                "Choca con una asistencia registrada fuera del kiosco. No se "
                "modifica el registro manual: hace falta decidir cual de los dos "
                "es correcto."
            ),
        })
        params = self._fold_params(employee)
        tz = self._employee_tz(employee)
        for pair in blocked:
            self.env["olive.attendance.anomaly"]._record(
                employee,
                self._jornada_date(pair["check_in"], tz, params["cutoff_hour"]),
                "immutable_conflict",
                _("El kiosco registro marcajes en un horario que ya tenia una "
                  "asistencia cargada a mano."),
                # La presencia no se pierde: la asistencia manual ya la
                # acredita. Lo que se pierde es la hora exacta del kiosco.
                _("No se toco el registro manual. Los marcajes del kiosco se "
                  "descartaron; hay que decidir cual de los dos horarios vale."),
                punches=pair["in_punch"] | (pair["out_punch"] or self.browse()),
                key=pair["in_punch"].uuid,
            )
        self._notify_manager(employee, _(
            "Marcajes del kiosco en conflicto con asistencias registradas a mano "
            "para %s. Requieren decision manual.", employee.display_name,
        ))
        return len(punches)

    def _notify_manager(self, employee, body):
        """Crea la actividad para el gestor de asistencia facial."""
        group = self.env.ref(
            "olive_hr_attendance_face.group_face_manager", raise_if_not_found=False)
        user = group.users[:1] if group else self.env["res.users"]
        if not user:
            _logger.warning("Sin gestor de asistencia facial: %s", body)
            return
        try:
            employee.sudo().activity_schedule(
                "mail.mail_activity_data_todo", user_id=user.id, note=body,
            )
        except Exception:  # noqa: BLE001 - avisar nunca debe tumbar el doblado
            _logger.warning("No se pudo crear la actividad de aviso: %s", body)

    def _mark_scope_error(self, employee, dt_from, dt_to):
        """Deja rastro del fallo sin dejar los marcajes reintentando a ciegas.

        Se hace en su propia transaccion porque la del ambito ya se deshizo.
        """
        punches = self.sudo().search([
            ("employee_id", "=", employee.id),
            ("punch_time", ">=", dt_from), ("punch_time", "<", dt_to),
            ("state", "=", "queued"),
        ])
        for punch in punches:
            attempts = punch.fold_attempts + 1
            values = {"fold_attempts": attempts}
            # Tras varios intentos fallidos deja de ser un problema transitorio.
            if attempts >= 3:
                values.update({
                    "state": "error", "review_state": "pending",
                    "error_message": _("El doblado fallo %s veces seguidas.", attempts),
                })
            punch.write(values)
