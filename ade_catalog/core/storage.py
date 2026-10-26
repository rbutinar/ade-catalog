"""
Cloud storage layer for ade-catalog.

Uses Supabase Storage for org-scoped file management.
Extractions, business docs, and exports are stored per-organization.

Usage:
    from ade_catalog.core.storage import get_storage
    storage = get_storage(config)
    storage.upload_extraction(org_id, "fabric", "workspaces.json", data)
    files = storage.list_extractions(org_id, "fabric")
    content = storage.download_extraction(org_id, "fabric", "workspaces.json")
"""

import json
import logging
from typing import Optional

from ade_catalog.core.config import CatalogConfig

logger = logging.getLogger(__name__)

BUCKET_NAME = "catalog-data"

# Storage folder structure within each org
FOLDERS = {
    "extractions": "extractions",
    "business_docs": "business_docs",
    "exports": "exports",
}


class SupabaseStorage:
    """Org-scoped file storage using Supabase Storage."""

    def __init__(self, config: CatalogConfig):
        from ade_catalog.core.auth import get_supabase_client
        self._sb = get_supabase_client(config)
        self._bucket = BUCKET_NAME
        self._ensure_bucket()

    def _ensure_bucket(self):
        """Create the storage bucket if it doesn't exist."""
        try:
            self._sb.storage.get_bucket(self._bucket)
        except Exception:
            try:
                self._sb.storage.create_bucket(
                    self._bucket,
                    options={"public": False, "file_size_limit": 52428800},  # 50MB
                )
                logger.info("Created storage bucket: %s", self._bucket)
            except Exception as e:
                if "already exists" not in str(e).lower():
                    logger.warning("Could not create bucket: %s", e)

    def _path(self, org_id: str, category: str, platform: str = "", filename: str = "") -> str:
        """Build storage path: {org_id}/{category}/{platform}/{filename}"""
        parts = [org_id, FOLDERS.get(category, category)]
        if platform:
            parts.append(platform)
        if filename:
            parts.append(filename)
        return "/".join(parts)

    # ----- Extractions -----

    def upload_extraction(self, org_id: str, platform: str, filename: str,
                          data: dict | list | bytes, content_type: str = "application/json") -> str:
        """Upload an extraction file. Returns the storage path."""
        path = self._path(org_id, "extractions", platform, filename)

        if isinstance(data, (dict, list)):
            file_bytes = json.dumps(data, indent=2, default=str).encode("utf-8")
            content_type = "application/json"
        elif isinstance(data, str):
            file_bytes = data.encode("utf-8")
        else:
            file_bytes = data

        self._sb.storage.from_(self._bucket).upload(
            path, file_bytes,
            file_options={"content-type": content_type, "upsert": "true"},
        )
        logger.info("Uploaded extraction: %s", path)
        return path

    def download_extraction(self, org_id: str, platform: str, filename: str) -> bytes:
        """Download an extraction file."""
        path = self._path(org_id, "extractions", platform, filename)
        response = self._sb.storage.from_(self._bucket).download(path)
        return response

    def download_extraction_json(self, org_id: str, platform: str, filename: str) -> dict | list:
        """Download and parse a JSON extraction file."""
        data = self.download_extraction(org_id, platform, filename)
        return json.loads(data)

    def list_extractions(self, org_id: str, platform: str = "") -> list[dict]:
        """List extraction files for an org/platform."""
        path = self._path(org_id, "extractions", platform)
        try:
            files = self._sb.storage.from_(self._bucket).list(path)
            return [f for f in files if f.get("name") and not f["name"].startswith(".")]
        except Exception as e:
            logger.warning("Failed to list %s: %s", path, e)
            return []

    def delete_extraction(self, org_id: str, platform: str, filename: str) -> bool:
        """Delete an extraction file."""
        path = self._path(org_id, "extractions", platform, filename)
        try:
            self._sb.storage.from_(self._bucket).remove([path])
            return True
        except Exception as e:
            logger.warning("Failed to delete %s: %s", path, e)
            return False

    # ----- Business Docs -----

    def upload_doc(self, org_id: str, filename: str, data: bytes,
                   content_type: str = "application/octet-stream") -> str:
        """Upload a business document."""
        path = self._path(org_id, "business_docs", filename=filename)
        self._sb.storage.from_(self._bucket).upload(
            path, data,
            file_options={"content-type": content_type, "upsert": "true"},
        )
        return path

    def list_docs(self, org_id: str) -> list[dict]:
        """List business documents for an org."""
        path = self._path(org_id, "business_docs")
        try:
            return self._sb.storage.from_(self._bucket).list(path)
        except Exception:
            return []

    # ----- Exports -----

    def upload_export(self, org_id: str, filename: str, data: bytes,
                      content_type: str = "text/csv") -> str:
        """Upload an export file (CSV, PDF, etc.)."""
        path = self._path(org_id, "exports", filename=filename)
        self._sb.storage.from_(self._bucket).upload(
            path, data,
            file_options={"content-type": content_type, "upsert": "true"},
        )
        return path

    def get_download_url(self, path: str, expires_in: int = 3600) -> str:
        """Get a signed download URL for a file."""
        result = self._sb.storage.from_(self._bucket).create_signed_url(path, expires_in)
        return result.get("signedURL", "")

    # ----- Org management -----

    def get_org_usage(self, org_id: str) -> dict:
        """Get storage usage summary for an org."""
        usage = {"extractions": 0, "business_docs": 0, "exports": 0, "total_files": 0}
        for category in FOLDERS:
            files = self._sb.storage.from_(self._bucket).list(
                self._path(org_id, category)
            )
            if files:
                count = len([f for f in files if f.get("name")])
                usage[category] = count
                usage["total_files"] += count
        return usage


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_storage_instance: Optional[SupabaseStorage] = None


def get_storage(config: CatalogConfig) -> SupabaseStorage:
    """Get the storage instance (singleton)."""
    global _storage_instance
    if _storage_instance is None:
        _storage_instance = SupabaseStorage(config)
    return _storage_instance
