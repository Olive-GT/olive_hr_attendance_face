// Pipeline de reconocimiento facial. Compartido por el enrolamiento (backend) y
// el kiosco: si fueran dos implementaciones distintas, los embeddings de captura
// y de reconocimiento dejarian de ser comparables y nadie sabria por que.
//
// Vive FUERA de los bundles de assets de Odoo, a proposito. Se carga bajo demanda
// con import() dinamico: meter ~50 MB de modelos y el runtime WASM en
// web.assets_backend le arruinaria el tiempo de carga a toda la base de datos,
// incluida la gente que no usa asistencia facial.

import * as yunet from "./yunet.js";
import * as align from "./align.js";
import * as sface from "./sface.js";

export { yunet, align, sface };

const state = {
    ort: null,
    profile: null,
    detSession: null,
    embSession: null,
    scratch: null,
    aligned: null,
};

/** Carga el script UMD de ONNX Runtime una sola vez. */
function loadOrtScript(url) {
    if (window.ort) {
        return Promise.resolve(window.ort);
    }
    if (loadOrtScript._pending) {
        return loadOrtScript._pending;
    }
    loadOrtScript._pending = new Promise((resolve, reject) => {
        const script = document.createElement("script");
        script.src = url;
        script.onload = () => resolve(window.ort);
        script.onerror = () => reject(new Error(`No se pudo cargar ONNX Runtime desde ${url}`));
        document.head.appendChild(script);
    });
    return loadOrtScript._pending;
}

/** true si el navegador esta en contexto seguro (HTTPS o localhost). */
export function isSecureContext() {
    return typeof window !== "undefined" && window.isSecureContext === true;
}

/**
 * Descarga un modelo verificando su hash, con Cache API de por medio.
 *
 * La Cache API y crypto.subtle SOLO existen en contexto seguro. Sin HTTPS
 * ambas son undefined, pero eso no impide procesar fotos: el cache es una
 * optimizacion y la verificacion de hash una defensa. Se degrada en vez de
 * fallar, porque procesar fotos no necesita camara y deberia funcionar
 * mientras se resuelve el certificado.
 */
async function fetchModel(url, expectedSha256, onProgress) {
    const cache = typeof caches !== "undefined"
        ? await caches.open("olive-face-models-v1").catch(() => null)
        : null;

    let resp = cache ? await cache.match(url) : null;
    if (!resp) {
        onProgress?.({ phase: "download", url });
        resp = await fetch(url);
        if (!resp.ok) {
            throw new Error(`No se pudo descargar ${url}: HTTP ${resp.status}`);
        }
        if (cache) {
            await cache.put(url, resp.clone());
        }
    }
    const buf = await resp.arrayBuffer();

    if (expectedSha256 && globalThis.crypto?.subtle) {
        // El hash viaja en el bootstrap desde Odoo. Verificarlo aqui es lo que
        // impide que una descarga truncada o alterada quede cacheada para
        // siempre produciendo embeddings basura.
        const digest = await crypto.subtle.digest("SHA-256", buf);
        const hex = [...new Uint8Array(digest)]
            .map((b) => b.toString(16).padStart(2, "0")).join("");
        if (hex !== expectedSha256) {
            await cache?.delete(url);
            throw new Error(
                `El modelo ${url} no coincide con su hash. Descarga corrupta; se borro de la cache.`
            );
        }
    }
    return new Uint8Array(buf);
}

/**
 * Prepara el pipeline. Idempotente: repetir la llamada con el mismo perfil no
 * vuelve a descargar ni a compilar nada.
 *
 * `profile` es lo que entrega el bootstrap de Odoo.
 */
export async function init(profile, onProgress) {
    if (state.profile && state.profile.code === profile.code && state.detSession) {
        return state;
    }
    const ort = await loadOrtScript(profile.ort_js_url);

    // WASM SIMD de UN hilo: el multihilo exige SharedArrayBuffer y con el
    // cabeceras COOP/COEP, que en Odoo rompen la carga de recursos externos.
    ort.env.wasm.wasmPaths = new URL(profile.ort_wasm_url, document.baseURI).href;
    ort.env.wasm.numThreads = 1;
    ort.env.wasm.simd = true;
    ort.env.wasm.proxy = false;
    ort.env.logLevel = "error";

    const opts = { executionProviders: ["wasm"], graphOptimizationLevel: "all" };
    const det = profile.artifacts.detector;
    const emb = profile.artifacts.embedder;

    onProgress?.({ phase: "detector" });
    state.detSession = await ort.InferenceSession.create(
        await fetchModel(det.url, det.sha256, onProgress), opts);
    onProgress?.({ phase: "embedder" });
    state.embSession = await ort.InferenceSession.create(
        await fetchModel(emb.url, emb.sha256, onProgress), opts);

    state.ort = ort;
    state.profile = profile;
    state.scratch = document.createElement("canvas");
    state.aligned = Object.assign(document.createElement("canvas"), { width: 112, height: 112 });
    onProgress?.({ phase: "ready" });
    return state;
}

export function isReady() {
    return Boolean(state.detSession && state.embSession);
}

/** Luminancia media del recorte alineado. Alimenta el diagnostico de iluminacion. */
function meanLuminance(canvas) {
    const { data } = canvas.getContext("2d", { willReadFrequently: true })
        .getImageData(0, 0, canvas.width, canvas.height);
    let sum = 0;
    for (let i = 0; i < data.length; i += 4) {
        sum += 0.2126 * data[i] + 0.7152 * data[i + 1] + 0.0722 * data[i + 2];
    }
    return sum / (data.length / 4);
}

/** Detecta rostros en un canvas ya pintado con el frame. */
export async function detect(frameCanvas, settings) {
    const size = settings.detector_input_size || 480;
    const tensor = yunet.preprocess(state.ort, frameCanvas, size, size, state.scratch);
    const outputs = await state.detSession.run({ input: tensor });
    let faces = yunet.decode(outputs, size, size, settings.detection_threshold ?? 0.6);
    faces = yunet.nms(faces, 0.3, 5);
    return yunet.rescale(faces, size, size, frameCanvas.width, frameCanvas.height);
}

/**
 * Elige el rostro a procesar entre los detectados.
 *
 * Con varias caras en cuadro se toma la mas grande — la persona al frente — y
 * solo se rechaza si la segunda alcanza `ambiguous_size_ratio` de la primera,
 * que es cuando de verdad no se sabe quien esta marcando. Trabar ante cualquier
 * segunda cara bloquearia el kiosco justo en el cambio de turno, que es cuando
 * la gente se amontona.
 */
export function pickFace(faces, settings) {
    if (!faces.length) {
        return { face: null, reason: "no_face" };
    }
    const sorted = faces.slice().sort((a, b) => b.w * b.h - a.w * a.h);
    const [first, second] = sorted;
    if (second) {
        const ratio = (second.w * second.h) / (first.w * first.h);
        if (ratio >= (settings.ambiguous_size_ratio ?? 0.8)) {
            return { face: null, reason: "ambiguous", faces: sorted };
        }
    }
    if (first.w < (settings.min_face_px ?? 110)) {
        return { face: null, reason: "too_small", face_px: first.w, faces: sorted };
    }
    return { face: first, faces: sorted };
}

/** Alinea y calcula el embedding L2-normalizado de un rostro. */
export async function embed(frameCanvas, face) {
    const transform = align.alignTo112(frameCanvas, face.landmarks, state.aligned);
    const tensor = sface.preprocess(state.ort, state.aligned);
    const out = await state.embSession.run({ data: tensor });
    const vector = sface.l2normalize(out[state.embSession.outputNames[0]].data);
    return {
        vector,
        base64: sface.encodeEmbedding(vector),
        dim: vector.length,
        luminance: meanLuminance(state.aligned),
        crop: state.aligned,
        degenerate: transform.degenerate,
    };
}

/** Pasada completa sobre un frame. Devuelve null en `result` si no hubo rostro usable. */
export async function process(frameCanvas, settings) {
    const faces = await detect(frameCanvas, settings);
    const picked = pickFace(faces, settings);
    if (!picked.face) {
        return { ok: false, reason: picked.reason, faces: picked.faces || [], face_px: picked.face_px };
    }
    const result = await embed(frameCanvas, picked.face);
    return { ok: true, face: picked.face, faces: picked.faces, ...result };
}

/** Dibuja el frame actual del video en un canvas del tamano nativo. */
export function grabFrame(video, canvas) {
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    canvas.getContext("2d", { willReadFrequently: true }).drawImage(video, 0, 0);
    return canvas;
}

/** Similitud coseno entre embeddings ya normalizados. */
export const cosine = sface.cosine;

/** Decodifica el base64 que guarda Odoo de vuelta a Float32Array. */
export function decodeEmbedding(b64) {
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) {
        bytes[i] = bin.charCodeAt(i);
    }
    return new Float32Array(bytes.buffer);
}
