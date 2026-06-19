from __future__ import annotations

import asyncio
import queue
import re
import threading
import tkinter as tk
from tkinter import ttk, messagebox

import httpx

from proxy_pool import ProxyPool, init_pool

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ENGLISH_RE = re.compile(r"^[A-Za-z]+$")
API_URL = "https://api.bilibili.com/x/web-interface/card"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    "Referer": "https://space.bilibili.com/",
}
CONCURRENCY = 5
MAX_RETRIES = 3
RETRY_DELAY = 3

# Palette
BG = "#faf9f7"
SURFACE = "#ffffff"
BORDER = "#e8e4df"
ACCENT = "#c96442"
ACCENT_HOVER = "#b5573a"
TEXT = "#1a1a1a"
TEXT2 = "#6b6b6b"
LOG_BG = "#1e1e1e"
LOG_FG = "#d4d4d4"
GREEN = "#4ec9b0"
YELLOW = "#c99a42"
RED = "#c94242"


def gen_palindromes(digits: int = 7) -> list[str]:
    uids = []
    half = digits // 2
    start = 10 ** (half + digits % 2 - 1)
    end = 10 ** (half + digits % 2)
    for left_half in range(start, end):
        s = str(left_half)
        if digits % 2 == 0:
            uid = s + s[::-1]
        else:
            uid = s + s[-2::-1]
        uids.append(uid)
    return uids


# ---------------------------------------------------------------------------
# Async scan engine (runs in a dedicated thread with its own event loop)
# ---------------------------------------------------------------------------

class ScanEngine:
    def __init__(self):
        self.running = False
        self.stop_event = threading.Event()
        self.log_q: queue.Queue[str] = queue.Queue()
        self.results: list[tuple[str, str, str]] = []
        self.found_count = 0
        self.total = 0
        self.done = 0
        self._thread: threading.Thread | None = None

    async def _check_one(self, client: httpx.AsyncClient, uid: str, idx: int, total: int):
        for attempt in range(MAX_RETRIES):
            if self.stop_event.is_set():
                return None
            try:
                resp = await client.get(API_URL, params={"mid": uid, "photo": "false"}, timeout=10)
                if resp.status_code == 412:
                    return "blocked"
                if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
                    await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                    continue
                data = resp.json()
                if data.get("code") == -412:
                    return "blocked"
                if data.get("code") != 0:
                    return (uid, "", f"error_{data.get('code')}")
                card = data["data"]["card"]
                level = card.get("level_info", {}).get("current_level", 0)
                nickname = card.get("name", "")
                if level != 0:
                    return (uid, nickname, f"Lv.{level}")
                if not ENGLISH_RE.fullmatch(nickname):
                    return (uid, nickname, "skip")
                return (uid, nickname, "found")
            except httpx.TimeoutException:
                await asyncio.sleep(RETRY_DELAY)
            except Exception:
                return (uid, "", "fail")
        return (uid, "", "fail")

    async def _worker(self, q: asyncio.Queue, total: int, pool: ProxyPool | None, fixed_proxy: str | None):
        proxy = fixed_proxy or (await pool.get() if pool else None)
        client = httpx.AsyncClient(headers=HEADERS, proxy=proxy)
        while True:
            item = await q.get()
            if item is None or self.stop_event.is_set():
                break
            idx, uid = item
            result = await self._check_one(client, uid, idx, total)
            if result == "blocked" and pool:
                old = proxy
                await client.aclose()
                await pool.remove(old)
                proxy = await pool.get()
                client = httpx.AsyncClient(headers=HEADERS, proxy=proxy) if proxy else httpx.AsyncClient(headers=HEADERS)
                result = (uid, "", "blocked")
            if result and result != "blocked":
                status = result[2]
                if status == "found":
                    self.found_count += 1
                self.results.append(result)
                tag = "FOUND" if status == "found" else status
                self.log_q.put(f"[{idx}/{total}][{tag}] {result[0]} {result[1]}".strip())
            self.done = idx
            q.task_done()
        await client.aclose()

    async def _run(self, uids: list[str], proxy: str | None, use_pool: bool, mode: str):
        self.total = len(uids)
        self.done = 0
        self.found_count = 0
        self.results = []
        self.log_q.put(f"开始扫描 — 模式: {mode}, 共 {len(uids)} 个 UID")

        pool = None
        if use_pool:
            self.log_q.put("正在获取免费代理...")
            pool = await init_pool()
            self.log_q.put(f"验证通过 {pool.alive} 个代理")
            if pool.alive == 0:
                self.log_q.put("没有可用代理，退出")
                self.running = False
                return

        q: asyncio.Queue = asyncio.Queue()
        workers = [
            asyncio.create_task(self._worker(q, len(uids), pool, proxy))
            for _ in range(CONCURRENCY)
        ]
        for i, uid in enumerate(uids, 1):
            if self.stop_event.is_set():
                break
            await q.put((i, uid))
        for _ in range(CONCURRENCY):
            await q.put(None)
        await asyncio.gather(*workers)

        found = [r for r in self.results if r[2] == "found"]
        self.log_q.put(f"扫描完成！共 {len(self.results)} 个结果，{len(found)} 个符合条件")
        self.running = False

    def _thread_target(self, uids, proxy, use_pool, mode):
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self._run(uids, proxy, use_pool, mode))
        finally:
            loop.close()

    def start(self, uids: list[str], proxy: str | None, use_pool: bool, mode: str) -> bool:
        if self.running:
            return False
        self.running = True
        self.stop_event.clear()
        self._thread = threading.Thread(target=self._thread_target, args=(uids, proxy, use_pool, mode), daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self.stop_event.set()

    def poll_log(self) -> str | None:
        try:
            return self.log_q.get_nowait()
        except queue.Empty:
            return None


engine = ScanEngine()

# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("bili-7UID-search")
        self.geometry("780x660")
        self.minsize(640, 520)
        self.configure(bg=BG)

        self._apply_styles()
        self._build_ui()
        self._poll()

    def _apply_styles(self):
        s = ttk.Style(self)
        s.theme_use("clam")

        s.configure(".", background=BG, foreground=TEXT, font=("Segoe UI", 10))
        s.configure("TFrame", background=BG)
        s.configure("TLabel", background=BG, foreground=TEXT2, font=("Segoe UI", 9))
        s.configure("TNotebook", background=BG, borderwidth=0)
        s.configure("TNotebook.Tab", padding=[16, 7], font=("Segoe UI", 10))
        s.map("TNotebook.Tab",
               background=[("selected", SURFACE)],
               foreground=[("selected", ACCENT)])
        s.configure("TEntry", padding=4, font=("Consolas", 11))
        s.configure("Accent.TButton", background=ACCENT, foreground="white",
                     font=("Segoe UI", 10, "bold"), padding=[14, 6])
        s.map("Accent.TButton",
               background=[("active", ACCENT_HOVER), ("disabled", "#ccc")])
        s.configure("Stop.TButton", background=RED, foreground="white",
                     font=("Segoe UI", 10, "bold"), padding=[14, 6])
        s.map("Stop.TButton",
               background=[("active", "#b33"), ("disabled", "#ccc")])
        s.configure("TCombobox", padding=4, font=("Segoe UI", 10))
        s.configure("Treeview", font=("Consolas", 9), rowheight=22, background=SURFACE)
        s.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"), foreground=TEXT2)
        s.configure("green.Horizontal.TProgressbar",
                     troughcolor=BORDER, background=ACCENT, thickness=6)

    def _build_ui(self):
        # -- Notebook (tabs) --
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="x", padx=16, pady=(12, 0))

        # Suffix tab
        tab_s = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab_s, text="  按后缀扫描  ")
        ttk.Label(tab_s, text="四位数后缀").grid(row=0, column=0, sticky="w")
        self.suffix_var = tk.StringVar()
        e = ttk.Entry(tab_s, textvariable=self.suffix_var, width=10)
        e.grid(row=1, column=0, sticky="w", padx=(0, 8))
        ttk.Label(tab_s, text="(如 1314)").grid(row=1, column=1, sticky="w")

        # Palindrome tab
        tab_p = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab_p, text="  按回文扫描  ")
        ttk.Label(tab_p, text="位数").grid(row=0, column=0, sticky="w")
        self.digits_var = tk.StringVar(value="7")
        ttk.Entry(tab_p, textvariable=self.digits_var, width=6).grid(row=1, column=0, sticky="w", padx=(0, 14))
        ttk.Label(tab_p, text="UID 下限").grid(row=0, column=1, sticky="w")
        self.lo_var = tk.StringVar()
        ttk.Entry(tab_p, textvariable=self.lo_var, width=12).grid(row=1, column=1, sticky="w", padx=(0, 14))
        ttk.Label(tab_p, text="UID 上限").grid(row=0, column=2, sticky="w")
        self.hi_var = tk.StringVar()
        ttk.Entry(tab_p, textvariable=self.hi_var, width=12).grid(row=1, column=2, sticky="w")

        # -- Proxy row --
        pf = ttk.Frame(self)
        pf.pack(fill="x", padx=16, pady=(8, 0))
        ttk.Label(pf, text="代理").pack(side="left")
        self.proxy_mode = tk.StringVar(value="none")
        ttk.Radiobutton(pf, text="直连", variable=self.proxy_mode, value="none").pack(side="left", padx=(8, 0))
        ttk.Radiobutton(pf, text="手动", variable=self.proxy_mode, value="manual").pack(side="left", padx=(10, 0))
        ttk.Radiobutton(pf, text="代理池", variable=self.proxy_mode, value="pool").pack(side="left", padx=(10, 0))
        self.proxy_entry = ttk.Entry(pf, width=26)
        self.proxy_entry.pack(side="left", padx=(12, 0))
        self.proxy_entry.insert(0, "http://host:port")
        self.proxy_entry.configure(state="disabled")

        self.proxy_mode.trace_add("write", self._on_proxy_toggle)

        # -- Buttons --
        bf = ttk.Frame(self)
        bf.pack(fill="x", padx=16, pady=(10, 0))
        self.btn_start = ttk.Button(bf, text="开始扫描", style="Accent.TButton", command=self._on_start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(bf, text="停止", style="Stop.TButton", command=self._on_stop, state="disabled")
        self.btn_stop.pack(side="left", padx=(8, 0))
        self.status_label = ttk.Label(bf, text="就绪")
        self.status_label.pack(side="right")

        # -- Progress --
        self.progress_var = tk.DoubleVar()
        ttk.Progressbar(self, style="green.Horizontal.TProgressbar",
                         variable=self.progress_var, maximum=100).pack(fill="x", padx=16, pady=(10, 0))

        # -- PanedWindow (log + results) --
        paned = ttk.PanedWindow(self, orient="vertical")
        paned.pack(fill="both", expand=True, padx=16, pady=(8, 12))

        # Log
        log_frame = ttk.Frame(paned)
        paned.add(log_frame, weight=3)
        self.log_text = tk.Text(log_frame, wrap="word", font=("Consolas", 9),
                                 bg=LOG_BG, fg=LOG_FG, insertbackground=LOG_FG,
                                 selectbackground="#264f78", relief="flat", borderwidth=0, padx=8, pady=6)
        self.log_text.tag_configure("found", foreground=GREEN, font=("Consolas", 9, "bold"))
        self.log_text.tag_configure("blocked", foreground=YELLOW)
        self.log_text.tag_configure("error", foreground=RED)
        log_sb = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_sb.set)
        log_sb.pack(side="right", fill="y")
        self.log_text.pack(fill="both", expand=True)
        self.log_text.configure(state="disabled")

        # Results
        res_frame = ttk.Frame(paned)
        paned.add(res_frame, weight=2)
        cols = ("uid", "nickname", "status")
        self.tree = ttk.Treeview(res_frame, columns=cols, show="headings", height=6)
        self.tree.heading("uid", text="UID")
        self.tree.heading("nickname", text="昵称")
        self.tree.heading("status", text="状态")
        self.tree.column("uid", width=120, minwidth=80)
        self.tree.column("nickname", width=220, minwidth=100)
        self.tree.column("status", width=100, minwidth=60)
        self.tree.tag_configure("found", foreground="#2d7a4f")
        self.tree.tag_configure("skip", foreground="#999")
        self.tree.tag_configure("error", foreground=RED)
        tree_sb = ttk.Scrollbar(res_frame, command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_sb.set)
        tree_sb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)

    # -- Callbacks --

    def _on_proxy_toggle(self, *_):
        self.proxy_entry.configure(state="normal" if self.proxy_mode.get() == "manual" else "disabled")

    def _on_start(self):
        if engine.running:
            return

        mode = "suffix" if self.notebook.index(self.notebook.select()) == 0 else "palindrome"

        if mode == "suffix":
            suffix = self.suffix_var.get().strip()
            if not suffix.isdigit() or len(suffix) != 4:
                messagebox.showerror("参数错误", "后缀必须是4位数字")
                return
            uids = [f"{p}{int(suffix):04d}" for p in range(1, 1000)]
        else:
            try:
                digits = int(self.digits_var.get() or "7")
            except ValueError:
                messagebox.showerror("参数错误", "位数必须是数字")
                return
            if digits < 2 or digits > 12:
                messagebox.showerror("参数错误", "位数需在 2-12 之间")
                return
            uids = gen_palindromes(digits)
            lo, hi = self.lo_var.get().strip(), self.hi_var.get().strip()
            if lo:
                uids = [u for u in uids if int(u) >= int(lo)]
            if hi:
                uids = [u for u in uids if int(u) <= int(hi)]

        proxy, use_pool = None, False
        pm = self.proxy_mode.get()
        if pm == "manual":
            proxy = self.proxy_entry.get().strip()
            if not proxy:
                messagebox.showerror("参数错误", "请输入代理地址")
                return
        elif pm == "pool":
            use_pool = True

        # Reset UI
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.progress_var.set(0)
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.status_label.configure(text="扫描中...")

        engine.start(uids, proxy, use_pool, mode)

    def _on_stop(self):
        engine.stop()
        self.btn_stop.configure(state="disabled")

    # -- Queue polling --

    def _poll(self):
        # Draining logs
        while True:
            msg = engine.poll_log()
            if msg is None:
                break
            self.log_text.configure(state="normal")
            tag = ""
            if "[FOUND]" in msg:
                tag = "found"
            elif "[BLOCKED]" in msg:
                tag = "blocked"
            elif "[error" in msg or "[FAIL]" in msg:
                tag = "error"
            self.log_text.insert("end", msg + "\n", tag)
            self.log_text.see("end")
            self.log_text.configure(state="disabled")

        # Progress
        if engine.total > 0:
            pct = engine.done / engine.total * 100
            self.progress_var.set(pct)
            self.status_label.configure(text=f"{engine.done} / {engine.total}  —  找到 {engine.found_count} 个")

        # Refresh results table periodically
        if not hasattr(self, "_tick"):
            self._tick = 0
        self._tick += 1
        if self._tick % 10 == 0:
            for item in self.tree.get_children():
                self.tree.delete(item)
            found = [r for r in engine.results if r[2] == "found"]
            others = [r for r in engine.results if r[2] != "found"]
            for uid, name, status in found + others:
                tag = "found" if status == "found" else ("error" if status.startswith("error") or status == "fail" else "skip")
                self.tree.insert("", "end", values=(uid, name or "—", status), tags=(tag,))

        # Done check
        if not engine.running and engine.total > 0 and engine.done >= engine.total:
            self.btn_start.configure(state="normal")
            self.btn_stop.configure(state="disabled")
            self.status_label.configure(text=f"完成 — 共 {engine.done} 个，找到 {engine.found_count} 个")

        self.after(50, self._poll)


if __name__ == "__main__":
    app = App()
    app.mainloop()
