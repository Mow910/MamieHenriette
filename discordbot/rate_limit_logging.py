"""
Journalisation des en-têtes HTTP sur les réponses 429 (rate limit Discord).

Réf. Discord : https://support-dev.discord.com/hc/en-us/articles/6223003921559-My-Bot-is-Being-Rate-Limited

discord.py transmet ce TraceConfig aiohttp via l’option Client(http_trace=...).
Le corps JSON (retry_after, global) est toujours loggé par le logger discord.http.
"""
import logging
from typing import Any

import aiohttp

logger = logging.getLogger('discord.ratelimit_headers')

# En-têtes utiles pour identifier le type de limite (global / user / shared, etc.)
_HEADER_KEYS = (
	'X-RateLimit-Limit',
	'X-RateLimit-Remaining',
	'X-RateLimit-Reset',
	'X-RateLimit-Reset-After',
	'X-RateLimit-Scope',
	'X-Ratelimit-Bucket',
	'X-Ratelimit-Limit',
	'X-Ratelimit-Remaining',
	'X-Ratelimit-Reset',
	'Retry-After',
	'Via',
)


def _collect_headers(resp: aiohttp.ClientResponse) -> dict[str, str]:
	h = resp.headers
	out: dict[str, str] = {}
	for key in _HEADER_KEYS:
		val = h.get(key)
		if val is not None:
			out[key] = val
	return out


def build_discord_http_trace_config() -> aiohttp.TraceConfig:
	trace = aiohttp.TraceConfig()

	async def on_request_end(
		session: aiohttp.ClientSession,
		trace_config_ctx: Any,
		params: Any,
	) -> None:
		try:
			resp = getattr(params, 'response', None)
			if resp is None or getattr(resp, 'status', None) != 429:
				return
			method = getattr(params, 'method', '?')
			url = getattr(params, 'url', '?')
			hdr = _collect_headers(resp)
			logger.warning(
				'Discord API 429 %s %s | rate_limit_headers=%s',
				method,
				url,
				hdr,
			)
		except Exception:
			logger.debug('rate_limit trace callback failed', exc_info=True)

	trace.on_request_end.append(on_request_end)
	return trace
