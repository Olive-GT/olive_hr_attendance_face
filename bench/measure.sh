#!/usr/bin/env bash
# Cronometra el pipeline por reloj de pared en Chrome headless.
#
# Para cada configuracion corre n=0 (solo carga) y n=N, y la diferencia dividida
# entre N da el coste real por iteracion. Es la unica forma honesta de medir en
# headless, donde performance.now() esta falseado por el reloj virtual.
set -euo pipefail

PORT="${PORT:-8765}"
N="${N:-60}"
CHROME="${CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
BASE="http://localhost:$PORT/timing.html"

run() {  # $1 = query string -> segundos de reloj de pared
    local t0 t1
    t0=$(python3 -c "import time;print(time.time())")
    "$CHROME" --headless --disable-gpu --no-sandbox \
        --virtual-time-budget=600000 --dump-dom "$BASE?$1" >/dev/null 2>&1
    t1=$(python3 -c "import time;print(time.time())")
    python3 -c "print(f'{$t1-$t0:.3f}')"
}

printf "%-26s %-6s %-9s %-9s %s\n" MODELO ENTRADA "ms/iter" "fps" ETAPA
printf -- "------------------------------------------------------------------\n"

for model in yunet_2026may.onnx; do
  for stage in det full; do
    for size in 160 224 320 480 640; do
        base=$(run "n=0&size=$size&model=$model&stage=$stage")
        full=$(run "n=$N&size=$size&model=$model&stage=$stage")
        python3 - "$model" "$size" "$stage" "$base" "$full" "$N" <<'PY'
import sys
model, size, stage, base, full, n = sys.argv[1:]
ms = (float(full) - float(base)) / int(n) * 1000
fps = 1000 / ms if ms > 0 else float("inf")
print(f"{model:<26} {size:<6} {ms:<9.1f} {fps:<9.1f} {stage}")
PY
    done
  done
done
