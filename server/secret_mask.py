"""Redact recently used private inputs without retaining their plaintext.

Salted fingerprints cover website echoes after fields disappear on navigation.
This is output filtering, not the credential store; secrets live in the OS vault.
"""
from collections import deque
import hashlib
import os
import threading

_salt = os.urandom(32)
_recent = deque(maxlen=96)
_lock = threading.Lock()


def protect(value: str):
    if not value or len(value) < 4:
        return
    fingerprint = (len(value), hashlib.sha256(_salt + value.encode()).digest())
    with _lock:
        if fingerprint not in _recent:
            _recent.append(fingerprint)


def redact(text: str) -> str:
    with _lock:
        recent = list(_recent)
    lengths = {}
    for length, digest in recent:
        lengths.setdefault(length, set()).add(digest)
    # Longest first so overlapping shorter values cannot reveal a suffix.
    for length, fingerprints in sorted(lengths.items(), reverse=True):
        out, start, index = [], 0, 0
        while index + length <= len(text):
            digest = hashlib.sha256(_salt + text[index:index + length].encode()).digest()
            if digest in fingerprints:
                out.extend((text[start:index], '[private input]'))
                start = index = index + length
            else:
                index += 1
        if out:
            text = ''.join(out) + text[start:]
    return text
