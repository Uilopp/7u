import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:couple_app/core/api.dart';
import 'package:couple_app/core/memory_pack.dart';
import 'package:couple_app/core/model_client.dart';
import 'package:couple_app/core/question_agent.dart';
import 'package:couple_app/core/question_cancellation.dart';
import 'package:flutter_test/flutter_test.dart';

const _request = <Map<String, Object?>>[
  {'role': 'user', 'content': '测试'},
];
const _httpModel = ModelClient(
  key: 'test-only-key-never-sent',
  baseUrl: 'https://model.invalid',
);

void main() {
  test(
    'token cancel is idempotent, supports unsubscribe and late listeners',
    () async {
      final token = QuestionCancellation();
      var calls = 0;
      final unsubscribe = token.onCancel(() => calls += 100);
      unsubscribe();
      token.onCancel(() => calls++);
      token.cancel();
      token.cancel();
      token.onCancel(() => calls++);
      await token.whenCancelled;
      expect(calls, 2);
      expect(token.isCancelled, isTrue);
      expect(
        token.throwIfCancelled,
        throwsA(isA<QuestionCancelledException>()),
      );
    },
  );

  test(
    'pre-cancelled request never dispatches transport or prompt reads',
    () async {
      final token = QuestionCancellation()..cancel();
      var calls = 0;
      final model = ModelClient(
        transport: (_) async {
          calls++;
          return {};
        },
      );
      await expectLater(
        model.complete(_request, cancellation: token),
        throwsA(isA<QuestionCancelledException>()),
      );
      final api = Api(
        agent: QuestionAgent(
          model: model,
          promptLoader: () async {
            calls++;
            return 'prompt';
          },
        ),
      );
      await expectLater(
        api.askQuestion('测试', cancellation: token),
        throwsA(isA<QuestionCancelledException>()),
      );
      expect(calls, 0);
    },
  );

  test(
    'cancelled transport returns promptly and observes a late error',
    () async {
      final token = QuestionCancellation();
      final delayed = Completer<Map<String, Object?>>();
      final model = ModelClient(transport: (_) => delayed.future);
      final checked = expectLater(
        model.complete(_request, cancellation: token),
        throwsA(isA<QuestionCancelledException>()),
      );
      token.cancel();
      await checked;
      delayed.completeError(StateError('late transport failure'));
      await Future<void>.delayed(Duration.zero);
    },
  );

  test('cancellation is scoped to one concurrent request', () async {
    final first = QuestionCancellation();
    final second = QuestionCancellation();
    final replies = [
      Completer<Map<String, Object?>>(),
      Completer<Map<String, Object?>>(),
    ];
    var index = 0;
    final model = ModelClient(transport: (_) => replies[index++].future);
    final rejected = expectLater(
      model.complete(_request, cancellation: first),
      throwsA(isA<QuestionCancelledException>()),
    );
    final success = model.complete(_request, cancellation: second);
    first.cancel();
    replies[1].complete({'content': 'other answer'});
    await rejected;
    expect((await success)['content'], 'other answer');
    expect(second.isCancelled, isFalse);
    replies[0].complete({});
  });

  test('cancelling a blocked prompt prevents any paid model round', () async {
    final token = QuestionCancellation();
    final prompt = Completer<String>();
    var calls = 0;
    final agent = QuestionAgent(
      promptLoader: () => prompt.future,
      model: ModelClient(
        transport: (_) async {
          calls++;
          return {};
        },
      ),
    );
    final stopped = expectLater(
      agent.ask('问题', cancellation: token),
      throwsA(isA<QuestionCancelledException>()),
    );
    token.cancel();
    await stopped;
    prompt.completeError(StateError('late read failure'));
    await Future<void>.delayed(Duration.zero);
    expect(calls, 0);
  });

  test(
    'cancel in first tool prevents later tools and next model call',
    () async {
      final token = QuestionCancellation();
      final memory = _Memory();
      var calls = 0;
      final model = ModelClient(
        transport: (_) async {
          calls++;
          return {
            'tool_calls': [
              for (final id in ['one', 'two'])
                {
                  'id': id,
                  'type': 'function',
                  'function': {'name': 'search_chats', 'arguments': '{}'},
                },
            ],
          };
        },
      );
      final agent = QuestionAgent(
        memory: memory,
        model: model,
        promptLoader: () async => 'prompt',
      );
      final stopped = expectLater(
        agent.ask('问题', cancellation: token),
        throwsA(isA<QuestionCancelledException>()),
      );
      await memory.started.future;
      token.cancel();
      await stopped;
      memory.reply.complete([]);
      await Future<void>.delayed(Duration.zero);
      expect(memory.searches, 1);
      expect(calls, 1);
    },
  );

  for (final phase in ['connect', 'headers', 'body', 'retry']) {
    testWidgets('cancel during $phase closes HTTP and never retries', (
      tester,
    ) async {
      final token = QuestionCancellation();
      final clients = <_Client>[];
      await HttpOverrides.runZoned(
        () async {
          final stopped = expectLater(
            _httpModel.complete(_request, cancellation: token),
            throwsA(isA<QuestionCancelledException>()),
          );
          await tester.pump();
          expect(clients, hasLength(1));
          token.cancel();
          await tester.pump();
          await stopped;
          // Any already-scheduled one-second backoff may expire, but must not
          // dispatch another HTTP request after its token has been cancelled.
          await tester.pump(const Duration(seconds: 2));
          expect(clients, hasLength(1));
          expect(clients.single.forceClosed, isTrue);
        },
        createHttpClient: (_) {
          final client = _Client(phase);
          clients.add(client);
          return client;
        },
      );
    });
  }
}

class _Memory extends MemoryPack {
  final started = Completer<void>();
  final reply = Completer<List<MemoryMessage>>();
  int searches = 0;

  @override
  Future<String?> latestMessageDate() async => '2026-09-27T12:28:13+08:00';

  @override
  Future<List<MemoryMessage>> search({
    String keyword = '',
    List<String> keywords = const [],
    bool matchAll = false,
    String? speaker,
    int? start,
    int? end,
    int limit = 30,
    bool ascending = false,
    String? cursor,
  }) {
    searches++;
    if (!started.isCompleted) started.complete();
    return reply.future;
  }
}

class _Client extends Fake implements HttpClient {
  final String phase;
  final connection = Completer<HttpClientRequest>();
  final headers = Completer<HttpClientResponse>();
  late final response = _Response(
    phase == 'retry' ? 503 : 200,
    phase == 'body',
  );
  bool forceClosed = false;
  _Client(this.phase);

  @override
  Duration? connectionTimeout;

  @override
  Future<HttpClientRequest> postUrl(Uri url) {
    expect(url.toString(), 'https://model.invalid/chat/completions');
    return phase == 'connect'
        ? connection.future
        : Future.value(_Request(this));
  }

  @override
  void close({bool force = false}) {
    forceClosed |= force;
    if (phase == 'connect' && !connection.isCompleted) {
      connection.completeError(const SocketException('closed'));
    }
    if (phase == 'headers' && !headers.isCompleted) {
      headers.completeError(const SocketException('closed'));
    }
    response.close();
  }
}

class _Request extends Fake implements HttpClientRequest {
  final _Client client;
  _Request(this.client);
  @override
  bool followRedirects = true;
  @override
  final HttpHeaders headers = _Headers();
  @override
  void write(Object? value) {}
  @override
  Future<HttpClientResponse> close() => client.phase == 'headers'
      ? client.headers.future
      : Future.value(client.response);
}

class _Headers extends Fake implements HttpHeaders {
  @override
  ContentType? contentType;
  @override
  void set(String name, Object value, {bool preserveHeaderCase = false}) {}
}

class _Response extends Stream<List<int>> implements HttpClientResponse {
  @override
  final int statusCode;
  final bool stall;
  StreamController<List<int>>? _body;
  _Response(this.statusCode, this.stall);
  @override
  bool get isRedirect => false;
  void close() {
    if (_body != null) unawaited(_body!.close());
  }

  @override
  StreamSubscription<List<int>> listen(
    void Function(List<int>)? onData, {
    Function? onError,
    void Function()? onDone,
    bool? cancelOnError,
  }) {
    final stream = stall
        ? (_body ??= StreamController<List<int>>()).stream
        : Stream.value(utf8.encode('{}'));
    return stream.listen(
      onData,
      onError: onError,
      onDone: onDone,
      cancelOnError: cancelOnError,
    );
  }

  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}
