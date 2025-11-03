import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent.parent
PYTHON_DIR = ROOT / "python"

for path in (ROOT, PYTHON_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

try:  # pragma: no cover - exercised implicitly during import
    importlib.import_module("opentelemetry")
except ModuleNotFoundError:  # pragma: no cover - depends on environment
    stub_path = Path(__file__).resolve().parent / "_otel_stub"
    sys.path.insert(0, str(stub_path))
