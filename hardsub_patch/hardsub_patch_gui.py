#!/usr/bin/env python3
"""
hardsub_patch_gui.py - 硬字幕 Rev 小助手（Windows Tkinter 图形界面）

功能特性：
- 优先识别绿色内置 runtime/ 中的 FFmpeg、AviSynth.dll、LSMASHSource.dll 与 VSFilterMod.dll
- 线程安全的日志队列刷新与弹窗回调，兼容 PyInstaller 无控制台模式（console=False）
- 支持中途取消任务并安全终止 ffmpeg 子进程
- 选择输入视频后后台异步读取元信息（分辨率/帧率/时长/编码/码率）并建议输出路径
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext
from typing import Any

from hardsub_patch_avs import (
    PatchCancelledError,
    check_avs_available,
    check_ffmpeg,
    find_avisynth_dll,
    find_ffmpeg_tool,
    find_lsmash_dll,
    find_vsfiltermod_dll,
    is_portable_path,
    parse_time,
    patch_video,
    probe_video,
)


def format_duration(seconds: float) -> str:
    """将秒数格式化为 H:MM:SS.ms 字符串。"""
    total = max(0.0, float(seconds))
    hours = int(total // 3600)
    minutes = int((total % 3600) // 60)
    secs = total % 60
    return f'{hours}:{minutes:02d}:{secs:05.2f}'


class QueueWriter:
    """将 stdout 转发到 Tkinter 队列；兼容 PyInstaller console=False 下 terminal 为 None 的情况。"""

    def __init__(self, log_queue: queue.Queue[str], terminal: Any) -> None:
        self.q = log_queue
        self.terminal = terminal

    def write(self, msg: str) -> None:
        if not msg:
            return
        for line in msg.splitlines():
            if line.strip():
                self.q.put(line)
        if self.terminal is not None:
            try:
                self.terminal.write(msg)
            except UnicodeEncodeError:
                enc = getattr(self.terminal, 'encoding', None) or 'utf-8'
                safe = msg.encode(enc, errors='replace').decode(enc, errors='replace')
                self.terminal.write(safe)
            except Exception:
                pass
            try:
                self.terminal.flush()
            except Exception:
                pass

    def flush(self) -> None:
        if self.terminal is not None:
            try:
                self.terminal.flush()
            except Exception:
                pass


class App:
    """主窗口应用控制器。"""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title('硬字幕 Rev 小助手（视频区段重压工具）')
        root.geometry('760x780')
        root.minsize(680, 650)
        root.resizable(True, True)
        root.protocol('WM_DELETE_WINDOW', self.on_close)

        # 文件与区间变量
        self.input_var = tk.StringVar()
        self.input_info_var = tk.StringVar(value='未选择输入视频')
        self.start_var = tk.StringVar(value='0:00:00.00')
        self.end_var = tk.StringVar(value='0:00:00.00')
        self.avs_var = tk.StringVar()
        self.raw_video_var = tk.StringVar()
        self.subtitle_var = tk.StringVar()
        self.vsfiltermod_var = tk.StringVar(value=find_vsfiltermod_dll() or '')
        self.output_var = tk.StringVar()

        # 模式与编码参数变量
        self.use_avs = tk.BooleanVar(value=True)
        self.auto_avs = tk.BooleanVar(value=True)
        self.encoder_var = tk.StringVar(value='自动')
        self.preset_var = tk.StringVar(value='fast')
        self.crf_var = tk.StringVar(value='')
        self.crf_enabled = tk.BooleanVar(value=False)
        self.gop_var = tk.StringVar(value='0')
        self.extra_var = tk.StringVar()

        # 异步任务与进程管理状态
        self.queue: queue.Queue[str] = queue.Queue()
        self.running = False
        self.cancel_event = threading.Event()
        self.active_process: subprocess.Popen[str] | None = None
        self.process_lock = threading.Lock()

        self.create_widgets()
        if not self.check_ffmpeg_on_start():
            return

        # 启动时校验 AviSynth(+) 与 VSFilterMod
        if self.use_avs.get() and not check_avs_available():
            messagebox.showwarning(
                'AviSynth 未就绪',
                '未检测到 AviSynth(+) 环境，AVS 模式已自动关闭。\n'
                '请将 AviSynth.dll 放入 runtime 目录或安装 AviSynth+ 后重试。',
            )
            self.use_avs.set(False)
            self.toggle_inputs()
        elif self.use_avs.get() and self.auto_avs.get() and not self.vsfiltermod_var.get():
            self.queue.put('提示：未自动检测到 VSFilterMod.dll，请在上方手动浏览指定其路径。')

    def check_ffmpeg_on_start(self) -> bool:
        """启动阶段校验 ffmpeg / ffprobe 可用性。"""
        try:
            check_ffmpeg()
            return True
        except Exception as exc:
            messagebox.showerror('错误', str(exc))
            self.root.destroy()
            return False

    # ==================== 界面构建 ====================

    def create_widgets(self) -> None:
        """初始化所有界面控件与布局。"""
        frm_files = tk.LabelFrame(self.root, text='文件设置', padx=10, pady=8)
        frm_files.pack(fill='x', padx=10, pady=5)
        frm_files.columnconfigure(1, weight=1)

        def browse(var, title, save=False, filetypes=None, on_selected=None):
            if save:
                path = filedialog.asksaveasfilename(
                    title=title,
                    defaultextension='.mkv',
                    filetypes=filetypes or [('All files', '*.*')],
                )
            else:
                path = filedialog.askopenfilename(
                    title=title,
                    filetypes=filetypes or [('All files', '*.*')],
                )
            if path:
                var.set(path)
                if on_selected:
                    on_selected(path)

        # Row 0: 输入视频
        tk.Label(frm_files, text='输入视频:').grid(row=0, column=0, sticky='w', pady=2)
        tk.Entry(frm_files, textvariable=self.input_var).grid(row=0, column=1, sticky='ew', padx=5)
        tk.Button(
            frm_files,
            text='浏览',
            width=8,
            command=lambda: browse(
                self.input_var,
                '选择原始视频文件',
                filetypes=[('视频文件', '*.mp4 *.mkv *.avi *.mov *.ts *.m2ts'), ('All', '*.*')],
                on_selected=self.on_input_selected,
            ),
        ).grid(row=0, column=2)

        # Row 1: 输入视频元信息摘要
        tk.Label(frm_files, textvariable=self.input_info_var, fg='#555555', font=('', 8)).grid(
            row=1, column=1, columnspan=2, sticky='w', padx=5, pady=(0, 3)
        )

        # Row 2: 替换区间起止时间
        tk.Label(frm_files, text='替换区间:').grid(row=2, column=0, sticky='w', pady=2)
        frm_time = tk.Frame(frm_files)
        frm_time.grid(row=2, column=1, columnspan=2, sticky='w', padx=5)
        tk.Label(frm_time, text='起始:').pack(side='left')
        tk.Entry(frm_time, textvariable=self.start_var, width=14).pack(side='left', padx=(2, 10))
        tk.Label(frm_time, text='结束:').pack(side='left')
        tk.Entry(frm_time, textvariable=self.end_var, width=14).pack(side='left', padx=(2, 10))
        tk.Label(frm_time, text='(H:MM:SS.ms 或 纯秒数)', fg='#666666', font=('', 8)).pack(side='left')

        # Row 3: AVS 模式开关
        frm_mode = tk.Frame(frm_files)
        frm_mode.grid(row=3, column=0, columnspan=3, sticky='w', pady=(6, 2))
        self.avs_check = tk.Checkbutton(
            frm_mode, text='使用 AVS 脚本', variable=self.use_avs, command=self.on_avs_toggle
        )
        self.avs_check.pack(side='left')
        self.auto_avs_check = tk.Checkbutton(
            frm_mode,
            text='自动生成 AVS 脚本（LSMASHSource + VSFilterMod）',
            variable=self.auto_avs,
            command=self.on_auto_avs_toggle,
        )
        self.auto_avs_check.pack(side='left', padx=(12, 0))

        # Row 4: 手动 AVS 脚本
        self.lbl_avs = tk.Label(frm_files, text='AVS 脚本:')
        self.lbl_avs.grid(row=4, column=0, sticky='w', pady=2)
        self.ent_avs = tk.Entry(frm_files, textvariable=self.avs_var)
        self.ent_avs.grid(row=4, column=1, sticky='ew', padx=5)
        self.btn_avs = tk.Button(
            frm_files,
            text='浏览',
            width=8,
            command=lambda: browse(
                self.avs_var, '选择 AviSynth 脚本', filetypes=[('AVS 脚本', '*.avs'), ('All', '*.*')]
            ),
        )
        self.btn_avs.grid(row=4, column=2)

        # Row 5: RAW 视频
        self.lbl_raw = tk.Label(frm_files, text='RAW 视频:')
        self.lbl_raw.grid(row=5, column=0, sticky='w', pady=2)
        self.ent_raw = tk.Entry(frm_files, textvariable=self.raw_video_var)
        self.ent_raw.grid(row=5, column=1, sticky='ew', padx=5)
        self.btn_raw = tk.Button(
            frm_files,
            text='浏览',
            width=8,
            command=lambda: browse(
                self.raw_video_var,
                '选择 RAW 视频文件',
                filetypes=[('视频文件', '*.mp4 *.mkv *.avi *.mov *.ts *.m2ts'), ('All', '*.*')],
            ),
        )
        self.btn_raw.grid(row=5, column=2)

        # Row 6: 字幕文件
        self.lbl_sub = tk.Label(frm_files, text='字幕文件:')
        self.lbl_sub.grid(row=6, column=0, sticky='w', pady=2)
        self.ent_sub = tk.Entry(frm_files, textvariable=self.subtitle_var)
        self.ent_sub.grid(row=6, column=1, sticky='ew', padx=5)
        self.btn_sub = tk.Button(
            frm_files,
            text='浏览',
            width=8,
            command=lambda: browse(
                self.subtitle_var,
                '选择字幕文件 (.ass/.ssa/.srt)',
                filetypes=[('字幕文件', '*.ass *.ssa *.srt *.idx *.sub'), ('All', '*.*')],
            ),
        )
        self.btn_sub.grid(row=6, column=2)

        # Row 7: VSFilterMod.dll 路径（仅自动 AVS 模式启用）
        self.lbl_vsf = tk.Label(frm_files, text='VSFilterMod:')
        self.lbl_vsf.grid(row=7, column=0, sticky='w', pady=2)
        self.ent_vsf = tk.Entry(frm_files, textvariable=self.vsfiltermod_var)
        self.ent_vsf.grid(row=7, column=1, sticky='ew', padx=5)
        self.btn_vsf = tk.Button(
            frm_files,
            text='浏览',
            width=8,
            command=lambda: browse(
                self.vsfiltermod_var,
                '选择 VSFilterMod.dll',
                filetypes=[('DLL 插件', '*.dll'), ('All', '*.*')],
            ),
        )
        self.btn_vsf.grid(row=7, column=2)

        # Row 8: 输出文件
        tk.Label(frm_files, text='输出文件:').grid(row=8, column=0, sticky='w', pady=2)
        tk.Entry(frm_files, textvariable=self.output_var).grid(row=8, column=1, sticky='ew', padx=5)
        tk.Button(
            frm_files,
            text='浏览',
            width=8,
            command=lambda: browse(
                self.output_var,
                '设置输出视频文件',
                save=True,
                filetypes=[('MKV 文件', '*.mkv'), ('MP4 文件', '*.mp4'), ('All', '*.*')],
            ),
        ).grid(row=8, column=2)

        self.toggle_inputs()

        # 编码参数区
        frm_enc = tk.LabelFrame(self.root, text='编码参数', padx=10, pady=8)
        frm_enc.pack(fill='x', padx=10, pady=5)
        frm_enc.columnconfigure(5, weight=1)

        tk.Label(frm_enc, text='编码器:').grid(row=0, column=0, sticky='w')
        enc_menu = tk.OptionMenu(frm_enc, self.encoder_var, '自动', 'libx264', 'libx265')
        enc_menu.config(width=9)
        enc_menu.grid(row=0, column=1, sticky='w')

        tk.Label(frm_enc, text='Preset:').grid(row=0, column=2, sticky='w', padx=(15, 0))
        preset_menu = tk.OptionMenu(
            frm_enc, self.preset_var, 'ultrafast', 'veryfast', 'fast', 'medium', 'slow', 'veryslow'
        )
        preset_menu.config(width=9)
        preset_menu.grid(row=0, column=3, sticky='w')

        self.crf_check = tk.Checkbutton(frm_enc, text='CRF:', variable=self.crf_enabled, command=self.toggle_crf)
        self.crf_check.grid(row=0, column=4, sticky='w', padx=(15, 0))
        self.crf_entry = tk.Entry(frm_enc, textvariable=self.crf_var, width=6)
        self.crf_entry.grid(row=0, column=5, sticky='w')
        self.crf_entry.config(state='disabled')

        tk.Label(frm_enc, text='GOP (关键帧间隔):').grid(row=1, column=0, sticky='w', pady=4)
        tk.Entry(frm_enc, textvariable=self.gop_var, width=8).grid(row=1, column=1, sticky='w', padx=4)
        tk.Label(frm_enc, text='帧数，0=自动检测原片', fg='#666666', font=('', 8)).grid(
            row=1, column=2, columnspan=4, sticky='w'
        )

        tk.Label(frm_enc, text='额外参数:').grid(row=2, column=0, sticky='w', pady=2)
        tk.Entry(frm_enc, textvariable=self.extra_var).grid(row=2, column=1, columnspan=5, sticky='ew', padx=4)

        # 操作按钮区
        frm_ctl = tk.Frame(self.root)
        frm_ctl.pack(pady=8)
        self.btn_start = tk.Button(
            frm_ctl,
            text='开始处理',
            command=self.start_processing,
            bg='#4CAF50',
            fg='white',
            font=('Arial', 10, 'bold'),
            width=14,
        )
        self.btn_start.pack(side='left', padx=6)

        self.btn_cancel = tk.Button(
            frm_ctl,
            text='取消处理',
            command=self.cancel_processing,
            bg='#E53935',
            fg='white',
            width=10,
            state='disabled',
        )
        self.btn_cancel.pack(side='left', padx=6)

        tk.Button(frm_ctl, text='检测环境', command=self.show_env_check, width=10).pack(side='left', padx=6)
        tk.Button(frm_ctl, text='清空日志', command=self.clear_log, width=10).pack(side='left', padx=6)
        tk.Button(frm_ctl, text='退出', command=self.on_close, width=8).pack(side='left', padx=6)

        # 日志输出区
        frm_log = tk.LabelFrame(self.root, text='处理日志', padx=5, pady=5)
        frm_log.pack(fill='both', expand=True, padx=10, pady=(0, 10))
        self.log = scrolledtext.ScrolledText(frm_log, wrap=tk.WORD, font=('Consolas', 9), height=14)
        self.log.pack(fill='both', expand=True)

        self.update_log()

    # ==================== 交互事件处理 ====================

    def on_input_selected(self, path: str) -> None:
        """选择输入视频后后台异步探测视频信息，并自动填充默认输出路径。"""
        if not self.output_var.get().strip():
            stem, ext = os.path.splitext(path)
            out_ext = ext if ext.lower() in ('.mkv', '.mp4') else '.mkv'
            self.output_var.set(f'{stem}_patched{out_ext}')

        self.input_info_var.set('正在读取视频信息...')

        def _probe() -> None:
            try:
                stream, duration = probe_video(path)
                br = stream.get('bit_rate') or stream.get('_estimated_bit_rate')
                br_str = f'{int(br) // 1000} kbps' if br and str(br).isdigit() else '未知码率'
                summary = (
                    f"{stream.get('width')}x{stream.get('height')} | "
                    f"{stream.get('_fps_str', '')} ({stream.get('_fps', 0):.3f} fps) | "
                    f'时长 {format_duration(duration)} | '
                    f"{stream.get('codec_name', '?')} | {br_str}"
                )
            except Exception as exc:
                summary = f'读取视频信息失败: {exc}'
            self.root.after(0, lambda: self.input_info_var.set(summary))

        threading.Thread(target=_probe, daemon=True).start()

    def show_env_check(self) -> None:
        """弹窗展示 FFmpeg / AviSynth+ / 插件的探测状态及来源（绿色内置或系统环境）。"""
        def _tag(path: str) -> str:
            return '[内置绿色版]' if is_portable_path(path) else '[系统环境]'

        lines: list[str] = []
        ff = find_ffmpeg_tool('ffmpeg')
        fp = find_ffmpeg_tool('ffprobe')
        lines.append(f"{'✅' if ff else '❌'} FFmpeg: {f'{_tag(ff)} {ff}' if ff else '未找到'}")
        lines.append(f"{'✅' if fp else '❌'} FFprobe: {f'{_tag(fp)} {fp}' if fp else '未找到'}")

        avs_ok = check_avs_available()
        avs_dll = find_avisynth_dll()
        if avs_ok and avs_dll:
            lines.append(f'✅ AviSynth+: {_tag(avs_dll)} {avs_dll}')
        elif avs_ok:
            lines.append('✅ AviSynth+: [系统环境] 已安装')
        else:
            lines.append('❌ AviSynth+: 未检测到（请将 AviSynth.dll 放入 runtime 目录或安装 AviSynth+）')

        lsmash = find_lsmash_dll()
        lines.append(
            f"{'✅' if lsmash else '⚠️'} LSMASHSource.dll: "
            f"{f'{_tag(lsmash)} {lsmash}' if lsmash else '未找到（请放入 runtime 目录或 AviSynth+ 插件目录）'}"
        )

        vsf = find_vsfiltermod_dll(self.vsfiltermod_var.get().strip() or None)
        if vsf and not self.vsfiltermod_var.get().strip():
            self.vsfiltermod_var.set(vsf)
        lines.append(
            f"{'✅' if vsf else '❌'} VSFilterMod.dll: "
            f"{f'{_tag(vsf)} {vsf}' if vsf else '未找到（自动 AVS 需要此插件）'}"
        )

        messagebox.showinfo('环境检测结果', '\n'.join(lines))

    def clear_log(self) -> None:
        self.log.delete('1.0', tk.END)

    def on_avs_toggle(self) -> None:
        if self.use_avs.get() and not check_avs_available():
            messagebox.showwarning(
                'AviSynth 未就绪',
                '未检测到 AviSynth(+) 环境，AVS 模式无法使用。\n'
                '请将 AviSynth.dll 放入 runtime 目录、安装 AviSynth+ 或改用 RAW+字幕模式。',
            )
            self.use_avs.set(False)
        self.toggle_inputs()

    def on_auto_avs_toggle(self) -> None:
        if self.auto_avs.get() and not self.vsfiltermod_var.get().strip():
            found = find_vsfiltermod_dll()
            if found:
                self.vsfiltermod_var.set(found)
        self.toggle_inputs()

    def toggle_inputs(self) -> None:
        """根据当前模式动态启用或禁用相关输入控件。"""
        use_avs = self.use_avs.get()
        auto_avs = self.auto_avs.get()

        if use_avs:
            self.auto_avs_check.config(state='normal')
        else:
            self.auto_avs_check.config(state='disabled')
            self.auto_avs.set(False)
            auto_avs = False

        manual_state = 'normal' if (use_avs and not auto_avs) else 'disabled'
        manual_fg = 'black' if manual_state == 'normal' else 'gray'
        self.ent_avs.config(state=manual_state)
        self.btn_avs.config(state=manual_state)
        self.lbl_avs.config(fg=manual_fg)

        raw_state = 'normal' if ((use_avs and auto_avs) or not use_avs) else 'disabled'
        raw_fg = 'black' if raw_state == 'normal' else 'gray'
        for widget in (self.ent_raw, self.btn_raw, self.ent_sub, self.btn_sub):
            widget.config(state=raw_state)
        self.lbl_raw.config(fg=raw_fg)
        self.lbl_sub.config(fg=raw_fg)

        vsf_state = 'normal' if (use_avs and auto_avs) else 'disabled'
        vsf_fg = 'black' if vsf_state == 'normal' else 'gray'
        self.ent_vsf.config(state=vsf_state)
        self.btn_vsf.config(state=vsf_state)
        self.lbl_vsf.config(fg=vsf_fg)

    def toggle_crf(self) -> None:
        if self.crf_enabled.get():
            if not self.crf_var.get().strip():
                self.crf_var.set('20')
            self.crf_entry.config(state='normal')
        else:
            self.crf_var.set('')
            self.crf_entry.config(state='disabled')

    def log_msg(self, msg: str) -> None:
        self.log.insert(tk.END, msg + '\n')
        self.log.see(tk.END)

    def update_log(self) -> None:
        """主线程定时从日志队列消费并刷新文本区。"""
        try:
            while True:
                self.log_msg(self.queue.get_nowait())
        except queue.Empty:
            pass
        self.root.after(100, self.update_log)

    # ==================== 参数校验与后台任务执行 ====================

    def validate_inputs(self) -> bool:
        """校验文件路径、时间区间与编码参数合法性。"""
        if not self.input_var.get() or not os.path.isfile(self.input_var.get()):
            messagebox.showerror('错误', '请选择有效的输入视频文件')
            return False

        use_avs = self.use_avs.get()
        auto_avs = self.auto_avs.get()

        if use_avs:
            if not check_avs_available():
                messagebox.showerror('AVS 依赖错误', '未找到 AviSynth 环境，无法使用 AVS 模式。')
                return False

            if auto_avs:
                if not self.raw_video_var.get() or not os.path.isfile(self.raw_video_var.get()):
                    messagebox.showerror('错误', '自动生成 AVS 模式需要有效的 RAW 视频文件')
                    return False
                if not self.subtitle_var.get() or not os.path.isfile(self.subtitle_var.get()):
                    messagebox.showerror('错误', '自动生成 AVS 模式需要有效的字幕文件')
                    return False
                vsf_path = self.vsfiltermod_var.get().strip() or None
                if not find_vsfiltermod_dll(vsf_path):
                    messagebox.showerror(
                        '缺少 VSFilterMod',
                        '未找到 VSFilterMod.dll。\n请在“VSFilterMod”栏选择 DLL 文件，或将其放到 runtime / 插件目录。',
                    )
                    return False
            elif not self.avs_var.get() or not os.path.isfile(self.avs_var.get()):
                messagebox.showerror('错误', '请选择有效的 AVS 脚本文件')
                return False
        else:
            if not self.raw_video_var.get() or not os.path.isfile(self.raw_video_var.get()):
                messagebox.showerror('错误', '请选择有效的 RAW 视频文件')
                return False
            if not self.subtitle_var.get() or not os.path.isfile(self.subtitle_var.get()):
                messagebox.showerror('错误', '请选择有效的字幕文件')
                return False

        try:
            t_start = parse_time(self.start_var.get())
            t_end = parse_time(self.end_var.get())
        except Exception:
            messagebox.showerror('错误', '时间格式错误，请使用 纯秒数 或 hh:mm:ss.ms')
            return False

        if t_start < 0 or t_end <= t_start:
            messagebox.showerror('错误', '结束时间必须大于起始时间，且起始时间不能为负数')
            return False

        try:
            gop_val = int(self.gop_var.get().strip() or '0')
            if gop_val < 0:
                raise ValueError
        except ValueError:
            messagebox.showerror('错误', 'GOP 必须为大于等于 0 的整数')
            return False

        if self.crf_enabled.get():
            try:
                crf_num = float(self.crf_var.get().strip())
                if not (0 <= crf_num <= 51):
                    raise ValueError
            except ValueError:
                messagebox.showerror('错误', 'CRF 必须为 0 ~ 51 之间的数值')
                return False

        if not self.output_var.get().strip():
            messagebox.showerror('错误', '请指定输出文件路径')
            return False

        if os.path.abspath(self.output_var.get()) == os.path.abspath(self.input_var.get()):
            messagebox.showerror('错误', '输出文件路径不能与输入视频相同')
            return False

        return True

    def _set_active_process(self, proc: subprocess.Popen[str] | None) -> None:
        with self.process_lock:
            self.active_process = proc

    def cancel_processing(self) -> None:
        """发出取消信号并终止当前活动的 ffmpeg 子进程。"""
        if not self.running:
            return
        self.cancel_event.set()
        self.btn_cancel.config(state='disabled', text='正在取消...')
        self.queue.put('正在取消任务并终止 FFmpeg...')
        with self.process_lock:
            proc = self.active_process
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass

    def start_processing(self) -> None:
        """校验输入并在后台线程启动区段替换任务。"""
        if not self.validate_inputs():
            return
        if self.running:
            messagebox.showwarning('提示', '任务正在运行中...')
            return

        # 在主线程一次性快照所有 Tk 变量，避免子线程直接访问 Tkinter 状态
        params = {
            'input_video': self.input_var.get().strip(),
            'start_str': self.start_var.get().strip(),
            'end_str': self.end_var.get().strip(),
            'avs_script': self.avs_var.get().strip(),
            'output_video': self.output_var.get().strip(),
            'encoder': None if self.encoder_var.get() == '自动' else self.encoder_var.get(),
            'preset': self.preset_var.get(),
            'crf': self.crf_var.get().strip() if self.crf_enabled.get() else None,
            'extra_enc_params': self.extra_var.get(),
            'use_avs': self.use_avs.get(),
            'auto_avs': self.auto_avs.get(),
            'raw_video': self.raw_video_var.get().strip(),
            'subtitle_file': self.subtitle_var.get().strip(),
            'gop': int(self.gop_var.get().strip() or '0'),
            'vsfiltermod_path': self.vsfiltermod_var.get().strip() or None,
        }

        self.running = True
        self.cancel_event.clear()
        self.btn_start.config(state='disabled', text='处理中...')
        self.btn_cancel.config(state='normal', text='取消处理')
        self.queue.put('========== 开始处理 ==========')

        threading.Thread(target=self.process_thread, args=(params,), daemon=True).start()

    def process_thread(self, params: dict[str, Any]) -> None:
        """后台工作线程：重定向 stdout 到日志队列并执行 patch_video。"""
        original_stdout = sys.stdout
        status = 'success'
        err_msg = ''

        try:
            sys.stdout = QueueWriter(self.queue, original_stdout)  # type: ignore[assignment]

            use_avs_mode = params['use_avs']
            auto_avs_mode = params['auto_avs']
            need_raw_sub = (use_avs_mode and auto_avs_mode) or not use_avs_mode

            patch_video(
                params['input_video'],
                params['start_str'],
                params['end_str'],
                params['avs_script'],
                params['output_video'],
                encoder=params['encoder'],
                preset=params['preset'],
                crf=params['crf'],
                extra_enc_params=params['extra_enc_params'],
                use_avs=use_avs_mode,
                raw_video=params['raw_video'] if need_raw_sub else None,
                subtitle_file=params['subtitle_file'] if need_raw_sub else None,
                gop=params['gop'],
                auto_avs=auto_avs_mode,
                vsfiltermod_path=params['vsfiltermod_path'],
                cancel_event=self.cancel_event,
                process_callback=self._set_active_process,
            )
            self.queue.put('========== 处理成功结束 ==========')
        except PatchCancelledError as exc:
            status = 'cancelled'
            err_msg = str(exc)
            self.queue.put(f'========== {err_msg} ==========')
        except (Exception, SystemExit) as exc:
            status = 'error'
            err_msg = str(exc)
            self.queue.put(f'发生错误: {err_msg}')
        finally:
            sys.stdout = original_stdout
            self.root.after(0, lambda: self._on_processing_done(status, err_msg))

    def _on_processing_done(self, status: str, err_msg: str) -> None:
        """主线程收尾回调：恢复按钮状态并根据执行结果弹窗提示。"""
        self.running = False
        self.btn_start.config(state='normal', text='开始处理')
        self.btn_cancel.config(state='disabled', text='取消处理')

        if status == 'success':
            messagebox.showinfo('提示', '处理完毕！')
        elif status == 'cancelled':
            messagebox.showinfo('已取消', '任务已取消。')
        else:
            messagebox.showerror('处理失败', f'处理过程中发生错误：\n{err_msg}')

    def on_close(self) -> None:
        """窗口关闭前确认并终止活动任务，避免残留 ffmpeg 子进程。"""
        if self.running:
            if not messagebox.askyesno('确认退出', '当前仍有处理任务在运行，确定要终止并退出吗？'):
                return
            self.cancel_processing()
        self.root.destroy()


if __name__ == '__main__':
    root = tk.Tk()
    App(root)
    root.mainloop()
