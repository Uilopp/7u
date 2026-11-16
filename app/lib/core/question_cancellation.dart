import 'dart:async';

/// Per-answer cancellation. Never share one token between conversations/retries.
class QuestionCancellation {
  final _cancelledSignal = Completer<void>();
  final _listeners = <void Function()>{};
  bool _cancelled = false;

  bool get isCancelled => _cancelled;
  Future<void> get whenCancelled => _cancelledSignal.future;

  void throwIfCancelled() {
    if (_cancelled) throw const QuestionCancelledException();
  }

  /// Returns an idempotent unsubscribe function for operation-scoped resources.
  void Function() onCancel(void Function() listener) {
    if (_cancelled) {
      listener();
      return () {};
    }
    _listeners.add(listener);
    return () => _listeners.remove(listener);
  }

  void cancel() {
    if (_cancelled) return;
    _cancelled = true;
    _cancelledSignal.complete();
    final listeners = _listeners.toList();
    _listeners.clear();
    for (final listener in listeners) {
      try {
        listener();
      } catch (_) {
        // One cleanup failure must not prevent other requests being closed.
      }
    }
  }

  /// Stops awaiting promptly, observes late errors, and never starts if cancelled.
  Future<T> run<T>(Future<T> Function() operation) {
    throwIfCancelled();
    final completion = Completer<T>();
    final unsubscribe = onCancel(() {
      if (!completion.isCompleted) {
        completion.completeError(const QuestionCancelledException());
      }
    });
    Future<T>.sync(operation).then(
      (value) {
        if (!completion.isCompleted) completion.complete(value);
      },
      onError: (Object error, StackTrace stack) {
        if (!completion.isCompleted) completion.completeError(error, stack);
      },
    );
    return completion.future.whenComplete(unsubscribe);
  }
}

class QuestionCancelledException implements Exception {
  final String message = '这次回答已停止，问题会保留，可以重新尝试。';
  const QuestionCancelledException();

  @override
  String toString() => message;
}
