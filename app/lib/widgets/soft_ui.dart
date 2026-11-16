import 'package:flutter/material.dart';

const softBackground = Color(0xffe9edf2);

class SoftPanel extends StatelessWidget {
  final Widget child;
  final EdgeInsets padding;
  final BorderRadius borderRadius;
  const SoftPanel({
    super.key,
    required this.child,
    this.padding = const EdgeInsets.all(14),
    this.borderRadius = const BorderRadius.all(Radius.circular(22)),
  });
  @override
  Widget build(BuildContext context) => AnimatedContainer(
    duration: const Duration(milliseconds: 140),
    padding: padding,
    decoration: BoxDecoration(
      color: softBackground,
      borderRadius: borderRadius,
      boxShadow: [
        const BoxShadow(
          color: Color(0x2d8190a0),
          offset: Offset(7, 7),
          blurRadius: 14,
        ),
        const BoxShadow(
          color: Color(0xeaffffff),
          offset: Offset(-7, -7),
          blurRadius: 14,
        ),
      ],
    ),
    child: child,
  );
}
