"""Thin async wrapper around the public Codeforces API (rate limited)."""
import asyncio
import json
import random
import re
import time

import aiohttp

API = "https://codeforces.com/api"
REQUEST_GAP = 1.0  # seconds between API calls; raise if Codeforces starts rejecting
PROBLEM_CACHE_SECONDS = 6 * 3600
HANDLE_RE = re.compile(r"[A-Za-z0-9_.\-]{3,24}")  # Codeforces handle rules; also blocks "a;b" multi-handle queries


class CFError(Exception):
    pass


def key_of(problem: dict) -> str:
    return f'{problem.get("contestId")}{problem["index"]}'


def valid_handle(handle: str) -> bool:
    return bool(HANDLE_RE.fullmatch(handle))


class CF:
    def __init__(self):
        self.session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._problems: list[dict] = []
        self._problems_at = 0.0

    async def get(self, method: str, **params):
        for attempt in range(3):
            data = await self._request(method, params)
            if data.get("status") == "OK":
                return data["result"]
            comment = data.get("comment", "unknown Codeforces error")
            if "limit exceeded" not in comment.lower() or attempt == 2:
                raise CFError(comment)
            await asyncio.sleep(2 * (attempt + 1))

    async def _request(self, method: str, params: dict) -> dict:
        """One API call. Network trouble and non-JSON pages (Cloudflare, 5xx) become CFError."""
        async with self._lock:
            wait = REQUEST_GAP - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            if self.session is None:
                self.session = aiohttp.ClientSession()
            try:
                async with self.session.get(
                    f"{API}/{method}", params=params, timeout=aiohttp.ClientTimeout(total=20)
                ) as r:
                    data = await r.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as e:
                raise CFError(f"Codeforces unreachable ({type(e).__name__})") from e
            finally:
                self._last = time.monotonic()
        if not isinstance(data, dict):
            raise CFError("unexpected Codeforces response")
        return data

    async def user(self, handle: str) -> dict:
        if not valid_handle(handle):
            raise CFError(f"handles: User with handle {handle} not found")
        return (await self.get("user.info", handles=handle))[0]

    async def submissions(self, handle: str, count: int | None = None) -> list[dict]:
        params = {"handle": handle, "from": 1}
        if count:
            params["count"] = count
        return await self.get("user.status", **params)

    async def seen(self, handle: str) -> set[str]:
        """Keys and names of every problem the user has ever submitted to (any verdict).

        Names are included because Div. 1 / Div. 2 twins share a name but not a key.
        """
        out = set()
        for s in await self.submissions(handle):
            out |= {key_of(s["problem"]), s["problem"].get("name")}
        out.discard(None)
        return out

    async def problems(self) -> list[dict]:
        if time.time() - self._problems_at > PROBLEM_CACHE_SECONDS:
            try:
                result = await self.get("problemset.problems")
            except CFError:
                if self._problems:  # stale list beats no match at all
                    return self._problems
                raise
            self._problems = [
                {
                    "key": key_of(p),
                    "name": p["name"],
                    "rating": p["rating"],
                    "url": f'https://codeforces.com/problemset/problem/{p["contestId"]}/{p["index"]}',
                }
                for p in result["problems"]
                if "rating" in p and "contestId" in p and "interactive" not in p.get("tags", [])
            ]
            self._problems_at = time.time()
        return self._problems

    async def pick(self, rating: int, exclude: set[str]) -> dict:
        """Random problem at `rating` whose key and name are both outside `exclude`."""
        pool = [
            p for p in await self.problems()
            if p["rating"] == rating and p["key"] not in exclude and p["name"] not in exclude
        ]
        if not pool:
            raise CFError(f"No unseen problems left at rating {rating}")
        return random.choice(pool)
