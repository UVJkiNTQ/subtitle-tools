#!/usr/bin/env python3
"""
hardsub_patch_avs.py - 视频区段替换核心模块（AVS / RAW+字幕）

核心设计（避免接缝卡顿与音画不同步）：
- 音频不拆分：替换区间与原片等长，整条原音频直接 copy 封装，接缝零偏移
- 纯视频分段：三段均为纯视频流，拼接后再与原音频一步封装
- 参数对齐原片：保留精确分数帧率（CFR）、精确分段帧数、profile/level/pix_fmt/色彩/SAR
- 高速单次探测：通过 ffprobe packet 元数据单次扫描获取关键帧与精确帧数，并自动检测 GOP
- 自动 AVS 渲染：显式加载 LSMASHSource + VSFilterMod（TextSubMod），通过 Trim 随机寻址提速
- 绿色便携兼容：优先加载程序自带 runtime/ 中的 FFmpeg、AviSynth.dll 与插件，兼容中文路径
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from fractions import Fraction
from typing import Any, Callable


class PatchCancelledError(RuntimeError):
    """用户主动取消处理任务时抛出。"""


# ==================== 运行环境与绿色便携目录探测 ====================


def _subprocess_creationflags() -> int:
    """返回 Windows 下隐藏子进程控制台窗口的创建标志。"""
    if os.name == 'nt':
        return getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    return 0


def get_app_dirs() -> list[str]:
    """返回程序根目录列表（PyInstaller 打包后的 exe 目录优先于源码目录）。"""
    dirs: list[str] = []
    if getattr(sys, 'frozen', False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        dirs.append(exe_dir)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    if script_dir not in dirs:
        dirs.append(script_dir)
    return dirs


def get_portable_runtime_dirs() -> list[str]:
    """返回程序自带的绿色便携运行目录列表（按优先级排序，仅包含已存在目录）。"""
    dirs: list[str] = []
    for base in get_app_dirs():
        for sub in ('runtime', 'bin', 'plugins64+', 'plugins64', 'plugins', ''):
            path = os.path.join(base, sub) if sub else base
            if os.path.isdir(path) and path not in dirs:
                dirs.append(path)
    return dirs


def is_portable_path(path: str | None) -> bool:
    """判断给定路径是否位于程序自带目录内（用于区分内置绿色版与系统环境）。"""
    if not path:
        return False
    abs_path = os.path.normcase(os.path.abspath(path))
    for base in get_app_dirs():
        abs_base = os.path.normcase(os.path.abspath(base))
        if abs_path == abs_base or abs_path.startswith(abs_base + os.sep):
            return True
    return False


def find_ffmpeg_tool(tool_name: str) -> str | None:
    """查找 ffmpeg/ffprobe 可执行文件（优先绿色 runtime 目录，回退系统 PATH）。"""
    exe_name = f'{tool_name}.exe' if os.name == 'nt' else tool_name
    for directory in get_portable_runtime_dirs():
        candidate = os.path.join(directory, exe_name)
        if os.path.isfile(candidate):
            return candidate
    return shutil.which(tool_name)


def find_avisynth_dll() -> str | None:
    """查找 AviSynth.dll（优先绿色 runtime 目录，回退 Windows System32）。"""
    for directory in get_portable_runtime_dirs():
        for name in ('AviSynth.dll', 'avisynth.dll'):
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate):
                return candidate
    if os.name == 'nt':
        sys_dll = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32', 'AviSynth.dll')
        if os.path.isfile(sys_dll):
            return sys_dll
    return None


def check_avs_available() -> bool:
    """检查 AviSynth(+) 运行环境是否可用（支持绿色 runtime/AviSynth.dll 与系统安装版）。"""
    if os.name != 'nt':
        return False

    # 1. 优先验证绿色内置 AviSynth.dll
    avs_dll = find_avisynth_dll()
    if avs_dll and is_portable_path(avs_dll):
        dll_dir = os.path.dirname(avs_dll)
        dir_handle = None
        try:
            if hasattr(os, 'add_dll_directory'):
                dir_handle = os.add_dll_directory(dll_dir)
            ctypes.windll.LoadLibrary(avs_dll)
            return True
        except OSError:
            pass
        finally:
            if dir_handle is not None:
                try:
                    dir_handle.close()
                except Exception:
                    pass

    # 2. 尝试加载系统 AviSynth.dll
    try:
        ctypes.windll.LoadLibrary('avisynth')
        return True
    except OSError:
        pass

    # 3. 检查 Windows 注册表兜底
    try:
        import winreg

        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for subkey in (r'SOFTWARE\AviSynth', r'SOFTWARE\WOW6432Node\AviSynth'):
                try:
                    with winreg.OpenKey(root, subkey):
                        return True
                except OSError:
                    continue
    except ImportError:
        pass

    return False


def _build_subprocess_env() -> dict[str, str]:
    """将内置绿色 runtime 目录前置到子进程 PATH，确保优先加载绿色 AviSynth.dll 与运行库。"""
    env = os.environ.copy()
    portable_dirs = get_portable_runtime_dirs()
    if portable_dirs:
        old_path = env.get('PATH', '')
        env['PATH'] = os.pathsep.join(portable_dirs + ([old_path] if old_path else []))
    return env


def check_ffmpeg() -> tuple[str, str]:
    """检查 ffmpeg 与 ffprobe 是否可用，返回两者的可执行文件路径。"""
    ffmpeg_path = find_ffmpeg_tool('ffmpeg')
    ffprobe_path = find_ffmpeg_tool('ffprobe')
    missing = [name for name, path in (('ffmpeg', ffmpeg_path), ('ffprobe', ffprobe_path)) if not path]
    if missing:
        raise RuntimeError(
            f"未找到 {' / '.join(missing)}，请将其放入程序 runtime 目录或加入系统 PATH。"
        )
    assert ffmpeg_path and ffprobe_path
    return ffmpeg_path, ffprobe_path


# ==================== 时间、帧率与视频元信息解析 ====================


def parse_time(value: int | float | str) -> float:
    """
    解析时间输入为秒数。

    支持格式：纯秒数（如 83.5）、mm:ss.ms（如 01:23.5）、hh:mm:ss.ms（如 0:01:23.50）。
    """
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        raise ValueError('时间不能为空')
    if ':' in text:
        parts = text.split(':')
        if len(parts) == 3:
            hours, minutes, seconds = parts
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        if len(parts) == 2:
            minutes, seconds = parts
            return int(minutes) * 60 + float(seconds)
        raise ValueError(f'无法解析时间格式：{text}')
    return float(text)


def parse_fps_fraction(rate: str | None) -> Fraction | None:
    """将 '24000/1001' 等帧率字符串解析为 Fraction，无效时返回 None。"""
    if not rate or rate in ('0/0', 'N/A'):
        return None
    try:
        frac = Fraction(rate)
        return frac if frac > 0 else None
    except (ValueError, ZeroDivisionError):
        try:
            val = float(rate)
            return Fraction(val).limit_denominator(100000) if val > 0 else None
        except (ValueError, TypeError):
            return None


def run_ffprobe_json(args: list[str]) -> dict[str, Any]:
    """执行 ffprobe 并解析 JSON 输出（强制 UTF-8，隐藏控制台黑框）。"""
    ffprobe_bin = find_ffmpeg_tool('ffprobe') or 'ffprobe'
    cmd = [ffprobe_bin, '-v', 'error'] + args
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
        env=_build_subprocess_env(),
        creationflags=_subprocess_creationflags(),
    )
    if result.returncode != 0:
        raise RuntimeError(f'ffprobe 失败：{result.stderr.strip()}')
    return json.loads(result.stdout)


def probe_video(path: str) -> tuple[dict[str, Any], float]:
    """
    单次 ffprobe 读取视频流参数、容器时长、估算码率与音频流数量。

    Returns:
        (stream, duration): 附带 _fps / _fps_frac / _fps_str / _duration / _audio_count 的视频流字典与时长（秒）。
    """
    info = run_ffprobe_json([
        '-hide_banner',
        '-show_entries',
        'stream=index,codec_type,codec_name,width,height,pix_fmt,bit_rate,profile,level,'
        'r_frame_rate,avg_frame_rate,time_base,sample_aspect_ratio,'
        'color_range,color_space,color_transfer,color_primaries,nb_frames',
        '-show_entries', 'format=duration,bit_rate',
        '-of', 'json',
        path,
    ])
    streams = info.get('streams', [])
    video_streams = [s for s in streams if s.get('codec_type') == 'video']
    if not video_streams:
        raise RuntimeError(f'输入文件中未找到视频流：{path}')

    stream = dict(video_streams[0])
    audio_streams = [s for s in streams if s.get('codec_type') == 'audio']
    fmt = info.get('format', {})
    duration_raw = fmt.get('duration') or stream.get('duration')
    if duration_raw is None:
        raise RuntimeError(f'无法获取视频时长：{path}')
    duration = float(duration_raw)

    # 优先保留精确分数帧率（如 24000/1001），避免转小数后产生长视频时间漂移
    fps_frac = parse_fps_fraction(stream.get('avg_frame_rate')) or parse_fps_fraction(stream.get('r_frame_rate'))
    r_frac = parse_fps_fraction(stream.get('r_frame_rate'))
    if fps_frac and r_frac and abs(float(fps_frac) - float(r_frac)) < 0.01:
        fps_frac = r_frac
    if fps_frac is None:
        fps_frac = Fraction(25, 1)

    # MKV 等容器常缺少单流 bit_rate，尝试用容器总码率扣除音频码率估算
    if not stream.get('bit_rate') or not str(stream.get('bit_rate')).isdigit():
        fmt_br = fmt.get('bit_rate')
        if fmt_br and str(fmt_br).isdigit() and int(fmt_br) > 0:
            audio_br = sum(
                int(a['bit_rate']) for a in audio_streams
                if a.get('bit_rate') and str(a['bit_rate']).isdigit()
            )
            stream['_estimated_bit_rate'] = str(max(100_000, int(fmt_br) - audio_br))

    stream['_fps'] = float(fps_frac)
    stream['_fps_frac'] = fps_frac
    stream['_fps_str'] = f'{fps_frac.numerator}/{fps_frac.denominator}'
    stream['_duration'] = duration
    stream['_audio_count'] = len(audio_streams)
    return stream, duration


def probe_video_packets(path: str) -> tuple[list[float], list[float]]:
    """
    仅读取视频流 packet 元数据（不解码视频帧），一次性获取全部帧 PTS 与关键帧 PTS。

    Returns:
        (all_pts, kf_times): 升序排列的全部视频帧时间戳列表与关键帧时间戳列表（秒）。
    """
    data = run_ffprobe_json([
        '-select_streams', 'v:0',
        '-show_entries', 'packet=pts_time,dts_time,flags',
        '-of', 'json',
        path,
    ])
    all_pts: list[float] = []
    kf_times: list[float] = []
    for pkt in data.get('packets', []):
        t_raw = pkt.get('pts_time') or pkt.get('dts_time')
        if t_raw is None or t_raw == 'N/A':
            continue
        try:
            ts = float(t_raw)
        except (ValueError, TypeError):
            continue
        all_pts.append(ts)
        if 'K' in (pkt.get('flags') or ''):
            kf_times.append(ts)
    all_pts.sort()
    kf_times.sort()
    return all_pts, kf_times


def detect_gop_size(kf_times: list[float], fps: float) -> int:
    """
    根据关键帧时间间隔估算固定 GOP 大小（帧数）。

    若关键帧数量不足或间隔波动超过 50%（动态场景切换 GOP），返回 0。
    """
    if not kf_times or len(kf_times) < 3:
        return 0

    intervals = [
        kf_times[i] - kf_times[i - 1]
        for i in range(1, min(len(kf_times), 11))
        if kf_times[i] - kf_times[i - 1] > 0
    ]
    if not intervals:
        return 0

    min_interval = min(intervals)
    max_interval = max(intervals)
    if min_interval > 0 and max_interval > min_interval * 1.5:
        return 0

    avg_interval = sum(intervals) / len(intervals)
    return max(1, round(avg_interval * fps))


def find_keyframe_bounds(kf_times: list[float], start: float, end: float, duration: float) -> tuple[float, float]:
    """将目标替换区间向外对齐到最近关键帧：seek_start <= start 且 seek_end >= end。"""
    seek_start = 0.0
    for ts in kf_times:
        if ts <= start + 1e-6:
            seek_start = ts
        else:
            break

    seek_end = duration
    for ts in reversed(kf_times):
        if ts >= end - 1e-6:
            seek_end = ts
        else:
            break

    if seek_start >= seek_end:
        print('警告：关键帧间距过大，将重压整个视频。')
        return 0.0, duration
    return seek_start, seek_end


def _compute_segment_frames(
    all_pts: list[float],
    seek_start: float,
    seek_end: float,
    duration: float,
    fps: float,
) -> tuple[int, int, int, int]:
    """
    计算 Part1 / Part2 / Part3 的精确帧数与总帧数。

    优先基于实际 packet PTS 计数，避免 23.976fps 下浮点估算出现 ±1 帧接缝偏差；
    无 packet 数据时回退为 duration * fps 估算。
    """
    eps = 0.5 / max(fps, 1.0)
    has_part1 = seek_start > eps
    has_part3 = seek_end < duration - max(0.01, eps)

    if all_pts:
        total_frames = len(all_pts)
        part1_frames = sum(1 for ts in all_pts if ts < seek_start - eps) if has_part1 else 0
        part3_frames = sum(1 for ts in all_pts if ts >= seek_end - eps) if has_part3 else 0
        part2_frames = max(1, total_frames - part1_frames - part3_frames)
        return part1_frames, part2_frames, part3_frames, total_frames

    total_frames = max(1, round(duration * fps))
    part1_frames = round(seek_start * fps) if has_part1 else 0
    part3_frames = round((duration - seek_end) * fps) if has_part3 else 0
    part2_frames = max(1, total_frames - part1_frames - part3_frames)
    return part1_frames, part2_frames, part3_frames, total_frames


# ==================== 编码参数构造 ====================


_H264_PROFILE = {
    'constrained baseline': 'baseline',
    'baseline': 'baseline',
    'main': 'main',
    'high': 'high',
    'high 10': 'high10',
    'high 4:2:2': 'high422',
    'high 4:4:4 predictive': 'high444',
}
_HEVC_PROFILE = {
    'main': 'main',
    'main 10': 'main10',
    'main still picture': 'mainstillpicture',
}


def _level_str(level: Any) -> str | None:
    """将 ffprobe 返回的整型 level（如 41）转换为编码器参数字符串（如 '4.1'）。"""
    try:
        lv = int(level)
    except (ValueError, TypeError):
        return None
    if lv <= 0:
        return None
    return f'{lv // 10}.{lv % 10}'


def _color_args(stream: dict[str, Any]) -> list[str]:
    """提取原片色彩空间、传输特性、色域与色阶范围参数。"""
    args: list[str] = []
    mapping = {
        'color_space': '-colorspace',
        'color_transfer': '-color_trc',
        'color_primaries': '-color_primaries',
        'color_range': '-color_range',
    }
    for key, flag in mapping.items():
        val = stream.get(key)
        if val and val not in ('unknown', 'reserved', 'unspecified'):
            args += [flag, val]
    return args


def _sar(stream: dict[str, Any]) -> str | None:
    """提取非 1:1 的采样宽高比（SAR）。"""
    sar = stream.get('sample_aspect_ratio')
    if not sar or sar in ('0:1', 'N/A', '1:1'):
        return None
    return sar


def _encoder_codec_name(encoder: str | None) -> str:
    """将编码器名称归一化为对应编码格式名（用于与原片编码一致性校验）。"""
    enc = (encoder or '').lower()
    if '265' in enc or 'hevc' in enc:
        return 'hevc'
    if '264' in enc:
        return 'h264'
    if 'mpeg4' in enc:
        return 'mpeg4'
    if 'av1' in enc:
        return 'av1'
    return enc


def _merge_codec_params(args: list[str], flag: str, extra_tokens: list[str]) -> list[str]:
    """当额外参数包含相同 -x264-params / -x265-params 时，以 ':' 合并避免互相覆盖。"""
    if flag not in args or flag not in extra_tokens:
        return args + extra_tokens

    idx_a = args.index(flag)
    idx_e = extra_tokens.index(flag)
    if idx_a + 1 < len(args) and idx_e + 1 < len(extra_tokens):
        merged = list(args)
        merged[idx_a + 1] = f'{args[idx_a + 1]}:{extra_tokens[idx_e + 1]}'
        rest = extra_tokens[:idx_e] + extra_tokens[idx_e + 2:]
        return merged + rest
    return args + extra_tokens


def build_encoder_args(
    stream: dict[str, Any],
    encoder: str | None = None,
    preset: str = 'fast',
    crf: str | float | None = None,
    extra: str | None = None,
    gop: int = 0,
) -> list[str]:
    """构造与原片尽量对齐的视频编码参数列表（用于接缝处 SPS/PPS 与帧率一致）。"""
    if crf in (None, ''):
        crf = None
    codec = (stream.get('codec_name') or 'h264').lower()
    if not encoder:
        encoder = {'h264': 'libx264', 'hevc': 'libx265', 'mpeg4': 'mpeg4'}.get(codec, 'libx264')
    enc_lower = encoder.lower()

    extra_tokens = shlex.split(extra.strip()) if extra and extra.strip() else []
    args = [encoder]

    # 仅支持 -preset 的编码器传入预设（mpeg4 等不支持）
    if preset and any(tag in enc_lower for tag in ('264', '265', 'hevc', 'nvenc')):
        args += ['-preset', preset]

    if crf is not None:
        args += ['-crf', str(crf)]
    else:
        bit_rate = stream.get('bit_rate') or stream.get('_estimated_bit_rate')
        if bit_rate and str(bit_rate).isdigit() and int(bit_rate) > 0:
            if not stream.get('bit_rate') and stream.get('_estimated_bit_rate'):
                print(
                    f'提示：视频流未记录码率，使用估算码率 {int(bit_rate) // 1000} kbps'
                    '（建议手动开启 CRF 以获得更稳定画质）'
                )
            args += ['-b:v', str(bit_rate)]
        elif '-b:v' not in extra_tokens and '-crf' not in extra_tokens:
            raise ValueError(
                '未指定 CRF 且无法从输入视频获取有效码率。'
                '请使用 --crf 指定 CRF 值，或通过 --extra 手动传入 -b:v 码率参数。'
            )

    pix_fmt = stream.get('pix_fmt')
    if pix_fmt:
        args += ['-pix_fmt', pix_fmt]

    profile = (stream.get('profile') or '').lower()
    if '264' in enc_lower and profile in _H264_PROFILE:
        args += ['-profile:v', _H264_PROFILE[profile]]
    elif ('265' in enc_lower or 'hevc' in enc_lower) and profile in _HEVC_PROFILE:
        args += ['-profile:v', _HEVC_PROFILE[profile]]

    level = _level_str(stream.get('level'))
    if level and ('264' in enc_lower or '265' in enc_lower or 'hevc' in enc_lower):
        args += ['-level', level]

    args += _color_args(stream)

    # 强制 CFR 精确分数帧率，输出总帧数由调用方通过 -frames:v 精确控制
    fps_str = stream.get('_fps_str')
    if fps_str:
        args += ['-r', fps_str, '-fps_mode', 'cfr']
    elif stream.get('_fps'):
        args += ['-r', f"{stream['_fps']:.6f}", '-fps_mode', 'cfr']

    # 固定 closed-GOP 与 IDR 间隔，提升拼接点兼容性
    if gop > 0:
        if '265' in enc_lower or 'hevc' in enc_lower:
            args += ['-x265-params', f'keyint={gop}:min-keyint={gop}:scenecut=0:open-gop=0']
        elif '264' in enc_lower:
            args += [
                '-g', str(gop), '-keyint_min', str(gop), '-sc_threshold', '0',
                '-x264-params', f'keyint={gop}:min-keyint={gop}:scenecut=0:open-gop=0',
            ]

    if extra_tokens:
        if '265' in enc_lower or 'hevc' in enc_lower:
            args = _merge_codec_params(args, '-x265-params', extra_tokens)
        elif '264' in enc_lower:
            args = _merge_codec_params(args, '-x264-params', extra_tokens)
        else:
            args += extra_tokens

    return args


def _movflags(output_video: str) -> list[str]:
    """MP4/MOV 输出自动启用 faststart，将 moov 移至文件头。"""
    return ['-movflags', '+faststart'] if output_video.lower().endswith(('.mp4', '.m4v', '.mov')) else []


# ==================== AviSynth+ 插件探测与自动 AVS 生成 ====================


def get_avisynth_plugin_dirs() -> list[str]:
    """
    收集 AviSynth+ 插件搜索目录（按优先级排序）：
    1. 程序内置绿色目录（runtime / bin / plugins64+ / plugins64 / plugins / 根目录）
    2. Windows 注册表配置的 AviSynth+ 插件目录
    3. 常见 AviSynth+ 与 Aegisub csri 安装目录
    """
    dirs = list(get_portable_runtime_dirs())

    if os.name == 'nt':
        # 2. 注册表读取 AviSynth+ 插件目录
        try:
            import winreg

            for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                for subkey in (r'SOFTWARE\AviSynth', r'SOFTWARE\WOW6432Node\AviSynth'):
                    try:
                        with winreg.OpenKey(root, subkey) as key:
                            for val_name in ('plugindir+', 'plugindir2_5', ''):
                                try:
                                    val, _ = winreg.QueryValueEx(key, val_name)
                                except OSError:
                                    continue
                                if not val or not os.path.isdir(val):
                                    continue
                                if val_name == '':
                                    for sub in ('plugins64+', 'plugins64', 'plugins+', 'plugins'):
                                        cand = os.path.join(val, sub)
                                        if os.path.isdir(cand) and cand not in dirs:
                                            dirs.append(cand)
                                elif val not in dirs:
                                    dirs.append(val)
                    except OSError:
                        continue
        except ImportError:
            pass

        # 3. 常见安装路径兜底
        for base in (
            r'C:\Program Files (x86)\AviSynth+',
            r'C:\Program Files\AviSynth+',
            r'C:\Program Files\Aegisub\csri',
            r'C:\Program Files (x86)\Aegisub\csri',
        ):
            if not os.path.isdir(base):
                continue
            if base.endswith('csri'):
                if base not in dirs:
                    dirs.append(base)
            else:
                for sub in ('plugins64+', 'plugins64', 'plugins+', 'plugins'):
                    cand = os.path.join(base, sub)
                    if os.path.isdir(cand) and cand not in dirs:
                        dirs.append(cand)

    return dirs


def find_avisynth_plugin(dll_names: str | tuple[str, ...], custom_path: str | None = None) -> str | None:
    """查找 AviSynth 插件 DLL，支持通过 custom_path 指定文件路径或所在目录。"""
    if isinstance(dll_names, str):
        dll_names = (dll_names,)

    if custom_path:
        custom_path = os.path.abspath(custom_path)
        if os.path.isfile(custom_path):
            return custom_path
        if os.path.isdir(custom_path):
            for name in dll_names:
                cand = os.path.join(custom_path, name)
                if os.path.isfile(cand):
                    return cand

    for directory in get_avisynth_plugin_dirs():
        for name in dll_names:
            cand = os.path.join(directory, name)
            if os.path.isfile(cand):
                return cand
    return None


def find_vsfiltermod_dll(custom_path: str | None = None) -> str | None:
    """查找 VSFilterMod.dll（优先内置绿色目录与 64 位插件目录）。"""
    return find_avisynth_plugin(
        ('VSFilterMod.dll', 'VSFilterMod64.dll', 'vsfiltermod.dll'),
        custom_path=custom_path,
    )


def find_lsmash_dll(custom_path: str | None = None) -> str | None:
    """查找 LSMASHSource.dll（优先内置绿色目录与 64 位插件目录）。"""
    return find_avisynth_plugin(
        ('LSMASHSource.dll', 'lsmashsource.dll'),
        custom_path=custom_path,
    )


def _avs_quote(path: str) -> str:
    """安全包裹 AviSynth 字符串字面量（保留原反斜杠不破坏 UNC 路径；含双引号时用三引号）。"""
    if '"' in path:
        return f'"""{path}"""'
    return f'"{path}"'


def _stage_ascii_file(
    src_path: str,
    target_name: str,
    temp_workdir: str | None = None,
    prefer_hardlink: bool = False,
) -> str:
    """
    将含非 ASCII 字符的文件路径转为纯 ASCII 路径，规避部分 AviSynth 插件的宽字符兼容问题：
    1. 路径本身为纯 ASCII 时直接返回；
    2. 尝试获取 Windows 8.3 短路径；
    3. 若仍含非 ASCII，在 temp_workdir 中创建硬链接（优先）或副本。
    """
    abs_path = os.path.abspath(src_path)
    if abs_path.isascii():
        return abs_path

    if os.name == 'nt':
        buf = ctypes.create_unicode_buffer(4096)
        n = ctypes.windll.kernel32.GetShortPathNameW(abs_path, buf, 4096)
        if n > 0 and buf.value.isascii() and os.path.isfile(buf.value):
            return buf.value

    if temp_workdir:
        dst = os.path.join(temp_workdir, target_name)
        if not os.path.exists(dst):
            linked = False
            if prefer_hardlink:
                try:
                    os.link(abs_path, dst)
                    linked = True
                except OSError:
                    linked = False
            if not linked:
                shutil.copy2(abs_path, dst)
        if dst.isascii():
            return dst
        if os.name == 'nt':
            buf = ctypes.create_unicode_buffer(4096)
            n = ctypes.windll.kernel32.GetShortPathNameW(dst, buf, 4096)
            if n > 0 and buf.value.isascii():
                return buf.value

    return abs_path


def _prepare_ascii_subtitle(subtitle_file: str, temp_workdir: str | None = None) -> str:
    """VSFilterMod.dll 的 TextSubMod() 无法直接打开非 ASCII 路径，自动转接为纯 ASCII 路径。"""
    ext = os.path.splitext(os.path.abspath(subtitle_file))[1] or '.ass'
    return _stage_ascii_file(subtitle_file, f'sub_input{ext}', temp_workdir=temp_workdir, prefer_hardlink=False)


def _write_avs_text(output_path: str, content: str) -> None:
    """
    写入 AviSynth+ 脚本文件：
    - 纯 ASCII 内容使用 utf-8（无 BOM）；
    - Windows 下含非 ASCII 路径且可无损编码为系统 ANSI（mbcs）时优先使用 mbcs，
      以兼容 AviSynth+ LoadPlugin 的 ANSI 路径解析；否则回退 utf-8（无 BOM）。
    """
    if content.isascii():
        encoding = 'utf-8'
    elif os.name == 'nt':
        try:
            content.encode('mbcs', errors='strict')
            encoding = 'mbcs'
        except UnicodeEncodeError:
            encoding = 'utf-8'
    else:
        encoding = 'utf-8'

    with open(output_path, 'w', encoding=encoding) as f:
        f.write(content)


def generate_avs_script(
    raw_video: str,
    subtitle_file: str,
    output_path: str,
    stream_info: dict[str, Any] | None = None,
    vsfiltermod_path: str | None = None,
    lsmash_path: str | None = None,
    trim_start_frame: int | None = None,
    trim_frame_count: int | None = None,
    temp_workdir: str | None = None,
) -> str:
    """
    自动生成 AviSynth+ 脚本：显式加载 LSMASHSource + VSFilterMod 并通过 TextSubMod 渲染字幕。

    Args:
        raw_video: 无字幕源视频路径。
        subtitle_file: ASS/SSA/SRT/IDX/SUB 字幕文件路径。
        output_path: 生成的 .avs 脚本路径。
        stream_info: 原片视频流信息字典（用于写入注释与精确 CFR 帧率对齐）。
        vsfiltermod_path: 可选，手动指定 VSFilterMod.dll 路径。
        lsmash_path: 可选，手动指定 LSMASHSource.dll 路径。
        trim_start_frame: 可选，AVS 内部 Trim 起始帧号（置于 TextSubMod 之后实现按需随机寻址）。
        trim_frame_count: 可选，AVS 内部 Trim 帧数（负数写法避免 Trim(0, 0) 全片语义陷阱）。
        temp_workdir: 可选，临时目录，用于在路径含非 ASCII 字符时中转字幕或插件 DLL。

    Returns:
        生成的 AVS 脚本文件路径。
    """
    raw_video = os.path.abspath(raw_video)
    subtitle_for_avs = _prepare_ascii_subtitle(subtitle_file, temp_workdir=temp_workdir)

    vsf_dll = find_vsfiltermod_dll(vsfiltermod_path)
    if not vsf_dll:
        raise RuntimeError(
            '未找到 VSFilterMod.dll！请将其放入程序 runtime 目录或 AviSynth+ 插件目录，'
            '或通过 --vsfiltermod 手动指定 DLL 路径。'
        )

    lsmash_dll = find_lsmash_dll(lsmash_path)
    vsf_for_avs = _stage_ascii_file(vsf_dll, os.path.basename(vsf_dll), temp_workdir=temp_workdir, prefer_hardlink=True)
    lsmash_for_avs = (
        _stage_ascii_file(lsmash_dll, os.path.basename(lsmash_dll), temp_workdir=temp_workdir, prefer_hardlink=True)
        if lsmash_dll
        else None
    )

    lines = [
        '# 自动生成的 AviSynth+ 脚本',
        '# 使用 LSMASHSource 解码视频，显式加载 VSFilterMod (TextSubMod) 渲染字幕',
        '',
    ]

    if stream_info:
        fps_val = stream_info.get('_fps')
        fps_text = (
            f'{fps_val:.3f} fps ({stream_info.get("_fps_str", "")})'
            if isinstance(fps_val, (int, float))
            else 'N/A'
        )
        lines += [
            '# 原片视频信息:',
            f"# - 分辨率: {stream_info.get('width')}x{stream_info.get('height')}",
            f'# - 帧率: {fps_text}',
            f"# - 编码: {stream_info.get('codec_name', 'N/A')}",
            f"# - 像素格式: {stream_info.get('pix_fmt', 'N/A')}",
            '',
        ]

    lines.append('# 显式加载插件（脱离注册表自动加载，避免与普通 VSFilter.dll 冲突）')
    if lsmash_for_avs:
        lines.append(f'LoadPlugin({_avs_quote(lsmash_for_avs)})')
    lines.append(f'LoadPlugin({_avs_quote(vsf_for_avs)})')
    lines.append('')

    lines.append('# 加载源视频（LSMASHSource 会自动生成或复用 .lwi 索引）')
    fps_frac = stream_info.get('_fps_frac') if stream_info else None
    if fps_frac is not None:
        lines.append(
            f'video = LWLibavVideoSource({_avs_quote(raw_video)}, '
            f'fpsnum={fps_frac.numerator}, fpsden={fps_frac.denominator})'
        )
    else:
        lines.append(f'video = LWLibavVideoSource({_avs_quote(raw_video)})')
    lines.append('')

    sub_ext = os.path.splitext(subtitle_for_avs)[1].lower()
    sub_func = 'VobSub' if sub_ext in ('.idx', '.sub') else 'TextSubMod'
    lines.append('# 在 Trim 之前渲染字幕，确保字幕时间轴与原片完全一致')
    lines.append(f'video = {sub_func}(video, {_avs_quote(subtitle_for_avs)})')

    if trim_start_frame is not None and trim_frame_count is not None and trim_frame_count > 0:
        lines += [
            '',
            f'# 仅按需渲染目标替换区间（第 {trim_start_frame} 帧起共 {trim_frame_count} 帧，跳过前段解码）',
            f'video = Trim(video, {int(trim_start_frame)}, -{int(trim_frame_count)})',
        ]

    lines += ['', 'return video', '']

    _write_avs_text(output_path, '\n'.join(lines))

    print(f'已生成 AVS 脚本: {output_path}')
    print(f'  - VSFilterMod: {vsf_dll}')
    if lsmash_dll:
        print(f'  - LSMASHSource: {lsmash_dll}')
    if subtitle_for_avs != os.path.abspath(subtitle_file):
        print(f'  - 字幕路径含非 ASCII 字符，已自动转接为: {subtitle_for_avs}')
    return output_path


# ==================== FFmpeg 执行与分段替换主流程 ====================


def run_ffmpeg(
    cmd: list[str],
    description: str = '',
    cancel_event: Any = None,
    process_callback: Callable[[subprocess.Popen[str] | None], None] | None = None,
) -> None:
    """执行 ffmpeg 命令，实时输出日志（UTF-8 解码），支持绿色内置版与外部取消。"""
    if cancel_event and cancel_event.is_set():
        raise PatchCancelledError('任务已取消')

    if description:
        print(f'\n--- {description} ---')
    cmd = list(cmd)
    if cmd and cmd[0] == 'ffmpeg':
        cmd[0] = find_ffmpeg_tool('ffmpeg') or 'ffmpeg'
        if '-hide_banner' not in cmd[1:3]:
            cmd.insert(1, '-hide_banner')
    print(' '.join(cmd))

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding='utf-8',
        errors='replace',
        bufsize=1,
        env=_build_subprocess_env(),
        creationflags=_subprocess_creationflags(),
    )
    if process_callback:
        process_callback(process)

    try:
        assert process.stdout is not None
        for line in iter(process.stdout.readline, ''):
            if cancel_event and cancel_event.is_set():
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                raise PatchCancelledError('任务已取消')
            if line:
                print(line.rstrip('\n\r'))
        process.wait()
    finally:
        if process.stdout:
            process.stdout.close()
        if process_callback:
            process_callback(None)

    if cancel_event and cancel_event.is_set():
        raise PatchCancelledError('任务已取消')
    if process.returncode != 0:
        raise subprocess.CalledProcessError(process.returncode, cmd)
    print('完成。')


@contextmanager
def temp_dir():
    """创建并在退出时自动清理的临时工作目录上下文管理器。"""
    workdir = tempfile.mkdtemp(prefix='patch_avs_')
    try:
        yield workdir
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _escape_concat_path(path: str) -> str:
    """转义 ffmpeg concat demuxer 文件列表中的单引号。"""
    return path.replace("'", "'\\''")


def _escape_subtitles_filter_path(path: str) -> str:
    """转义 ffmpeg subtitles 滤镜参数中的路径特殊字符。"""
    escaped = os.path.abspath(path).replace('\\', '/')
    for ch in (':', "'", '[', ']', ',', ';'):
        escaped = escaped.replace(ch, f'\\{ch}')
    return escaped


def _build_part2_cmd(
    replace_src: str,
    stream: dict[str, Any],
    seek_start: float,
    seek_end: float,
    part2_frames: int,
    use_avs: bool,
    avs_pretrimmed: bool,
    subtitle_file: str | None,
    enc_args: list[str],
    output_path: str,
    output_format: str = 'mpegts',
) -> list[str]:
    """
    构造 Part2 重压命令：
    - auto_avs（avs_pretrimmed=True）：已在 AVS 内部 Trim，直接显式 `-f avisynth` 编码；
    - 手动 AVS：利用输入端 `-ss` 配合 `-f avisynth` 快速定位；
    - RAW+字幕（ffmpeg libass）：提前 2 秒 `-copyts -ss` 粗定位后再精确 `trim` 并归零 PTS。
    """
    fps_str = stream.get('_fps_str') or f"{stream.get('_fps') or 25.0:.6f}"
    sar = _sar(stream)

    if use_avs and avs_pretrimmed:
        vf_parts = [f'setsar={sar}'] if sar else []
        cmd = ['ffmpeg', '-y', '-f', 'avisynth', '-i', replace_src, '-an']
        if vf_parts:
            cmd += ['-vf', ','.join(vf_parts)]
    elif use_avs:
        vf_parts = [f'fps={fps_str}', 'setpts=PTS-STARTPTS']
        if sar:
            vf_parts.append(f'setsar={sar}')
        cmd = ['ffmpeg', '-y']
        if seek_start > 0.0:
            cmd += ['-ss', f'{seek_start:.6f}']
        cmd += ['-f', 'avisynth', '-i', replace_src, '-an', '-vf', ','.join(vf_parts)]
    else:
        assert subtitle_file is not None
        safe_sub = _escape_subtitles_filter_path(subtitle_file)
        vf_parts = [
            f"subtitles='{safe_sub}'",
            f'fps={fps_str}',
            f'trim=start={seek_start:.6f}:end={seek_end:.6f}',
            'setpts=PTS-STARTPTS',
        ]
        if sar:
            vf_parts.append(f'setsar={sar}')
        # 提前 2 秒 coarse seek，保证解码器拿到 seek_start 前的参考关键帧同时避免从 0 秒扫描
        pre_seek = max(0.0, seek_start - 2.0)
        cmd = ['ffmpeg', '-y']
        if pre_seek > 0.0:
            cmd += ['-copyts', '-ss', f'{pre_seek:.6f}']
        cmd += ['-i', replace_src, '-an', '-vf', ','.join(vf_parts)]

    cmd += ['-c:v'] + enc_args + ['-frames:v', str(part2_frames)]
    if output_format:
        cmd += ['-f', output_format]
    cmd.append(output_path)
    return cmd


def patch_video(
    input_video: str,
    start_str: str | float,
    end_str: str | float,
    avs_script: str,
    output_video: str,
    encoder: str | None = None,
    preset: str = 'fast',
    crf: str | float | None = None,
    extra_enc_params: str = '',
    use_avs: bool = True,
    raw_video: str | None = None,
    subtitle_file: str | None = None,
    gop: int = 0,
    auto_avs: bool = False,
    vsfiltermod_path: str | None = None,
    lsmash_path: str | None = None,
    cancel_event: Any = None,
    process_callback: Callable[[subprocess.Popen[str] | None], None] | None = None,
) -> None:
    """
    核心区段替换入口：仅重压对齐到关键帧的目标区间，其余视频段无损 copy，原音频整条复用。

    Args:
        input_video: 待局部替换的原已压制视频路径。
        start_str: 替换起始时间（秒或 hh:mm:ss.ms）。
        end_str: 替换结束时间（秒或 hh:mm:ss.ms）。
        avs_script: 手动 AVS 模式的 .avs 脚本路径（auto_avs 或 --no-avs 模式可为空）。
        output_video: 输出文件路径（推荐 .mkv）。
        encoder: 可选编码器名（None 表示按原片编码自动选择）。
        preset: 编码预设（默认 fast）。
        crf: 可选 CRF 值（None 表示跟随原片码率）。
        extra_enc_params: 额外编码器参数字符串。
        use_avs: True 使用 AviSynth 模式，False 使用 ffmpeg libass 模式。
        raw_video: 无字幕源片路径（auto_avs 或 use_avs=False 时必选）。
        subtitle_file: 字幕文件路径（auto_avs 或 use_avs=False 时必选）。
        gop: 固定 GOP 帧数（0 表示自动检测原片 GOP）。
        auto_avs: True 时自动生成显式加载 LSMASHSource + VSFilterMod 的临时 AVS 脚本。
        vsfiltermod_path: 可选，手动指定 VSFilterMod.dll 路径。
        lsmash_path: 可选，手动指定 LSMASHSource.dll 路径。
        cancel_event: 可选 threading.Event，用于中途取消任务。
        process_callback: 可选回调，用于向调用方暴露当前运行的 ffmpeg 子进程句柄。
    """
    check_ffmpeg()

    if not input_video or not os.path.isfile(input_video):
        raise FileNotFoundError(f'输入视频不存在：{input_video}')

    if auto_avs:
        if not raw_video or not os.path.isfile(raw_video):
            raise FileNotFoundError(f'auto_avs 模式需要有效的 RAW 视频文件：{raw_video}')
        if not subtitle_file or not os.path.isfile(subtitle_file):
            raise FileNotFoundError(f'auto_avs 模式需要有效的字幕文件：{subtitle_file}')
        if not find_vsfiltermod_dll(vsfiltermod_path):
            raise FileNotFoundError('未找到 VSFilterMod.dll，请放入 runtime / 插件目录或手动指定路径。')
    elif use_avs:
        if not avs_script or not os.path.isfile(avs_script):
            raise FileNotFoundError(f'AVS 脚本不存在：{avs_script}')
    else:
        if not raw_video or not os.path.isfile(raw_video):
            raise FileNotFoundError(f'RAW 视频文件不存在：{raw_video}')
        if not subtitle_file or not os.path.isfile(subtitle_file):
            raise FileNotFoundError(f'字幕文件不存在：{subtitle_file}')

    if not output_video:
        raise ValueError('未指定输出文件路径')

    out_dir = os.path.dirname(os.path.abspath(output_video))
    if out_dir and not os.path.exists(out_dir):
        os.makedirs(out_dir, exist_ok=True)

    print('分析视频...')
    stream, duration = probe_video(input_video)
    has_audio = stream.get('_audio_count', 0) > 0
    fps = stream['_fps']

    start = parse_time(start_str)
    end = parse_time(end_str)
    if start < 0 or end > duration + 0.05 or start >= end:
        raise ValueError(f'时间区间无效 ({start:.3f}s - {end:.3f}s，视频总长 {duration:.3f}s)')
    end = min(end, duration)

    if cancel_event and cancel_event.is_set():
        raise PatchCancelledError('任务已取消')

    print('扫描关键帧与帧索引...')
    all_pts, kf_times = probe_video_packets(input_video)
    if not kf_times:
        kf_times = [0.0]

    if gop == 0:
        detected_gop = detect_gop_size(kf_times, fps)
        if detected_gop > 0:
            gop = detected_gop
            print(f'自动检测 GOP: {gop} 帧 ({detected_gop / fps:.2f}秒)')
        else:
            print('提示：原片为动态 GOP（场景切换关键帧），重压段使用编码器默认 GOP')
    else:
        print(f'使用指定 GOP: {gop} 帧')

    seek_start, seek_end = find_keyframe_bounds(kf_times, start, end, duration)
    part1_frames, part2_frames, part3_frames, total_frames = _compute_segment_frames(
        all_pts, seek_start, seek_end, duration, fps
    )
    print(
        f'修正区间：{seek_start:.3f}s → {seek_end:.3f}s'
        f'（帧率 {stream["_fps_str"]} ≈ {fps:.3f}fps，总帧数 {total_frames}：'
        f'Part1={part1_frames}, Part2={part2_frames}, Part3={part3_frames}）'
    )

    enc_args = build_encoder_args(stream, encoder, preset, crf, extra_enc_params, gop)
    target_encoder = enc_args[0]

    with temp_dir() as tmp:
        avs_pretrimmed = False
        if auto_avs:
            assert raw_video and subtitle_file
            avs_path = os.path.join(tmp, 'auto_hardsub.avs')
            print('\n自动生成 AVS 脚本模式')
            print(f'  - 源视频: {raw_video}')
            print(f'  - 字幕: {subtitle_file}')
            generate_avs_script(
                raw_video,
                subtitle_file,
                avs_path,
                stream_info=stream,
                vsfiltermod_path=vsfiltermod_path,
                lsmash_path=lsmash_path,
                trim_start_frame=part1_frames,
                trim_frame_count=part2_frames,
                temp_workdir=tmp,
            )
            replace_src = avs_path
            use_avs = True
            avs_pretrimmed = True
        elif use_avs:
            replace_src = avs_script
        else:
            assert raw_video
            replace_src = raw_video

        _patch_fast(
            input_video=input_video,
            replace_src=replace_src,
            stream=stream,
            seek_start=seek_start,
            seek_end=seek_end,
            part1_frames=part1_frames,
            part2_frames=part2_frames,
            part3_frames=part3_frames,
            use_avs=use_avs,
            avs_pretrimmed=avs_pretrimmed,
            subtitle_file=subtitle_file,
            enc_args=enc_args,
            target_encoder=target_encoder,
            has_audio=has_audio,
            output_video=output_video,
            tmp=tmp,
            cancel_event=cancel_event,
            process_callback=process_callback,
        )

    print(f'✅ 全部处理完毕 → {output_video}')


def _patch_fast(
    input_video: str,
    replace_src: str,
    stream: dict[str, Any],
    seek_start: float,
    seek_end: float,
    part1_frames: int,
    part2_frames: int,
    part3_frames: int,
    use_avs: bool,
    avs_pretrimmed: bool,
    subtitle_file: str | None,
    enc_args: list[str],
    target_encoder: str,
    has_audio: bool,
    output_video: str,
    tmp: str,
    cancel_event: Any = None,
    process_callback: Callable[[subprocess.Popen[str] | None], None] | None = None,
) -> None:
    """执行三段纯视频切分/重压、concat 拼接并与整条原音频一步封装。"""
    segments: list[str] = []

    # 全片替换快捷路径：跳过 concat 拼接，直接重压后与原音频封装
    if part1_frames == 0 and part3_frames == 0:
        print('提示：替换区间覆盖全片，直接重压视频并与原音频封装。')
        full_v = os.path.join(tmp, 'full_video.ts')
        cmd_full = _build_part2_cmd(
            replace_src, stream, seek_start, seek_end, part2_frames,
            use_avs, avs_pretrimmed, subtitle_file, enc_args, full_v, output_format='mpegts',
        )
        run_ffmpeg(
            cmd_full,
            f'全片重压视频 ({part2_frames} 帧)',
            cancel_event=cancel_event,
            process_callback=process_callback,
        )
        run_ffmpeg(
            ['ffmpeg', '-y', '-i', full_v, '-i', input_video, '-map', '0:v:0']
            + (['-map', '1:a?'] if has_audio else [])
            + ['-c', 'copy']
            + _movflags(output_video)
            + [output_video],
            '封装整条原音频',
            cancel_event=cancel_event,
            process_callback=process_callback,
        )
        return

    if part1_frames > 0:
        part1 = os.path.join(tmp, 'part1.ts')
        run_ffmpeg(
            [
                'ffmpeg', '-y', '-i', input_video,
                '-an', '-map', '0:v:0', '-c:v', 'copy',
                '-frames:v', str(part1_frames),
                '-f', 'mpegts', part1,
            ],
            f'Part1 视频 (0 - {seek_start:.3f}s, {part1_frames} 帧)',
            cancel_event=cancel_event,
            process_callback=process_callback,
        )
        segments.append(part1)

    part2 = os.path.join(tmp, 'part2.ts')
    cmd_p2 = _build_part2_cmd(
        replace_src, stream, seek_start, seek_end, part2_frames,
        use_avs, avs_pretrimmed, subtitle_file, enc_args, part2, output_format='mpegts',
    )
    run_ffmpeg(
        cmd_p2,
        f'Part2 重压视频 ({seek_start:.3f}s - {seek_end:.3f}s, {part2_frames} 帧)',
        cancel_event=cancel_event,
        process_callback=process_callback,
    )
    segments.append(part2)

    if part3_frames > 0:
        part3 = os.path.join(tmp, 'part3.ts')
        run_ffmpeg(
            [
                'ffmpeg', '-y',
                '-ss', f'{seek_end:.6f}', '-i', input_video,
                '-an', '-map', '0:v:0', '-c:v', 'copy',
                '-f', 'mpegts', part3,
            ],
            f'Part3 视频 ({seek_end:.3f}s - 结尾, 约 {part3_frames} 帧)',
            cancel_event=cancel_event,
            process_callback=process_callback,
        )
        segments.append(part3)

    concat_list = os.path.join(tmp, 'list.txt')
    with open(concat_list, 'w', encoding='utf-8') as f:
        for seg in segments:
            f.write(f"file '{_escape_concat_path(seg)}'\n")

    run_ffmpeg(
        [
            'ffmpeg', '-y',
            '-f', 'concat', '-safe', '0', '-i', concat_list,
            '-i', input_video,
            '-map', '0:v:0',
        ]
        + (['-map', '1:a?'] if has_audio else [])
        + ['-c', 'copy']
        + _movflags(output_video)
        + [output_video],
        '拼接视频 + 封装整条原音频（一步完成）',
        cancel_event=cancel_event,
        process_callback=process_callback,
    )

    src_codec = (stream.get('codec_name') or '').lower()
    dst_codec = _encoder_codec_name(target_encoder)
    if output_video.lower().endswith('.mp4') and src_codec and dst_codec and src_codec != dst_codec:
        print(
            f'提示：原片编码 ({src_codec}) 与重压段编码 ({dst_codec}) 不同，'
            'MP4 接缝可能存在兼容性问题；建议改用 .mkv 输出或选择与原片相同的编码器。'
        )


# ==================== 命令行入口 ====================


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='视频区段替换工具（音频不拆分，接缝无延迟）',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
使用示例:

1. 自动生成 AVS 模式（推荐，自动加载 VSFilterMod）:
   python hardsub_patch_avs.py input.mp4 10.5 20.3 output.mkv --auto-avs --raw-video raw.mp4 --subtitle sub.ass

2. 使用现有 AVS 脚本:
   python hardsub_patch_avs.py input.mp4 10.5 20.3 script.avs output.mkv

3. 使用 RAW+字幕模式（ffmpeg libass 渲染）:
   python hardsub_patch_avs.py input.mp4 10.5 20.3 output.mkv --no-avs --raw-video raw.mp4 --subtitle sub.ass
        ''',
    )
    parser.add_argument('input', help='被替换的原视频文件')
    parser.add_argument('start', help='替换区间起始时间（秒 或 hh:mm:ss.ms）')
    parser.add_argument('end', help='替换区间结束时间（秒 或 hh:mm:ss.ms）')
    parser.add_argument(
        'rest',
        nargs='+',
        metavar='[avs] output',
        help='手动 AVS 模式传 <avs> <output>；--auto-avs 或 --no-avs 模式直接传 <output>',
    )
    parser.add_argument('--encoder', default=None, help='视频编码器（不指定则自动匹配原片）')
    parser.add_argument('--preset', default='fast', help='编码预设（默认 fast）')
    parser.add_argument('--crf', default=None, help='CRF 值（若不指定将尝试从输入视频获取码率）')
    parser.add_argument('--extra', default='', help='额外编码参数')
    parser.add_argument('--gop', type=int, default=0, help='固定 GOP 大小（帧数），0 为自动检测原片 GOP')
    parser.add_argument('--auto-avs', action='store_true', help='自动生成 AVS 脚本（显式加载 LSMASHSource + VSFilterMod）')
    parser.add_argument('--use-avs', dest='use_avs', action='store_true', help='使用 AVS 脚本模式（默认）')
    parser.add_argument('--no-avs', dest='use_avs', action='store_false', help='使用 RAW+字幕模式（ffmpeg libass 渲染）')
    parser.add_argument('--raw-video', help='RAW 视频文件（--auto-avs 或 --no-avs 模式必选）')
    parser.add_argument('--subtitle', help='字幕文件（--auto-avs 或 --no-avs 模式必选）')
    parser.add_argument('--vsfiltermod', default=None, help='可选：手动指定 VSFilterMod.dll 路径')
    parser.add_argument('--lsmash', default=None, help='可选：手动指定 LSMASHSource.dll 路径')
    parser.set_defaults(use_avs=True)
    return parser


def main(argv: list[str] | None = None) -> None:
    """命令行程序主入口。"""
    parser = _build_cli_parser()
    args = parser.parse_args(argv)

    if len(args.rest) == 1:
        avs_path = ''
        output_path = args.rest[0]
    elif len(args.rest) == 2:
        avs_path, output_path = args.rest
    else:
        parser.error('位置参数过多，请传入 [avs] output')

    if args.auto_avs:
        if not args.raw_video or not args.subtitle:
            parser.error('--auto-avs 模式需要 --raw-video 和 --subtitle 参数')
    elif args.use_avs:
        if not avs_path:
            parser.error('手动 AVS 模式需要传入 <avs> 和 <output> 两个参数（或使用 --auto-avs 模式）')
    elif not args.raw_video or not args.subtitle:
        parser.error('--no-avs 模式需要 --raw-video 和 --subtitle 参数')

    try:
        patch_video(
            args.input,
            args.start,
            args.end,
            avs_path,
            output_path,
            encoder=args.encoder,
            preset=args.preset,
            crf=args.crf,
            extra_enc_params=args.extra,
            use_avs=args.use_avs,
            raw_video=args.raw_video,
            subtitle_file=args.subtitle,
            gop=args.gop,
            auto_avs=args.auto_avs,
            vsfiltermod_path=args.vsfiltermod,
            lsmash_path=args.lsmash,
        )
    except Exception as exc:
        print(f'错误：{exc}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
