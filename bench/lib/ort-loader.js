// Configuracion de ONNX Runtime Web para el kiosco.
//
// Decision de arquitectura (ver plan, seccion "Decisiones tomadas"): WASM SIMD
// de UN SOLO HILO. El modo multihilo necesita SharedArrayBuffer, que exige
// cabeceras COOP/COEP; configurarlas en Odoo rompe la carga de recursos de
// otros origenes y es un paso de tecnico en cada despliegue.

export function configureOrt(ort, wasmPath = "./vendor/") {
    // ORT resuelve wasmPaths relativo a la ubicacion de su propio script, no a
    // la del documento. Con una ruta relativa termina buscando en
    // vendor/vendor/, asi que se le pasa siempre una URL absoluta.
    ort.env.wasm.wasmPaths = new URL(wasmPath, document.baseURI).href;
    ort.env.wasm.numThreads = 1;  // <- lo que evita necesitar SharedArrayBuffer
    ort.env.wasm.simd = true;
    ort.env.wasm.proxy = false;
    ort.env.logLevel = "error";
    return ort;
}

export async function createSession(ort, modelUrl, onProgress) {
    const resp = await fetch(modelUrl);
    if (!resp.ok) {
        throw new Error(`No se pudo descargar ${modelUrl}: HTTP ${resp.status}`);
    }
    const buf = new Uint8Array(await resp.arrayBuffer());
    if (onProgress) {
        onProgress(modelUrl, buf.byteLength);
    }
    return ort.InferenceSession.create(buf, {
        executionProviders: ["wasm"],
        graphOptimizationLevel: "all",
    });
}
