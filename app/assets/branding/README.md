# 7u 字标

`7u-mark.svg` 是唯一手工维护的图标源文件：108 × 108 画布、两条原创路径，深墨绿 `#243D35` 与暖白 `#F5F0E5`。不依赖字体或外部图片；字标位于 Android adaptive icon 的中央安全圆内。

从仓库根目录生成（Node.js + sharp 0.35.4）：

```powershell
# 只需在需要修改图标的电脑上安装一次；也可使用已有 sharp。
npm install --prefix "$env:LOCALAPPDATA\7u-icon-tools" --no-save sharp@0.35.4
$env:SEVENU_SHARP_MODULE = "$env:LOCALAPPDATA\7u-icon-tools\node_modules\sharp"
node tools/build_android_icons.cjs
dart format app/lib/widgets/seven_u_mark.dart
```

生成器会校验字标安全范围、PNG 尺寸与透明通道，并生成：

- Android 8+ 自适应图标和 Android 13+ 主题单色图标。
- Android 旧版 launcher PNG（48、72、96、144、192 px）。
- `lib/widgets/seven_u_mark.dart`：开屏使用相同路径，没有额外 Flutter 依赖。
- `7u-icon.png`：512 px 单图；`7u-icon-preview.png`：圆形、圆角、主题单色及小尺寸预览。

路径只使用 `M / L / H / V / C` 绝对命令。调整时改 SVG，再重新生成；不要手改各分辨率 PNG 或生成后的 Android/Dart 路径。
