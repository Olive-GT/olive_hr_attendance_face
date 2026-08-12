#!/usr/bin/env bash
# Sirve el banco de pruebas F0 y abre Chrome.
#
# Tiene que ser por HTTP y no abriendo el archivo: getUserMedia solo funciona en
# contexto seguro, y localhost cuenta como tal.
set -euo pipefail

PORT="${PORT:-8765}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ ! -f "$DIR/vendor/sface_int8.onnx" ]; then
    echo "Faltan los pesos en bench/vendor/. Corre primero: ./fetch-models.sh" >&2
    exit 1
fi

echo "Sirviendo $DIR en http://localhost:$PORT"
cd "$DIR"
python3 -m http.server "$PORT" --bind 127.0.0.1 >/dev/null 2>&1 &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null || true' EXIT

sleep 1

URL="http://localhost:$PORT/index.html"
case "$(uname -s)" in
    Darwin) open -a "Google Chrome" "$URL" 2>/dev/null || open "$URL" ;;
    Linux)  xdg-open "$URL" >/dev/null 2>&1 || true ;;
    *)      echo "Abri manualmente: $URL" ;;
esac

echo "Ctrl+C para detener."
wait $SERVER_PID
