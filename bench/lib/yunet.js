// YuNet 2023mar: deteccion de rostros + 5 puntos de referencia.
//
// Entrada  : "input", NCHW float32 [1,3,H,W], BGR, rango 0-255 SIN normalizar.
// Salidas  : cls_{8,16,32}  [1,N,1]  score de clasificacion
//            obj_{8,16,32}  [1,N,1]  objectness
//            bbox_{8,16,32} [1,N,4]  (dx, dy, log_w, log_h)
//            kps_{8,16,32}  [1,N,10] 5 puntos, (dx,dy) cada uno
//
// El decodificado replica el postproceso de OpenCV (FaceDetectorYN): las
// coordenadas vienen como desplazamiento respecto de la celda del mapa de
// caracteristicas, y el score final es la media geometrica de cls y obj.

const STRIDES = [8, 16, 32];

/** Prepara el tensor de entrada: redimensiona a inputW x inputH, pasa a BGR y a NCHW. */
export function preprocess(ort, sourceCanvas, inputW, inputH, scratchCanvas) {
    const ctx = scratchCanvas.getContext("2d", { willReadFrequently: true });
    scratchCanvas.width = inputW;
    scratchCanvas.height = inputH;
    ctx.drawImage(sourceCanvas, 0, 0, inputW, inputH);
    const { data } = ctx.getImageData(0, 0, inputW, inputH);

    const plane = inputW * inputH;
    const chw = new Float32Array(3 * plane);
    for (let i = 0, p = 0; p < plane; p++, i += 4) {
        // BGR, que es el orden con el que se entreno (convencion OpenCV).
        chw[p] = data[i + 2];
        chw[plane + p] = data[i + 1];
        chw[2 * plane + p] = data[i];
    }
    return new ort.Tensor("float32", chw, [1, 3, inputH, inputW]);
}

/** Decodifica las 12 salidas a una lista de rostros con bbox, score y 5 puntos. */
export function decode(outputs, inputW, inputH, scoreThreshold) {
    const faces = [];
    for (const stride of STRIDES) {
        const cls = outputs[`cls_${stride}`].data;
        const obj = outputs[`obj_${stride}`].data;
        const bbox = outputs[`bbox_${stride}`].data;
        const kps = outputs[`kps_${stride}`].data;

        const cols = Math.floor(inputW / stride);
        const rows = Math.floor(inputH / stride);

        for (let r = 0; r < rows; r++) {
            for (let c = 0; c < cols; c++) {
                const idx = r * cols + c;
                // Media geometrica de ambos cabezales, como hace OpenCV.
                const score = Math.sqrt(
                    Math.max(0, Math.min(1, cls[idx])) * Math.max(0, Math.min(1, obj[idx]))
                );
                if (score < scoreThreshold) {
                    continue;
                }
                const b = idx * 4;
                const cx = (c + bbox[b]) * stride;
                const cy = (r + bbox[b + 1]) * stride;
                const w = Math.exp(bbox[b + 2]) * stride;
                const h = Math.exp(bbox[b + 3]) * stride;

                const k = idx * 10;
                const landmarks = new Float32Array(10);
                for (let p = 0; p < 5; p++) {
                    landmarks[2 * p] = (c + kps[k + 2 * p]) * stride;
                    landmarks[2 * p + 1] = (r + kps[k + 2 * p + 1]) * stride;
                }
                faces.push({
                    x: cx - w / 2,
                    y: cy - h / 2,
                    w,
                    h,
                    score,
                    landmarks,
                });
            }
        }
    }
    return faces;
}

/** Supresion de no-maximos por IoU. */
export function nms(faces, iouThreshold = 0.3, topK = 20) {
    const sorted = faces.slice().sort((a, b) => b.score - a.score).slice(0, topK * 10);
    const kept = [];
    for (const cand of sorted) {
        let overlaps = false;
        for (const k of kept) {
            const x1 = Math.max(cand.x, k.x);
            const y1 = Math.max(cand.y, k.y);
            const x2 = Math.min(cand.x + cand.w, k.x + k.w);
            const y2 = Math.min(cand.y + cand.h, k.y + k.h);
            const inter = Math.max(0, x2 - x1) * Math.max(0, y2 - y1);
            const union = cand.w * cand.h + k.w * k.h - inter;
            if (union > 0 && inter / union > iouThreshold) {
                overlaps = true;
                break;
            }
        }
        if (!overlaps) {
            kept.push(cand);
            if (kept.length >= topK) {
                break;
            }
        }
    }
    return kept;
}

/** Reescala rostros del espacio de entrada del modelo al espacio del frame original. */
export function rescale(faces, inputW, inputH, frameW, frameH) {
    const sx = frameW / inputW;
    const sy = frameH / inputH;
    for (const f of faces) {
        f.x *= sx;
        f.y *= sy;
        f.w *= sx;
        f.h *= sy;
        for (let p = 0; p < 5; p++) {
            f.landmarks[2 * p] *= sx;
            f.landmarks[2 * p + 1] *= sy;
        }
    }
    return faces;
}
