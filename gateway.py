#!/usr/bin/env python3
"""
PokéTracker – cloudová HTTP brána s curl_cffi pro Alza.cz a Smarty.cz.

Slouží jako lehký kontejner (Koyeb Free / Render Free / vlastní VPS) pro Supabase Edge Function `monitor`:
- GET / nebo /health        -> veřejný healthcheck (200 OK, bez citlivých dat)
- GET /fetch?url=...        -> stáhne cílovou stránku přes curl_cffi (TLS otisk Chromu + cookies)
                               a vrátí zmenšené HTML pro `config.proxy_url` v monitoru.
                               Volitelný parametr `extra` (lze uvést víckrát) stáhne další stránky
                               téhož výpisu (např. 2. stranu Alzy) s pauzou a hlavičkou Referer.

Ověření: hlavička `x-cron-token` (nebo query parametr `token`) musí odpovídat `POKETRACKER_CRON_TOKEN`.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlsplit

from aiohttp import web
from curl_cffi.requests import AsyncSession

GATEWAY_VERSION = "2026-11-04.1"
FETCH_TIMEOUT_SEC = 25
DOMAIN_GAP_SEC = 2.0
PAGE_GAP_SEC = (1.2, 2.5)
DEFAULT_ALLOWED_SUFFIXES = ("alza.cz", "alza.sk", "smarty.cz", "smarty.sk")

BLOCK_MARKERS = (
    "cf-chl",
    "cf_chl_opt",
    "challenge-platform",
    "just a moment...",
    "attention required! | cloudflare",
    "zablokovaly váš přístup",
    "z masa a kostí",
    "kontrolujeme vaši bezpečnost",
    "moc přístupů",
    "_incapsula_",
    "px-captcha",
    "captcha-delivery",
)

_RE_SCRIPT = re.compile(r"<script\b(?![^>]*application/ld\+json)[^>]*>.*?</script\s*>", re.I | re.S)
_RE_DROP = re.compile(r"<(style|svg|noscript)\b[^>]*>.*?</\1\s*>", re.I | re.S)
_RE_COMMENT = re.compile(r"<!--.*?-->", re.S)
_RE_WS = re.compile(r"\s{2,}")

log = logging.getLogger("poketracker.gateway")


def slim_html(html: str) -> str:
    """Vyhodí skripty (kromě JSON-LD), styly, SVG a komentáře; produktové karty zůstanou nedotčené."""
    out = _RE_SCRIPT.sub(" ", html)
    out = _RE_DROP.sub(" ", out)
    out = _RE_COMMENT.sub(" ", out)
    return _RE_WS.sub(" ", out)


def decode_body(content: bytes, content_type: Optional[str]) -> str:
    charset = None
    m = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    if m:
        charset = m.group(1)
    else:
        m2 = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", content[:4096], re.I)
        if m2:
            charset = m2.group(1).decode("ascii", "ignore")
    try:
        return content.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return content.decode("utf-8", errors="replace")


@dataclass
class Page:
    url: str
    final_url: str
    status: int
    html: str
    ms: int

    def block_reason(self) -> Optional[str]:
        sample = self.html[:30000].lower()
        if self.status in (403, 429, 503):
            cf = " (Cloudflare)" if ("cloudflare" in sample or "ray id" in sample) else ""
            return f"HTTP {self.status}{cf}"
        if "/cdn-cgi/challenge-platform" in self.final_url:
            return "Cloudflare challenge redirect"
        if len(self.html) < 120_000:
            for mk in BLOCK_MARKERS:
                if mk in sample:
                    return f"bot protection ({mk})"
        return None


class Fetcher:
    """Drží samostatnou curl_cffi session (cookies) pro každou doménu; při blokaci ji zahodí."""

    def __init__(self, default_impersonate: str, proxy: Optional[str]):
        self.default_impersonate = default_impersonate
        self.proxy = proxy
        self.sessions: Dict[Tuple[str, str], AsyncSession] = {}
        self.warmed: Set[Tuple[str, str]] = set()
        self.locks: Dict[str, asyncio.Lock] = {}
        self.last_hit: Dict[str, float] = {}

    def _create_session(self, impersonate: str) -> AsyncSession:
        kwargs: Dict[str, Any] = {"impersonate": impersonate, "timeout": FETCH_TIMEOUT_SEC}
        if self.proxy:
            kwargs["proxies"] = {"http": self.proxy, "https": self.proxy}
        try:
            return AsyncSession(**kwargs)
        except Exception:
            kwargs["impersonate"] = "chrome"
            return AsyncSession(**kwargs)

    def _session(self, host: str, impersonate: str) -> AsyncSession:
        key = (host, impersonate)
        s = self.sessions.get(key)
        if s is None:
            s = self._create_session(impersonate)
            self.sessions[key] = s
        return s

    async def reset(self, host: str) -> None:
        for key in [k for k in self.sessions if k[0] == host]:
            s = self.sessions.pop(key)
            self.warmed.discard(key)
            try:
                await s.close()
            except Exception:
                pass

    async def close(self) -> None:
        for s in list(self.sessions.values()):
            try:
                await s.close()
            except Exception:
                pass
        self.sessions.clear()

    async def get(self, url: str, impersonate: str, referer: Optional[str] = None) -> Page:
        parts = urlsplit(url)
        host = parts.netloc.lower()
        lock = self.locks.setdefault(host, asyncio.Lock())
        async with lock:
            gap = DOMAIN_GAP_SEC - (time.monotonic() - self.last_hit.get(host, 0.0))
            if gap > 0:
                await asyncio.sleep(gap)
            s = self._session(host, impersonate)
            key = (host, impersonate)
            headers = {"Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8"}
            if key not in self.warmed:
                try:
                    await s.get(f"{parts.scheme}://{parts.netloc}/", headers=headers, timeout=FETCH_TIMEOUT_SEC)
                except Exception as e:
                    log.debug("warm-up %s selhal: %s", host, e)
                self.warmed.add(key)
                await asyncio.sleep(random.uniform(1.0, 2.0))
            if referer:
                headers["Referer"] = referer
            t0 = time.monotonic()
            try:
                r = await s.get(url, headers=headers, timeout=FETCH_TIMEOUT_SEC, allow_redirects=True)
            finally:
                self.last_hit[host] = time.monotonic()
            html = decode_body(r.content, r.headers.get("content-type"))
            return Page(
                url=url,
                final_url=str(r.url or url),
                status=r.status_code,
                html=html,
                ms=int((time.monotonic() - t0) * 1000),
            )


def is_allowed_url(raw_url: str, suffixes: Tuple[str, ...]) -> bool:
    try:
        parts = urlsplit(raw_url)
    except Exception:
        return False
    if parts.scheme.lower() != "https" or not parts.hostname:
        return False
    host = parts.hostname.lower().rstrip(".")
    return any(host == sfx or host.endswith("." + sfx) for sfx in suffixes)


def build_app() -> web.Application:
    token = os.environ.get("POKETRACKER_CRON_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Chybí proměnná prostředí POKETRACKER_CRON_TOKEN.")

    impersonate = os.environ.get("POKETRACKER_IMPERSONATE", "").strip() or "chrome"
    proxy = os.environ.get("POKETRACKER_PROXY", "").strip() or None
    raw_hosts = os.environ.get("POKETRACKER_ALLOWED_HOSTS", "").strip()
    suffixes = (
        tuple(h.strip().lower().lstrip(".") for h in raw_hosts.split(",") if h.strip())
        if raw_hosts
        else DEFAULT_ALLOWED_SUFFIXES
    )
    started_at = time.monotonic()
    fetcher = Fetcher(default_impersonate=impersonate, proxy=proxy)

    async def handle_health(_req: web.Request) -> web.Response:
        return web.json_response(
            {
                "ok": True,
                "service": "poketracker-gateway",
                "version": GATEWAY_VERSION,
                "uptime_sec": int(time.monotonic() - started_at),
            }
        )

    async def handle_fetch(req: web.Request) -> web.Response:
        supplied = (req.headers.get("x-cron-token") or req.query.get("token") or "").strip()
        if not supplied or not hmac.compare_digest(supplied, token):
            return web.Response(status=403, text="forbidden\n")

        url = (req.query.get("url") or "").strip()
        if not url or not is_allowed_url(url, suffixes):
            return web.json_response({"ok": False, "error": "nepovolená nebo chybějící URL"}, status=400)

        extras = [u.strip() for u in req.query.getall("extra", []) if u.strip()]
        for u in extras:
            if not is_allowed_url(u, suffixes):
                return web.json_response({"ok": False, "error": f"nepovolená extra URL: {u}"}, status=400)

        imp = (req.query.get("impersonate") or "").strip() or fetcher.default_impersonate
        do_slim = (req.query.get("slim") or "1").strip().lower() not in ("0", "false", "no")

        urls: List[str] = [url] + extras[:3]
        pages: List[Page] = []
        referer: Optional[str] = None

        for idx, target_url in enumerate(urls):
            if idx > 0:
                await asyncio.sleep(random.uniform(*PAGE_GAP_SEC))
            try:
                page = await fetcher.get(target_url, imp, referer=referer)
            except Exception as e:
                log.warning("fetch selhal (%s): %s", target_url, e)
                if idx == 0:
                    return web.json_response({"ok": False, "error": str(e)[:300]}, status=502)
                break

            reason = page.block_reason()
            if reason or page.status >= 400:
                log.warning("blokace / HTTP %s na %s: %s", page.status, target_url, reason)
                if idx == 0:
                    if reason:
                        await fetcher.reset(urlsplit(target_url).netloc.lower())
                    status_out = page.status if page.status >= 400 else 403
                    return web.Response(
                        status=status_out,
                        text=page.html[:60_000] or (reason or f"HTTP {status_out}"),
                        content_type="text/html",
                        charset="utf-8",
                        headers={"x-block-reason": reason or f"HTTP {page.status}"},
                    )
                break

            pages.append(page)
            referer = page.final_url

        combined = "\n".join(p.html for p in pages)
        if do_slim:
            combined = slim_html(combined)
        total_ms = sum(p.ms for p in pages)
        log.info("OK %s (+%d extra) -> %d kB za %d ms", url, len(pages) - 1, len(combined) // 1024, total_ms)
        return web.Response(
            status=200,
            text=combined,
            content_type="text/html",
            charset="utf-8",
            headers={
                "cache-control": "no-store",
                "x-pages-fetched": str(len(pages)),
                "x-fetch-ms": str(total_ms),
            },
        )

    async def on_cleanup(_app: web.Application) -> None:
        await fetcher.close()

    app = web.Application()
    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/fetch", handle_fetch)
    app.on_cleanup.append(on_cleanup)
    return app


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, os.environ.get("POKETRACKER_LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    port = int(os.environ.get("PORT", "8000"))
    app = build_app()
    web.run_app(app, host="0.0.0.0", port=port, print=lambda msg: log.info("%s", msg))


if __name__ == "__main__":
    main()
