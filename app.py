"""Read-only Flask entrypoint for Vercel's Python runtime."""

import os

# Ingestion runs separately against a candidate database. A request worker must
# never bootstrap or modify the immutable database bundled with this deployment.
os.environ["DISABLE_SCHEDULER"] = "1"
os.environ["DISABLE_PREWARM"] = "1"

from dashboard.app import app  # noqa: E402,F401


@app.url_value_preprocessor
def decode_route_values(endpoint, values):
    """Decode escaped path values retained by the Vercel WSGI adapter."""
    from urllib.parse import unquote

    if values:
        for key, value in values.items():
            if isinstance(value, str):
                values[key] = unquote(value)
