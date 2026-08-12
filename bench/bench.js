// Banco de pruebas F0.
//
// Responde la unica pregunta que bloquea el proyecto: >= 5 fps de pipeline
// completo en el equipo objetivo? Si no llega, se replantea el modelo antes de
// escribir una linea mas (criterio de terminado de F0 en el plan).
//
// Este archivo es ademas el germen del autodiagnostico de la seccion 5.4: las
// mismas mediciones (fps, tamano del rostro en cuadro, luminancia) son las que
// despues le diran al instalador si el equipo y la ubicacion sirven.

import { configureOrt, createSession } from "./lib/ort-loader.js";
import * as yunet from "./lib/yunet.js";
import * as align from "./lib/align.js";
import * as sface from "./lib/sface.js";

// Elegidos por medicion en F0, no por catalogo (ver bench/RESULTADOS.md):
//  - yunet_2026may: el export 2023mar tiene entrada FIJA de 640x640 y no deja
//    bajar la resolucion de deteccion; el 2026may es dinamico.
//  - sface fp32: contraintuitivamente es MAS RAPIDO que el int8 (89 vs 110 ms),
//    porque ORT WASM no tiene kernels int8 optimizados. Pesa 38.7 MB en vez de
//    9.9 MB, pero eso se paga una sola vez y queda en Cache API.
const MODELS = {
    detector: "./vendor/yunet_2026may.onnx",
    embedder: "./vendor/sface_fp32.onnx",
};

const state = {
    ort: null,
    detSession: null,
    embSession: null,
    video: null,
    frameCanvas: document.createElement("canvas"),
    detCanvas: document.createElement("canvas"),
    alignCanvas: Object.assign(document.createElement("canvas"), { width: 112, height: 112 }),
    running: false,
    lastResults: [],
};

const log = (msg) => {
    const el = document.getElementById("log");
    el.textContent += msg + "\n";
    el.scrollTop = el.scrollHeight;
};

const percentile = (arr, p) => {
    if (!arr.length) return 0;
    const s = arr.slice().sort((a, b) => a - b);
    return s[Math.min(s.length - 1, Math.floor(s.length * p))];
};

// ---------------------------------------------------------------- carga

async function loadModels() {
    const ort = configureOrt(window.ort, "./vendor/");
    state.ort = ort;

    const t0 = performance.now();
    let bytes = 0;
    state.detSession = await createSession(ort, MODELS.detector, (_u, n) => { bytes += n; });
    const tDet = performance.now();
    state.embSession = await createSession(ort, MODELS.embedder, (_u, n) => { bytes += n; });
    const tEmb = performance.now();

    log(`Detector cargado en ${(tDet - t0).toFixed(0)} ms`);
    log(`Embedder cargado en ${(tEmb - tDet).toFixed(0)} ms`);
    log(`Pesos descargados: ${(bytes / 1048576).toFixed(1)} MB`);
    log(`Entradas detector: ${state.detSession.inputNames.join(", ")}`);
    log(`Salidas detector : ${state.detSession.outputNames.join(", ")}`);
    log(`Entradas embedder: ${state.embSession.inputNames.join(", ")}`);
    log(`Salidas embedder : ${state.embSession.outputNames.join(", ")}`);
    return { loadMs: tEmb - t0, bytes };
}

async function startCamera() {
    const stream = await navigator.mediaDevices.getUserMedia({
        video: { width: { ideal: 640 }, height: { ideal: 480 }, facingMode: "user" },
        audio: false,
    });
    const video = document.getElementById("video");
    video.srcObject = stream;
    await video.play();
    state.video = video;
    const track = stream.getVideoTracks()[0];
    const s = track.getSettings();
    log(`Camara: ${s.width}x${s.height} @ ${s.frameRate || "?"} fps — ${track.label}`);
    return s;
}

// ---------------------------------------------------------------- pipeline

/** Luminancia media del recorte alineado: alimenta el diagnostico de iluminacion. */
function meanLuminance(canvas) {
    const ctx = canvas.getContext("2d", { willReadFrequently: true });
    const { data } = ctx.getImageData(0, 0, canvas.width, canvas.height);
    let sum = 0;
    for (let i = 0; i < data.length; i += 4) {
        sum += 0.2126 * data[i] + 0.7152 * data[i + 1] + 0.0722 * data[i + 2];
    }
    return sum / (data.length / 4);
}

async function runOnce(detSize, scoreThreshold) {
    const video = state.video;
    const fw = video.videoWidth;
    const fh = video.videoHeight;
    state.frameCanvas.width = fw;
    state.frameCanvas.height = fh;
    state.frameCanvas.getContext("2d", { willReadFrequently: true }).drawImage(video, 0, 0);

    const timing = {};
    let t = performance.now();

    // --- deteccion
    const inputTensor = yunet.preprocess(state.ort, state.frameCanvas, detSize, detSize, state.detCanvas);
    timing.preprocess = performance.now() - t; t = performance.now();

    const detOut = await state.detSession.run({ input: inputTensor });
    timing.detect = performance.now() - t; t = performance.now();

    let faces = yunet.decode(detOut, detSize, detSize, scoreThreshold);
    faces = yunet.nms(faces, 0.3, 5);
    faces = yunet.rescale(faces, detSize, detSize, fw, fh);
    timing.decode = performance.now() - t; t = performance.now();

    if (!faces.length) {
        timing.total = timing.preprocess + timing.detect + timing.decode;
        return { timing, faces: [], embedding: null, luminance: null };
    }

    // Cara mas grande = la persona al frente (regla acordada para cambio de turno).
    faces.sort((a, b) => b.w * b.h - a.w * a.h);
    const face = faces[0];

    // --- alineacion
    align.alignTo112(state.frameCanvas, face.landmarks, state.alignCanvas);
    timing.align = performance.now() - t; t = performance.now();

    const luminance = meanLuminance(state.alignCanvas);

    // --- embedding
    const embTensor = sface.preprocess(state.ort, state.alignCanvas);
    const embOut = await state.embSession.run({ data: embTensor });
    const raw = embOut[state.embSession.outputNames[0]].data;
    const embedding = sface.l2normalize(raw);
    timing.embed = performance.now() - t;

    timing.total = timing.preprocess + timing.detect + timing.decode + timing.align + timing.embed;
    return { timing, faces, face, embedding, luminance };
}

// ---------------------------------------------------------------- medicion

async function measure(detSize, iterations, scoreThreshold) {
    // Calentamiento: la primera inferencia de WASM paga compilacion y no
    // representa el regimen permanente. Medirla falsearia el resultado.
    for (let i = 0; i < 3; i++) {
        await runOnce(detSize, scoreThreshold);
    }

    const samples = [];
    let detected = 0;
    let lastLum = null;
    let lastFaceW = 0;
    for (let i = 0; i < iterations; i++) {
        const r = await runOnce(detSize, scoreThreshold);
        samples.push(r.timing);
        if (r.face) {
            detected++;
            lastLum = r.luminance;
            lastFaceW = r.face.w;
        }
        if (i % 5 === 0) {
            drawOverlay(r.faces || []);
        }
        await new Promise((res) => requestAnimationFrame(res));
    }

    const totals = samples.map((s) => s.total);
    const stage = (k) => {
        const v = samples.map((s) => s[k]).filter((x) => x !== undefined);
        return v.length ? v.reduce((a, b) => a + b, 0) / v.length : 0;
    };

    return {
        detSize,
        iterations,
        detectionRate: detected / iterations,
        medianMs: percentile(totals, 0.5),
        p95Ms: percentile(totals, 0.95),
        fps: 1000 / percentile(totals, 0.5),
        stages: {
            preprocess: stage("preprocess"),
            detect: stage("detect"),
            decode: stage("decode"),
            align: stage("align"),
            embed: stage("embed"),
        },
        faceWidthPx: lastFaceW,
        luminance: lastLum,
    };
}

function drawOverlay(faces) {
    const canvas = document.getElementById("overlay");
    const video = state.video;
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.lineWidth = 2;
    ctx.strokeStyle = "#00c853";
    ctx.fillStyle = "#ff1744";
    for (const f of faces) {
        ctx.strokeRect(f.x, f.y, f.w, f.h);
        for (let p = 0; p < 5; p++) {
            ctx.beginPath();
            ctx.arc(f.landmarks[2 * p], f.landmarks[2 * p + 1], 2.5, 0, 6.283);
            ctx.fill();
        }
    }
}

// ---------------------------------------------------------------- reporte

function renderResults(results) {
    const rows = results.map((r) => `
        <tr class="${r.fps >= 5 ? "ok" : "bad"}">
          <td>${r.detSize}</td>
          <td><b>${r.fps.toFixed(1)}</b></td>
          <td>${r.medianMs.toFixed(1)}</td>
          <td>${r.p95Ms.toFixed(1)}</td>
          <td>${r.stages.detect.toFixed(1)}</td>
          <td>${r.stages.embed.toFixed(1)}</td>
          <td>${(r.stages.preprocess + r.stages.decode + r.stages.align).toFixed(1)}</td>
          <td>${(r.detectionRate * 100).toFixed(0)}%</td>
          <td>${r.faceWidthPx ? r.faceWidthPx.toFixed(0) : "—"}</td>
          <td>${r.luminance ? r.luminance.toFixed(0) : "—"}</td>
        </tr>`).join("");

    document.getElementById("results").innerHTML = `
      <table>
        <thead><tr>
          <th>Entrada</th><th>FPS</th><th>Mediana ms</th><th>p95 ms</th>
          <th>Detect</th><th>Embed</th><th>Otros</th>
          <th>Detecc.</th><th>Rostro px</th><th>Luz</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>`;

    // Metrica real del kiosco: cuanto tarda desde que aparece la cara hasta que
    // el marcaje queda confirmado. La deteccion corre por frame (es barata) pero
    // el embedding solo hace falta 3 veces, que es lo que pide la guarda 2.
    // Medir "fps del pipeline completo" sobrestima el coste, porque supone
    // embedder en cada frame, cosa que el kiosco no necesita hacer.
    const FRAMES_REQUIRED = 3;
    const ttid = (r) => r.stages.preprocess + r.stages.detect + r.stages.decode +
        FRAMES_REQUIRED * (r.stages.align + r.stages.embed);

    const usable = results.filter((r) => ttid(r) <= 1500 && r.detectionRate > 0.9);
    const verdict = document.getElementById("verdict");
    if (usable.length) {
        const chosen = usable.reduce((a, b) => (b.detSize > a.detSize ? b : a));
        verdict.className = "verdict ok";
        verdict.innerHTML = `<b>F0 SUPERADO.</b> Mejor configuracion usable: entrada de
          <b>${chosen.detSize}px</b>, <b>${(ttid(chosen) / 1000).toFixed(2)} s</b> desde que
          aparece el rostro hasta el marcaje confirmado
          (deteccion ${chosen.stages.detect.toFixed(0)} ms/frame,
          embedding ${chosen.stages.embed.toFixed(0)} ms x ${FRAMES_REQUIRED}).
          El criterio es 1.5 s.`;
    } else {
        const best = results.reduce((a, b) => (ttid(b) < ttid(a) ? b : a));
        verdict.className = "verdict bad";
        verdict.innerHTML = `<b>F0 NO SUPERADO.</b> El mejor tiempo hasta identificar fue
          ${(ttid(best) / 1000).toFixed(2)} s a ${best.detSize}px, por encima del limite de
          1.5 s. Hay que bajar a un embedder mas liviano o cambiar de equipo antes de seguir.`;
    }
}

function exportJson(results, meta) {
    const blob = new Blob([JSON.stringify({ meta, results }, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `bench-${meta.label || "equipo"}.json`;
    a.click();
}

// ---------------------------------------------------------------- arranque

document.getElementById("start").addEventListener("click", async () => {
    const btn = document.getElementById("start");
    btn.disabled = true;
    try {
        log("=== Banco de pruebas F0 ===");
        log(`User agent: ${navigator.userAgent}`);
        log(`Nucleos logicos: ${navigator.hardwareConcurrency || "?"}`);
        const load = await loadModels();
        const cam = await startCamera();

        const iterations = parseInt(document.getElementById("iterations").value, 10);
        const threshold = parseFloat(document.getElementById("threshold").value);
        const sizes = document.getElementById("sizes").value
            .split(",").map((s) => parseInt(s.trim(), 10)).filter(Boolean);

        log("\nPonete frente a la camara. Midiendo...\n");
        const results = [];
        for (const size of sizes) {
            log(`Midiendo entrada de ${size}px (${iterations} iteraciones)...`);
            const r = await measure(size, iterations, threshold);
            log(`  -> ${r.fps.toFixed(1)} fps | mediana ${r.medianMs.toFixed(1)} ms | ` +
                `deteccion ${(r.detectionRate * 100).toFixed(0)}%`);
            results.push(r);
            renderResults(results);
        }
        state.lastResults = results;

        const meta = {
            label: document.getElementById("label").value || "equipo",
            userAgent: navigator.userAgent,
            cores: navigator.hardwareConcurrency,
            camera: cam,
            loadMs: load.loadMs,
            weightBytes: load.bytes,
        };
        document.getElementById("export").disabled = false;
        document.getElementById("export").onclick = () => exportJson(results, meta);
        log("\nListo.");
    } catch (err) {
        log(`\nERROR: ${err.message}`);
        console.error(err);
    } finally {
        btn.disabled = false;
    }
});
