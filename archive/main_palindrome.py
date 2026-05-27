import asyncio
import re
from typing import Optional, Tuple

from playwright.async_api import async_playwright

english_name_pattern = re.compile(r'^[A-Za-z]+$')
NICKNAME_RE = re.compile(r'<div[^>]*class="nickname"[^>]*>(.*?)</div>')
CONCURRENCY = 5


def gen_palindromes(digits: int = 7) -> list[str]:
    """生成指定位数的回文 UID"""
    uids = []
    half = digits // 2
    start = 10 ** (half + digits % 2 - 1)  # 首位不为 0
    end = 10 ** (half + digits % 2)
    for left_half in range(start, end):
        s = str(left_half)
        if digits % 2 == 0:
            uid = s + s[::-1]
        else:
            uid = s + s[-2::-1]
        uids.append(uid)
    return uids


async def block_useless(route):
    if route.request.resource_type in ("image", "stylesheet", "font", "media"):
        await route.abort()
    else:
        await route.continue_()


async def check_one(context, uid: str, idx: int, total: int) -> Optional[Tuple[str, str]]:
    page = await context.new_page()
    try:
        await page.route("**/*", block_useless)

        await page.goto(
            f"https://space.bilibili.com/{uid}",
            wait_until="domcontentloaded",
            timeout=8000,
        )

        try:
            await page.wait_for_selector(".nickname", timeout=3000)
        except Exception:
            pass

        html = await page.content()

        if "sic-BDC_svg-user_level_0" not in html:
            level_match = re.search(r'sic-BDC_svg-user_level_(\d+)', html)
            lv = level_match.group(1) if level_match else "?"
            print(f"[{idx}/{total}][SKIP]{uid}：非零级 Lv.{lv}")
            return None

        m = NICKNAME_RE.search(html)
        if not m:
            print(f"[{idx}/{total}][WARN]{uid}：找不到昵称")
            return None

        nickname = m.group(1).strip()
        if not english_name_pattern.fullmatch(nickname):
            print(f"[{idx}/{total}][SKIP]{uid}：昵称不合规：{nickname}")
            return None

        print(f"[{idx}/{total}][FOUND]{uid}：昵称 {nickname}")
        return (uid, nickname)
    except Exception as e:
        print(f"[{idx}/{total}][FAIL]{uid}：{e}")
        return None
    finally:
        await page.close()


async def worker(browser, queue: asyncio.Queue, results: list, total: int):
    context = await browser.new_context()
    while True:
        item = await queue.get()
        if item is None:
            break
        idx, uid = item
        result = await check_one(context, uid, idx, total)
        if result is not None:
            results.append(result)
        queue.task_done()
    await context.close()


async def run(uids: list[str]):
    total = len(uids)

    queue: asyncio.Queue = asyncio.Queue()
    results = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        workers_list = [
            asyncio.create_task(worker(browser, queue, results, total))
            for _ in range(CONCURRENCY)
        ]

        for i, uid in enumerate(uids, 1):
            await queue.put((i, uid))

        for _ in range(CONCURRENCY):
            await queue.put(None)

        await asyncio.gather(*workers_list)
        await browser.close()

    valid = sorted(results, key=lambda x: uids.index(x[0]))
    with open("uid_palindrome.txt", "w", encoding="utf-8") as f:
        for uid, name in valid:
            f.write(f"{uid}\n{name}\n")

    print(f"\n完成！共扫描 {total} 个回文 UID，找到 {len(valid)} 个符合条件的账号")


if __name__ == "__main__":
    digits = int(input("回文位数 (默认7): ") or "7")
    uids = gen_palindromes(digits)
    print(f"共生成 {len(uids)} 个回文 UID")

    lo = input("UID 下限 (默认全部): ")
    hi = input("UID 上限 (默认全部): ")

    if lo or hi:
        low = int(lo) if lo else 0
        high = int(hi) if hi else 999999999
        uids = [u for u in uids if low <= int(u) <= high]
        print(f"筛选 [{low}, {high}]，实际 {len(uids)} 个")

    input(f"按回车开始扫描 ({len(uids)} 个)...")
    asyncio.run(run(uids))
