"""Restore into a NEW database; never overwrite the running production database."""
import argparse
import hashlib
import io
import json
import re
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet
from dotenv import dotenv_values
from backup import ROOT, run


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('bundle')
    parser.add_argument('--database', default='keyacross_restore_' + datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S'))
    args = parser.parse_args()
    if not re.fullmatch(r'keyacross_restore_[A-Za-z0-9_]+', args.database):
        raise SystemExit('Restore database must be a NEW keyacross_restore_* database')
    key = dotenv_values(ROOT / '.env')['ENCRYPTION_KEY'].encode()
    raw = Fernet(key).decrypt(Path(args.bundle).read_bytes())
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        manifest = json.loads(archive.extractfile('manifest.json').read())
        dump = archive.extractfile('database.dump').read()
        if hashlib.sha256(dump).hexdigest() != manifest['database_sha256']:
            raise SystemExit('Database checksum does not match')
        destination = ROOT / '.local' / 'restored' / args.database / 'attachments'
        destination.mkdir(parents=True, exist_ok=False)
        for member in archive.getmembers():
            if member.isfile() and re.fullmatch(r'attachments/[A-Za-z0-9-]+\.bin', member.name):
                (destination / Path(member.name).name).write_bytes(archive.extractfile(member).read())
        run('docker', 'compose', 'exec', '-T', 'postgres', 'createdb', '-U', 'keyacross', args.database)
        run('docker', 'compose', 'exec', '-T', 'postgres', 'pg_restore', '-U', 'keyacross',
            '--no-owner', '--no-privileges', '--exit-on-error', '-d', args.database, data=dump)
    print(f'Restored NEW database: {args.database}')
    print(f'Private encrypted attachments: {destination}')
    print('Production is unchanged. Verify users, credential decryption, bills and unfinished tasks before switching configuration.')


if __name__ == '__main__':
    main()
