"""Azure / gunicorn entrypoint: `gunicorn ... app:app` → same object as wsgi:app."""
from wsgi import app

__all__ = ["app"]
