// GENERATED from assets/branding/7u-mark.svg by tools/build_android_icons.cjs.
// Edit the SVG and regenerate; this artwork does not depend on a device font.
import 'package:flutter/material.dart';

const sevenUInk = Color(0xff243D35);
const sevenUIvory = Color(0xffF5F0E5);

class SevenUMark extends StatelessWidget {
  final Color color;
  const SevenUMark({super.key, this.color = sevenUIvory});

  @override
  Widget build(BuildContext context) =>
      CustomPaint(painter: _MarkPainter(color));
}

class _MarkPainter extends CustomPainter {
  final Color color;
  const _MarkPainter(this.color);

  @override
  void paint(Canvas canvas, Size size) {
    canvas.save();
    canvas.scale(size.width / 108, size.height / 108);
    final paint = Paint()
      ..color = color
      ..style = PaintingStyle.stroke
      ..strokeWidth = 6
      ..strokeCap = StrokeCap.round
      ..strokeJoin = StrokeJoin.round;
    final path0 = Path()
      ..moveTo(32, 36)
      ..lineTo(55, 36)
      ..lineTo(37, 72);
    canvas.drawPath(path0, paint);
    final path1 = Path()
      ..moveTo(57, 47)
      ..lineTo(57, 61)
      ..cubicTo(57, 68, 60.5, 72, 67, 72)
      ..cubicTo(73.5, 72, 77, 68, 77, 61)
      ..lineTo(77, 47);
    canvas.drawPath(path1, paint);
    canvas.restore();
  }

  @override
  bool shouldRepaint(_MarkPainter oldDelegate) => oldDelegate.color != color;
}
