# -*- coding: utf-8 -*-
"""
磁盘空间扫描分析工具 (DiskReport)

功能：
  1. 扫描指定磁盘/目录（默认 D:），统计每个文件夹占用的总容量；
  2. 按容量从大到小排序，生成可视化报告（HTML），并列出每个文件夹中体积最大的文件；
  3. 同时可导出 CSV / TXT，便于二次处理。

运行方式：
  图形界面： DiskReport.exe
  命令行  ： DiskReport.exe --cli --root D:\\ --out report.html
"""

from __future__ import annotations

import argparse
import csv
import heapq
import html as _html
import json
import os
import queue
import socket
import sys
import threading
import time
import traceback
from datetime import datetime

APP_NAME = "磁盘空间扫描分析工具"
APP_VER = "1.0"

GLOBAL_TOP_FILES = 300          # 报告中“全盘最大文件”数量
DEFAULT_KEEP_FILES = 160        # 每个文件夹在内存中保留的最大文件条目数
DEFAULT_FILES_PER_DIR = 5       # 报告中每个文件夹列出的最大文件数
DEFAULT_BIG_MB = 100            # “大文件”阈值(MB)，同时决定哪些文件夹附大文件明细
DEFAULT_WORKERS = 8

UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def human_size(num) -> str:
    """字节数 -> 1.23 GB 这种易读形式（1024 进制）。"""
    if num is None:
        return "-"
    try:
        val = float(num)
    except (TypeError, ValueError):
        return "-"
    neg = val < 0
    val = abs(val)
    idx = 0
    while val >= 1024.0 and idx < len(UNITS) - 1:
        val /= 1024.0
        idx += 1
    text = ("%d %s" % (int(val), UNITS[idx])) if idx == 0 else ("%.2f %s" % (val, UNITS[idx]))
    return ("-" + text) if neg else text


def norm_root(path: str) -> str:
    """规范化根目录写法：D: -> D:\\ ；去掉末尾多余分隔符（盘符根除外）。"""
    path = (path or "").strip().strip('"')
    if not path:
        return path
    if len(path) == 2 and path[1] == ":":
        path += "\\"
    drive, tail = os.path.splitdrive(os.path.abspath(path))
    if tail in ("", "\\", "/"):
        return (drive.upper() + "\\") if drive else os.path.abspath(path)
    return (drive + tail).rstrip("\\/")


def ext_path(path: str) -> str:
    r"""Windows 超长路径前缀，绕过 260 字符路径限制。"""
    if os.name != "nt":
        return path
    if path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def smart_scandir(path: str):
    r"""先按普通路径打开，失败（超长路径等）时用 \\?\ 前缀重试。"""
    try:
        return os.scandir(path)
    except OSError:
        if os.name == "nt":
            return os.scandir(ext_path(path))
        raise


def is_link_entry(entry: os.DirEntry) -> bool:
    """是否为符号链接 / 目录联接（junction）：避免重复统计与死循环。"""
    try:
        if entry.is_symlink():
            return True
    except OSError:
        pass
    checker = getattr(entry, "is_junction", None)
    if checker is not None:
        try:
            if checker():
                return True
        except OSError:
            pass
    try:
        st = entry.stat(follow_symlinks=False)
        attrs = getattr(st, "st_file_attributes", 0)
        if attrs & getattr(os.stat_result, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            return True
    except OSError:
        pass
    return False


def open_in_explorer(path: str) -> None:
    try:
        if hasattr(os, "startfile"):
            os.startfile(path)  # type: ignore[attr-defined]
    except Exception:
        pass


def clean_path_text(text: str) -> str:
    r"""去掉 \\?\ / \\?\UNC\ 前缀，让错误信息更易读。"""
    return str(text).replace("\\\\?\\UNC\\", "\\\\").replace("\\\\?\\", "")


def describe_error(exc: OSError) -> str:
    """把 OSError 变成一句人话，避免 str(exc) 里带 repr 转义和重复路径。"""
    code = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
    reason = getattr(exc, "strerror", None) or str(exc)
    if isinstance(reason, bytes):
        reason = reason.decode("mbcs", "replace")
    if code:
        return "%s（错误码 %s）" % (reason, code)
    return str(reason)


def default_report_name(root: str, ext: str) -> str:
    drive = os.path.splitdrive(root)[0].replace(":", "") or "scan"
    return "磁盘空间报告_%s_%s.%s" % (drive, datetime.now().strftime("%Y%m%d_%H%M%S"), ext)


def ensure_output_stream() -> str | None:
    """窗口化打包的 exe 在命令行下运行时 stdout 可能是空的：
    优先连接父控制台，其次退化为写入日志文件。返回日志文件路径（若使用了日志）。"""
    if os.name != "nt" or not getattr(sys, "frozen", False) or sys.stdout is not None:
        return None
    try:
        import ctypes
        if ctypes.windll.kernel32.AttachConsole(-1):
            cp = ctypes.windll.kernel32.GetConsoleOutputCP() or 936
            enc = "utf-8" if cp == 65001 else "cp%d" % cp
            stream = open("CONOUT$", "w", encoding=enc, errors="replace", buffering=1)
            sys.stdout = sys.stderr = stream
            return None
    except Exception:
        pass
    try:
        import tempfile
        path = os.path.join(tempfile.gettempdir(), "DiskReport_cli.log")
        fh = open(path, "w", encoding="utf-8", errors="replace", buffering=1)
        sys.stdout = sys.stderr = fh
        return path
    except Exception:
        return None


def warn_box(title: str, text: str) -> None:
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 扫描核心
# --------------------------------------------------------------------------- #
class Node:
    """一个文件夹节点。"""

    __slots__ = ("name", "path", "parent", "children", "files",
                 "direct_size", "direct_files", "sub_dirs", "total",
                 "total_files", "total_dirs", "errors", "accessible")

    def __init__(self, name: str, path: str, parent: "Node | None" = None):
        self.name = name
        self.path = path
        self.parent = parent
        self.children: list[Node] = []
        self.files: list[tuple[int, str]] = []   # (size, name)，近似 Top-K
        self.direct_size = 0
        self.direct_files = 0
        self.sub_dirs = 0
        self.total = 0
        self.total_files = 0
        self.total_dirs = 0
        self.errors = 0
        self.accessible = True

    def add_file(self, size: int, name: str, keep: int) -> None:
        lst = self.files
        lst.append((size, name))
        if len(lst) > keep * 2:
            lst.sort(key=lambda x: x[0], reverse=True)
            del lst[keep:]

    def top_files(self, count: int) -> list[tuple[int, str]]:
        if len(self.files) > count:
            return heapq.nlargest(count, self.files, key=lambda x: x[0])
        return sorted(self.files, key=lambda x: x[0], reverse=True)


class Stats:
    """线程安全的扫描计数器。"""

    __slots__ = ("_lock", "dirs", "files", "size", "errors", "links", "current", "errors_detail")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.dirs = 0
        self.files = 0
        self.size = 0
        self.errors = 0
        self.links = 0
        self.current = ""
        self.errors_detail: list[tuple[str, str]] = []

    def bump(self, dirs: int, files: int, size: int, errors: int, links: int,
             current: str, err_list: list[tuple[str, str]]) -> None:
        with self._lock:
            self.dirs += dirs
            self.files += files
            self.size += size
            self.errors += errors
            self.links += links
            self.current = current
            if err_list:
                self.errors_detail.extend(err_list)
                if len(self.errors_detail) > 500:
                    del self.errors_detail[500:]

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "dirs": self.dirs,
                "files": self.files,
                "size": self.size,
                "errors": self.errors,
                "links": self.links,
                "current": self.current,
            }


class ScanResult:
    def __init__(self) -> None:
        self.root: Node | None = None
        self.stats = Stats()
        self.big_files: list[tuple[int, str]] = []      # 全盘最大的文件
        self.errors: list[tuple[str, str]] = []
        self.links: list[str] = []
        self.started = 0.0
        self.finished = 0.0
        self.cancelled = False

    @property
    def duration(self) -> float:
        return max(0.0, self.finished - self.started)


class Scanner:
    """多线程扫描器：一个目录一个任务，不同目录并行处理。"""

    def __init__(self, root_path: str, workers: int = DEFAULT_WORKERS,
                 keep_files: int = DEFAULT_KEEP_FILES,
                 cancel_event: threading.Event | None = None):
        self.root_path = norm_root(root_path)
        self.workers = max(1, int(workers))
        self.keep_files = max(16, int(keep_files))
        self.cancel_event = cancel_event or threading.Event()
        self.result = ScanResult()
        self._errors_lock = threading.Lock()

    # -- 内部 -------------------------------------------------------------- #
    def _add_errors(self, pairs: list[tuple[str, str]]) -> None:
        if not pairs:
            return
        pairs = [(clean_path_text(p), clean_path_text(m)) for p, m in pairs]
        with self._errors_lock:
            if len(self.result.errors) < 4000:
                self.result.errors.extend(pairs)

    def _finalize_totals(self, root: Node) -> None:
        """自底向上汇总：文件夹总容量 = 直接文件之和 + 所有子文件夹。"""
        stack: list[tuple[Node, bool]] = [(root, False)]
        while stack:
            node, processed = stack.pop()
            if not processed:
                stack.append((node, True))
                for child in node.children:
                    stack.append((child, False))
                continue
            total = node.direct_size
            files = node.direct_files
            dirs = node.sub_dirs
            for child in node.children:
                total += child.total
                files += child.total_files
                dirs += child.total_dirs
            node.total = total
            node.total_files = files
            node.total_dirs = dirs
            if node.files:
                node.files.sort(key=lambda x: x[0], reverse=True)
                if len(node.files) > self.keep_files:
                    del node.files[self.keep_files:]

    def run(self) -> ScanResult:
        res = self.result
        res.started = time.time()

        root_display = self.root_path
        root_node = Node(name=root_display, path=root_display)
        res.root = root_node

        work: "queue.Queue[tuple[Node, str]]" = queue.Queue()
        stop = threading.Event()
        local = threading.local()
        big_heap: list[tuple[int, str]] = []
        big_lock = threading.Lock()

        def worker() -> None:
            if not hasattr(local, "heap"):
                local.heap = []
                local.threshold = 0
            while not stop.is_set():
                try:
                    node, path = work.get(timeout=0.15)
                except queue.Empty:
                    continue
                try:
                    if self.cancel_event.is_set():
                        continue
                    self._scan_one_dir(node, path, work, local)
                except Exception as exc:                     # 兜底：单目录异常不影响整体
                    node.errors += 1
                    self._add_errors([(path, "扫描出错：%s: %s" % (type(exc).__name__, exc))])
                finally:
                    work.task_done()
            if local.heap:
                with big_lock:
                    big_heap.extend(local.heap)
                local.heap.clear()

        threads = [threading.Thread(target=worker, name="scan-%d" % i, daemon=True)
                   for i in range(self.workers)]
        for t in threads:
            t.start()

        work.put((root_node, root_display))

        while True:                                          # 主线程等待 / 响应取消
            if self.cancel_event.is_set():
                res.cancelled = True
                stop.set()
                break
            if work.unfinished_tasks <= 0:
                stop.set()
                break
            time.sleep(0.08)

        for t in threads:
            t.join(timeout=2.0)

        big_heap.sort(key=lambda x: x[0], reverse=True)
        res.big_files = big_heap[:GLOBAL_TOP_FILES]
        res.errors = res.errors[:4000]
        self._finalize_totals(root_node)
        res.finished = time.time()
        return res

    def _scan_one_dir(self, node: Node, path: str, work: "queue.Queue", local) -> None:
        stats = self.result.stats
        heap = local.heap
        threshold = local.threshold

        d_cnt = f_cnt = d_size = e_cnt = l_cnt = 0      # 本目录累计
        p_d = p_f = p_s = p_e = p_l = 0                 # 已上报给进度条的部分
        err_list: list[tuple[str, str]] = []
        idx = 0

        try:
            iterator = smart_scandir(path)
        except OSError as exc:
            node.accessible = False
            node.errors += 1
            self._add_errors([(path, "无法访问：%s" % describe_error(exc))])
            stats.bump(1, 0, 0, 1, 0, path, [])
            return

        with iterator as it:
            for entry in it:
                if self.cancel_event.is_set():
                    break
                idx += 1
                if idx % 512 == 0:                      # 分批上报，减少锁竞争
                    stats.bump(d_cnt - p_d, f_cnt - p_f, d_size - p_s,
                               e_cnt - p_e, l_cnt - p_l, path, err_list)
                    p_d, p_f, p_s, p_e, p_l = d_cnt, f_cnt, d_size, e_cnt, l_cnt
                    err_list = []
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if is_link_entry(entry):
                            l_cnt += 1
                            if len(self.result.links) < 200:
                                self.result.links.append(entry.path)
                            continue
                        child_path = os.path.join(path, entry.name)
                        child = Node(name=entry.name, path=child_path, parent=node)
                        node.children.append(child)
                        d_cnt += 1
                        work.put((child, child_path))
                        continue

                    if is_link_entry(entry):
                        l_cnt += 1
                        if len(self.result.links) < 200:
                            self.result.links.append(entry.path)
                        continue

                    try:
                        size = entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        size = 0
                    d_size += size
                    f_cnt += 1
                    node.add_file(size, entry.name, self.keep_files)

                    if size >= threshold:                    # 维护本线程的全局 Top 堆
                        heapq.heappush(heap, (size, os.path.join(path, entry.name)))
                        if len(heap) > GLOBAL_TOP_FILES:
                            heapq.heappop(heap)
                        local.threshold = threshold = heap[0][0]
                except OSError as exc:
                    e_cnt += 1
                    if len(err_list) < 40:
                        err_list.append((entry.path, "读取失败：%s" % describe_error(exc)))

        node.direct_size = d_size
        node.direct_files = f_cnt
        node.sub_dirs = d_cnt
        node.errors += e_cnt
        if e_cnt:
            node.accessible = False
        self._add_errors(err_list)
        stats.bump(d_cnt - p_d + 1, f_cnt - p_f, d_size - p_s,
                   e_cnt - p_e, l_cnt - p_l, path, [])


# --------------------------------------------------------------------------- #
# 报告数据组装
# --------------------------------------------------------------------------- #
def walk_nodes(root: Node):
    """先序遍历所有文件夹节点（迭代实现，不受递归深度限制）。"""
    stack: list[tuple[Node, int]] = [(root, 0)]
    while stack:
        node, depth = stack.pop()
        yield node, depth
        for child in reversed(node.children):
            stack.append((child, depth + 1))


def collect_folder_records(root: Node, options: dict) -> list[dict]:
    """文件夹列表：按容量降序；体积达标的文件夹附带其最大文件明细。"""
    files_per_dir = options["files_per_dir"]
    big_bytes = options["big_bytes"]
    root_total = max(1, root.total)
    records: list[dict] = []

    for node, depth in walk_nodes(root):
        is_root = node is root
        with_detail = is_root or depth <= 1 or node.total >= big_bytes
        top = node.top_files(files_per_dir) if with_detail else []
        records.append({
            "path": node.path,
            "name": node.path if is_root else node.name,
            "depth": depth,
            "size": node.total,
            "pct": node.total * 100.0 / root_total,
            "files": node.total_files,
            "dirs": node.total_dirs,
            "direct_files": node.direct_files,
            "errors": node.errors,
            "accessible": node.accessible,
            "children": len(node.children),
            "top": [{"n": name, "s": size, "p": os.path.join(node.path, name)}
                    for size, name in top],
        })

    records.sort(key=lambda r: (-r["size"], r["path"].lower()))
    return records


def build_report_data(res: ScanResult, options: dict) -> dict:
    root = res.root
    assert root is not None
    snap = res.stats.snapshot()
    folders = collect_folder_records(root, options)

    top_level: list[Node] = []
    root_norm = root.path.rstrip("\\/")
    for child, _ in walk_nodes(root):
        if child is root:
            continue
        parent = os.path.dirname(child.path.rstrip("\\/")).rstrip("\\/")
        if parent == root_norm or parent == "":
            top_level.append(child)
    top_level.sort(key=lambda n: n.total, reverse=True)
    toplevel_rows = [{
        "path": n.path,
        "name": n.name,
        "size": n.total,
        "pct": n.total * 100.0 / max(1, root.total),
        "files": n.total_files,
        "dirs": n.total_dirs,
    } for n in top_level]

    return {
        "meta": {
            "app": APP_NAME,
            "ver": APP_VER,
            "root": root.path,
            "started": datetime.fromtimestamp(res.started).strftime("%Y-%m-%d %H:%M:%S"),
            "finished": datetime.fromtimestamp(res.finished).strftime("%Y-%m-%d %H:%M:%S"),
            "duration": round(res.duration, 2),
            "total": root.total,
            "files": root.total_files,
            "dirs": root.total_dirs,
            "errors": snap["errors"],
            "links": snap["links"],
            "cancelled": res.cancelled,
            "big_threshold": options["big_bytes"],
            "files_per_dir": options["files_per_dir"],
            "workers": options.get("workers", DEFAULT_WORKERS),
            "host": socket.gethostname(),
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
        "toplevel": toplevel_rows,
        "folders": folders,
        "bigfiles": [{"p": path, "s": size} for size, path in res.big_files],
        "errors": [{"p": p, "m": m} for p, m in res.errors[:200]],
        "links": res.links[:100],
    }


def summary_text(data: dict) -> str:
    m = data["meta"]
    lines = []
    lines.append("%s v%s" % (m["app"], m["ver"]))
    lines.append("=" * 66)
    lines.append("扫描目录 : %s" % m["root"])
    lines.append("开始时间 : %s" % m["started"])
    lines.append("结束时间 : %s" % m["finished"])
    lines.append("扫描耗时 : %.2f 秒" % m["duration"])
    lines.append("占用空间 : %s (%d 字节)" % (human_size(m["total"]), m["total"]))
    lines.append("文件数量 : %d" % m["files"])
    lines.append("文件夹数 : %d" % m["dirs"])
    lines.append("无法访问 : %d 个目录   跳过的链接: %d" % (m["errors"], m["links"]))
    if m.get("cancelled"):
        lines.append("注意     : 本次扫描被提前中止，数据不完整")
    lines.append("")
    lines.append("一级文件夹容量排行")
    lines.append("-" * 66)
    for i, t in enumerate(data["toplevel"][:40], 1):
        lines.append("%2d. %10s  %6.2f%%  %8d 个文件  %s"
                     % (i, human_size(t["size"]), t["pct"], t["files"], t["path"]))
    lines.append("")
    lines.append("占用空间最大的文件夹 TOP 50（含其中最大的文件）")
    lines.append("-" * 66)
    for i, f in enumerate(data["folders"][:50], 1):
        lines.append("%2d. %10s  %6.2f%%  %s" % (i, human_size(f["size"]), f["pct"], f["path"]))
        for t in f["top"][:5]:
            lines.append("        └─ %10s  %s" % (human_size(t["s"]), t["n"]))
    lines.append("")
    lines.append("全盘最大文件 TOP 50")
    lines.append("-" * 66)
    for i, b in enumerate(data["bigfiles"][:50], 1):
        lines.append("%2d. %10s  %s" % (i, human_size(b["s"]), b["p"]))
    if data["errors"]:
        lines.append("")
        lines.append("无法访问的目录（前 50 条，建议以管理员身份重新扫描）")
        lines.append("-" * 66)
        for e in data["errors"][:50]:
            lines.append("%s  ->  %s" % (e["p"], e["m"]))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# 导出
# --------------------------------------------------------------------------- #
def write_txt(data: dict, out_path: str) -> None:
    with open(out_path, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write(summary_text(data))


def write_csv(data: dict, out_dir: str, base_name: str) -> list[str]:
    """导出两个 CSV：文件夹排行 + 大文件清单。返回生成的文件路径。"""
    os.makedirs(out_dir, exist_ok=True)
    folders_path = os.path.join(out_dir, base_name + "_文件夹排行.csv")
    files_path = os.path.join(out_dir, base_name + "_大文件清单.csv")

    with open(folders_path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["排名", "文件夹路径", "占用字节", "占用空间", "占全盘百分比",
                    "文件数(含子目录)", "子文件夹数", "无法访问项", "该文件夹内最大的文件"])
        for i, f in enumerate(data["folders"], 1):
            detail = " ; ".join("%s (%s)" % (t["p"], human_size(t["s"])) for t in f["top"])
            w.writerow([i, f["path"], f["size"], human_size(f["size"]),
                        "%.4f%%" % f["pct"], f["files"], f["dirs"], f["errors"], detail])

    with open(files_path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["排名", "文件路径", "字节", "大小"])
        for i, b in enumerate(data["bigfiles"], 1):
            w.writerow([i, b["p"], b["s"], human_size(b["s"])])
    return [folders_path, files_path]


def write_html(data: dict, out_path: str) -> None:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)
    payload = payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    page = (HTML_TEMPLATE
            .replace("__DATA__", payload)
            .replace("__ROOT__", _html.escape(data["meta"]["root"]))
            .replace("__BIGTH__", human_size(data["meta"]["big_threshold"]))
            .replace("__FPD__", str(data["meta"]["files_per_dir"])))
    with open(out_path, "w", encoding="utf-8", newline="") as fh:
        fh.write(page)


# --------------------------------------------------------------------------- #
# 命令行模式
# --------------------------------------------------------------------------- #
def run_cli(args) -> int:
    log_path = ensure_output_stream()
    root = norm_root(args.root)
    if not os.path.isdir(root):
        print("[错误] 目录不存在: %s" % root)
        if log_path:
            warn_box("磁盘空间扫描工具", "目录不存在：%s" % root)
        return 2

    cancel = threading.Event()
    scanner = Scanner(root, workers=args.workers, cancel_event=cancel)
    done = threading.Event()
    box: dict = {}

    def work() -> None:
        try:
            box["res"] = scanner.run()
        except Exception:
            box["err"] = traceback.format_exc()
        finally:
            done.set()

    th = threading.Thread(target=work, daemon=True)
    th.start()

    last = 0.0
    while not done.wait(timeout=0.2):
        if args.quiet:
            continue
        now = time.time()
        if now - last >= 1.0:
            last = now
            s = scanner.result.stats.snapshot()
            cur = s["current"]
            if len(cur) > 48:
                cur = cur[:20] + "..." + cur[-24:]
            print("\r 已扫描 %d 个文件夹 / %d 个文件 / %s，无法访问 %d 处   当前: %-50s"
                  % (s["dirs"], s["files"], human_size(s["size"]), s["errors"], cur),
                  end="", flush=True)
    if not args.quiet:
        print()

    if "err" in box:
        print(box["err"])
        return 1

    res: ScanResult = box["res"]
    options = {
        "files_per_dir": args.files_per_dir,
        "big_bytes": int(args.big_mb * 1024 * 1024),
        "workers": args.workers,
    }
    data = build_report_data(res, options)

    out = args.out or default_report_name(root, "html")
    out = os.path.abspath(out)
    formats = [f.strip().lower() for f in (args.format or "html").split(",") if f.strip()]
    base = os.path.splitext(os.path.basename(out))[0]
    written: list[str] = []
    if "html" in formats or "all" in formats:
        target = out if out.lower().endswith(".html") else out + ".html"
        write_html(data, target)
        written.append(target)
    if "txt" in formats or "all" in formats:
        target = os.path.join(os.path.dirname(out), base + ".txt")
        write_txt(data, target)
        written.append(target)
    if "csv" in formats or "all" in formats:
        written.extend(write_csv(data, os.path.dirname(out), base))

    if not args.quiet:
        print(summary_text(data))
    print("[完成] 用时 %.2f 秒，报告文件：" % data["meta"]["duration"])
    for path in written:
        print("   " + path)
    if log_path:
        warn_box("磁盘空间扫描工具", "扫描完成（耗时 %.1f 秒）。\n\n"
                 "本次以无控制台方式运行，日志写入：\n%s\n\n生成的文件：\n%s"
                 % (data["meta"]["duration"], log_path, "\n".join(written)))
    return 0


# --------------------------------------------------------------------------- #
# 图形界面
# --------------------------------------------------------------------------- #
def make_app_class():
    """导入 tkinter 并定义界面类；返回 (类, tk, ttk)，便于自动化测试直接驱动界面。"""
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    class App:
        def __init__(self, win: "tk.Tk") -> None:
            self.win = win
            win.title("%s v%s" % (APP_NAME, APP_VER))
            win.geometry("1180x760")
            win.minsize(960, 620)

            self.cancel = threading.Event()
            self.scanner: Scanner | None = None
            self.done_flag = threading.Event()
            self.state = "idle"          # idle / scanning / processing / done
            self.data: dict | None = None
            self.result: ScanResult | None = None
            self.error_text = ""
            self.scan_start = 0.0

            self.root_var = tk.StringVar(value=self._default_drive())
            self.workers_var = tk.IntVar(value=min(DEFAULT_WORKERS, max(2, (os.cpu_count() or 4))))
            self.fpd_var = tk.IntVar(value=DEFAULT_FILES_PER_DIR)
            self.big_var = tk.DoubleVar(value=float(DEFAULT_BIG_MB))

            self._build_ui()
            self.win.after(300, self._poll)

        # -- 界面 ---------------------------------------------------------- #
        def _default_drive(self) -> str:
            for d in ("D:\\", "E:\\", "C:\\"):
                if os.path.isdir(d):
                    return d
            return "C:\\"

        def _build_ui(self) -> None:
            top = ttk.Frame(self.win, padding=(12, 10, 12, 6))
            top.pack(fill="x")
            ttk.Label(top, text="扫描目录：").pack(side="left")
            ttk.Entry(top, textvariable=self.root_var, width=42).pack(side="left", padx=(0, 6))
            ttk.Button(top, text="浏览…", command=self.choose_dir).pack(side="left")
            ttk.Checkbutton(top, text="使用管理员权限重启",
                            command=self.relaunch_as_admin).pack(side="left", padx=(14, 0))

            opt = ttk.Frame(self.win, padding=(12, 0, 12, 8))
            opt.pack(fill="x")
            ttk.Label(opt, text="线程数：").pack(side="left")
            ttk.Spinbox(opt, from_=1, to=64, width=4, textvariable=self.workers_var).pack(side="left")
            ttk.Label(opt, text="   大文件阈值(MB)：").pack(side="left")
            ttk.Spinbox(opt, from_=1, to=102400, width=7, textvariable=self.big_var).pack(side="left")
            ttk.Label(opt, text="   每个文件夹列出大文件数：").pack(side="left")
            ttk.Spinbox(opt, from_=1, to=50, width=4, textvariable=self.fpd_var).pack(side="left")
            ttk.Label(opt, text="   （体积达到阈值的文件夹会在报告中附带其最大文件明细）",
                      foreground="#666").pack(side="left")

            bar = ttk.Frame(self.win, padding=(12, 0, 12, 8))
            bar.pack(fill="x")
            self.btn_scan = ttk.Button(bar, text="开始扫描", command=self.start_scan)
            self.btn_scan.pack(side="left")
            self.btn_stop = ttk.Button(bar, text="停止", command=self.stop_scan, state="disabled")
            self.btn_stop.pack(side="left", padx=6)
            self.btn_html = ttk.Button(bar, text="导出 HTML 报告", command=self.export_html, state="disabled")
            self.btn_html.pack(side="left", padx=(18, 0))
            self.btn_csv = ttk.Button(bar, text="导出 CSV", command=self.export_csv, state="disabled")
            self.btn_csv.pack(side="left", padx=6)
            self.btn_txt = ttk.Button(bar, text="导出 TXT", command=self.export_txt, state="disabled")
            self.btn_txt.pack(side="left")
            self.btn_open = ttk.Button(bar, text="打开报告目录", command=self.open_report_dir, state="disabled")
            self.btn_open.pack(side="left", padx=6)
            self.last_dir = ""

            self.prog = ttk.Progressbar(self.win, mode="determinate", maximum=100)
            self.prog.pack(fill="x", padx=12)
            self.status = ttk.Label(self.win, text="就绪。选择目录后点击“开始扫描”。",
                                    padding=(12, 4, 12, 2))
            self.status.pack(fill="x")
            self.status2 = ttk.Label(self.win, text="", padding=(12, 0, 12, 6), foreground="#555")
            self.status2.pack(fill="x")

            nb = ttk.Notebook(self.win)
            nb.pack(fill="both", expand=True, padx=12, pady=(0, 12))
            self.nb = nb

            self.txt_overview = tk.Text(nb, wrap="none", font=("Consolas", 10))
            nb.add(self.txt_overview, text="概览")

            frm = ttk.Frame(nb)
            nb.add(frm, text="文件夹容量排行")
            cols = ("rank", "path", "size", "pct", "files", "dirs", "big")
            self.tree = ttk.Treeview(frm, columns=cols, show="headings", height=18)
            heads = [("rank", "#", 50), ("path", "文件夹", 520), ("size", "占用空间", 110),
                     ("pct", "占全盘", 80), ("files", "文件数", 90), ("dirs", "子文件夹", 90),
                     ("big", "其中最大的文件", 380)]
            for key, text, width in heads:
                self.tree.heading(key, text=text,
                                  command=lambda k=key: self.sort_tree(k))
                self.tree.column(key, width=width, anchor="w" if key in ("path", "big") else "e")
            vs = ttk.Scrollbar(frm, orient="vertical", command=self.tree.yview)
            hs = ttk.Scrollbar(frm, orient="horizontal", command=self.tree.xview)
            self.tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
            self.tree.grid(row=0, column=0, sticky="nsew")
            vs.grid(row=0, column=1, sticky="ns")
            hs.grid(row=1, column=0, sticky="ew")
            frm.rowconfigure(0, weight=1)
            frm.columnconfigure(0, weight=1)
            self.tree.bind("<Double-1>", self.on_tree_open)
            self.tree.bind("<Button-3>", self.on_tree_open)

            frm2 = ttk.Frame(nb)
            nb.add(frm2, text="全盘最大文件")
            cols2 = ("rank", "size", "path")
            self.tree2 = ttk.Treeview(frm2, columns=cols2, show="headings", height=18)
            for key, text, width, anchor in (("rank", "#", 50, "e"), ("size", "大小", 110, "e"),
                                             ("path", "文件路径", 900, "w")):
                self.tree2.heading(key, text=text)
                self.tree2.column(key, width=width, anchor=anchor)
            vs2 = ttk.Scrollbar(frm2, orient="vertical", command=self.tree2.yview)
            self.tree2.configure(yscrollcommand=vs2.set)
            self.tree2.grid(row=0, column=0, sticky="nsew")
            vs2.grid(row=0, column=1, sticky="ns")
            frm2.rowconfigure(0, weight=1)
            frm2.columnconfigure(0, weight=1)
            self.tree2.bind("<Double-1>", self.on_tree_open)

            self.txt_log = tk.Text(nb, wrap="none", font=("Consolas", 9))
            nb.add(self.txt_log, text="日志 / 无法访问的目录")

        # -- 交互 ---------------------------------------------------------- #
        def choose_dir(self) -> None:
            path = filedialog.askdirectory(title="选择要扫描的磁盘或文件夹",
                                           initialdir=self.root_var.get() or "C:\\")
            if path:
                self.root_var.set(norm_root(path))

        def relaunch_as_admin(self) -> None:
            import ctypes
            try:
                exe = sys.executable
                params = "" if getattr(sys, "frozen", False) else '"%s"' % os.path.abspath(__file__)
                rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
                if rc <= 32:
                    messagebox.showwarning("提示", "提权被取消或失败（错误码 %s）。" % rc)
                else:
                    self.win.destroy()
            except Exception as exc:
                messagebox.showwarning("提示", "无法以管理员身份重启：%s" % exc)

        def _options(self) -> dict:
            try:
                fpd = max(1, int(self.fpd_var.get()))
            except Exception:
                fpd = DEFAULT_FILES_PER_DIR
            try:
                big = max(0.0, float(self.big_var.get()))
            except Exception:
                big = float(DEFAULT_BIG_MB)
            try:
                workers = max(1, int(self.workers_var.get()))
            except Exception:
                workers = DEFAULT_WORKERS
            return {"files_per_dir": fpd,
                    "big_bytes": int(big * 1024 * 1024),
                    "workers": workers}

        def start_scan(self) -> None:
            root = norm_root(self.root_var.get())
            if not root or not os.path.isdir(root):
                messagebox.showerror("错误", "目录不存在：%s" % (root or "(空)"))
                return
            self.root_var.set(root)
            self.cancel = threading.Event()
            self.done_flag.clear()
            self.state = "scanning"
            self.data = None
            self.result = None
            self.error_text = ""
            opts = self._options()
            self.scanner = Scanner(root, workers=opts["workers"], cancel_event=self.cancel)
            self.scan_start = time.time()

            self.btn_scan.configure(state="disabled")
            self.btn_stop.configure(state="normal")
            for b in (self.btn_html, self.btn_csv, self.btn_txt, self.btn_open):
                b.configure(state="disabled")
            self.prog.configure(mode="indeterminate")
            self.prog.start(60)
            self.txt_log.delete("1.0", "end")
            self.txt_log.insert("end", "开始扫描：%s\n" % root)

            def work() -> None:
                try:
                    res = self.scanner.run()
                    self.result = res
                    threading.Event().wait(0)   # 让出 GIL
                    self.data = build_report_data(res, opts)
                except Exception:
                    self.error_text = traceback.format_exc()
                finally:
                    self.done_flag.set()

            threading.Thread(target=work, daemon=True, name="scan-main").start()

        def stop_scan(self) -> None:
            if self.state != "scanning":
                return
            self.cancel.set()
            self.status.configure(text="正在停止…（已扫描的数据仍会保留并用于生成报告）")

        def _poll(self) -> None:
            try:
                if self.state == "scanning" and self.scanner is not None:
                    if self.done_flag.is_set():
                        self.state = "processing"
                    else:
                        s = self.scanner.result.stats.snapshot()
                        elapsed = time.time() - self.scan_start
                        self.status.configure(
                            text="扫描中…  文件夹 %s 个 ｜ 文件 %s 个 ｜ 已统计 %s ｜ 耗时 %d 秒"
                                 % (format(s["dirs"], ","), format(s["files"], ","),
                                    human_size(s["size"]), int(elapsed)))
                        cur = s["current"]
                        self.status2.configure(text=("当前：%s" % cur) if cur else "")
                elif self.state == "processing":
                    if self.data is not None or self.error_text:
                        self._finish()
                    else:
                        self.status.configure(text="扫描结束，正在汇总数据并生成排名…")
                elif self.state == "done":
                    self.state = "idle"
            except Exception:
                pass
            self.win.after(300, self._poll)

        def _finish(self) -> None:
            self.state = "done"
            self.prog.stop()
            self.prog.configure(mode="determinate", value=100)
            self.btn_scan.configure(state="normal")
            self.btn_stop.configure(state="disabled")

            if self.error_text:
                messagebox.showerror("扫描失败", self.error_text[-2000:])
                self.txt_log.insert("end", self.error_text)
                self.status.configure(text="扫描失败，详见“日志”标签页。")
                return
            if self.data is None or self.result is None:
                return

            m = self.data["meta"]
            status = ("完成：占用空间 %s ｜ 文件 %d 个 ｜ 文件夹 %d 个 ｜ 无法访问 %d 处 ｜ 耗时 %.1f 秒"
                      % (human_size(m["total"]), m["files"], m["dirs"], m["errors"], m["duration"]))
            if m["cancelled"]:
                status = "已中止（数据为部分结果）：" + status
            self.status.configure(text=status)
            self.status2.configure(text="提示：双击表格中的文件夹可直接在资源管理器中打开。")
            self._fill_ui()
            for b in (self.btn_html, self.btn_csv, self.btn_txt):
                b.configure(state="normal")
            self.nb.select(1)

        def _fill_ui(self) -> None:
            assert self.data is not None and self.result is not None
            data = self.data
            m = data["meta"]

            head = []
            head.append("%s v%s" % (m["app"], m["ver"]))
            head.append("=" * 78)
            head.append("扫描目录 : %s" % m["root"])
            head.append("开始/结束: %s  →  %s" % (m["started"], m["finished"]))
            head.append("扫描耗时 : %.2f 秒（%d 线程）" % (m["duration"], m["workers"]))
            head.append("占用空间 : %s   文件 %d 个   文件夹 %d 个"
                        % (human_size(m["total"]), m["files"], m["dirs"]))
            head.append("无法访问 : %d 个目录      跳过链接: %d"
                        % (m["errors"], m["links"]))
            if m["cancelled"]:
                head.append("注意     : 本次扫描被中止，以上为部分结果")
            head.append("")
            head.append(summary_text(data))
            self.txt_overview.delete("1.0", "end")
            self.txt_overview.insert("end", "\n".join(head))

            self.folders = data["folders"]
            self.tree.delete(*self.tree.get_children())
            limit = 20000
            for i, f in enumerate(self.folders[:limit], 1):
                top = f["top"][0] if f["top"] else None
                big = ("%s  ←  %s" % (human_size(top["s"]), top["n"])) if top else "—"
                self.tree.insert("", "end", iid=str(i - 1),
                                 values=(i, f["path"], human_size(f["size"]),
                                         "%.2f%%" % f["pct"], f["files"], f["dirs"], big))

            self.tree2.delete(*self.tree2.get_children())
            for i, b in enumerate(data["bigfiles"], 1):
                self.tree2.insert("", "end", iid=str(i - 1),
                                  values=(i, human_size(b["s"]), b["p"]))

            log = ["扫描目录：%s" % m["root"],
                   "完成时间：%s    耗时：%.2f 秒" % (m["finished"], m["duration"]),
                   "无法访问的目录 %d 个，跳过链接 %d 个" % (m["errors"], m["links"]),
                   "-" * 100]
            for e in data["errors"]:
                log.append("%s    ->  %s" % (e["p"], e["m"]))
            if data.get("links"):
                log.append("-" * 100)
                log.append("跳过（未重复统计）的链接：")
                log.extend(data["links"])
            if len(self.folders) > limit:
                log.append("-" * 100)
                log.append("提示：界面仅显示占用空间最大的 %d 个文件夹，完整数据请导出 HTML/CSV。"
                           % limit)
            self.txt_log.delete("1.0", "end")
            self.txt_log.insert("end", "\n".join(log))

        def sort_tree(self, key: str) -> None:
            if not getattr(self, "folders", None):
                return
            mapping = {"rank": "size", "path": "path", "size": "size",
                       "pct": "pct", "files": "files", "dirs": "dirs"}
            k = mapping.get(key, "size")
            rev = not getattr(self, "_sort_desc", True)
            if getattr(self, "_sort_key", None) != k:
                rev = True
            self._sort_key, self._sort_desc = k, rev
            self.folders.sort(key=lambda r: r[k] if k != "path" else r["path"].lower(),
                              reverse=rev)
            self.tree.delete(*self.tree.get_children())
            limit = 20000
            for i, f in enumerate(self.folders[:limit], 1):
                top = f["top"][0] if f["top"] else None
                big = ("%s  ←  %s" % (human_size(top["s"]), top["n"])) if top else "—"
                self.tree.insert("", "end", iid=str(i - 1),
                                 values=(i, f["path"], human_size(f["size"]),
                                         "%.2f%%" % f["pct"], f["files"], f["dirs"], big))

        def on_tree_open(self, event) -> None:
            tree = event.widget
            row = tree.identify_row(event.y)
            if not row:
                return
            col = tree.identify_column(event.x)
            try:
                idx = int(row)
            except ValueError:
                return
            if tree is self.tree:
                folder = self.folders[idx]["path"] if idx < len(self.folders) else ""
                if col == "#7":          # 右击/双击“其中最大的文件”列 -> 打开文件所在目录
                    files = self.folders[idx]["top"]
                    if files:
                        open_in_explorer(os.path.dirname(files[0]["p"]))
                        return
                if folder:
                    open_in_explorer(folder)
            elif tree is self.tree2:
                files = self.data["bigfiles"]
                if idx < len(files):
                    p = files[idx]["p"]
                    open_in_explorer(p if os.path.isfile(p) else os.path.dirname(p))

        # -- 导出 ---------------------------------------------------------- #
        def _save(self, kind: str) -> None:
            if not self.data:
                return
            root = self.data["meta"]["root"]
            if kind == "html":
                path = filedialog.asksaveasfilename(
                    title="保存 HTML 报告", defaultextension=".html",
                    initialfile=default_report_name(root, "html"),
                    filetypes=[("HTML 报告", "*.html")])
                if not path:
                    return
                write_html(self.data, path)
                self.last_dir = os.path.dirname(path)
                self.btn_open.configure(state="normal")
                if messagebox.askyesno("完成", "报告已生成：\n%s\n\n现在打开吗？" % path):
                    open_in_explorer(path)
            elif kind == "txt":
                path = filedialog.asksaveasfilename(
                    title="保存 TXT 摘要", defaultextension=".txt",
                    initialfile=default_report_name(root, "txt"),
                    filetypes=[("文本文件", "*.txt")])
                if not path:
                    return
                write_txt(self.data, path)
                self.last_dir = os.path.dirname(path)
                self.btn_open.configure(state="normal")
                messagebox.showinfo("完成", "已保存：\n%s" % path)
            elif kind == "csv":
                out_dir = filedialog.askdirectory(title="选择 CSV 保存位置")
                if not out_dir:
                    return
                base = "磁盘空间报告_" + datetime.now().strftime("%Y%m%d_%H%M%S")
                paths = write_csv(self.data, out_dir, base)
                self.last_dir = out_dir
                self.btn_open.configure(state="normal")
                messagebox.showinfo("完成", "已保存：\n" + "\n".join(paths))

        def export_html(self) -> None:
            self._save("html")

        def export_csv(self) -> None:
            self._save("csv")

        def export_txt(self) -> None:
            self._save("txt")

        def open_report_dir(self) -> None:
            if self.last_dir:
                open_in_explorer(self.last_dir)

    return App, tk, ttk


def main_gui() -> int:
    App, tk, _ttk = make_app_class()
    win = tk.Tk()
    App(win)
    win.mainloop()
    return 0


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="%s v%s" % (APP_NAME, APP_VER),
                                add_help=True)
    p.add_argument("--cli", action="store_true", help="命令行模式（不打开窗口）")
    p.add_argument("--root", default="D:\\", help="要扫描的目录，默认 D:\\")
    p.add_argument("--out", default="", help="输出报告路径（HTML）")
    p.add_argument("--format", default="html", help="导出格式：html,csv,txt,all")
    p.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="扫描线程数")
    p.add_argument("--big-mb", dest="big_mb", type=float, default=float(DEFAULT_BIG_MB),
                   help="大文件阈值(MB)，默认 100")
    p.add_argument("--files-per-dir", dest="files_per_dir", type=int,
                   default=DEFAULT_FILES_PER_DIR, help="每个文件夹列出的大文件数")
    p.add_argument("--quiet", action="store_true", help="安静模式，只输出结果路径")
    p.add_argument("--version", action="version", version="%s v%s" % (APP_NAME, APP_VER))
    return p


# --------------------------------------------------------------------------- #
# HTML 报告模板（自包含，纯静态，双击即可用浏览器打开）
# --------------------------------------------------------------------------- #
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>磁盘空间报告 - __ROOT__</title>
<style>
  :root{
    --bg:#0f1419; --panel:#161d26; --panel2:#1d2734; --line:#2a3644;
    --fg:#e6edf3; --dim:#8b9bb0; --accent:#4aa3ff; --warn:#ffb020; --bad:#ff6b6b;
    --bar1:#4aa3ff; --bar2:#39c5a3; --bar3:#ffb020; --bar4:#c779ff;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:14px/1.6 "Microsoft YaHei UI","Segoe UI",system-ui,sans-serif}
  header{padding:26px 30px 18px;background:linear-gradient(135deg,#14202e,#0f1419);
         border-bottom:1px solid var(--line)}
  h1{margin:0 0 6px;font-size:22px;font-weight:600}
  .sub{color:var(--dim);font-size:13px}
  main{padding:22px 30px 60px;max-width:1600px;margin:0 auto}
  h2{font-size:16px;margin:30px 0 12px;padding-left:10px;border-left:3px solid var(--accent)}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
  .card .k{color:var(--dim);font-size:12px;letter-spacing:.4px}
  .card .v{font-size:20px;font-weight:600;margin-top:4px;word-break:break-all}
  .card .n{color:var(--dim);font-size:12px;margin-top:2px;word-break:break-all}
  .warn{color:var(--warn)}
  table{width:100%;border-collapse:collapse;background:var(--panel);
        border:1px solid var(--line);border-radius:10px;overflow:hidden}
  th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
  th{position:sticky;top:0;background:var(--panel2);cursor:pointer;user-select:none;
     font-weight:600;font-size:13px;white-space:nowrap;z-index:2}
  th:hover{color:var(--accent)}
  th.sorted:after{content:" \25BE";color:var(--accent)}
  th.sorted.asc:after{content:" \25B4"}
  tr:hover>td{background:#1b2532}
  td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
  .path{font-family:"Cascadia Mono",Consolas,monospace;font-size:12.5px;word-break:break-all}
  .nm{font-weight:600}
  .pp{color:var(--dim);font-size:11.5px}
  .ind{display:inline-block}
  .bars{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
  .bar{margin:7px 0}
  .bar .lbl{display:flex;justify-content:space-between;gap:12px;font-size:13px;margin-bottom:3px}
  .bar .track{height:9px;background:#0d1319;border-radius:5px;overflow:hidden}
  .bar .fill{height:100%;border-radius:5px;background:var(--bar1)}
  .ctl{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:10px 0}
  input,select{background:var(--panel2);color:var(--fg);border:1px solid var(--line);
               border-radius:7px;padding:6px 10px;font:inherit;outline:none}
  input:focus,select:focus{border-color:var(--accent)}
  button{background:var(--panel2);color:var(--fg);border:1px solid var(--line);
         border-radius:7px;padding:4px 10px;font:inherit;cursor:pointer}
  button:hover{border-color:var(--accent);color:var(--accent)}
  .tag{display:inline-block;background:#22303f;color:var(--dim);border-radius:5px;
       padding:1px 7px;font-size:11.5px;margin-left:6px}
  .detail>td{background:#131a23;padding:0}
  .detail table{border:none;background:transparent;border-radius:0}
  .detail th,.detail td{border-bottom:1px dashed #26313d;background:transparent;font-size:12.5px}
  .foot{color:var(--dim);font-size:12px;margin-top:26px}
  .err{color:var(--bad)}
  summary{cursor:pointer;color:var(--accent)}
  @media print{body{background:#fff;color:#000}table,.card,.bars,th{background:#fff;color:#000}
    .detail>td{background:#fafafa}}
</style>
</head>
<body>
<header>
  <h1>磁盘空间分析报告</h1>
  <div class="sub" id="subtitle"></div>
</header>
<main>
  <h2>总览</h2>
  <div class="cards" id="cards"></div>

  <h2>一级文件夹容量排行</h2>
  <div class="bars" id="bars"></div>

  <h2>文件夹容量排行（按占用空间从大到小）</h2>
  <div class="ctl">
    <input id="q" type="search" placeholder="搜索文件夹路径…" size="34">
    <select id="minsize">
      <option value="0">显示全部文件夹</option>
      <option value="104857600">仅 ≥ 100 MB</option>
      <option value="1073741824">仅 ≥ 1 GB</option>
      <option value="10737418240">仅 ≥ 10 GB</option>
      <option value="107374182400">仅 ≥ 100 GB</option>
    </select>
    <span class="sub" id="foldstat"></span>
    <button id="more" style="display:none">显示更多</button>
  </div>
  <table id="ftable">
    <thead><tr>
      <th data-k="rank" class="num" style="width:56px">排名</th>
      <th data-k="path">文件夹</th>
      <th data-k="size" class="num" style="width:110px">占用空间</th>
      <th data-k="pct" class="num" style="width:88px">占全盘</th>
      <th data-k="files" class="num" style="width:96px">文件数</th>
      <th data-k="dirs" class="num" style="width:96px">子文件夹</th>
      <th style="width:130px">其中最大的文件</th>
    </tr></thead>
    <tbody></tbody>
  </table>

  <h2>全盘最大文件</h2>
  <div class="ctl">
    <input id="qf" type="search" placeholder="搜索文件名…" size="34">
    <span class="sub" id="bigstat"></span>
  </div>
  <table id="btable">
    <thead><tr>
      <th data-k="rank" class="num" style="width:56px">排名</th>
      <th data-k="size" class="num" style="width:120px">大小</th>
      <th data-k="path">文件路径</th>
    </tr></thead>
    <tbody></tbody>
  </table>

  <h2>说明</h2>
  <div class="bars">
    <div>· 文件夹“占用空间”= 该文件夹及其所有子文件夹中文件的字节数合计（1 GB = 1024 MB）。</div>
    <div>· 每一行的“其中最大的文件”列出直接位于该文件夹中的最大文件（最多 __FPD__ 个），点击可展开查看完整路径与大小。</div>
    <div>· 体积 ≥ __BIGTH__ 的文件夹，以及一级、二级文件夹，都会在报告中附带大文件明细。</div>
    <div>· 符号链接 / 目录联接（junction）不重复统计，避免容量虚高与死循环。</div>
    <div>· 受权限保护的目录会计入“无法访问”，以管理员身份重新扫描可获得更完整的结果。</div>
    <div>· <code>$RECYCLE.BIN</code> 是回收站、<code>System Volume Information</code> 是系统还原/卷影副本，
     它们占用空间较大属于正常现象，清理回收站即可释放。</div>
    <div>· 若某个文件夹显示“无法访问”，可在主界面勾选“使用管理员权限重启”后重新扫描。</div>
    <div id="errbox"></div>
  </div>
  <div class="foot" id="foot"></div>
</main>
<script>
const DATA = __DATA__;
const el = id => document.getElementById(id);
const fmt = n => {
  if (n === null || n === undefined) return '-';
  const u = ['B','KB','MB','GB','TB','PB']; let i = 0, v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return (i === 0 ? String(Math.round(v)) : v.toFixed(2)) + ' ' + u[i];
};
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const M = DATA.meta;

document.title = '磁盘空间报告 - ' + M.root + ' - ' + fmt(M.total);
el('subtitle').textContent = '扫描目录 ' + M.root + ' ｜ 扫描时间 ' + M.started + ' → ' + M.finished
  + '（耗时 ' + M.duration + ' 秒，' + M.workers + ' 线程）'
  + (M.cancelled ? ' ｜ 注意：本次扫描被提前中止，数据不完整' : '');

const biggest = DATA.bigfiles[0];
const cards = [
  ['占用空间合计', fmt(M.total), '共 ' + M.files.toLocaleString() + ' 个文件 / ' + M.dirs.toLocaleString() + ' 个子文件夹'],
  ['全盘最大文件', biggest ? fmt(biggest.s) : '-', biggest ? biggest.p : '无'],
  ['大文件阈值', fmt(M.big_threshold), '每个文件夹列出前 ' + M.files_per_dir + ' 个大文件'],
  ['无法访问的目录', M.errors.toLocaleString(), '权限不足或被系统占用'],
  ['跳过的链接', M.links.toLocaleString(), '符号链接 / junction，不重复统计'],
  ['生成时间', M.generated, '由 ' + M.app + ' v' + M.ver + ' 生成（' + M.host + '）']
];
el('cards').innerHTML = cards.map(c =>
  '<div class="card"><div class="k">' + esc(c[0]) + '</div><div class="v'
  + (c[0].indexOf('无法访问') >= 0 && M.errors > 0 ? ' warn' : '') + '">' + esc(String(c[1]))
  + '</div><div class="n path">' + esc(String(c[2])) + '</div></div>').join('');

const colors = ['var(--bar1)','var(--bar2)','var(--bar3)','var(--bar4)'];
const maxPct = Math.max(1, ...DATA.toplevel.map(t => t.pct));
el('bars').innerHTML = DATA.toplevel.length ? DATA.toplevel.slice(0, 40).map((t, i) =>
  '<div class="bar"><div class="lbl"><span class="path">' + esc(t.path) + '</span><span>'
  + fmt(t.size) + ' ｜ ' + t.pct.toFixed(2) + '% ｜ ' + t.files.toLocaleString() + ' 个文件</span></div>'
  + '<div class="track"><div class="fill" style="width:'
  + Math.max(0.4, t.pct / maxPct * 100).toFixed(2) + '%;background:' + colors[i % 4] + '"></div></div></div>'
).join('') : '<div class="sub">（无数据）</div>';

const folders = DATA.folders.map((f, i) => Object.assign({rank: i + 1, i: i}, f));
const fBody = document.querySelector('#ftable tbody');
let fsort = {k: 'size', asc: false}, fShown = 400, fQ = '', fMin = 0;

function folderFiltered() {
  const q = fQ.toLowerCase();
  return folders.filter(f => f.size >= fMin && (!q || f.path.toLowerCase().includes(q)));
}
function folderSorted() {
  const arr = folderFiltered(), sgn = fsort.asc ? 1 : -1, k = fsort.k;
  arr.sort((a, b) => {
    let x = a[k], y = b[k];
    if (k === 'path') { x = a.path.toLowerCase(); y = b.path.toLowerCase(); }
    if (x === y) return a.path.localeCompare(b.path);
    return x < y ? -sgn : sgn;
  });
  return arr;
}
function renderFolders() {
  const all = folderSorted(), arr = all.slice(0, fShown);
  fBody.innerHTML = arr.map(f => {
    const indent = Math.min(f.depth, 12) * 14;
    const parent = f.path.slice(0, Math.max(0, f.path.length - f.name.length - 1));
    const has = f.top && f.top.length;
    return '<tr data-i="' + f.i + '"><td class="num">' + f.rank + '</td>'
      + '<td class="path"><span class="ind" style="width:' + indent + 'px"></span>'
      + '<span class="nm">' + esc(f.name) + '</span> '
      + (parent ? '<span class="pp">' + esc(parent) + '</span>' : '')
      + (f.errors ? '<span class="tag err">' + f.errors + ' 处无法访问</span>' : '') + '</td>'
      + '<td class="num"><b>' + fmt(f.size) + '</b></td>'
      + '<td class="num">' + f.pct.toFixed(2) + '%</td>'
      + '<td class="num">' + f.files.toLocaleString() + '</td>'
      + '<td class="num">' + f.dirs.toLocaleString() + '</td>'
      + '<td>' + (has ? '<button data-t="' + f.i + '">查看 ' + f.top.length + ' 个</button>'
                      : '<span class="pp">—</span>') + '</td></tr>';
  }).join('');
  el('foldstat').textContent = '符合条件 ' + all.length.toLocaleString() + ' 个文件夹，当前显示前 '
    + arr.length.toLocaleString() + ' 个';
  el('more').style.display = all.length > arr.length ? '' : 'none';
}
fBody.addEventListener('click', ev => {
  const btn = ev.target.closest('button[data-t]');
  if (!btn) return;
  const f = folders[+btn.dataset.t], tr = btn.closest('tr'), next = tr.nextElementSibling;
  if (next && next.classList.contains('detail')) { next.remove(); return; }
  const listed = f.top.reduce((a, b) => a + b.s, 0);
  const rest = f.files - f.top.length;
  tr.insertAdjacentHTML('afterend', '<tr class="detail"><td colspan="7">'
    + '<table><thead><tr><th>该文件夹中最大的文件（直接子文件）</th>'
    + '<th class="num" style="width:120px">大小</th></tr></thead><tbody>'
    + f.top.map(t => '<tr><td class="path">' + esc(t.p) + '</td><td class="num">' + fmt(t.s)
        + '</td></tr>').join('')
    + (rest > 0 ? '<tr><td class="pp">其余 ' + rest.toLocaleString() + ' 个文件合计 '
        + fmt(Math.max(0, f.size - listed)) + '</td><td></td></tr>' : '')
    + '</tbody></table></td></tr>');
});
document.querySelectorAll('#ftable th[data-k]').forEach(th => th.addEventListener('click', () => {
  const k = th.dataset.k;
  if (fsort.k === k) fsort.asc = !fsort.asc;
  else fsort = {k: k, asc: (k === 'path' || k === 'rank')};
  document.querySelectorAll('#ftable th').forEach(x => x.classList.remove('sorted', 'asc'));
  th.classList.add('sorted'); if (fsort.asc) th.classList.add('asc');
  fShown = 400; renderFolders();
}));
el('q').addEventListener('input', e => { fQ = e.target.value.trim(); fShown = 400; renderFolders(); });
el('minsize').addEventListener('change', e => { fMin = +e.target.value; fShown = 400; renderFolders(); });
el('more').addEventListener('click', () => { fShown += 1000; renderFolders(); });
document.querySelector('#ftable th[data-k="size"]').classList.add('sorted');
renderFolders();

const bigs = DATA.bigfiles.map((b, i) => ({rank: i + 1, s: b.s, p: b.p}));
const bBody = document.querySelector('#btable tbody');
function renderBigs() {
  const q = el('qf').value.trim().toLowerCase();
  const arr = q ? bigs.filter(b => b.p.toLowerCase().includes(q)) : bigs;
  bBody.innerHTML = arr.map(b => '<tr><td class="num">' + b.rank + '</td><td class="num"><b>'
    + fmt(b.s) + '</b></td><td class="path">' + esc(b.p) + '</td></tr>').join('');
  el('bigstat').textContent = '共列出 ' + bigs.length + ' 个全盘最大文件';
}
el('qf').addEventListener('input', renderBigs);
renderBigs();

if (DATA.errors.length) {
  el('errbox').innerHTML = '<details><summary>查看 ' + DATA.errors.length
    + ' 条无法访问的目录记录</summary>'
    + DATA.errors.map(e => '<div class="path err">' + esc(e.p) + ' — ' + esc(e.m) + '</div>').join('')
    + '</details>';
}
el('foot').textContent = M.app + ' v' + M.ver + ' ｜ 报告生成于 ' + M.generated
  + ' ｜ 文件夹总数 ' + M.dirs.toLocaleString();
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(argv)
    if args.cli:
        return run_cli(args)
    try:
        return main_gui()
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
