import asyncio
import hashlib
import re
import random
import time
from typing import Optional, Tuple, List
from urllib.parse import urlencode

import aiohttp

english_name_pattern = re.compile(r'^[A-Za-z]+$')

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 52, 44, 34
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]

PROXY_API = "https://proxy.scdn.io/api/get_proxy.php"
BAD_PROXIES: set = set()  # 记录已失效的代理


def build_headers(referer: str = "https://www.bilibili.com/") -> dict:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Referer": referer,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }


def get_mixin_key(raw_key: str) -> str:
    return ''.join(raw_key[i] for i in MIXIN_KEY_ENC_TAB if i < len(raw_key))[:32]


async def fetch_mixin_key(session: aiohttp.ClientSession) -> str:
    async with session.get(
        "https://api.bilibili.com/x/web-interface/nav",
        headers=build_headers(),
        timeout=aiohttp.ClientTimeout(total=10),
    ) as resp:
        data = await resp.json()
        wbi_img = data["data"]["wbi_img"]
        img_key = wbi_img["img_url"].rsplit("/", 1)[-1].split(".")[0]
        sub_key = wbi_img["sub_url"].rsplit("/", 1)[-1].split(".")[0]
        return get_mixin_key(img_key + sub_key)


def sign_params(params: dict, mixin_key: str) -> dict:
    params["wts"] = int(time.time())
    sorted_pairs = sorted(params.items())
    query = urlencode(sorted_pairs)
    params["w_rid"] = hashlib.md5((query + mixin_key).encode()).hexdigest()
    return params


def build_space_params(uid: str, mixin_key: str) -> dict:
    return sign_params({
        "mid": uid,
        "token": "",
        "platform": "web",
        "web_location": "1550101",
    }, mixin_key)


async def fetch_proxies(session: aiohttp.ClientSession, count: int = 20) -> List[str]:
    """从代理池 API 获取一批代理"""
    params = {"protocol": "http", "count": count, "country_code": "CN"}
    try:
        async with session.get(PROXY_API, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
            if data.get("code") == 200:
                proxies = [p for p in data["data"]["proxies"] if p not in BAD_PROXIES]
                return proxies
    except Exception:
        pass
    return []


async def test_proxy(session: aiohttp.ClientSession, proxy: str) -> bool:
    """测试代理是否可用"""
    try:
        async with session.get(
            "https://api.bilibili.com/x/web-interface/nav",
            proxy=f"http://{proxy}",
            timeout=aiohttp.ClientTimeout(total=8),
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("code") == 0
    except Exception:
        pass
    return False


async def check_one(
    session: aiohttp.ClientSession,
    uid: str,
    mixin_key: str,
    proxy_pool: List[str],
    proxy_lock: asyncio.Lock,
    idx: int,
    total: int,
) -> Optional[Tuple[str, str]]:
    params = build_space_params(uid, mixin_key)
    headers = build_headers(f"https://space.bilibili.com/{uid}")

    # 从代理池中随机选一个
    async with proxy_lock:
        if not proxy_pool:
            return None
        proxy = random.choice(proxy_pool)

    proxy_url = f"http://{proxy}"
    try:
        async with session.get(
            "https://api.bilibili.com/x/space/wbi/acc/info",
            params=params,
            headers=headers,
            proxy=proxy_url,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                print(f"[{idx}/{total}][PROXY_ERR]{uid}：HTTP {resp.status} via {proxy}")
                return None
            data = await resp.json()
    except asyncio.TimeoutError:
        print(f"[{idx}/{total}][TIMEOUT]{uid} via {proxy}")
        BAD_PROXIES.add(proxy)
        async with proxy_lock:
            if proxy in proxy_pool:
                proxy_pool.remove(proxy)
        return None
    except Exception as e:
        print(f"[{idx}/{total}][FAIL]{uid} via {proxy}: {e}")
        return None

    code = data.get("code", -1)
    if code == -352:
        print(f"[{idx}/{total}][WARN]{uid}：风控 via {proxy}")
        return None
    if code == -799:
        print(f"[{idx}/{total}][WARN]{uid}：WBI 签名过期")
        return None
    if code != 0:
        print(f"[{idx}/{total}][WARN]{uid}：code={code} via {proxy}")
        return None

    info = data.get("data", {})
    level = info.get("level", -1)
    name = info.get("name", "")

    if level != 0:
        print(f"[{idx}/{total}][SKIP]{uid}：非零级 (Lv.{level})")
        return None

    if not name or not english_name_pattern.fullmatch(name):
        print(f"[{idx}/{total}][SKIP]{uid}：昵称不合规：{name}")
        return None

    print(f"[{idx}/{total}][FOUND]{uid}：昵称 {name}")
    return (uid, name)


async def main(suffix: int):
    uids = [f"{p}{suffix:04d}" for p in range(400, 1000)]
    total = len(uids)

    proxy_pool: List[str] = []
    proxy_lock = asyncio.Lock()

    connector = aiohttp.TCPConnector(limit=10)
    async with aiohttp.ClientSession(connector=connector) as session:
        # 获取 WBI 密钥（不用代理，先直连）
        print("正在获取 WBI 签名密钥...")
        try:
            mixin_key = await fetch_mixin_key(session)
        except Exception as e:
            print(f"获取 WBI 密钥失败: {e}")
            return
        print("密钥获取成功")

        # 拉第一批代理并测试
        print("正在拉取代理池...")
        raw_proxies = await fetch_proxies(session, 20)
        print(f"获取到 {len(raw_proxies)} 个代理，测试中...")
        for p in raw_proxies:
            if await test_proxy(session, p):
                proxy_pool.append(p)
        print(f"可用代理: {len(proxy_pool)} 个")

        if not proxy_pool:
            print("没有可用代理，退出")
            return

        print(f"开始扫描 {total} 个 UID\n")

        results = []
        proxy_fail_count = 0

        for i, uid in enumerate(uids, 1):
            # 每 30 个请求检查一下代理池余量
            if len(proxy_pool) < 3 or proxy_fail_count >= 5:
                print(f"\n--- 代理池不足({len(proxy_pool)}个)，补充中... ---")
                new_proxies = await fetch_proxies(session, 20)
                for p in new_proxies:
                    if p not in proxy_pool and p not in BAD_PROXIES:
                        if await test_proxy(session, p):
                            proxy_pool.append(p)
                print(f"--- 代理池现在 {len(proxy_pool)} 个 ---\n")
                proxy_fail_count = 0

            await asyncio.sleep(random.uniform(0.3, 0.8))
            result = await check_one(session, uid, mixin_key, proxy_pool, proxy_lock, i, total)
            if result is None and len(proxy_pool) == 0:
                proxy_fail_count += 1
            results.append(result)

    valid = [r for r in results if r is not None]
    with open("uid.txt", "w", encoding="utf-8") as f:
        for uid, name in valid:
            f.write(f"{uid}\n{name}\n")

    print(f"\n完成！共扫描 {total} 个 UID，找到 {len(valid)} 个符合条件的账号")


if __name__ == "__main__":
    suffix_input = input("请输入四位数后缀（如1234）：")
    if not suffix_input.isdigit() or len(suffix_input) != 4:
        print("输入有误，必须是4位数字！")
    else:
        asyncio.run(main(int(suffix_input)))
