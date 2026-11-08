"""钉钉企业内部应用 access_token 供给：Adapter 与 ReportSink 共用。

POST /v1.0/oauth2/accessToken → {accessToken, expireIn}；进程内缓存，提前 60s 刷新。
"""
from __future__ import annotations

import time

import httpx

_API_BASE = "https://api.dingtalk.com"


class DingTalkAuth:
    def __init__(self, client_id: str, client_secret: str,
                 http: httpx.AsyncClient | None = None):
        self.client_id = client_id
        self.client_secret = client_secret
        self._http = http or httpx.AsyncClient(timeout=15)
        self._token: str | None = None
        self._token_expire_at = 0.0

    async def access_token(self) -> str:
        if self._token and time.time() < self._token_expire_at - 60:
            return self._token
        r = await self._http.post(f"{_API_BASE}/v1.0/oauth2/accessToken", json={
            "appKey": self.client_id, "appSecret": self.client_secret})
        r.raise_for_status()
        data = r.json()
        self._token = data["accessToken"]
        self._token_expire_at = time.time() + float(data["expireIn"])
        return self._token

    async def auth_headers(self) -> dict:
        return {"x-acs-dingtalk-access-token": await self.access_token()}

    @property
    def http(self) -> httpx.AsyncClient:
        return self._http
