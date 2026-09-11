"""Create local secrets exactly once, without exposing passwords in logs."""
import argparse
import base64
import getpass
import secrets
from pathlib import Path

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--username', default='')
parser.add_argument('--generate-password', action='store_true', help='Save a random bootstrap password in the private .env')
args = parser.parse_args()
destination = root / '.env'
if destination.exists():
    raise SystemExit('.env already exists; existing keys and data have not been changed.')
username = args.username or input('Initial superadmin username: ').strip()
password = secrets.token_urlsafe(24) if args.generate_password else getpass.getpass('初始超级管理员密码（至少 6 位）：')
if not username or not 6 <= len(password) <= 256 or any(c in password + username for c in '\n\r\"\''):
    raise SystemExit('用户名或密码无效；密码长度应为 6–256 位，用户名及密码不能包含引号或换行。')
pg, redis = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
values = dict(ENVIRONMENT='production', POSTGRES_PASSWORD=pg, REDIS_PASSWORD=redis,
    ENCRYPTION_KEY=base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(), SECRET_KEY=secrets.token_urlsafe(48),
    BOOTSTRAP_USERNAME=username, BOOTSTRAP_PASSWORD=password,
    DATABASE_URL=f'postgresql+psycopg://keyacross:{pg}@127.0.0.1:55432/keyacross',
    REDIS_URL=f'redis://:{redis}@127.0.0.1:56379/0',
    CORS_ORIGINS='http://localhost:5173,http://127.0.0.1:5173,http://localhost:8080,http://127.0.0.1:8080', COOKIE_SECURE='false',
    SESSION_IDLE_SECONDS='7200', SESSION_MAX_SECONDS='86400', SYNC_INTERVAL_SECONDS='300',
    ALLOW_HTTP_SITES='false', ALLOWED_PRIVATE_HOSTS='', ATTACHMENT_DIR='./data/attachments')
with destination.open('x', encoding='utf-8', newline='\n') as handle:
    handle.write('\n'.join(f'{k}="{v}"' for k, v in values.items()) + '\n')
try:
    destination.chmod(0o600)
except OSError:
    pass
print('Created private .env with unique secrets. The bootstrap password is stored there, never printed.')
