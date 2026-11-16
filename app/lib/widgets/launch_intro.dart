import 'package:flutter/material.dart';

import 'seven_u_mark.dart';
import 'soft_ui.dart';

/// A short, local introduction. The app underneath can initialize immediately.
class LaunchIntro extends StatefulWidget {
  final Widget child;

  const LaunchIntro({super.key, required this.child});

  @override
  State<LaunchIntro> createState() => _LaunchIntroState();
}

class _LaunchIntroState extends State<LaunchIntro>
    with SingleTickerProviderStateMixin {
  late final AnimationController _controller;
  late final Animation<double> _markOpacity;
  late final Animation<double> _markScale;
  late final Animation<Offset> _markPosition;
  late final Animation<double> _curtainOpacity;
  bool _started = false;
  bool _finished = false;

  @override
  void initState() {
    super.initState();
    _controller =
        AnimationController(
          vsync: this,
          duration: const Duration(milliseconds: 1180),
        )..addStatusListener((status) {
          if (status == AnimationStatus.completed && mounted) {
            setState(() => _finished = true);
          }
        });
    final arrival = CurvedAnimation(
      parent: _controller,
      curve: const Interval(0, 0.48, curve: Curves.easeOutCubic),
    );
    _markOpacity = arrival;
    _markScale = Tween<double>(begin: 0.94, end: 1).animate(arrival);
    _markPosition = Tween<Offset>(
      begin: const Offset(0, 0.065),
      end: Offset.zero,
    ).animate(arrival);
    _curtainOpacity = Tween<double>(begin: 1, end: 0).animate(
      CurvedAnimation(
        parent: _controller,
        curve: const Interval(0.68, 1, curve: Curves.easeInOut),
      ),
    );
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    if (MediaQuery.disableAnimationsOf(context)) {
      _controller.stop();
      _finished = true;
    }
    if (!_started && !_finished) {
      _started = true;
      _controller.forward();
    }
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => Stack(
    fit: StackFit.expand,
    children: [
      IgnorePointer(
        ignoring: !_finished,
        child: ExcludeSemantics(excluding: !_finished, child: widget.child),
      ),
      if (!_finished)
        Positioned.fill(
          child: FadeTransition(
            opacity: _curtainOpacity,
            child: ColoredBox(
              key: const ValueKey('launch-intro'),
              color: softBackground,
              child: Center(
                child: SlideTransition(
                  position: _markPosition,
                  child: ScaleTransition(
                    scale: _markScale,
                    child: FadeTransition(
                      opacity: _markOpacity,
                      child: Semantics(
                        label: '7u',
                        image: true,
                        child: ExcludeSemantics(
                          child: Container(
                            width: 138,
                            height: 138,
                            alignment: Alignment.center,
                            decoration: BoxDecoration(
                              color: sevenUInk,
                              borderRadius: BorderRadius.circular(34),
                              boxShadow: const [
                                BoxShadow(
                                  color: Color(0x408b99a8),
                                  offset: Offset(12, 12),
                                  blurRadius: 28,
                                ),
                                BoxShadow(
                                  color: Color(0xf2ffffff),
                                  offset: Offset(-12, -12),
                                  blurRadius: 28,
                                ),
                              ],
                            ),
                            child: const SizedBox.expand(child: SevenUMark()),
                          ),
                        ),
                      ),
                    ),
                  ),
                ),
              ),
            ),
          ),
        ),
    ],
  );
}
