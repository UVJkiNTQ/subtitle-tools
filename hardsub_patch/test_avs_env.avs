# 测试 AVS 环境：LSMASHSource + VSFilterMod
# 用法：ffmpeg -f avisynth -i test_avs_env.avs -t 5 test_output.mp4

# 显式加载插件（请根据本机 AviSynth+ 插件目录调整路径）
LoadPlugin("C:\Program Files (x86)\AviSynth+\plugins64\LSMASHSource.dll")
LoadPlugin("C:\Program Files (x86)\AviSynth+\plugins64\VSFilterMod.dll")

# 加载源视频（使用 LSMASHSource，生成 .lwi 索引）
video = LWLibavVideoSource("C:\Users\lxdlu\Desktop\replaceAB\input_sample.mp4")

# 测试字幕渲染（VSFilterMod 使用 TextSubMod，注意路径建议使用纯 ASCII 字符）
# video = TextSubMod(video, "C:\Users\lxdlu\Desktop\replaceAB\test.ass")

return video
