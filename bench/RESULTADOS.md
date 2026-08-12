# F0 — Resultados del banco de pruebas

Mediciones reales, no estimaciones de catalogo. Cinco hallazgos cambiaron
decisiones que el plan daba por cerradas.

## Equipo de referencia

| | |
|---|---|
| CPU | Apple M4 Pro (14 nucleos) |
| RAM | 24 GB |
| Navegador | Chrome 151 |
| Runtime | ONNX Runtime Web 1.27.0, WASM SIMD, **1 hilo** |

> ⚠️ Un M4 Pro es de lo mas rapido que hay. **Una laptop de planta sera 3–5×
> mas lenta en WASM de un hilo.** Estos numeros son el techo, no el piso. Hay
> que repetir la medicion en el equipo real antes de cerrar F0.

## Hallazgo 1 — El export 2023mar de YuNet tiene entrada FIJA

```
yunet.onnx (2023mar)      entrada "input" shape=[1,3,640,640]   <- fija
yunet_2026may.onnx        entrada "input" shape=[1,3,H,W]       <- dinamica
```

El 2023mar rechaza cualquier tamano que no sea 640×640, lo que obliga a pagar
siempre la deteccion mas cara. **Se adopta el export 2026may.**

## Hallazgo 2 — El int8 es MAS LENTO que el fp32

| Embedder | Peso | ms/iter | fps |
|---|---|---|---|
| `sface_int8` | 9.9 MB | 109.9 | 9.1 |
| **`sface_fp32`** | 38.7 MB | **88.7** | **11.3** |

Contraintuitivo pero consistente: ORT WASM no tiene kernels int8 optimizados, y
el modelo cuantizado termina pagando dequantizacion. **Se adopta fp32.** El
costo es 29 MB mas de descarga, que se paga una sola vez y queda en Cache API;
la velocidad se cobra en cada marcaje.

## Hallazgo 3 — SFace no es un MobileFaceNet

El plan asumia "MobileFaceNet, ~4 MB, 40–80 ms". SFace fp32 pesa **38.7 MB**
(~10M parametros): es una red bastante mas grande, y por eso cuesta ~89 ms.
La suposicion del plan era incorrecta.

## Hallazgo 4 — La deteccion es barata; el embedding es todo el costo

| Entrada | Detector solo | Pipeline completo |
|---|---|---|
| 160 px | ~0.5 ms* | 121.3 ms |
| 224 px | 5.4 ms | 123.3 ms |
| 320 px | 9.0 ms | 126.8 ms |
| 480 px | 18.5 ms | 133.1 ms |
| 640 px | 33.1 ms | 149.8 ms |

<small>*por debajo de ~5 ms la medicion queda dominada por el ruido de arranque
del proceso.</small>

Bajar la resolucion de deteccion casi no mueve la aguja: el embedder domina.
**Conviene detectar a 320 px o mas** — mejor deteccion, coste marginal.

## Hallazgo 5 — La metrica de F0 estaba mal planteada

El plan pedia "≥5 fps de pipeline completo", lo que supone correr el embedder en
**cada frame**. El kiosco no necesita eso: la guarda 2 pide que la misma
identidad gane en **3 frames**, no que el embedder corra 30 veces por segundo.

Lo que le importa al usuario es **cuanto tarda desde que aparece su cara hasta
que el marcaje queda confirmado**:

```
tiempo_hasta_identificar = deteccion + 3 x (alineacion + embedding)
```

En el M4 Pro a 320 px: `9 + 3 x 89` ≈ **0.28 s**.
Extrapolado a una laptop de planta 4× mas lenta: ≈ **1.1 s**.

**Propuesta: reemplazar el criterio de F0 de "≥5 fps" por "≤1.5 s hasta
identificar".** Mide lo que la persona experimenta y no penaliza al diseno por
un costo que no va a pagar. El banco ya emite el veredicto con esta metrica.

## Verificaciones de correccion (prueba de humo)

Todas pasan en `smoke.html`:

- Las 12 salidas del detector coinciden con lo que espera el decodificador.
- Aritmetica de celdas correcta: `cls_8` = 1×1600×1 a 320 px = (320/8)².
- **Alineacion verificada numericamente: error maximo 1.03 px** al mapear los 5
  puntos sobre las referencias de ArcFace. Si esto fallara, SFace recibiria
  recortes torcidos y nada mas lo delataria.
- Embedding de 128 dimensiones, norma L2 = 1.000000.
- Base64 de 684 caracteres, exactamente lo que preve el campo del modelo.

## Bug encontrado y corregido

ORT resuelve `wasmPaths` relativo a la ubicacion de **su propio script**, no a
la del documento. Con una ruta relativa buscaba en `vendor/vendor/` y no
levantaba ningun backend. Se corrigio pasando siempre una URL absoluta
(`lib/ort-loader.js`).

## Pendiente

- **Medir en la laptop de planta real.** Es lo que decide F0 de verdad.
- **Medicion interactiva con caras reales** (`index.html`): tasa de deteccion,
  tamano del rostro en pixeles a distancia de uso, luminancia.
- **Liveness**: MiniFASNetV2 no tiene un ONNX canonico publicado; el repositorio
  original distribuye `.pth` de PyTorch. Hay que resolver la conversion antes de
  F4, y su costo se suma al presupuesto de tiempo medido aqui.

## Reproducir

```bash
cd bench
./fetch-models.sh          # baja pesos y runtime a vendor/
./run.sh                   # medicion interactiva con camara

# verificaciones sin camara
python3 -m http.server 8765 --bind 127.0.0.1 &
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless \
  --disable-gpu --virtual-time-budget=300000 --dump-dom \
  http://localhost:8765/smoke.html
N=80 ./measure.sh          # coste por reloj de pared
```
