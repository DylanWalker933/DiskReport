# -*- coding: utf-8 -*-
"""界面自检脚本：不需要鼠标操作，直接驱动 GUI 走完 扫描 -> 填表 -> 排序 -> 导出 全流程。

用法： python gui_selftest.py [测试目录]
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tkinter.messagebox as mb

mb.showerror = lambda *a, **k: print("[messagebox.error]", a[0] if a else "")
mb.showinfo = lambda *a, **k: print("[messagebox.info]", a[0] if a else "")
mb.showwarning = lambda *a, **k: print("[messagebox.warning]", a[0] if a else "")
mb.askyesno = lambda *a, **k: False

import DiskReport as D  # noqa: E402


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else r"D:\Typora"
    App, tk, _ttk = D.make_app_class()
    win = tk.Tk()
    app = App(win)
    app.root_var.set(target)
    app.fpd_var.set(3)
    app.big_var.set(0.2)          # 0.2 MB 阈值，保证小测试目录也能命中大文件明细
    app.workers_var.set(4)

    app.start_scan()
    print("已触发“开始扫描”，目标：%s" % target)

    t0 = time.time()
    while time.time() - t0 < 300:
        win.update()
        time.sleep(0.05)
        if app.error_text:
            break
        if app.state in ("done", "idle") and app.data is not None:
            break

    if app.error_text:
        print("扫描出错：\n" + app.error_text)
        return 1
    if app.data is None:
        print("超时：未生成数据")
        return 1

    m = app.data["meta"]
    print("完成状态 : %s" % app.state)
    print("状态栏   : %s" % app.status.cget("text"))
    print("占用空间 : %s ｜ 文件 %d ｜ 文件夹 %d ｜ 耗时 %.2f 秒"
          % (D.human_size(m["total"]), m["files"], m["dirs"], m["duration"]))

    rows = app.tree.get_children()
    print("文件夹表格行数 : %d" % len(rows))
    if rows:
        first = app.tree.item(rows[0])["values"]
        print("  第 1 行      : %s | %s | %s | %s" % (first[1], first[2], first[3], first[4]))
        print("  同行大文件列 : %s" % first[6])
    print("大文件表格行数 : %d" % len(app.tree2.get_children()))
    if app.tree2.get_children():
        print("  最大文件     : %s" % (app.tree2.item(app.tree2.get_children()[0])["values"],))

    overview = app.txt_overview.get("1.0", "end").strip().splitlines()
    print("概览首行       : %s" % overview[0])
    print("概览行数       : %d" % len(overview))
    print("日志行数       : %d" % len(app.txt_log.get("1.0", "end").strip().splitlines()))

    app.sort_tree("size")
    by_size = app.tree.item(app.tree.get_children()[0])["values"]
    app.sort_tree("path")
    by_path = app.tree.item(app.tree.get_children()[0])["values"]
    app.sort_tree("files")
    by_files = app.tree.item(app.tree.get_children()[0])["values"]
    print("按体积排序首行 : %s (%s)" % (by_size[1], by_size[2]))
    print("按名称排序首行 : %s" % by_path[1])
    print("按文件数排序首行: %s (%s 个文件)" % (by_files[1], by_files[4]))

    tmp = os.environ.get("TEMP", ".")
    base = os.path.join(tmp, "gui_selftest")
    D.write_html(app.data, base + ".html")
    D.write_txt(app.data, base + ".txt")
    csvs = D.write_csv(app.data, tmp, "gui_selftest_csv")
    for path in [base + ".html", base + ".txt"] + csvs:
        print("已导出  : %-28s %8.1f KB"
              % (os.path.basename(path), os.path.getsize(path) / 1024.0))

    win.destroy()
    print("界面自检通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
