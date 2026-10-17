"""The real HTTP client for the live relay (httpx). Everything smart is in liverelay.py; this only fetches and translates errors."""
import httpx

from .liverelay import FetchError, FetchTimeout, UpstreamResponse

CHUNK = 64 * 1024


async def _chunks(resp: httpx.Response):
    try:
        async for c in resp.aiter_raw(CHUNK):
            yield c
    except httpx.TimeoutException as e:
        raise FetchTimeout(str(e))
    except (httpx.TransportError, httpx.StreamError) as e:
        raise FetchError(str(e))


class HttpxFetcher:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    def _c(self) -> httpx.AsyncClient:
        if self._client is None:
            # redirects are followed by the relay itself (every hop is checked); one pool shared by all viewers
            self._client = httpx.AsyncClient(follow_redirects=False, http2=False,
                                             timeout=httpx.Timeout(connect=10, read=30, write=15, pool=15),
                                             limits=httpx.Limits(max_connections=100, max_keepalive_connections=20))
        return self._client

    async def get(self, url: str, headers: dict) -> UpstreamResponse:
        c = self._c()
        try:
            resp = await c.send(c.build_request("GET", url, headers=headers), stream=True)
        except httpx.TimeoutException as e:
            raise FetchTimeout(str(e))
        except (httpx.TransportError, httpx.InvalidURL, ValueError) as e:
            raise FetchError(str(e))
        return UpstreamResponse(resp.status_code, {k.lower(): v for k, v in resp.headers.items()}, _chunks(resp), resp.aclose)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


fetcher = HttpxFetcher()
