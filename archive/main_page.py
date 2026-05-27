import asyncio
import json
import re
import random
from typing import Optional, Tuple

import aiohttp

english_name_pattern = re.compile(r'^[A-Za-z]+$')
# 从 HTML 中提取 window.__INITIAL_STATE__
init_state_pattern = re.compile(r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\});\s*\(function', re.DOTALL)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]


def build_headers(referer: str = "https://www.bilibili.com/") -> dict:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Referer": referer,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }


async def check_one(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    uid: str,
    idx: int,
    total: int,
) -> Optional[Tuple[str, str]]:
    headers = build_headers(f"https://space.bilibili.com/{uid}")

    async with sem:
        try:
            async with session.get(
                f"https://space.bilibili.com/{uid}",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    print(f"[{idx}/{total}][WARN]{uid}：HTTP {resp.status}")
                    return None
                html = await resp.text()
        except Exception as e:
            print(f"[{idx}/{total}][WARN]{uid}：请求失败 {e}")
            return None

    m = init_state_pattern.search(html)
    if not m:
        print(f"[{idx}/{total}][WARN]{uid}：未找到 __INITIAL_STATE__")
        return None

    try:
        state = json.loads(m.group(1))
    except json.JSONDecodeError:
        print(f"[{idx}/{total}][WARN]{uid}：JSON 解析失败")
        return None

    space_info = state.get("spaceInfo", {})
    if not space_info:
        card = state.get("card", {})
        level = card.get("level", -1)
        name = card.get("name", "")
    else:
        level = space_info.get("level", -1)
        name = space_info.get("name", "")

    if level == -1 or not name:
        print(f"[{idx}/{total}][WARN]{uid}：数据为空，可能页面被拦截 code={state.get('code', '')}")
        return None

    if level != 0:
        print(f"[{idx}/{total}][SKIP]{uid}：非零级 (Lv.{level})")
        return None

    if not english_name_pattern.fullmatch(name):
        print(f"[{idx}/{total}][SKIP]{uid}：昵称不合规：{name}")
        return None

    print(f"[{idx}/{total}][FOUND]{uid}：昵称 {name}")
    return (uid, name)


async def main(suffix: int):
    uids = [f"{p}{suffix:04d}" for p in range(400, 1000)]
    total = len(uids)

    # 页面走 CDN，可以适当放开并发和间隔
    sem = asyncio.Semaphore(3)
    connector = aiohttp.TCPConnector(limit=10)
    cookie_jar = aiohttp.CookieJar()
    async with aiohttp.ClientSession(connector=connector, cookie_jar=cookie_jar) as session:
        print(f"共 {total} 个 UID 待扫描，方案：空间页面 SSR\n")

        tasks = []
        for i, uid in enumerate(uids, 1):
            # 请求间隔 0.3-0.8s
            await asyncio.sleep(random.uniform(0.3, 0.8))
            tasks.append(check_one(session, sem, uid, i, total))

        results = await asyncio.gather(*tasks)

    valid = [r for r in results if r is not None]
    with open("uid_srr.txt", "w", encoding="utf-8") as f:
        for uid, name in valid:
            f.write(f"{uid}\n{name}\n")

    print(f"\n完成！共扫描 {total} 个 UID，找到 {len(valid)} 个符合条件的账号")


if __name__ == "__main__":
    suffix_input = input("请输入四位数后缀（如1234）：")
    if not suffix_input.isdigit() or len(suffix_input) != 4:
        print("输入有误，必须是4位数字！")
    else:
        asyncio.run(main(int(suffix_input)))
