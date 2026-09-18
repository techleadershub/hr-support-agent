"""Thin PostgREST client for the Supabase-backed HR system of record.

Only the hr_* tables and the hr_reset / hr_apply_leave functions are touched.
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from . import config


class DBError(Exception):
    def __init__(self, status: int, detail: Any):
        super().__init__(f"db error {status}: {detail}")
        self.status = status
        self.detail = detail


def _headers(extra: dict | None = None) -> dict:
    h = {
        "apikey": config.SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {config.SUPABASE_ANON_KEY}",
        "Content-Type": "application/json",
    }
    if extra:
        h.update(extra)
    return h


def _client() -> httpx.Client:
    return httpx.Client(base_url=f"{config.SUPABASE_URL}/rest/v1", timeout=15.0)


def _raise(resp: httpx.Response) -> None:
    if resp.status_code >= 400:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        raise DBError(resp.status_code, detail)


def select(table: str, params: dict[str, str] | None = None, single: bool = False) -> Any:
    with _client() as c:
        resp = c.get(f"/{table}", params=params or {}, headers=_headers())
    _raise(resp)
    rows = resp.json()
    if single:
        return rows[0] if rows else None
    return rows


def insert(table: str, rows: dict | list[dict], returning: bool = True) -> Any:
    prefer = "return=representation" if returning else "return=minimal"
    with _client() as c:
        resp = c.post(f"/{table}", content=json.dumps(rows, default=str), headers=_headers({"Prefer": prefer}))
    _raise(resp)
    return resp.json() if returning else None


def update(table: str, match: dict[str, str], values: dict) -> Any:
    params = {k: f"eq.{v}" for k, v in match.items()}
    with _client() as c:
        resp = c.patch(f"/{table}", params=params, content=json.dumps(values, default=str),
                       headers=_headers({"Prefer": "return=representation"}))
    _raise(resp)
    return resp.json()


def rpc(fn: str, args: dict | None = None) -> Any:
    with _client() as c:
        resp = c.post(f"/rpc/{fn}", content=json.dumps(args or {}, default=str), headers=_headers())
    _raise(resp)
    return resp.json()
