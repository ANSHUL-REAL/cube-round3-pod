"""The ONLY code that reads SUPABASE_SERVICE_ROLE_KEY (§6.5; test T-SEC-08 enforces this).

The service-role key bypasses Supabase's own access rules, so it is confined here and used for exactly two
things: private-bucket storage operations (upload, signed URLs, bucket setup) and the Auth admin API used
by `returns-manager seed demo` to create demo users. It is never used for table queries (the app's
tables are reached only as rm_app_login) and is never sent to a browser.
"""

from __future__ import annotations

from typing import Any

import httpx
from storage3 import AsyncStorageClient

from returns_manager.config import Settings


class ServiceCredentials:
    def __init__(self, settings: Settings) -> None:
        settings.require("supabase_url", "supabase_service_role_key")
        assert settings.supabase_url is not None
        assert settings.supabase_service_role_key is not None
        self.base_url = settings.supabase_url.rstrip("/")
        self._key = settings.supabase_service_role_key.get_secret_value()

    def __repr__(self) -> str:  # never print the key
        return f"ServiceCredentials(base_url={self.base_url!r})"

    def _headers(self) -> dict[str, str]:
        return {"apikey": self._key, "Authorization": f"Bearer {self._key}"}

    def storage_client(self) -> AsyncStorageClient:
        return AsyncStorageClient(f"{self.base_url}/storage/v1", self._headers())

    async def auth_admin(self, method: str, path: str, json: dict[str, Any] | None = None) -> httpx.Response:
        """Call the Supabase Auth admin API (e.g. POST /admin/users). Used by `seed demo` only."""
        async with httpx.AsyncClient(base_url=f"{self.base_url}/auth/v1", timeout=30) as client:
            return await client.request(method, path, json=json, headers=self._headers())
