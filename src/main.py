"""Run the Flask server with ``python -m uvicorn src.main:app --reload``."""

from uvicorn.middleware.wsgi import WSGIMiddleware

from web_app import app as flask_app

# Flask speaks WSGI; Uvicorn's default interface expects an ASGI callable.
app = WSGIMiddleware(flask_app)
