"""
Production entrypoint. Railway (via gunicorn, see Procfile) imports `app`
from this file. Keeping it at the repo root, separate from api/app.py,
means gunicorn's working directory doesn't matter -- this file's own
location is used to find everything else.
"""
from api.app import app  # noqa: F401
