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
        // Senales de vida, calculadas del recorte que ya tenemos: no cuestan
        // ni un modelo mas ni un megabyte mas.
        sharpness: laplacianVariance(state.aligned),
        shape: normalizedShape(face.landmarks),
    };
}

/**
 * Varianza del laplaciano del recorte alineado: cuanto detalle fino tiene.
 *
 * Una cara real a metro y medio de una webcam decente tiene textura —poros,
 * pelo, bordes de los ojos—. La foto de una cara en la pantalla de un telefono
 * pasa por dos muestreos y pierde casi todo ese detalle. No es una prueba, pero
 * es una diferencia medible y gratis.
 */
function laplacianVariance(canvas) {
    const ctx = canvas.getContext("2d", { willReadFrequently: true });
    const { data, width, height } = ctx.getImageData(0, 0, canvas.width, canvas.height);
    const gray = new Float32Array(width * height);
    for (let i = 0; i < gray.length; i++) {
        const p = i * 4;
        gray[i] = 0.299 * data[p] + 0.587 * data[p + 1] + 0.114 * data[p + 2];
    }
    let sum = 0;
    let sumSq = 0;
    let count = 0;
    for (let y = 1; y < height - 1; y++) {
        for (let x = 1; x < width - 1; x++) {
            const i = y * width + x;
            const value = 4 * gray[i] - gray[i - 1] - gray[i + 1]
                - gray[i - width] - gray[i + width];
            sum += value;
            sumSq += value * value;
            count += 1;
        }
    }
    const mean = sum / count;
    return sumSq / count - mean * mean;
}

/**
 * Los 5 puntos llevados a una escala y orientacion canonicas.
 *
 * Quitando posicion, tamano y giro, lo que queda es la GEOMETRIA del rostro. En
 * una persona real esa geometria fluctua sola entre frames: parpadea, respira,
 * gira minimamente la cabeza y la perspectiva cambia. Una foto impresa o en una
 * pantalla es plana y rigida: por mucho que se la mueva, su geometria
 * normalizada queda casi congelada.
 *
 * Comparando esta forma entre varios frames sale una senal de vida sin modelo
 * de por medio. Es debil —un video en el telefono la enganaria— pero corta el
 * ataque facil, que es una foto fija.
 */
function normalizedShape(landmarks) {
    const leftEye = [landmarks[0], landmarks[1]];
    const rightEye = [landmarks[2], landmarks[3]];
    const dx = rightEye[0] - leftEye[0];
    const dy = rightEye[1] - leftEye[1];
    const scale = Math.hypot(dx, dy) || 1;
    const angle = Math.atan2(dy, dx);
    const cos = Math.cos(-angle) / scale;
    const sin = Math.sin(-angle) / scale;
    const cx = (leftEye[0] + rightEye[0]) / 2;
    const cy = (leftEye[1] + rightEye[1]) / 2;
    const out = new Float32Array(10);
    for (let i = 0; i < 5; i++) {
        const x = landmarks[2 * i] - cx;
        const y = landmarks[2 * i + 1] - cy;
        out[2 * i] = x * cos - y * sin;
        out[2 * i + 1] = x * sin + y * cos;
    }
    return out;
}

/**
 * Dispersion de la forma entre varios frames. Cuanto mas alta, mas "viva".
 *
 * Se descartan los dos primeros puntos (los ojos), que por construccion quedan
 * fijos al normalizar: solo informan nariz y comisuras.
 */
export function shapeVariation(shapes) {
    if (!shapes || shapes.length < 2) {
        return 0;
    }
    let total = 0;
    for (let k = 4; k < 10; k++) {
        let sum = 0;
        for (const shape of shapes) {
            sum += shape[k];
        }
        const mean = sum / shapes.length;
        let variance = 0;
        for (const shape of shapes) {
            variance += (shape[k] - mean) ** 2;
        }
        total += Math.sqrt(variance / shapes.length);
    }
    return total / 6;
}

/** Devuelve un canvas nuevo con la imagen rotada los grados indicados. */
function rotateCanvas(source, degrees) {
    const rad = (degrees * Math.PI) / 180;
    const swap = degrees === 90 || degrees === 270;
    const out = document.createElement("canvas");
    out.width = swap ? source.height : source.width;
    out.height = swap ? source.width : source.height;
    const ctx = out.getContext("2d", { willReadFrequently: true });
    ctx.translate(out.width / 2, out.height / 2);
    ctx.rotate(rad);
    ctx.drawImage(source, -source.width / 2, -source.height / 2);
    return out;
}

/**
 * Pasada completa sobre un frame.
 *
 * Si no encuentra rostro, reintenta rotando la imagen. Las fotos de celular
 * llegan con la orientacion en los metadatos EXIF, y al redimensionarlas se
 * pierde ese dato: la imagen queda fisicamente de costado y el detector, que
 * fue entrenado con caras derechas, no ve nada. Sin este reintento, media
 * plantilla enrolada desde el telefono fallaria sin explicacion.
 *
 * Solo se aplica a imagenes fijas: `tryRotations` va en false para el video del
 * kiosco, donde la camara nunca cambia de orientacion y probar rotaciones seria
 * gastar tiempo en cada frame.
 */
export async function process(frameCanvas, settings, { tryRotations = true } = {}) {
    const attempts = tryRotations ? [0, 90, 270, 180] : [0];
    let first = null;

    for (const degrees of attempts) {
        const canvas = degrees === 0 ? frameCanvas : rotateCanvas(frameCanvas, degrees);
        const faces = await detect(canvas, settings);
        const picked = pickFace(faces, settings);
        if (!picked.face) {
            first = first || { ok: false, reason: picked.reason,
                               faces: picked.faces || [], face_px: picked.face_px };
            continue;
        }
        const result = await embed(canvas, picked.face);
        return { ok: true, face: picked.face, faces: picked.faces, rotation: degrees, ...result };
    }
    return first;
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
