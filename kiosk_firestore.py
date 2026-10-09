# -*- coding: utf-8 -*-
"""Least-privilege Firestore access for the kiosk (audit C-02, roadmap 7.4).

The kiosk signs in as its own Firebase Auth user (custom claim `kiosk: true`, created by
tools/set_claims.py) and talks to Firestore over the REST API with that user's ID token, so the
Security Rules apply to it. firebase/firestore.rules lets this identity do exactly four things:
create a well-formed QR token, read a token by id, write its own heartbeat, add a detection.
A stolen kiosk therefore cannot read users, points or redemptions, and its access is revoked by
disabling that one Auth user - unlike the service-account key, which bypasses all rules.

The config file (default ~/.ecoreward/kiosk.json, written by tools/set_claims.py) holds the project's
public Web API key, the project id and a revocable refresh token - never a password or a private key.

Only the small subset of the firebase_admin Firestore API that app.py uses is implemented:
    db.collection(c).document(id).set(data, merge=False)
    db.collection(c).document(id).get() / .on_snapshot(callback)   (polled: REST has no listeners)
    db.collection(c).add(data)
"""
import base64
import json
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests

SERVER_TIMESTAMP = object()   # sentinel: replaced by the server's request time, like firestore.SERVER_TIMESTAMP

_FIRESTORE = "https://firestore.googleapis.com/v1"
_SECURETOKEN = "https://securetoken.googleapis.com/v1/token"
_TIMEOUT = 10


class KioskFirestoreError(RuntimeError):
    def __init__(self, status, message):
        super().__init__(f"Firestore {status}: {message}")
        self.status = status


# ── value encoding (Firestore REST "Value" objects) ───────────────────────────
def _encode(v):
    if v is None:
        return {"nullValue": None}
    if isinstance(v, bool):
        return {"booleanValue": v}
    if isinstance(v, int):
        return {"integerValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    if isinstance(v, str):
        return {"stringValue": v}
    if isinstance(v, datetime):
        if v.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware (UTC)")
        return {"timestampValue": v.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")}
    if isinstance(v, (list, tuple)):
        return {"arrayValue": {"values": [_encode(x) for x in v]}}
    if isinstance(v, dict):
        return {"mapValue": {"fields": {k: _encode(x) for k, x in v.items()}}}
    raise TypeError(f"cannot store {type(v).__name__} in Firestore")


def _decode(v):
    if "nullValue" in v:
        return None
    if "booleanValue" in v:
        return v["booleanValue"]
    if "integerValue" in v:
        return int(v["integerValue"])
    if "doubleValue" in v:
        return float(v["doubleValue"])
    if "stringValue" in v:
        return v["stringValue"]
    if "timestampValue" in v:
        s = v["timestampValue"].rstrip("Z")
        main, _, frac = s.partition(".")
        dt = datetime.strptime(main, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.replace(microsecond=int((frac + "000000")[:6])) if frac else dt
    if "arrayValue" in v:
        return [_decode(x) for x in v["arrayValue"].get("values", [])]
    if "mapValue" in v:
        return {k: _decode(x) for k, x in v["mapValue"].get("fields", {}).items()}
    return None   # bytes / geo / reference values are not used by this project


def build_write(doc_name, data, merge=False, must_not_exist=False):
    """One Firestore `Write` for documents:commit. SERVER_TIMESTAMP values become server transforms."""
    fields, transforms = {}, []
    for key, value in data.items():
        if value is SERVER_TIMESTAMP:
            transforms.append({"fieldPath": key, "setToServerValue": "REQUEST_TIME"})
        else:
            fields[key] = _encode(value)
    write = {"update": {"name": doc_name, "fields": fields}}
    if merge:
        write["updateMask"] = {"fieldPaths": sorted(fields)}
    if transforms:
        write["updateTransforms"] = transforms
    if must_not_exist:
        write["currentDocument"] = {"exists": False}
    return write


# ── auth ───────────────────────────────────────────────────────────────────────
class RefreshTokenSource:
    """Exchanges the stored refresh token for short-lived ID tokens (cached until 5 min before expiry)."""

    def __init__(self, api_key, refresh_token, http=requests):
        self._api_key, self._refresh, self._http = api_key, refresh_token, http
        self._token, self._expires, self.uid = None, 0.0, None
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            if self._token and time.time() < self._expires - 300:
                return self._token
            r = self._http.post(f"{_SECURETOKEN}?key={self._api_key}",
                                data={"grant_type": "refresh_token", "refresh_token": self._refresh},
                                timeout=_TIMEOUT)
            if r.status_code != 200:
                raise KioskFirestoreError(r.status_code, _error_message(r) or "could not sign in the kiosk")
            body = r.json()
            self._token, self._refresh = body["id_token"], body.get("refresh_token", self._refresh)
            self._expires = time.time() + int(body.get("expires_in", 3600))
            self.uid = body.get("user_id")
            return self._token


def _error_message(response):
    try:
        return response.json().get("error", {}).get("message", "")
    except ValueError:
        return response.text[:200]


def _uid_from_jwt(token):
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return claims.get("user_id") or claims.get("sub")
    except (IndexError, ValueError):
        return None


# ── client ─────────────────────────────────────────────────────────────────────
class KioskFirestore:
    def __init__(self, project_id, token_source, base_url=_FIRESTORE, http=requests):
        self.project_id, self._token, self._http = project_id, token_source, http
        self._root = f"projects/{project_id}/databases/(default)/documents"
        self._base = f"{base_url.rstrip('/')}/{self._root}"

    @classmethod
    def from_config(cls, path):
        cfg = json.loads(Path(path).read_text(encoding="utf-8"))
        missing = [k for k in ("apiKey", "projectId", "refreshToken") if not cfg.get(k)]
        if missing:
            raise ValueError(f"kiosk config {path} is missing: {', '.join(missing)}")
        return cls(cfg["projectId"], RefreshTokenSource(cfg["apiKey"], cfg["refreshToken"]))

    @property
    def uid(self):
        """The kiosk's Auth uid (used as its kiosks/{uid} id). Signs in if needed."""
        token = self._token()
        return getattr(self._token, "uid", None) or _uid_from_jwt(token)

    def check(self):
        """Sign in once so a wrong/revoked config fails at start-up, not at the first QR."""
        return self.uid

    def collection(self, name):
        return _Collection(self, name)

    # -- HTTP --
    def _headers(self):
        return {"Authorization": f"Bearer {self._token()}"}

    def _commit(self, writes):
        r = self._http.post(f"{self._base}:commit", json={"writes": writes}, headers=self._headers(), timeout=_TIMEOUT)
        if r.status_code != 200:
            raise KioskFirestoreError(r.status_code, _error_message(r))
        return r.json()

    def _get(self, path):
        r = self._http.get(f"{self._base}/{path}", headers=self._headers(), timeout=_TIMEOUT)
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise KioskFirestoreError(r.status_code, _error_message(r))
        return {k: _decode(v) for k, v in r.json().get("fields", {}).items()}


class _Collection:
    def __init__(self, db, name):
        self._db, self.name = db, name

    def document(self, doc_id):
        return _Document(self._db, self.name, doc_id)

    def add(self, data):
        doc_id = uuid.uuid4().hex[:20]
        self.document(doc_id).set(data, _create=True)
        return doc_id


class _Snapshot:
    def __init__(self, doc_id, data):
        self.id, self._data = doc_id, data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class _Document:
    def __init__(self, db, collection, doc_id):
        self._db, self._path, self.id = db, f"{collection}/{doc_id}", doc_id

    def set(self, data, merge=False, _create=False):
        name = f"{self._db._root}/{self._path}"
        self._db._commit([build_write(name, data, merge=merge, must_not_exist=_create)])

    def get(self):
        return _Snapshot(self.id, self._db._get(self._path))

    def on_snapshot(self, callback):
        return _PollWatcher(self, callback)


class _PollWatcher:
    """Stands in for a realtime listener: polls the document and calls back when it changes.
    Every 2 s for the first 2 minutes a QR is shown, then every 5 s (fewer billed reads)."""

    def __init__(self, document, callback, fast=2.0, slow=5.0, fast_for=120.0):
        self._doc, self._cb = document, callback
        self._fast, self._slow, self._fast_for = fast, slow, fast_for
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"poll-{document.id[:8]}")
        self._thread.start()

    def _run(self):
        started, last = time.monotonic(), None
        while True:
            interval = self._fast if time.monotonic() - started < self._fast_for else self._slow
            if self._stop.wait(interval):
                return
            try:
                snap = self._doc.get()
            except Exception:
                continue   # network hiccup: try again next tick
            fingerprint = json.dumps(snap.to_dict(), sort_keys=True, default=str)
            if fingerprint != last and not self._stop.is_set():
                last = fingerprint
                self._cb([snap], [], None)

    def unsubscribe(self):
        self._stop.set()
