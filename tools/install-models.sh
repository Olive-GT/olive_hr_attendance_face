#!/usr/bin/env bash
# Instala los pesos y el runtime dentro del modulo, con el hash en el nombre.
#
# El hash en el nombre del archivo hace que el navegador nunca revalide y que
# un cambio de modelo sea una URL nueva. Si cambias un modelo, tambien tiene que
# cambiar embedding_version en el perfil: vectores de modelos distintos no son
# comparables y mezclarlos deja al kiosco reconociendo mal en silencio.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$DIR/bench/vendor"
PROFILE="yunet_sface_v1"

[ -f "$SRC/sface_fp32.onnx" ] || { echo "Faltan pesos. Corre bench/fetch-models.sh" >&2; exit 1; }

install_hashed() {  # $1=origen  $2=prefijo  $3=destino
    local h; h=$(shasum -a 256 "$1" | cut -c1-12)
    cp "$1" "$3/$2-$h.onnx"
    echo "  $2-$h.onnx  ($(du -h "$1" | cut -f1))"
}
mkdir -p "$DIR/static/lib/models/$PROFILE" "$DIR/static/lib/ort"
echo "Modelos:"
install_hashed "$SRC/yunet_2026may.onnx" yunet "$DIR/static/lib/models/$PROFILE"
install_hashed "$SRC/sface_fp32.onnx"    sface "$DIR/static/lib/models/$PROFILE"
echo "Runtime:"
for f in ort.wasm.min.js ort-wasm-simd-threaded.mjs ort-wasm-simd-threaded.wasm; do
    cp "$SRC/$f" "$DIR/static/lib/ort/$f"; echo "  $f  ($(du -h "$SRC/$f" | cut -f1))"
done
echo; echo "Total en el modulo: $(du -sh "$DIR/static/lib" | cut -f1)"
