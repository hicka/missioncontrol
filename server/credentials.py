"""Website credentials in the OS vault; only non-secret metadata lives on disk.

Exact HTTPS origins are checked against the record inside the vault, not just
the editable index. There is deliberately no API or agent tool to read secrets.
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit
import uuid

from apphome import ROOT

INDEX = ROOT / 'secrets' / 'websites.json'
_lock = threading.RLock()


class VaultError(ValueError):
    pass


def origin(url: str) -> str:
    try:
        parsed = urlsplit(url.strip())
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError()
        host = parsed.hostname.encode('idna').decode('ascii').lower()
        if ':' in host:
            host = f'[{host}]'
        port = parsed.port
        return f'https://{host}' + (f':{port}' if port and port != 443 else '')
    except (ValueError, UnicodeError):
        raise VaultError('Saved logins and private input require an exact HTTPS website address, without embedded credentials.') from None


def backend():
    """Select trusted OS backends explicitly; never load a plaintext fallback."""
    try:
        if sys.platform == 'darwin':
            from keyring.backends.macOS import Keyring
            result = Keyring()
        elif sys.platform == 'win32':
            from keyring.backends.Windows import WinVaultKeyring
            result = WinVaultKeyring()
            result.persist = 'local machine'
        else:
            raise VaultError('Website credential storage requires macOS Keychain or Windows Credential Manager.')
        result.priority  # Validate that the native backend is available.
        return result
    except VaultError:
        raise
    except Exception:
        raise VaultError('The operating system credential vault is unavailable. Check the app installation and unlock your login session.') from None


def status() -> dict:
    name = 'macOS Keychain' if sys.platform == 'darwin' else 'Windows Credential Manager' if sys.platform == 'win32' else 'Unavailable'
    try:
        backend()
        return {'available': True, 'name': name}
    except VaultError as exc:
        return {'available': False, 'name': name, 'error': str(exc)}


def _service(cid: str) -> str:
    workspace = hashlib.sha256(str(ROOT.resolve()).encode()).hexdigest()[:24]
    # Separate services also avoid Windows keyring's multi-account collisions.
    return f'com.missioncontrol.websites/{workspace}/{cid}'


def _read() -> list:
    if not INDEX.exists():
        return []
    try:
        rows = json.loads(INDEX.read_text(encoding='utf-8'))
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError()
        return rows
    except (OSError, ValueError):
        raise VaultError('The website credential index cannot be read. Restore it from backup before saving changes.') from None


def _write(rows):
    INDEX.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.websites-', dir=INDEX.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(rows, stream, ensure_ascii=False)
        os.replace(temporary, INDEX)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def list_entries(site: str | None = None) -> list:
    wanted = origin(site) if site else None
    with _lock:
        return [{key: row[key] for key in ('id', 'origin', 'label', 'updated')}
                for row in _read() if not wanted or row['origin'] == wanted]


def save(url: str, username: str, password: str, label: str = '', cid: str | None = None) -> dict:
    site = origin(url)
    if not username.strip() or not password:
        raise VaultError('Enter both the username and password to save a login.')
    if len(username) > 320 or len(password) > 4096:
        raise VaultError('Username or password exceeds the supported length.')
    with _lock:
        rows = _read()
        replacing = bool(cid)
        if cid and not any(row['id'] == cid and row['origin'] == site for row in rows):
            raise VaultError('This saved login does not belong to this website.')
        cid = cid or uuid.uuid4().hex
        payload = json.dumps({'origin': site, 'username': username, 'password': password}, ensure_ascii=False)
        try:
            backend().set_password(_service(cid), 'login', payload)
        except VaultError:
            raise
        except Exception:
            raise VaultError('Could not save the login in the OS vault. Unlock it and try again.') from None
        row = {'id': cid, 'origin': site, 'label': label.strip()[:100] or site, 'updated': time.time()}
        try:
            _write([r for r in rows if r['id'] != cid] + [row])
        except OSError:
            if not replacing:
                try:
                    backend().delete_password(_service(cid), 'login')
                except Exception:
                    pass
            raise VaultError('The credential index could not be written. Check workspace permissions before saving again.') from None
        return row


def get(cid: str, url: str) -> dict:
    site = origin(url)
    with _lock:
        if not any(row['id'] == cid and row['origin'] == site for row in _read()):
            raise VaultError('No saved login matches this exact website address.')
        try:
            raw = backend().get_password(_service(cid), 'login')
            value = json.loads(raw) if raw else None
        except Exception:
            raise VaultError('Could not read the saved login. Unlock the OS vault and try again.') from None
        if not value or value.get('origin') != site:
            raise VaultError('The saved login is missing or belongs to a different website. Save it again in Settings > Websites.')
        return {'username': value['username'], 'password': value['password']}


def delete(cid: str):
    with _lock:
        rows = _read()
        if not any(row['id'] == cid for row in rows):
            raise VaultError('This saved login no longer exists.')
        try:
            vault = backend()
            if vault.get_password(_service(cid), 'login') is not None:
                vault.delete_password(_service(cid), 'login')
            _write([row for row in rows if row['id'] != cid])
        except Exception:
            raise VaultError('Could not remove the saved login. Unlock the OS vault and try again.') from None
