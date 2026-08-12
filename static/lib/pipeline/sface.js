// SFace 2021dec (fp32): embedding facial de 128 dimensiones.
//
// Entrada : "data", NCHW float32 [1,3,112,112], BGR, rango 0-255 sin normalizar.
// Salida  : "fc1", [1,128]. (Se lee por outputNames[0], no por nombre fijo.)
//
// Es el par canonico de YuNet en el OpenCV Zoo: la alineacion de 5 puntos de
// YuNet es literalmente la entrada para la que SFace fue entrenado.

export const EMBEDDING_DIM = 128;

/** Convierte el recorte alineado de 112x112 en el tensor de entrada. */
export function preprocess(ort, alignedCanvas) {
    const ctx = alignedCanvas.getContext("2d", { willReadFrequently: true });
    const { data } = ctx.getImageData(0, 0, 112, 112);
    const plane = 112 * 112;
    const chw = new Float32Array(3 * plane);
    for (let i = 0, p = 0; p < plane; p++, i += 4) {
        chw[p] = data[i + 2];          // B
        chw[plane + p] = data[i + 1];  // G
        chw[2 * plane + p] = data[i];  // R
    }
    return new ort.Tensor("float32", chw, [1, 3, 112, 112]);
}

/**
 * Normaliza L2 el embedding.
 *
 * Se guarda siempre normalizado para que la similitud coseno se reduzca a un
 * producto punto: con 100 empleados x 3 plantillas son 300 productos punto de
 * 128 dimensiones por frame, coste despreciable frente a la inferencia.
 */
export function l2normalize(vec) {
    let norm = 0;
    for (let i = 0; i < vec.length; i++) {
        norm += vec[i] * vec[i];
    }
    norm = Math.sqrt(norm) || 1;
    const out = new Float32Array(vec.length);
    for (let i = 0; i < vec.length; i++) {
        out[i] = vec[i] / norm;
    }
    return out;
}

/** Similitud coseno entre dos embeddings ya normalizados. */
export function cosine(a, b) {
    let s = 0;
    for (let i = 0; i < a.length; i++) {
        s += a[i] * b[i];
    }
    return s;
}

/** Codifica un Float32Array como base64, tal como viaja en el sync (§2 del plan). */
export function encodeEmbedding(vec) {
    const bytes = new Uint8Array(vec.buffer, vec.byteOffset, vec.byteLength);
    let bin = "";
    for (let i = 0; i < bytes.length; i++) {
        bin += String.fromCharCode(bytes[i]);
    }
    return btoa(bin);
}
