#!/usr/bin/env bash
# Descarga los pesos y el runtime que necesita el banco de pruebas F0.
#
# No van al repositorio: bench/vendor/ esta en .gitignore. Los pesos definitivos
# del modulo viven en static/lib/models/ via Git LFS (ver plan, seccion 7).
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/vendor"
mkdir -p "$DIR"
cd "$DIR"

ZOO="https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models"

# Variantes elegidas por medicion en F0 (ver RESULTADOS.md), no por catalogo.
echo "== YuNet 2026may (deteccion + 5 puntos, entrada dinamica) =="
curl -fL -o yunet_2026may.onnx "$ZOO/face_detection_yunet/face_detection_yunet_2026may.onnx"

echo "== SFace fp32 (embedding 128-D) =="
echo "   fp32 y no int8: en ORT WASM el int8 resulta MAS LENTO (110 vs 89 ms)."
curl -fL -o sface_fp32.onnx "$ZOO/face_recognition_sface/face_recognition_sface_2021dec.onnx"

# Variantes descartadas, se bajan solo para poder reproducir la comparacion.
echo "== descartadas (para reproducir la comparacion) =="
curl -fL -o yunet.onnx "$ZOO/face_detection_yunet/face_detection_yunet_2023mar.onnx"
curl -fL -o sface_int8.onnx "$ZOO/face_recognition_sface/face_recognition_sface_2021dec_int8.onnx"

echo "== ONNX Runtime Web =="
VER=$(curl -fs https://registry.npmjs.org/onnxruntime-web/latest \
      | python3 -c "import sys,json;print(json.load(sys.stdin)['version'])")
echo "   version $VER"
curl -fL -o ort.tgz "https://registry.npmjs.org/onnxruntime-web/-/onnxruntime-web-${VER}.tgz"
tar -xzf ort.tgz --strip-components=2 \
    package/dist/ort.wasm.min.js \
    package/dist/ort-wasm-simd-threaded.mjs \
    package/dist/ort-wasm-simd-threaded.wasm
rm ort.tgz

echo
echo "Listo. Verificacion de integridad:"
shasum -a 256 yunet_2026may.onnx sface_fp32.onnx
