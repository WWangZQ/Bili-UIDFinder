from __future__ import annotations

import asyncio
import random

import httpx

TEST_URL = "https://api.bilibili.com/x/web-interface/card"
TEST_PARAMS = {"mid": "1", "photo": "false"}
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    "Referer": "https://space.bilibili.com/",
}
CONCURRENT_CHECK = 20


async def fetch_from_geonode(client: httpx.AsyncClient) -> list[str]:
    proxies = []
    for page in range(1, 4):
        url = "https://proxylist.geonode.com/api/proxy-list"
        params = {
            "limit": 50,
            "page": page,
            "sort_by": "lastChecked",
            "sort_type": "desc",
            "protocols": "http",
        }
        try:
            r = await client.get(url, params=params, timeout=15)
            data = r.json()
            for p in data.get("data", []):
                proxies.append(f"http://{p['ip']}:{p['port']}")
        except Exception:
            break
    return proxies


async def fetch_from_pubproxy(client: httpx.AsyncClient) -> list[str]:
    proxies = []
    url = "http://pubproxy.com/api/proxy"
    params = {"type": "http", "limit": 20}
    try:
        r = await client.get(url, params=params, timeout=10)
        for p in r.json().get("data", []):
            proxies.append(f"http://{p['ip']}:{p['port']}")
    except Exception:
        pass
    return proxies


async def fetch_proxies() -> list[str]:
    async with httpx.AsyncClient(headers=HEADERS) as c:
        geonode, pubproxy = await asyncio.gather(
            fetch_from_geonode(c),
            fetch_from_pubproxy(c),
        )
    seen = set()
    result = []
    for p in geonode + pubproxy:
        if p not in seen:
            seen.add(p)
            result.append(p)
    return result


async def check_proxy(proxy: str) -> bool:
    try:
        async with httpx.AsyncClient(headers=HEADERS, proxy=proxy, timeout=10) as c:
            r = await c.get(TEST_URL, params=TEST_PARAMS)
            return r.status_code == 200 and "json" in r.headers.get("content-type", "")
    except Exception:
        return False


async def validate_proxies(proxies: list[str]) -> list[str]:
    sem = asyncio.Semaphore(CONCURRENT_CHECK)
    valid = []

    async def _check(p):
        async with sem:
            if await check_proxy(p):
                valid.append(p)

    await asyncio.gather(*(_check(p) for p in proxies))
    return valid


class ProxyPool:
    def __init__(self, proxies: list[str]):
        self._proxies = proxies
        self._lock = asyncio.Lock()
        self._idx = 0

    async def get(self) -> str | None:
        async with self._lock:
            if not self._proxies:
                return None
            p = self._proxies[self._idx % len(self._proxies)]
            self._idx += 1
            return p

    async def remove(self, proxy: str):
        async with self._lock:
            if proxy in self._proxies:
                self._proxies.remove(proxy)
                print(f"  移除失效代理：{proxy}（剩余 {len(self._proxies)} 个）")

    @property
    def alive(self) -> int:
        return len(self._proxies)


async def init_pool() -> ProxyPool:
    print("正在获取免费代理...")
    proxies = await fetch_proxies()
    print(f"获取到 {len(proxies)} 个代理，正在验证...")

    valid = await validate_proxies(proxies)
    print(f"验证通过 {len(valid)} 个代理")

    if not valid:
        print("没有可用代理！请稍后重试或使用付费代理")
        return ProxyPool([])

    random.shuffle(valid)
    return ProxyPool(valid)
