
set -e

cd "$(dirname "$0")/backend"

PYTHON_BIN="${PYTHON_BIN:-python3}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "ERROR: $PYTHON_BIN not found. Install Python 3.10+ and try again." >&2
  exit 1
fi

if [ ! -d ".venv" ]; then
  echo "==> Creating virtual environment in backend/.venv"
  "$PYTHON_BIN" -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "==> Installing dependencies"
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

echo
echo "================================================================"
echo "  Loan Wizard is starting on http://localhost:8000"
echo "  Admin dashboard at        http://localhost:8000/admin"
echo "  API docs at               http://localhost:8000/docs"
echo "  Press Ctrl+C to stop."
echo "================================================================"
echo

exec uvicorn main:app --host 0.0.0.0 --port 8000
