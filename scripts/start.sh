#!/bin/bash
set -e

echo "============================================"
echo "  Memory Vault — starting up"
echo "============================================"

# Wait for Postgres
echo "Waiting for database..."
for i in $(seq 1 30); do
    if python -c "
import socket, sys
s = socket.socket()
s.settimeout(2)
try:
    s.connect(('${DB_HOST:-localhost}', ${DB_PORT:-5432}))
    s.close()
except:
    sys.exit(1)
" 2>/dev/null; then
        echo "Database is ready."
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "ERROR: Database not reachable after 30 seconds."
        exit 1
    fi
    sleep 1
done

# Run migrations
echo "Running migrations..."
python -m src.cli migrate

# Show status
echo ""
python -m src.cli status

echo ""
echo "============================================"
echo "  Memory Vault is ready"
echo "============================================"
echo ""
echo "  REST API: http://${API_HOST:-0.0.0.0}:${API_PORT:-8000}"
echo "  Docs:     http://${API_HOST:-0.0.0.0}:${API_PORT:-8000}/docs"
echo ""

# Start the REST API (uvicorn)
exec python -m src.cli api
