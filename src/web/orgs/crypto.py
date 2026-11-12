"""End-to-end encryption for Phone a Friend.

What a call shares (the thread, and what a friend writes into it) is locked
before it leaves a VibeNode, so the cloud storage, and whoever owns it, holds
only scrambled text. The database still decides who may fetch what; this makes
what they fetch unreadable to anyone but the two ends.

* Each member has an X25519 key pair. The private key stays in this VibeNode's
  membership file (mode 600); the public key is registered with the org.
* Each shared session has a thread key (a Fernet key), made by the caller's
  VibeNode. For every friend on the call it is wrapped with that friend's public
  key (``wrap``: an ephemeral X25519 exchange, HKDF-SHA256, AES-256-GCM) and
  stored on their grant.
* Hanging up or revoking anyone rotates the thread key: later lines use a new
  version, wrapped only for the people still on the call, so a removed friend
  cannot read anything sent after.
* What stays readable to the cloud storage: who called whom, when, the level,
  the session's title and project, the note, and how many lines there are.
"""

from __future__ import annotations

import base64
import json
import os

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_INFO = b"vibenode-phone-a-friend-wrap-v1"


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def new_keypair() -> tuple[str, str]:
    """(private, public), both base64."""
    priv = X25519PrivateKey.generate()
    raw_priv = priv.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                  serialization.NoEncryption())
    raw_pub = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return _b64(raw_priv), _b64(raw_pub)


def new_thread_key() -> str:
    return Fernet.generate_key().decode()


def _kek(shared: bytes, epk: bytes, rpk: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=epk + rpk, info=_INFO).derive(shared)


def wrap(thread_key: str, recipient_public: str) -> str:
    """Lock a thread key for one recipient's public key."""
    rpk = _unb64(recipient_public)
    eph = X25519PrivateKey.generate()
    epk = eph.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    shared = eph.exchange(X25519PublicKey.from_public_bytes(rpk))
    nonce = os.urandom(12)
    ct = AESGCM(_kek(shared, epk, rpk)).encrypt(nonce, thread_key.encode(), _INFO)
    return json.dumps({"v": 1, "epk": _b64(epk), "n": _b64(nonce), "ct": _b64(ct)})


def unwrap(wrapped: str, private_key: str) -> str:
    """The thread key, or ValueError when it is not for this key."""
    try:
        w = json.loads(wrapped)
        priv = X25519PrivateKey.from_private_bytes(_unb64(private_key))
        rpk = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        epk = _unb64(w["epk"])
        shared = priv.exchange(X25519PublicKey.from_public_bytes(epk))
        return AESGCM(_kek(shared, epk, rpk)).decrypt(_unb64(w["n"]), _unb64(w["ct"]), _INFO).decode()
    except Exception as e:
        raise ValueError("not readable with this key") from e


def seal(thread_key: str, obj) -> str:
    return Fernet(thread_key.encode()).encrypt(json.dumps(obj).encode()).decode()


def open_(thread_key: str, token: str):
    try:
        return json.loads(Fernet(thread_key.encode()).decrypt(token.encode()))
    except (InvalidToken, ValueError) as e:
        raise ValueError("not readable with this key") from e
