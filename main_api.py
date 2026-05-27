from __future__ import annotations

import asyncio
import os
import re
import sys

import httpx

from proxy_pool import ProxyPool, init_pool

english_name_pattern = re.compile(r'^[A-Za-z]+$')
CONCURRENCY = 5
API_URL = "https://api.bilibili.com/x/web-interface/card"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    "Referer": "https://space.bilibili.com/",
}
MAX_RETRIES = 3
RETRY_DELAY = 3


async def check_one(client: httpx.AsyncClient, uid: str, idx: int, total: int):
    """返回 (uid, nickname, status) / 'blocked'"""
    for attempt in range(MAX_RETRIES):
        try:
            resp = await client.get(API_URL, params={"mid": uid, "photo": "false"}, timeout=10)
            if resp.status_code == 412:
                print(f"[{idx}/{total}][BLOCKED]{uid}：HTTP 412，需要换代理")
                return "blocked"
            if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
                wait = RETRY_DELAY * (attempt + 1)
                print(f"[{idx}/{total}][RETRY]{uid}：HTTP {resp.status_code}，等待 {wait}s（第{attempt+1}次）")
                await asyncio.sleep(wait)
                continue
            data = resp.json()

            if data.get("code") == -412:
                print(f"[{idx}/{total}][BLOCKED]{uid}：-412 限流，需要换代理")
                return "blocked"

            if data.get("code") != 0:
                print(f"[{idx}/{total}][SKIP]{uid}：接口错误 code={data.get('code')}")
                return (uid, "", f"error_{data.get('code')}")

            card = data["data"]["card"]
            level = card.get("level_info", {}).get("current_level", 0)
            nickname = card.get("name", "")

            if level != 0:
                print(f"[{idx}/{total}][SKIP]{uid}：Lv.{level}")
                return (uid, nickname, f"Lv.{level}")

            if not english_name_pattern.fullmatch(nickname):
                print(f"[{idx}/{total}][SKIP]{uid}：昵称不合规：{nickname}")
                return (uid, nickname, "skip")

            print(f"[{idx}/{total}][FOUND]{uid}：昵称 {nickname}")
            return (uid, nickname, "found")

        except httpx.TimeoutException:
            print(f"[{idx}/{total}][RETRY]{uid}：超时（第{attempt+1}次）")
            await asyncio.sleep(RETRY_DELAY)
        except Exception as e:
            print(f"[{idx}/{total}][FAIL]{uid}：{e}")
            return (uid, "", "fail")

    print(f"[{idx}/{total}][FAIL]{uid}：重试{MAX_RETRIES}次后放弃")
    return (uid, "", "fail")


async def worker(queue: asyncio.Queue, results: list, total: int,
                 pool: ProxyPool | None = None, fixed_proxy: str | None = None):
    proxy = fixed_proxy or (await pool.get() if pool else None)
    client = httpx.AsyncClient(headers=HEADERS, proxy=proxy)

    while True:
        item = await queue.get()
        if item is None:
            break
        idx, uid = item

        result = await check_one(client, uid, idx, total)

        if result == "blocked" and pool:
            old = proxy
            await client.aclose()
            await pool.remove(old)
            proxy = await pool.get()
            if proxy:
                client = httpx.AsyncClient(headers=HEADERS, proxy=proxy)
                print(f"  已切换代理 → {proxy}")
            else:
                client = httpx.AsyncClient(headers=HEADERS)
                proxy = None
                print(f"  代理池已空，切换直连")
            result = (uid, "", "blocked")

        results.append(result)
        queue.task_done()

    await client.aclose()


async def run(uids: list[str], suffix: str, proxy: str = None, use_pool: bool = False):
    total = len(uids)
    queue: asyncio.Queue = asyncio.Queue()
    results = []
    pool = None

    if use_pool:
        pool = await init_pool()
        if pool.alive == 0:
            print("没有可用代理，退出")
            return

    workers = [
        asyncio.create_task(worker(queue, results, total, pool, proxy))
        for _ in range(CONCURRENCY)
    ]

    for i, uid in enumerate(uids, 1):
        await queue.put((i, uid))

    for _ in range(CONCURRENCY):
        await queue.put(None)

    await asyncio.gather(*workers)

    all_results = sorted(results, key=lambda x: uids.index(x[0]))
    found = [r for r in all_results if r[2] == "found"]
    with open(f"uid_{suffix}.txt", "w", encoding="utf-8") as f:
        for uid, name, status in found:
            f.write(f"{name} {uid}\n")
        f.write("\n")
        for uid, name, status in all_results:
            f.write(f"{name} {uid}\n")

    print(f"\n完成！共扫描 {total} 个后缀 UID，找到 {len(found)} 个符合条件的账号")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="按后缀扫描 B站 UID")
    parser.add_argument("--pool", action="store_true", help="使用免费代理池")
    parser.add_argument("--proxy", type=str, help="手动指定代理 (http://host:port)")
    parser.add_argument("--suffix", "-s", type=str, help="四位数后缀")
    args = parser.parse_args()

    proxy = args.proxy or os.environ.get("PROXY")
    if proxy:
        print(f"使用代理：{proxy}")
    if not args.pool and not proxy:
        args.pool = input("使用代理池? (y/N): ").strip().lower() == "y"
    if args.pool:
        print("启用代理池模式")

    suffix_input = args.suffix or input("请输入四位数后缀（如1234）：")
    if not suffix_input.isdigit() or len(suffix_input) != 4:
        print("输入有误，必须是4位数字！")
    else:
        suffix = int(suffix_input)
        uids = [f"{p}{suffix:04d}" for p in range(1, 1000)]
        print(f"共生成 {len(uids)} 个 UID（后缀 {suffix_input}）")
        asyncio.run(run(uids, suffix_input, proxy, args.pool))
