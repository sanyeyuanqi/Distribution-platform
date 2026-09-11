"""Run tests against the dedicated local PostgreSQL test database."""
import os
import subprocess
import sys
from pathlib import Path
from dotenv import dotenv_values
from sqlalchemy.engine import make_url

root = Path(__file__).resolve().parents[1]
configuration = dotenv_values(root / '.env')
environment = dict(os.environ)
environment['TEST_DATABASE_URL'] = make_url(configuration['DATABASE_URL']).set(database='keyacross_test').render_as_string(hide_password=False)
raise SystemExit(subprocess.call([sys.executable, '-m', 'pytest', *sys.argv[1:]], cwd=root / 'backend', env=environment))
