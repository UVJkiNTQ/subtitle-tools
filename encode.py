#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ffmpeg 交互式压制脚本 (Python 版, 支持任务队列, 可边压边加)
===========================================================
- 后台工作线程串行执行队列 (每次只压一个), 编码进行中仍可继续添加任务
- 方向键(↑/↓)选择, Enter 确认, q 取消
- 编码模式: CRF (默认 23) 或 2-pass (默认 6M)
- 音频: 复制原音轨 或 转 24bit FLAC (soxr→48kHz, s32p)
- 字幕: 硬字幕烧录 (libass)
- 编码器: libx264, 输出容器: mp4

依赖: python3 + ffmpeg (需 libx264 / libsoxr)
用法:  cd <视频所在目录> && python3 encode.py
       python3 encode.py <视频文件> [<字幕文件>]   # 预填源文件, 随后进入交互
       字幕传 - / none / 无字幕 表示不烧录字幕
"""

import glob
import os
import re
import select
import shutil
import subprocess
import sys
import termios
import threading
import time
import tty
from collections import deque

VIDEO_EXTS = ("mp4", "mkv", "avi", "mov", "webm", "ts", "m2ts", "flv", "wmv", "mpg", "mpeg")
SUB_EXTS = ("srt", "ass", "ssa", "vtt", "sup")

C_RED = "\033[1;31m"; C_GRN = "\033[1;32m"; C_CYN = "\033[1;36m"
C_BLU = "\033[1;34m"; C_RST = "\033[0m"


def eprint(*a, **kw):
    print(*a, file=sys.stderr, **kw)


def cls():
    sys.stdout.write("\033[H\033[J")
    sys.stdout.flush()


def get_key(timeout=None):
    """读取单个按键; 方向键返回 b'\\x1b[A' 等 (需 tty). 超时返回 None.
    必须先 setraw 再 select: 若在 canonical 模式先 select, 切换 raw 时内核会
    冲刷已缓冲的按键, 导致输入丢失."""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        if timeout is not None:
            r, _, _ = select.select([fd], [], [], timeout)
            if not r:
                return None
        ch = os.read(fd, 1)
        if ch == b"\x1b":
            ch += os.read(fd, 2)
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def pick(title, items, header_fn=None):
    """方向键选择器; 返回下标 (0-based), 取消返回 -1.
    items 可为列表, 或每次重绘时返回列表的函数 (用于动态菜单, 实时反映队列状态).
    header_fn: 每次重绘前调用, 返回若干行显示在标题下方 (用于实时状态)."""
    sel = 0
    sys.stdout.write("\033[?25l")
    try:
        while True:
            menu = items() if callable(items) else items
            n = len(menu)
            if n == 0:
                return -1
            if sel >= n:
                sel = n - 1
            cls()
            sys.stdout.write(f"{C_BLU}── {title} ──{C_RST}\n")
            if header_fn:
                for line in header_fn():
                    sys.stdout.write(line + "\n")
                sys.stdout.write("\n")
            for i, it in enumerate(menu):
                sys.stdout.write(f"{C_CYN}▸ {it}{C_RST}\n" if i == sel else f"  {it}\n")
            sys.stdout.write("\033[30m(↑/↓ 移动, Enter 确认, q 退出)\033[0m\n")
            sys.stdout.flush()
            k = get_key(0.5)   # 空闲 0.5s 自动重绘, 刷新后台编码进度
            if k is None:
                continue
            if k == b"\x1b[A":
                sel = (sel - 1) % n
            elif k == b"\x1b[B":
                sel = (sel + 1) % n
            elif k in (b"\r", b"\n"):
                return sel
            elif k in (b"q", b"Q"):
                return -1
    finally:
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()


def ask(prompt, default):
    """带默认值的输入 (raw 模式手动行编辑: 退格删除/回车确认, 不依赖终端行编辑).
    直接回车返回 default; 单独按 ESC 返回上级 (返回 None)."""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    buf = []
    sys.stdout.write(prompt)
    sys.stdout.flush()
    try:
        tty.setraw(fd)
        while True:
            r, _, _ = select.select([fd], [], [], None)
            if not r:
                continue
            ch = os.read(fd, 1)
            if ch in (b"\r", b"\n"):
                break
            elif ch in (b"\x7f", b"\x08"):      # Backspace / DEL
                if buf:
                    buf.pop()
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
            elif ch == b"\x03":                 # Ctrl+C
                raise KeyboardInterrupt
            elif ch == b"\x1b":                 # ESC: 单独按=返回上级; 方向键则跳过
                r2, _, _ = select.select([fd], [], [], 0.1)
                if r2:
                    os.read(fd, 2)
                else:
                    return None
            else:
                c = ch.decode("utf-8", "replace")
                buf.append(c)
                sys.stdout.write(c)
                sys.stdout.flush()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\r\n")
        sys.stdout.flush()
    s = "".join(buf).strip()
    return s if s else default


def list_files(exts):
    fs = []
    for e in exts:
        fs += glob.glob(f"*.{e}")
    return sorted(fs)


# ---------------- 队列状态 & 后台工作线程 ----------------

LOG_PATH = "encode.log"

lock = threading.Lock()      # 保护 pending / done / current / progress / proc
pending = []                 # 待处理任务 (先进先出)
done = []                    # 已完成任务 (task, rc)
current = None               # 正在执行的任务
progress = {}                # 正在执行任务的进度
proc = None                  # 当前 ffmpeg 子进程 (退出时终止用)
stop_event = threading.Event()
paused = threading.Event()   # 暂停: 不再领取新任务 (当前任务继续跑完)

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
stderr_buf = deque(maxlen=6)   # 最近几条 ffmpeg 输出, 顶置状态区实时显示


def log(msg):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except OSError:
        pass


def run_ffmpeg(cmd):
    """后台执行 ffmpeg: stderr 进 encode.log + 环形缓冲(顶置状态区显示), 进度经 -progress 解析."""
    global proc
    cmd = cmd + ["-progress", "pipe:1", "-nostats", "-hide_banner"]
    log("$ " + " ".join(cmd))
    logf = open(LOG_PATH, "ab")
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    with lock:
        progress.clear()
        stderr_buf.clear()
        proc = p

    def read_stderr():
        for raw in p.stderr:
            try:
                logf.write(raw)
                logf.flush()
            except OSError:
                pass
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            line = ANSI_RE.sub("", line).strip()
            if line:
                with lock:
                    stderr_buf.append(line)
        logf.close()

    rt = threading.Thread(target=read_stderr, daemon=True)
    rt.start()
    try:
        for raw in p.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if "=" in line:
                k, _, v = line.partition("=")
                if k in ("frame", "fps", "speed", "out_time_us", "out_time_ms", "bitrate"):
                    with lock:
                        progress[k] = v
                elif k == "progress" and v == "end":
                    break
    finally:
        p.stdout.close()
    rc = p.wait()
    rt.join(timeout=2)
    with lock:
        proc = None
    return rc


def worker():
    """后台线程: 串行执行 pending 队列, 每次只压一个, 可边压边加."""
    global current   # 必须声明, 否则 current=task 只是局部变量, 全局永远 None
    while not stop_event.is_set():
        if paused.is_set():
            time.sleep(0.3)
            continue
        with lock:
            if pending:
                task = pending.pop(0)
            else:
                task = None
        if task is None:
            time.sleep(0.3)
            continue
        with lock:
            current = task
        log(f"==== 开始: {task['output']}")
        try:
            rc = encode_one(task)
        except Exception as e:
            # 单个任务异常不能杀死 worker 线程, 否则后续任务不再自动执行
            log(f"!! 任务异常: {task['output']}: {e!r}")
            rc = 1
        with lock:
            done.append((task, rc))
            current = None
            progress.clear()
        log(f"==== 结束 ({'OK' if rc == 0 else 'FAIL'}): {task['output']} rc={rc}")


def status_lines():
    """主菜单顶部状态区, 每次重绘实时刷新."""
    with lock:
        cur = current
        pp = dict(progress)
        plist = list(pending)
        dn = len(done)
        sdl = list(stderr_buf)
    ok = sum(1 for _, rc in done if rc == 0)
    lines = []
    if cur:
        lines.append(f"{C_CYN}▶ 运行中: {cur['output']}{C_RST}")
        t = 0.0
        try:
            # 2pass 第一遍时 ffmpeg 可能输出 out_time_*=N/A, 不能直接 float()
            if "out_time_us" in pp:
                t = float(pp["out_time_us"]) / 1e6
            elif "out_time_ms" in pp:
                t = float(pp["out_time_ms"]) / 1e3
        except ValueError:
            t = 0.0
        bits = []
        if "frame" in pp:
            bits.append(f"帧:{pp['frame']}")
        if t:
            bits.append(f"进度:{t:.1f}s")
        if "speed" in pp:
            bits.append(f"速度:{pp['speed']}")
        if bits:
            lines.append("   " + " | ".join(bits))
    else:
        lines.append("▶ 运行中: (空闲)")
    for ln in sdl[-2:]:
        if len(ln) > 64:
            ln = ln[:64] + "…"
        lines.append(f"  {C_BLU}ffmpeg: {ln}{C_RST}")
    for t in plist:
        lines.append(f"  待压: {t['output']}")
    lines.append(f"完成: {dn} (成功 {ok} / 失败 {dn - ok})")
    return lines


# ---------------- 任务构建 ----------------

def make_task(video=None, subtitle=None):
    """交互式构建一个任务 (状态机, 每步支持 ← 返回上级; 最顶层返回/取消返回 None).
    给定 video 时跳过视频/字幕选择, 直接从编码模式开始; subtitle 为 "" 表示无字幕."""
    preset = video is not None
    mode = param = None
    if preset:
        subtitle = subtitle or ""
        step = 2
    else:
        videos = list_files(VIDEO_EXTS)
        if not videos:
            eprint(f"{C_RED}当前目录没有找到视频文件{C_RST}")
            return None
        subs = list_files(SUB_EXTS)
        subtitle = None
        step = 0
    while True:
        if step == 0:   # 选择视频源 (最顶层, 返回 = 取消)
            i = pick("选择视频源", ["← 取消"] + videos)
            if i < 0:
                return None
            if i == 0:
                return None
            video = videos[i - 1]
            step = 1
        elif step == 1:   # 选择字幕源
            i = pick("选择字幕源", ["← 返回上级", "无字幕"] + subs)
            if i < 0:
                return None
            if i == 0:
                step = 0
            else:
                subtitle = "" if i == 1 else subs[i - 2]
                step = 2
        elif step == 2:   # 编码模式
            back = "← 取消" if preset else "← 返回上级"
            i = pick("编码模式", [back, "CRF 模式 (默认 23)", "2-pass 模式 (默认 6M)"])
            if i < 0:
                return None
            if i == 0:
                if preset:
                    return None
                step = 1
            else:
                mode = "crf" if i == 1 else "2pass"
                step = 3
        elif step == 3:   # 编码参数 (CRF 值 / 目标码率, ESC 返回)
            if mode == "crf":
                crf = ask("CRF 值 (0-51, 默认 23, ESC 返回): ", "23")
                if crf is None:
                    step = 2
                    continue
                if not re.fullmatch(r"\d+", crf) or not (0 <= int(crf) <= 51):
                    eprint(f"{C_RED}无效的 CRF 值: {crf}{C_RST}")
                    continue
                param, tag = crf, f"crf{crf}"
            else:
                bitrate = ask("目标码率 (如 6M / 6000k, 默认 6M, ESC 返回): ", "6M")
                if bitrate is None:
                    step = 2
                    continue
                if not re.fullmatch(r"\d+\s*[kKmM]?", bitrate):
                    eprint(f"{C_RED}无效的码率: {bitrate}{C_RST}")
                    continue
                param, tag = bitrate, bitrate
            step = 4
        elif step == 4:   # 音频处理
            i = pick("音频处理", ["← 返回上级", "复制原音轨 (copy)", "转 24bit FLAC (soxr→48kHz)"])
            if i < 0:
                return None
            if i == 0:
                step = 3
            else:
                audio = "copy" if i == 1 else "24bit"
                break

    # 输出放在视频所在目录, 文件名只用 basename, 避免路径里再嵌子目录
    video_dir = os.path.dirname(video)
    output_stem = os.path.splitext(os.path.basename(video))[0]
    if subtitle:
        # 有字幕时把字幕名并入输出名, 避免同视频不同字幕的任务互相覆盖
        sub_stem = os.path.splitext(os.path.basename(subtitle))[0]
        output = os.path.join(video_dir, f"{output_stem}.{tag}.{sub_stem}.mp4")
    else:
        output = os.path.join(video_dir, f"{output_stem}.{tag}.mp4")
    return {"video": video, "subtitle": subtitle, "mode": mode, "param": param,
            "audio": audio, "output": output}


# ---------------- 编码 ----------------

def build_cmd(task, passno=None, passlog=None):
    """组装 ffmpeg 参数.
    passno=None -> 单遍(CRF)或 2-pass 第二遍; passno=1 -> 2-pass 第一遍(仅分析).
    passlog: 2-pass 日志路径, 放在输出文件之前避免 trailing option 警告."""
    v = task
    # 不传 -preset, 使用 x264 默认预设 (medium)
    venc = ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    venc += ["-crf", v["param"]] if v["mode"] == "crf" else ["-b:v", v["param"]]
    vf = ["-vf", f"ass='{v['subtitle']}'"] if v["subtitle"] else []
    if v["audio"] == "copy":
        aargs = ["-c:a", "copy"]
    else:
        aargs = ["-af", "aresample=resampler=soxr:osr=48000, aformat=sample_fmts=s32p",
                 "-c:a", "flac", "-sample_fmt", "s32", "-bits_per_raw_sample", "24"]

    cmd = ["ffmpeg", "-y", "-i", v["video"]] + vf
    if passno == 1:
        cmd += ["-map", "0:v:0"] + venc + ["-pass", "1"]
        if passlog:
            cmd += ["-passlogfile", passlog]
        cmd += ["-an", "-f", "null", "/dev/null"]
    else:
        cmd += ["-map", "0:v:0", "-map", "0:a:0?", "-map_metadata", "0"] + venc + aargs
        if passno == 2:
            cmd += ["-pass", "2"]
            if passlog:
                cmd += ["-passlogfile", passlog]
        cmd += [v["output"]]
    return cmd


def _rm_passlog(passlog):
    for f in (f"{passlog}-0.log", f"{passlog}-0.log.mbtree"):
        try:
            os.remove(f)
        except OSError:
            pass


def encode_one(task):
    """执行单个任务 (后台线程调用), 返回 ffmpeg 退出码."""
    if task["mode"] == "2pass":
        passlog = f"/tmp/ffmpeg2pass-{os.getpid()}"
        log("[1/2] 第一遍 (分析, 含字幕烧录)...")
        rc = run_ffmpeg(build_cmd(task, passno=1, passlog=passlog))
        if rc != 0:
            _rm_passlog(passlog)
            return rc
        log("[2/2] 第二遍 (编码)...")
        rc = run_ffmpeg(build_cmd(task, passno=2, passlog=passlog))
        _rm_passlog(passlog)
        return rc
    return run_ffmpeg(build_cmd(task))


# ---------------- 主程序 ----------------

def enqueue(task):
    """把任务加入队列并打印提示 (输出名与待处理任务重复时给出警告)."""
    with lock:
        dup = any(x["output"] == task["output"] for x in pending)
        pending.append(task)
    log("加入队列: " + task["output"])
    if dup:
        print(f"\n{C_RED}⚠ 输出名重复, 后面的任务会覆盖前面的文件!{C_RST}")
    print(f"\n{C_GRN}▶ 已加入队列: {task['output']}{C_RST}")


def parse_args(argv):
    """解析命令行预填参数; 无参数返回 None, 参数有误返回 False(已打印错误)."""
    if not argv:
        return None
    prog = os.path.basename(sys.argv[0])
    usage = f"用法: python3 {prog} [<视频文件> [<字幕文件>]]  (字幕传 - 表示无字幕)"
    if len(argv) > 2:
        eprint(f"{C_RED}{usage}{C_RST}")
        return False
    video = argv[0]
    if not os.path.isfile(video):
        eprint(f"{C_RED}视频文件不存在: {video}{C_RST}")
        return False
    subtitle = argv[1] if len(argv) > 1 else ""
    if subtitle.lower() in ("-", "none", "无字幕"):
        subtitle = ""
    if subtitle and not os.path.isfile(subtitle):
        eprint(f"{C_RED}字幕文件不存在: {subtitle}{C_RST}")
        return False
    return video, subtitle


def main():
    if not shutil.which("ffmpeg"):
        eprint(f"{C_RED}未找到 ffmpeg, 请先安装{C_RST}")
        return 1
    preset = parse_args(sys.argv[1:])
    if preset is False:
        return 2
    wt = threading.Thread(target=worker, daemon=True)
    wt.start()

    def build_menu():
        with lock:
            busy = bool(pending or current)
            npend = len(pending)
        m = ["添加压制任务"]
        if busy or paused.is_set():
            m.append("继续处理" if paused.is_set() else "暂停处理")
        if npend:
            m.append("清空待处理")
        m.append("退出")
        return m

    try:
        # 命令行预填了视频/字幕: 先走完剩余交互 (编码模式/参数/音频) 并入队
        if preset:
            t = make_task(*preset)
            if t:
                enqueue(t)
        while True:
            # 动态菜单: 每次重绘重算, 任务跑完后选项立即更新为 [添加, 退出]
            i = pick("主菜单", build_menu, header_fn=status_lines)
            if i < 0:
                break
            menu = build_menu()
            choice = menu[i] if i < len(menu) else "退出"
            if choice == "退出":
                with lock:
                    active = bool(pending or current)
                if active:
                    if pick("队列仍有任务在跑/待处理, 退出将终止当前任务并丢弃队列", ["取消", "退出"]) != 1:
                        continue
                break
            if choice == "添加压制任务":
                t = make_task()
                if t:
                    enqueue(t)
            elif choice == "暂停处理":
                paused.set()
                log("已暂停 (当前任务会继续跑完)")
            elif choice == "继续处理":
                paused.clear()
                log("已继续")
            elif choice == "清空待处理":
                if pick("确认清空全部待处理任务?", ["取消", "清空"]) == 1:
                    with lock:
                        n = len(pending)
                        pending.clear()
                    log(f"清空待处理: 移除 {n} 个")
    except KeyboardInterrupt:
        print("\n已取消")
    finally:
        stop_event.set()
        with lock:
            p = proc
        if p is not None:
            try:
                p.terminate()
            except Exception:
                pass
        wt.join(timeout=2)
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
