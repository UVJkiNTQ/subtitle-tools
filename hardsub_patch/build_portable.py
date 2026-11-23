#!/usr/bin/env python3
"""
build_portable.py - 一键构建「硬字幕Rev小助手」免安装绿色便携包

构建步骤：
1. 调用 PyInstaller + UPX 重新打包 `dist/硬字幕Rev小助手.exe`
2. 收集 64 位 AviSynth.dll、LSMASHSource.dll、VSFilterMod.dll 与 VC++ 运行库到 `runtime/`
3. 默认自动下载并缓存 Gyan.dev `ffmpeg-release-full-shared.7z`，提取精简版 ffmpeg/ffprobe + 共享 DLL
   （传 `--no-ffmpeg` 可制作不含 FFmpeg 的轻量绿色包）
4. 生成 `使用说明.txt` 并打包为 `dist/硬字幕Rev小助手_一键绿色版.zip`
5. 自动清理 `build/` 与 `__pycache__/`
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tempfile

from hardsub_patch_avs import find_avisynth_dll, find_lsmash_dll, find_vsfiltermod_dll

ROOT = os.path.dirname(os.path.abspath(__file__))
DIST_DIR = os.path.join(ROOT, 'dist')
CACHE_DIR = os.path.join(ROOT, '.cache')
FFMPEG_SHARED_URL = 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-full-shared.7z'
APP_NAME = '硬字幕Rev小助手'


def _render_readme(include_ffmpeg: bool) -> str:
    """生成绿色包内附带的使用说明文本。"""
    runtime_desc = (
        '已内置 64 位 AviSynth+ 核心 (AviSynth.dll)、LSMASHSource.dll、VSFilterMod.dll、\n'
        '     VC++ 运行库以及 FFmpeg / FFprobe，目标电脑无需安装 AviSynth+ 或配置环境变量。'
        if include_ffmpeg
        else '已内置 64 位 AviSynth+ 核心 (AviSynth.dll)、LSMASHSource.dll、VSFilterMod.dll 与 VC++ 运行库。\n'
        '     当前为轻量版，请确保目标电脑已安装支持 AviSynth 的 FFmpeg / FFprobe 并加入 PATH。'
    )
    return (
        f'硬字幕 Rev 小助手（免安装绿色{"一键版" if include_ffmpeg else "轻量版"}）\n'
        '========================================\n\n'
        '1. 开箱即用\n'
        f'   - 解压到任意文件夹后，直接双击「{APP_NAME}.exe」即可使用。\n'
        f'   - {runtime_desc}\n\n'
        '2. 使用步骤\n'
        '   - 输入视频：选择需要局部替换（Rev）的已压制硬字幕视频。\n'
        '   - 替换区间：填写需要重新压制的起止时间（如 0:01:23.45 或纯秒数 83.45）。\n'
        '   - 默认勾选「使用 AVS 脚本」+「自动生成 AVS 脚本（LSMASHSource + VSFilterMod）」：\n'
        '     - RAW 视频：选择对应的无字幕源片。\n'
        '     - 字幕文件：选择修改后的 .ass / .ssa / .srt 字幕（支持中文路径与 VSFilterMod 扩展标签）。\n'
        '     - VSFilterMod：已自动指向内置 runtime\\VSFilterMod.dll，无需修改。\n'
        '   - 输出文件：推荐输出 .mkv，点击「开始处理」即可。\n\n'
        '3. 注意事项\n'
        '   - 请勿删除或移动同目录下的 runtime 文件夹。\n'
        '   - 可点击界面上的「检测环境」按钮确认组件识别状态。\n'
        '   - 字幕所用字体仍需已在当前 Windows 系统中安装。\n'
    )


def run_cmd(cmd: list[str], cwd: str = ROOT) -> None:
    """打印并执行外部命令。"""
    print('>', ' '.join(cmd))
    subprocess.run(cmd, cwd=cwd, check=True)


def build_exe() -> str:
    """通过 PyInstaller + UPX 打包主程序 exe。"""
    pyinstaller = os.path.join(ROOT, '.venv', 'Scripts', 'pyinstaller.exe')
    if not os.path.isfile(pyinstaller):
        pyinstaller = shutil.which('pyinstaller') or ''
    if not pyinstaller:
        raise RuntimeError('未找到 pyinstaller，请先在 .venv 中安装 pyinstaller。')

    cmd = [pyinstaller, '--clean', '--noconfirm']
    upx_dir = os.path.join(ROOT, 'upx-5.1.1-win64')
    if os.path.isdir(upx_dir):
        cmd += ['--upx-dir', upx_dir]
    cmd.append(os.path.join(ROOT, 'hardsub_patch_gui.spec'))
    run_cmd(cmd)

    exe_path = os.path.join(DIST_DIR, f'{APP_NAME}.exe')
    if not os.path.isfile(exe_path):
        raise FileNotFoundError(f'打包后未找到输出文件：{exe_path}')
    return exe_path


def download_and_extract_ffmpeg_shared(runtime_dir: str) -> None:
    """下载（或复用缓存）Gyan.dev FFmpeg shared 版，并提取 ffmpeg/ffprobe 与共享 DLL 到 runtime 目录。"""
    os.makedirs(CACHE_DIR, exist_ok=True)
    archive_path = os.path.join(CACHE_DIR, 'ffmpeg-release-full-shared.7z')
    if not os.path.isfile(archive_path) or os.path.getsize(archive_path) < 10_000_000:
        print(f'正在下载 FFmpeg shared 运行包: {FFMPEG_SHARED_URL}')
        tmp_dl = archive_path + '.part'
        run_cmd(['curl.exe', '-fL', '--retry', '3', '-o', tmp_dl, FFMPEG_SHARED_URL])
        os.replace(tmp_dl, archive_path)
    else:
        print(f'使用已缓存的 FFmpeg shared 包: {archive_path}')

    extract_tmp = tempfile.mkdtemp(prefix='ff_extract_')
    try:
        run_cmd(['tar.exe', '-xf', archive_path, '-C', extract_tmp])
        bin_dir = None
        for dirpath, _, filenames in os.walk(extract_tmp):
            if 'ffmpeg.exe' in filenames and 'ffprobe.exe' in filenames:
                bin_dir = dirpath
                break
        if not bin_dir:
            raise RuntimeError('解压 FFmpeg shared 包后未找到 bin/ffmpeg.exe')

        copied = 0
        for fname in os.listdir(bin_dir):
            lower = fname.lower()
            # 剔除 ffplay.exe，仅保留 ffmpeg.exe / ffprobe.exe 与共享 DLL
            if lower in ('ffmpeg.exe', 'ffprobe.exe') or lower.endswith('.dll'):
                shutil.copy2(os.path.join(bin_dir, fname), os.path.join(runtime_dir, fname))
                copied += 1
        print(f'已复制 {copied} 个 FFmpeg shared 运行文件到 runtime/')
    finally:
        shutil.rmtree(extract_tmp, ignore_errors=True)


def assemble_portable(exe_path: str, include_ffmpeg: bool = True) -> tuple[str, str]:
    """组装绿色便携目录并压缩为 zip 分发包。"""
    pkg_name = f'{APP_NAME}_一键绿色版' if include_ffmpeg else f'{APP_NAME}_轻量绿色版'
    pkg_dir = os.path.join(DIST_DIR, pkg_name)
    runtime_dir = os.path.join(pkg_dir, 'runtime')

    if os.path.exists(pkg_dir):
        shutil.rmtree(pkg_dir)
    os.makedirs(runtime_dir, exist_ok=True)

    # 1. 主程序与使用说明
    shutil.copy2(exe_path, os.path.join(pkg_dir, f'{APP_NAME}.exe'))
    with open(os.path.join(pkg_dir, '使用说明.txt'), 'w', encoding='utf-8') as f:
        f.write(_render_readme(include_ffmpeg))

    # 2. AviSynth+ 核心与插件 DLL
    for label, path in (
        ('AviSynth.dll', find_avisynth_dll()),
        ('LSMASHSource.dll', find_lsmash_dll()),
        ('VSFilterMod.dll', find_vsfiltermod_dll()),
    ):
        if not path or not os.path.isfile(path):
            raise FileNotFoundError(f'未能在本机找到 {label}，无法组装绿色包')
        shutil.copy2(path, os.path.join(runtime_dir, os.path.basename(path)))
        print(f'已内置 {label}: {path}')

    # 3. VC++ 2015-2022 (x64) 运行库 DLL
    sys32 = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32')
    for dll in ('vcruntime140.dll', 'vcruntime140_1.dll', 'msvcp140.dll'):
        src = os.path.join(sys32, dll)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(runtime_dir, dll))
            print(f'已内置运行库: {dll}')

    # 4. 可选内置 FFmpeg + FFprobe (shared)
    if include_ffmpeg:
        download_and_extract_ffmpeg_shared(runtime_dir)

    # 5. 压缩 zip 分发包
    zip_path = os.path.join(DIST_DIR, f'{pkg_name}.zip')
    if os.path.exists(zip_path):
        os.remove(zip_path)
    print(f'正在压缩分发包: {zip_path}')
    run_cmd(['tar.exe', '-a', '-c', '-f', zip_path, '-C', DIST_DIR, pkg_name])

    total_bytes = sum(
        os.path.getsize(os.path.join(dp, fn))
        for dp, _, fns in os.walk(pkg_dir)
        for fn in fns
    )
    zip_bytes = os.path.getsize(zip_path)
    print('\n✅ 绿色包构建完成！')
    print(f'  - 解压目录: {pkg_dir} ({total_bytes / 1024 / 1024:.1f} MB)')
    print(f'  - 压缩包:   {zip_path} ({zip_bytes / 1024 / 1024:.1f} MB)')
    return pkg_dir, zip_path


def cleanup() -> None:
    """清理 PyInstaller 中间产物与 Python 字节码缓存。"""
    for rel in ('build', '__pycache__'):
        path = os.path.join(ROOT, rel)
        if os.path.exists(path):
            shutil.rmtree(path, ignore_errors=True)
            print(f'已清理: {path}')


def main() -> None:
    """一键构建脚本入口。"""
    parser = argparse.ArgumentParser(description='一键构建硬字幕Rev小助手绿色便携包')
    parser.add_argument('--no-ffmpeg', action='store_true', help='不内置 FFmpeg（仅含 AviSynth+ 与插件）')
    parser.add_argument('--skip-exe', action='store_true', help='跳过 PyInstaller，直接复用现有 dist exe')
    args = parser.parse_args()

    exe_path = os.path.join(DIST_DIR, f'{APP_NAME}.exe')
    if not args.skip_exe or not os.path.isfile(exe_path):
        exe_path = build_exe()

    assemble_portable(exe_path, include_ffmpeg=not args.no_ffmpeg)
    cleanup()


if __name__ == '__main__':
    main()
