# olive_hr_attendance_face

Kiosco de ingresos y egresos de empleados por reconocimiento facial para **Odoo 18**.

El reconocimiento corre **entero en el navegador**. Odoo solo entrega el material
(empleados, embeddings, pesos de los modelos, umbrales) y recibe los marcajes.
El servidor nunca ejecuta inferencia y no participa en el camino critico de un
marcaje, asi que el kiosco sigue funcionando con la red caida.

> **Odoo es despensa y buzon. La laptop es el cerebro.**

## Estado

En desarrollo. **Todavia no hay kiosco funcional**: la URL del dispositivo aun
no responde (llega en F3).

| Fase | Que | Estado |
|---|---|---|
| F0 | Esqueleto + banco de pruebas del pipeline | **hecho** |
| F1 | Modelo de datos y seguridad | **hecho** |
| F2 | Doblado de marcajes → `hr.attendance` | siguiente |
| F3 | Endpoints + kiosco offline (sin rostro) | **primer hito probable** |
| F4 | Pipeline de inferencia y las 5 guardas | pendiente |
| F5 | Enrolamiento + consentimiento | pendiente |
| F6 | Calibracion y endurecimiento | pendiente |
| F6.5 | Instalacion sin tecnico | pendiente |

## Montaje fisico

Laptop con la tapa cerrada dentro de la caseta (es la "computadorcita con
bateria": su bateria hace de UPS), conectada por HDMI a una pantalla sobre
**tripode contrapesado**, con la webcam encima a ~1.50 m y un aro de luz por
encima de la camara, inclinado hacia abajo.

Afuera solo hay pantalla, camara y luz: **ningun dispositivo de entrada**.
Cuando el rostro no se reconoce, la pantalla indica pasar con el guardia, que
registra la entrada a mano en Odoo — y el sistema nunca pisa un registro manual.

> El contrapeso en la base del tripode no es opcional: pantalla y aro arriba
> hacen un conjunto pesado en lo alto, en una entrada con transito.

## Modelos de IA

| Rol | Modelo | Tamano | Licencia |
|---|---|---|---|
| Deteccion + 5 puntos | YuNet 2023mar | 232 KB | MIT |
| Embedding 128-D | SFace 2021dec int8 | 9.9 MB | Apache-2.0 |
| Liveness pasivo | MiniFASNetV2 | ~1.9 MB | Apache-2.0 |

Se descarto InsightFace: su *codigo* es MIT pero **sus pesos son "non-commercial
research only"**, lo que exigiria licencia comercial para desplegarlo en un
cliente.

Los pesos viajan dentro del modulo (`static/lib/models/`, via Git LFS) y los
sirve Odoo. Estan en `static/` pero **fuera de todo bundle de assets**, asi que
no afectan el tiempo de carga del resto de la base de datos. Instalar en un
cliente nuevo es `git pull` + `-u olive_hr_attendance_face`, sin configurar
infraestructura.

## Banco de pruebas (F0)

Mide el pipeline completo en el equipo real. Responde la unica pregunta que
bloquea el proyecto: **¿alcanza 5 fps?**

```bash
cd bench
./run.sh          # sirve en http://localhost:8765 y abre Chrome
```

Presiona *Iniciar medicion*, dale permiso a la camara y ponete enfrente. Barre
varios tamanos de entrada y reporta fps, latencia por etapa, tasa de deteccion,
tamano del rostro en pixeles y luminancia.

Mide en **al menos dos equipos**, incluido el mas viejo que se vaya a usar:
interesa el piso, no el techo.

## Instalacion

```bash
# En el servidor Odoo 18
git clone <repo> /mnt/extra-addons/olive_hr_attendance_face
odoo -d <base> -i olive_hr_attendance_face --stop-after-init
```

### Fotos de identificacion

**Un registro = una foto + su vector.** La foto es la fuente de verdad; el
vector es un derivado recalculable. Eso hace que cambiar de modelo se resuelva
reprocesando las fotos guardadas, sin volver a llamar a nadie.

Tres procedencias, un solo flujo:

| Origen | Como entra |
|---|---|
| `avatar` | La foto de la ficha del empleado. Se toma sola al procesar. |
| `upload` | Se sube en **Fotos de identificacion**, como cualquier foto de Odoo. |
| `camera` | Se captura desde **Verificar identificacion** en la ficha. |
| `auto` | La aprende el kiosco con el uso (pendiente, F4). |

Flujo normal, sin enrolar a nadie a mano:

1. **Asistencias → Reconocimiento Facial → Procesar fotos.** Toma la foto de la
   ficha de cada empleado y le calcula el vector. 100 personas, unos segundos.
2. A quien falle (sin rostro, varias caras, rostro muy pequeno) se le reemplaza
   la foto y se vuelve a procesar.
3. En la ficha, **Verificar identificacion** abre la camara y muestra en vivo el
   puntaje contra las fotos guardadas. Es la comprobacion de que el sistema
   reconoce de verdad a esa persona.
4. Desde ahi mismo se agregan capturas con casco y lentes bajo la luz del
   kiosco, que **rinden mas que una foto de archivo**.

Sin consentimiento biometrico registrado se pueden guardar fotos, pero no
activarlas.

### Lo que todavia NO funciona

La URL del kiosco devuelve 404: el controller y la pagina publica llegan en F3.

## Convenciones

Siguen las de la suite hermana `odoo_nomina`: `# -*- coding: utf-8 -*-` en todo
`.py`, comillas dobles, docstrings y comentarios en espanol, `<list>` en vez de
`<tree>`. Modelos propios con prefijo `olive.attendance.*`, campos anadidos a
modelos nativos con prefijo `olive_`, metodos helper `_olive_*`.

Tests con `@tagged("post_install", "-at_install", "olive_face")`:

```bash
odoo -d <base> --test-enable --test-tags olive_face --stop-after-init
```
