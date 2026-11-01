"""Create private demo settings without displaying credentials or making requests."""
import argparse
import getpass
import os
from pathlib import Path
import secrets
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))
from dotenv import dotenv_values
from aura_demo.config import DemoSettings


def configure(origin, context, *, provider_key=None):
    folder = ROOT / '.local/judging-demo'
    path = folder / 'server.env'
    if path.exists():
        raise ValueError('Configuration already exists; keep its access code and durable usage ledger.')
    code = secrets.token_urlsafe(32)
    DemoSettings(root=ROOT, data=folder, origin=origin, access_code=code, context=context)
    if provider_key is not None and (not provider_key or not provider_key.isascii()
                                    or any(c.isspace() or c in "'\"\\#\x00" for c in provider_key)):
        raise ValueError('The API key must be one non-empty token.')
    folder.mkdir(parents=True, exist_ok=True)
    if os.name != 'nt':
        folder.chmod(0o700)
    settings = {'AURA_DEMO_ORIGIN': origin, 'AURA_DEMO_ACCESS_CODE': code,
                'AURA_DEMO_DOCKER_CONTEXT': context, 'AURA_DEMO_MAX_RUNS': '20', 'AURA_DEMO_MAX_CALLS': '100'}
    if provider_key:
        settings['BASETEN_API_KEY'] = provider_key
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as out:
        out.write(''.join(f'{key}={value}\n' for key, value in settings.items()))
    # Share this code with judges; it is deliberately different from the API key.
    code_path = folder / 'access-code.txt'
    fd = os.open(code_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as out:
        out.write(code + '\n')
    return path, code_path


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--origin', required=True, help='Exact public HTTPS origin, or localhost HTTP origin')
    parser.add_argument('--context', default='default')
    args = parser.parse_args()
    if (ROOT / '.local/judging-demo/server.env').exists():
        parser.error('Settings already exist. Edit the existing private file if the origin changes.')
    existing = os.environ.get('BASETEN_API_KEY') or dotenv_values(ROOT / '.env').get('BASETEN_API_KEY')
    key = None if existing else getpass.getpass('Baseten API key (hidden, stored only on this server): ').strip()
    try:
        settings_file, code_file = configure(args.origin, args.context, provider_key=key)
    except ValueError as exc:
        parser.error(str(exc))
    print(f'Settings: {settings_file}\nJudge access code: {code_file}\nNo API requests made.')
