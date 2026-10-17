import time

import httpx

from .config import settings

TMDB = "https://api.themoviedb.org/3"
CACHE_TTL = 6 * 3600
_cache: dict[tuple, tuple[float, dict]] = {}


class TmdbError(Exception):
    pass


class TmdbNotFound(TmdbError):
    pass


def _dedupe(names, exclude, limit=30):
    seen = {n for n in exclude if n}
    out = []
    for n in names:
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out[:limit]


async def build_meta(media_type: str, tmdb_id: int, season: int | None = None,
                     episode: int | None = None) -> dict:
    """Turn a TMDB id (+ season/episode) into the context dict handed to source scripts."""
    ck = (media_type, tmdb_id, season, episode)
    hit = _cache.get(ck)
    if hit and time.monotonic() - hit[0] < CACHE_TTL:
        return dict(hit[1])
    if not settings.tmdb_api_key:
        raise TmdbError("TMDB_API_KEY is not configured")

    key = settings.tmdb_api_key
    headers = {"Authorization": f"Bearer {key}"} if key.startswith("eyJ") else {}
    base_params = {} if headers else {"api_key": key}

    async def get(client, path, **params):
        r = await client.get(f"{TMDB}{path}", params={**base_params, **params})
        if r.status_code == 404:
            raise TmdbNotFound("not found on TMDB")
        r.raise_for_status()
        return r.json()

    try:
        async with httpx.AsyncClient(timeout=15, headers=headers) as c:
            extra = "translations,alternative_titles,external_ids"
            if media_type == "movie":
                d = await get(c, f"/movie/{tmdb_id}", append_to_response=extra)
                title, original = d.get("title"), d.get("original_title")
                date = d.get("release_date")
                imdb = d.get("imdb_id") or (d.get("external_ids") or {}).get("imdb_id")
                alts = [t.get("title") for t in (d.get("alternative_titles") or {}).get("titles", [])]
                alts += [(t.get("data") or {}).get("title")
                         for t in (d.get("translations") or {}).get("translations", [])]
                episode_title = None
            else:
                d = await get(c, f"/tv/{tmdb_id}", append_to_response=extra)
                title, original = d.get("name"), d.get("original_name")
                date = d.get("first_air_date")
                imdb = (d.get("external_ids") or {}).get("imdb_id")
                alts = [t.get("title") for t in (d.get("alternative_titles") or {}).get("results", [])]
                alts += [(t.get("data") or {}).get("name")
                         for t in (d.get("translations") or {}).get("translations", [])]
                episode_title = None
                if season is not None and episode is not None:
                    ep = await get(c, f"/tv/{tmdb_id}/season/{season}/episode/{episode}")
                    episode_title = ep.get("name")
    except httpx.HTTPError as e:
        raise TmdbError(f"TMDB request failed: {e}") from e

    meta = {
        "tmdb_id": tmdb_id,
        "type": media_type,
        "title": title,
        "original_title": original,
        "alt_titles": _dedupe(alts, [title, original]),
        "year": int(date[:4]) if date else None,
        "imdb_id": imdb,
        "original_language": d.get("original_language"),
        "season": season,
        "episode": episode,
        "episode_title": episode_title,
    }
    _cache[ck] = (time.monotonic(), meta)
    return dict(meta)
