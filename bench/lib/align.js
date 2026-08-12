// Alineacion del rostro a 112x112.
//
// Paso OBLIGATORIO, no cosmetico: SFace fue entrenado sobre recortes alineados
// con estos mismos 5 puntos de referencia. Sin la alineacion la precision se
// desploma, por mucho que el resto del pipeline sea correcto.

// Puntos de referencia canonicos de ArcFace para 112x112, en el orden que
// entrega YuNet: ojo derecho, ojo izquierdo, nariz, comisura derecha, comisura
// izquierda.
export const REFERENCE_112 = new Float32Array([
    38.2946, 51.6963,
    73.5318, 51.5014,
    56.0252, 71.7366,
    41.5493, 92.3655,
    70.7299, 92.2041,
]);

/**
 * Transformada de similitud (escala + rotacion + traslacion, 4 grados de
 * libertad) por minimos cuadrados entre dos conjuntos de puntos.
 *
 * Se resuelve en forma cerrada en vez de con SVD (Umeyama): para una similitud
 * el problema es lineal en (a, b, tx, ty), y ademas asi queda excluida por
 * construccion la solucion con reflexion, que en alineacion facial nunca se
 * quiere.
 *
 *   x' = a*x - b*y + tx
 *   y' = b*x + a*y + ty
 *
 * Devuelve {a, b, tx, ty}.
 */
export function similarityTransform(src, dst) {
    const n = src.length / 2;
    let mx = 0, my = 0, Mx = 0, My = 0;
    for (let i = 0; i < n; i++) {
        mx += src[2 * i];
        my += src[2 * i + 1];
        Mx += dst[2 * i];
        My += dst[2 * i + 1];
    }
    mx /= n; my /= n; Mx /= n; My /= n;

    let num_a = 0, num_b = 0, den = 0;
    for (let i = 0; i < n; i++) {
        const dx = src[2 * i] - mx;
        const dy = src[2 * i + 1] - my;
        const dX = dst[2 * i] - Mx;
        const dY = dst[2 * i + 1] - My;
        num_a += dx * dX + dy * dY;
        num_b += dx * dY - dy * dX;
        den += dx * dx + dy * dy;
    }
    if (den < 1e-9) {
        // Puntos degenerados (todos coincidentes): identidad, y que el llamador
        // lo descarte por calidad.
        return { a: 1, b: 0, tx: Mx - mx, ty: My - my, degenerate: true };
    }
    const a = num_a / den;
    const b = num_b / den;
    return {
        a,
        b,
        tx: Mx - (a * mx - b * my),
        ty: My - (b * mx + a * my),
        degenerate: false,
    };
}

/** Escala implicita de la transformada: sirve como medida de tamano del rostro. */
export function transformScale(t) {
    return Math.hypot(t.a, t.b);
}

/**
 * Recorta y alinea el rostro sobre un canvas de 112x112.
 * `outCanvas` debe medir 112x112.
 */
export function alignTo112(sourceCanvas, landmarks, outCanvas) {
    const t = similarityTransform(landmarks, REFERENCE_112);
    const ctx = outCanvas.getContext("2d", { willReadFrequently: true });
    ctx.save();
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, 112, 112);
    // El canvas aplica  x' = a*x + c*y + e ;  y' = b*x + d*y + f
    // Nuestra similitud: x' = a*x - b*y + tx ; y' = b*x + a*y + ty
    ctx.setTransform(t.a, t.b, -t.b, t.a, t.tx, t.ty);
    ctx.drawImage(sourceCanvas, 0, 0);
    ctx.restore();
    return t;
}
