# 硬字幕 Rev 小助手（replaceAB）

面向字幕组硬字幕修订（Rev）的局部区段无损替换工具（支持 Windows GUI 与 CLI）。  
仅重压对齐到关键帧的目标区段，其余视频段无损 copy，原音频整条直接复用，解决接缝卡顿与音画漂移问题。

## 主要特性

- **音频零拆分**：整条原音频直接复制封装，接缝无爆音、无延迟偏移。
- **自动 AVS + VSFilterMod 渲染**：自动生成 AviSynth+ 脚本，显式 `LoadPlugin` 加载 `LSMASHSource.dll` 与 `VSFilterMod.dll`，调用 `TextSubMod` 完整支持 VSFilterMod 扩展标签（`\3vc`、`\xbord`、`\1img` 等），并自动处理中文/非 ASCII 路径。
- **高速局部定位**：
  - 单次 `ffprobe` packet 元数据扫描获取关键帧与精确分段帧数，自动检测原片 GOP。
  - 自动 AVS 在 `TextSubMod` 之后插入 `Trim(start, -count)` 按需拉帧，跳过前段解码。
- **参数对齐原片**：保留精确分数帧率（如 `24000/1001` CFR）、profile/level、像素格式、色彩信息与 SAR。
- **绿色便携运行**：优先加载同级 `runtime/` 目录中的 `ffmpeg`、`ffprobe`、`AviSynth.dll`、`LSMASHSource.dll`、`VSFilterMod.dll`，支持打包免安装一键绿色版。

## 文件说明

| 文件 | 说明 |
| --- | --- |
| `hardsub_patch_avs.py` | 核心替换引擎与命令行入口 |
| `hardsub_patch_gui.py` | Windows Tkinter 图形界面 |
| `hardsub_patch_gui.spec` | PyInstaller 打包配置 |
| `build_portable.py` | 一键构建免安装绿色便携包脚本 |
| `test_avs_env.avs` | AviSynth+ / LSMASHSource / VSFilterMod 测试脚本模板 |

## 快速使用

### 1. 图形界面

```bash
python hardsub_patch_gui.py
```

### 2. 命令行

```bash
# 自动生成 AVS（推荐，显式加载 LSMASHSource + VSFilterMod）
python hardsub_patch_avs.py input.mp4 0:01:10.50 0:01:20.30 output.mkv \
  --auto-avs --raw-video raw.mp4 --subtitle sub.ass --crf 18

# 使用自定义 AVS 脚本
python hardsub_patch_avs.py input.mp4 10.5 20.3 script.avs output.mkv

# 使用 FFmpeg libass 渲染（不依赖 AviSynth）
python hardsub_patch_avs.py input.mp4 10.5 20.3 output.mkv \
  --no-avs --raw-video raw.mp4 --subtitle sub.ass --crf 18
```

### 3. 一键构建免安装绿色包

```bash
# 构建完整一键绿色包（内置 FFmpeg shared + AviSynth.dll + 插件 + VC++ 运行库）
python build_portable.py

# 构建轻量绿色包（不含 FFmpeg，仅含 AviSynth.dll 与插件）
python build_portable.py --no-ffmpeg
```

构建产物位于 `dist/` 目录，默认输出 `dist/硬字幕Rev小助手_一键绿色版.zip`。
