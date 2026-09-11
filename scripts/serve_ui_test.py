import os
import subprocess
import sys
from pathlib import Path
from dotenv import dotenv_values
from sqlalchemy.engine import make_url

root = Path(__file__).resolve().parents[1]
configuration = dotenv_values(root / '.env')
environment = dict(os.environ)
environment['DATABASE_URL'] = make_url(configuration['DATABASE_URL']).set(database='keyacross_ui_test').render_as_string(hide_password=False)
environment['CORS_ORIGINS'] = 'http://127.0.0.1:5175,http://localhost:5175'
raise SystemExit(subprocess.call([sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8001'], cwd=root / 'backend', env=environment))
