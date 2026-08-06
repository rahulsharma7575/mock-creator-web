#!/usr/bin/env python3
"""
magnific_mcp.py — Magnific image generation for mock_next.py.

Two paths:
  A) MAGNIFIC_API_KEY REST (preferred, stable): POST /v1/ai/text-to-image/z-image
     on api.magnific.com with `x-magnific-api-key` header -> poll task -> image URL.
  B) OAuth/MCP fallback: reads the opencode OAuth token and speaks MCP JSON-RPC
     over HTTP (used only when no API key is set).

    from magnific_mcp import generate_image
    url = generate_image("...prompt...")
"""
import json
import os
import sys
import time
from pathlib import Path

import httpx

URL = "https://mcp.magnific.com"
API_URL = "https://api.magnific.com"
TOKEN_FILE = Path.home() / ".local" / "share" / "opencode" / "mcp-auth.json"
PROTOCOL = "2025-03-26"
CLIENT = {"name": "mock-next", "version": "1.0"}
ZIMAGE_COST = 5  # credits per z-image (verified via simulate_cost, 2026-08-04)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
except Exception:
    pass


class MagnificError(RuntimeError):
    pass


class ModelNotFoundError(MagnificError):
    """The requested model is not exposed on the REST endpoint (HTTP 404)."""


# ---------------------------------------------------------------------------
# Path A — API-key REST (preferred)
# ---------------------------------------------------------------------------

def api_key():
    key = os.environ.get("MAGNIFIC_API_KEY")
    if key:
        return key
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("MAGNIFIC_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def _api_headers():
    key = api_key()
    if not key:
        return None
    return {"x-magnific-api-key": key, "Content-Type": "application/json",
            "Accept": "application/json"}


def generate_image_api(prompt, model="z-image", image_size="square_hd", timeout_s=240):
    """REST text-to-image: POST task for `model` -> poll status -> image URL.

    model is any slug served under /v1/ai/text-to-image/<model> (verified:
    "z-image"). Models the REST API does not expose (e.g. p-image-ideogram)
    return HTTP 404 -> ModelNotFoundError so callers can auto-degrade."""
    headers = _api_headers()
    r = httpx.post(API_URL + f"/v1/ai/text-to-image/{model}", headers=headers,
                   json={"prompt": prompt, "image_size": image_size}, timeout=60)
    if r.status_code == 401:
        raise MagnificError("Magnific API key rejected (401) — check MAGNIFIC_API_KEY in .env")
    if r.status_code == 404:
        raise ModelNotFoundError(f"model '{model}' not exposed on Magnific REST API (404) — degrading to z-image")
    if r.status_code != 200:
        raise MagnificError(f"{model} POST HTTP {r.status_code}: {r.text[:200]}")
    task = (r.json().get("data") or {}).get("task_id")
    if not task:
        raise MagnificError(f"no task_id in {model} response: {r.text[:200]}")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        s = httpx.get(API_URL + f"/v1/ai/text-to-image/{model}/{task}",
                      headers=headers, timeout=60)
        if s.status_code != 200:
            raise MagnificError(f"{model} status HTTP {s.status_code}: {s.text[:200]}")
        data = s.json().get("data") or {}
        status = str(data.get("status", "")).upper()
        if status in ("COMPLETED", "SUCCESS"):
            urls = data.get("generated") or []
            if not urls:
                raise MagnificError("task completed but no generated URLs")
            return urls[0]
        if status in ("FAILED", "ERROR", "CANCELLED"):
            raise MagnificError(f"{model} task {status}: {s.text[:200]}")
        time.sleep(4)
    raise MagnificError(f"{model} task timed out after {timeout_s}s")


def _tokens():
    if not TOKEN_FILE.exists():
        raise MagnificError(
            f"mcp-auth.json not found at {TOKEN_FILE} — start opencode once (it logs in Magnific), then retry")
    data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    mag = data.get("magnific") or {}
    toks = mag.get("tokens") or {}
    return toks


def read_access_token() -> str:
    toks = _tokens()
    tok = toks.get("accessToken")
    if not tok:
        raise MagnificError("no Magnific accessToken in mcp-auth.json")
    expires = toks.get("expiresAt")
    if expires and isinstance(expires, (int, float)) and expires < time.time() + 120:
        tok = _refresh(toks)
    return tok


def _refresh(toks) -> str:
    """OAuth2 refresh_token grant. Endpoint discovered via well-known metadata."""
    refresh = toks.get("refreshToken")
    if not refresh:
        raise MagnificError("Magnific token expired and no refreshToken stored — restart opencode to re-login")
    endpoint = None
    try:
        r = httpx.get(URL.rstrip("/") + "/.well-known/oauth-authorization-server", timeout=20)
        if r.status_code == 200:
            endpoint = r.json().get("token_endpoint")
    except Exception:
        pass
    if not endpoint:
        endpoint = URL.rstrip("/") + "/oauth/token"
    r = httpx.post(endpoint, data={
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": "opencode",
    }, timeout=30)
    if r.status_code != 200:
        raise MagnificError(f"token refresh failed HTTP {r.status_code} — restart opencode to re-login Magnific")
    new = r.json()
    toks["accessToken"] = new.get("access_token", toks["accessToken"])
    if new.get("refresh_token"):
        toks["refreshToken"] = new["refresh_token"]
    if new.get("expires_in"):
        toks["expiresAt"] = time.time() + new["expires_in"]
    TOKEN_FILE.write_text(json.dumps(json.loads(TOKEN_FILE.read_text(encoding="utf-8")),
                                     ensure_ascii=False, indent=2), encoding="utf-8")
    return toks["accessToken"]


class MagnificSession:
    """One MCP session over Streamable HTTP."""

    def __init__(self, token: str):
        self._id = 0
        self._client = httpx.Client(timeout=300)
        self._headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
        }
        self._open(token)

    def _open(self, token):
        r = self._client.post(URL, headers=self._headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": PROTOCOL, "capabilities": {},
                       "clientInfo": CLIENT},
        })
        if r.status_code == 401:
            raise MagnificError("Magnific auth failed (401) — token invalid")
        if r.status_code != 200:
            raise MagnificError(f"Magnific initialize HTTP {r.status_code}: {r.text[:200]}")
        sid = r.headers.get("mcp-session-id") or r.headers.get("Mcp-Session-Id")
        if sid:
            self._headers["Mcp-Session-Id"] = sid
        self._client.post(URL, headers=self._headers,
                          json={"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _rpc(self, method: str, params: dict):
        self._id += 1
        r = self._client.post(URL, headers=self._headers, json={
            "jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
        if r.status_code == 401:
            raise MagnificError("Magnific session expired (401) — re-run to refresh token")
        if r.status_code != 200:
            raise MagnificError(f"Magnific {method} HTTP {r.status_code}: {r.text[:200]}")
        payload = self._parse(r)
        if "error" in payload:
            raise MagnificError(f"Magnific {method} error: {json.dumps(payload['error'], ensure_ascii=False)[:300]}")
        return payload.get("result", {})

    @staticmethod
    def _parse(r):
        if r.headers.get("content-type", "").startswith("text/event-stream"):
            for line in r.text.splitlines():
                if line.startswith("data:"):
                    try:
                        d = json.loads(line[5:].strip())
                        if isinstance(d, dict) and ("result" in d or "error" in d):
                            return d
                    except Exception:
                        continue
            raise MagnificError("SSE response contained no result")
        return r.json()

    def call_tool(self, name: str, args: dict) -> dict:
        """tools/call -> parsed result. Prefers structuredContent; falls back to content[0].text JSON."""
        result = self._rpc("tools/call", {"name": name, "arguments": args})
        sc = result.get("structuredContent")
        if isinstance(sc, dict):
            return sc
        content = result.get("content") or []
        for block in content:
            if block.get("type") == "text":
                text = block.get("text", "")
                try:
                    return json.loads(text)
                except Exception:
                    return {"text": text}
        return {"text": ""}

    def close(self):
        try:
            self._client.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def check_balance() -> dict:
    """API-key mode: no balance endpoint (returns None). OAuth mode: account_balance."""
    if api_key():
        return None
    with MagnificSession(read_access_token()) as s:
        return s.call_tool("account_balance", {})


def simulate_cost(prompt: str) -> dict:
    with MagnificSession(read_access_token()) as s:
        return s.call_tool("simulate_cost", {
            "tool": "images_generate",
            "arguments": {"mode": "z-image", "prompt": prompt, "aspectRatio": "1:1", "count": 1},
        })


def generate_image(prompt: str, model: str = "z-image", aspect: str = "1:1", timeout_s: int = 240) -> str:
    """Generate ONE image for `model`; returns the full-resolution image URL.

    API-key mode: REST /v1/ai/text-to-image/<model>; models the REST API does
    not expose (404, e.g. p-image-ideogram) auto-degrade to z-image with a
    warning. OAuth/MCP mode: images_generate with mode=<model>.
    """
    if api_key():
        try:
            return generate_image_api(prompt, model=model, timeout_s=timeout_s)
        except ModelNotFoundError as e:
            print(f"[magnific] WARNING: {e}")
            if model != "z-image":
                return generate_image_api(prompt, model="z-image", timeout_s=timeout_s)
            raise
    with MagnificSession(read_access_token()) as s:
        res = s.call_tool("images_generate", {
            "mode": model, "prompt": prompt, "aspectRatio": aspect, "count": 1})
        ids = _extract_ids(res)
        if not ids:
            raise MagnificError(f"no creation identifier in images_generate result: {str(res)[:300]}")
        return _wait_and_url(s, ids[0], timeout_s)


def _extract_ids(res) -> list:
    if isinstance(res, list):
        return [i for i in res if isinstance(i, str)]
    sc = res.get("structuredContent") if isinstance(res, dict) else None
    if isinstance(sc, dict):
        creations = sc.get("creations") or []
        if creations:
            return [c.get("identifier") for c in creations if c.get("identifier")]
    for key in ("creations", "identifiers", "creationIdentifiers", "ids", "results"):
        v = res.get(key)
        if isinstance(v, list):
            out = []
            for i in v:
                if isinstance(i, dict):
                    out.append(i.get("identifier") or i.get("id"))
                else:
                    out.append(str(i))
            return [x for x in out if x]
    for key in ("identifier", "creationIdentifier", "id"):
        v = res.get(key)
        if v:
            return [v]
    return []


def _blob_parse(text) -> dict:
    """Parse 'key: value' / 'key: \"value\"' text blobs into a dict."""
    out = {}
    for line in (text or "").splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip().strip('"')
    return out


def _wait_and_url(s: MagnificSession, ident: str, timeout_s: int) -> str:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            wait = s.call_tool("creations_wait", {"identifiers": [ident], "timeoutSeconds": 25})
            if isinstance(wait, dict) and "text" in wait and len(wait) == 1:
                wait = _blob_parse(wait["text"])
            entries = wait.get("results") if isinstance(wait, dict) else None
            if isinstance(entries, list):
                for e in entries:
                    if e.get("identifier") == ident:
                        st = str(e.get("status", "")).upper()
                        if st in ("COMPLETED", "DONE", "SUCCESS"):
                            return _creation_url(s, ident)
                        if st in ("FAILED", "ERROR", "CANCELLED"):
                            raise MagnificError(f"creation {ident} {st}: {str(e)[:200]}")
            elif wait.get("status"):
                st = str(wait.get("status")).upper()
                if st in ("COMPLETED", "DONE", "SUCCESS"):
                    return _creation_url(s, ident)
                if st in ("FAILED", "ERROR", "CANCELLED"):
                    raise MagnificError(f"creation {ident} {st}")
        except MagnificError:
            raise
        except Exception:
            pass
        time.sleep(4)
    raise MagnificError(f"creation {ident} timed out after {timeout_s}s")


def _creation_url(s: MagnificSession, ident: str) -> str:
    got = s.call_tool("creations_get", {"creationIdentifier": ident})
    if isinstance(got, dict):
        if "text" in got and len(got) == 1:
            blob = _blob_parse(got["text"])
            if blob.get("url"):
                return blob["url"]
        for key in ("url", "originalUrl"):
            if got.get(key):
                return got[key]
        for k, v in got.items():
            if isinstance(v, dict):
                for key in ("url", "originalUrl"):
                    if v.get(key):
                        return v[key]
    raise MagnificError(f"no url in creations_get: {str(got)[:300]}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Magnific MCP direct client (z-image)")
    ap.add_argument("--balance", action="store_true", help="show account balance")
    ap.add_argument("--cost", default="", help="simulate cost for a prompt")
    ap.add_argument("--gen", default="", help="generate one image from a prompt (prints URL)")
    ap.add_argument("--model", default="z-image", help="Magnific model slug (default z-image)")
    args = ap.parse_args()
    if args.balance:
        print(check_balance())
    elif args.cost:
        print(simulate_cost(args.cost))
    elif args.gen:
        print(generate_image(args.gen, model=args.model))
    else:
        ap.print_help()
