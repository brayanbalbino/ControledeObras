"""Ponto de entrada da Vercel: expõe o app Dash (Flask/WSGI) como `app`."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from controle_obras import server as app  # noqa: E402,F401
