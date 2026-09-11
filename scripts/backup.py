"""Create an encrypted database + private-attachment bundle; keys stay separate."""
import argparse
import hashlib
import io
import json
import re
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


def run(*args, data=None):
    result = subprocess.run(args, input=data, cwd=ROOT, capture_output=True)
    if result.returncode:
        raise RuntimeError('Backup/restore command failed; inspect local service health (no secrets printed).')
    return result.stdout


def archive_entry(archive, name, data):
    entry = tarfile.TarInfo(name)
    entry.size = len(data)
    entry.mode = 0o600
    archive.addfile(entry, io.BytesIO(data))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', default=str(ROOT / '.local' / 'backups'))
    parser.add_argument('--database', default='keyacross')
    parser.add_argument('--attachments-dir', help='Local-development attachment path; default reads the API container volume')
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_]+', args.database):
        raise SystemExit('Invalid database name')
    key = dotenv_values(ROOT / '.env')['ENCRYPTION_KEY'].encode()
    dump = run('docker', 'compose', 'exec', '-T', 'postgres', 'pg_dump', '-U', 'keyacross', '-Fc', args.database)
    manifest = {'version': 1, 'database': args.database, 'created_at': datetime.now(timezone.utc).isoformat(),
        'database_sha256': hashlib.sha256(dump).hexdigest(), 'key_id': hashlib.sha256(key).hexdigest()[:16]}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        archive_entry(archive, 'database.dump', dump)
        archive_entry(archive, 'manifest.json', json.dumps(manifest).encode())
        if args.attachments_dir:
            directory = Path(args.attachments_dir).resolve()
            for file in directory.glob('*.bin'):
                archive_entry(archive, 'attachments/' + file.name, file.read_bytes())
        else:
            attachments = run('docker', 'compose', 'exec', '-T', 'api', 'python', '-c',
                'import io,tarfile,sys; b=io.BytesIO(); t=tarfile.open(fileobj=b,mode="w"); t.add("/data/attachments",arcname="attachments"); t.close(); sys.stdout.buffer.write(b.getvalue())')
            with tarfile.open(fileobj=io.BytesIO(attachments)) as files:
                for member in files.getmembers():
                    if member.isfile() and re.fullmatch(r'attachments/[A-Za-z0-9-]+\.bin', member.name):
                        archive_entry(archive, member.name, files.extractfile(member).read())
    destination = Path(args.output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    output = destination / f'keyacross-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.kaenc'
    with output.open('xb') as handle:
        handle.write(Fernet(key).encrypt(buffer.getvalue()))
    print(f'Encrypted backup saved: {output}')
    print('Keep the original ENCRYPTION_KEY in a separate secure location. It is not in this bundle.')


if __name__ == '__main__':
    main()
