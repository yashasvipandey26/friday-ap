"""Where the original invoice files are kept: a local folder, or a private Supabase Storage bucket.

Runs store only a key (`runs/3f2a….pdf`). The pipeline reads the bytes back through `load`, and the
browser gets either a short-lived signed link (Supabase) or the file served by the app (local).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)


class FileStoreError(Exception):
    """The file couldn't be saved or fetched. The message is shown to the user as-is."""


class FileStore(Protocol):
    def save(self, key: str, data: bytes, media_type: str) -> None: ...
    def load(self, key: str) -> bytes: ...
    def link(self, key: str) -> str | None:
        """A direct link for the browser, or None when the app should serve the file itself."""


class LocalFileStore:
    def __init__(self, folder: Path) -> None:
        self._folder = folder
        folder.mkdir(parents=True, exist_ok=True)

    def path(self, key: str) -> Path:
        path = (self._folder / key).resolve()
        if self._folder.resolve() not in path.parents:
            raise FileStoreError("That file key points outside the upload folder.")
        return path

    def save(self, key: str, data: bytes, media_type: str) -> None:
        path = self.path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def load(self, key: str) -> bytes:
        try:
            return self.path(key).read_bytes()
        except FileNotFoundError as error:
            raise FileStoreError("The original file is no longer stored.") from error

    def link(self, key: str) -> str | None:
        return None


class SupabaseFileStore:
    """Supabase Storage over its REST API. The bucket is private; every link expires after an hour."""

    def __init__(self, project_url: str, service_key: str, bucket: str, client: httpx.Client | None = None) -> None:
        self._base = project_url.rstrip("/") + "/storage/v1"
        self._bucket = bucket
        self._client = client or httpx.Client(timeout=30)
        # A legacy service_role key is a JWT and also goes in Authorization; the newer sb_secret_ keys
        # go in the apikey header only, and Supabase turns them into a token itself.
        self._headers = {"apikey": service_key}
        if service_key.startswith("eyJ"):
            self._headers["Authorization"] = f"Bearer {service_key}"

    def ensure_bucket(self) -> None:
        """Create the bucket on first start, so there is nothing to click in the Supabase dashboard."""
        response = self._request("GET", f"/bucket/{self._bucket}")
        if response.status_code == 200:
            return
        response = self._request("POST", "/bucket", json={"id": self._bucket, "name": self._bucket, "public": False})
        if response.status_code not in (200, 201) and "already exists" not in response.text.lower():
            raise FileStoreError(f"Couldn't create the storage bucket “{self._bucket}”: {response.text[:200]}")

    def save(self, key: str, data: bytes, media_type: str) -> None:
        response = self._request("POST", f"/object/{self._bucket}/{quote(key)}", content=data,
                                 headers={"Content-Type": media_type, "x-upsert": "true"})
        if response.status_code not in (200, 201):
            raise FileStoreError(f"Couldn't store the file ({response.status_code}): {response.text[:200]}")

    def load(self, key: str) -> bytes:
        response = self._request("GET", f"/object/{self._bucket}/{quote(key)}")
        if response.status_code != 200:
            raise FileStoreError(f"Couldn't fetch the stored file ({response.status_code}).")
        return response.content

    def link(self, key: str) -> str | None:
        response = self._request("POST", f"/object/sign/{self._bucket}/{quote(key)}", json={"expiresIn": 3600})
        if response.status_code != 200:
            raise FileStoreError(f"Couldn't make a link to the stored file ({response.status_code}).")
        return self._base + response.json()["signedURL"]

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        headers = {**self._headers, **kwargs.pop("headers", {})}
        try:
            return self._client.request(method, self._base + path, headers=headers, **kwargs)
        except httpx.HTTPError as error:
            raise FileStoreError("Couldn't reach file storage. Check SUPABASE_URL and the network.") from error


# Decide the file type from its first bytes; a file name or browser-supplied type can say anything.
_FILE_SIGNATURES = {
    b"%PDF": ("application/pdf", ".pdf"),
    b"\x89PNG": ("image/png", ".png"),
    b"\xff\xd8\xff": ("image/jpeg", ".jpg"),
}


def identify_file(data: bytes) -> tuple[str, str] | None:
    """(media type, extension) for a PDF, PNG, JPEG or WebP; None for anything else."""
    for signature, file_type in _FILE_SIGNATURES.items():
        if data.startswith(signature):
            return file_type
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    return None
