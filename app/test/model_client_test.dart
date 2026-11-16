import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:couple_app/core/model_client.dart';
import 'package:flutter_test/flutter_test.dart';

const _client = ModelClient(
  key: 'test-only-key-never-sent',
  baseUrl: 'https://model.invalid',
);
const _messages = <Map<String, Object?>>[
  {'role': 'user', 'content': '测试问题'},
];

void main() {
  for (final (status, message) in [
    (400, '模型服务暂时不可用（400）。'),
    (402, '模型账户余额不足。'),
    (401, '问答密钥不可用，请重新打包。'),
    (403, '问答密钥不可用，请重新打包。'),
  ]) {
    testWidgets('HTTP $status returns its specific error without retry', (
      tester,
    ) async {
      final server = _FakeServer([_Response(status, '{}')]);
      await HttpOverrides.runZoned(() async {
        await expectLater(
          _client.complete(_messages),
          throwsA(
            isA<ModelException>().having(
              (error) => error.message,
              'message',
              message,
            ),
          ),
        );
      }, createHttpClient: server.createClient);
      expect(server.clients.length, 1);
      expect(server.clients.single.closed, isTrue);
    });
  }

  testWidgets('HTTP 503 retries and returns the subsequent response', (
    tester,
  ) async {
    final server = _FakeServer([
      _Response(503, '{}'),
      _Response(
        200,
        jsonEncode({
          'choices': [
            {
              'message': {'role': 'assistant', 'content': '回答完成'},
            },
          ],
        }),
      ),
    ]);
    await HttpOverrides.runZoned(() async {
      final result = _client.complete(_messages);
      final expectation = expectLater(
        result,
        completion(containsPair('content', '回答完成')),
      );
      await tester.pump();
      expect(server.clients.length, 1);
      await tester.pump(const Duration(seconds: 1));
      await expectation;
    }, createHttpClient: server.createClient);
    expect(server.clients.length, 2);
    expect(server.clients.every((client) => client.closed), isTrue);
    final request = server.clients.last.request!;
    expect(request.followRedirects, isFalse);
    expect(jsonDecode(request.payload)['messages'], _messages);
    expect(request.payload, isNot(contains(_client.key)));
    expect(
      (request.headers as _Headers).values[HttpHeaders.authorizationHeader],
      'Bearer ${_client.key}',
    );
  });

  testWidgets(
    'socket failure retries three attempts then explains network error',
    (tester) async {
      final server = _FakeServer([
        const SocketException('test offline'),
        const SocketException('test offline'),
        const SocketException('test offline'),
      ]);
      await HttpOverrides.runZoned(() async {
        final expectation = expectLater(
          _client.complete(_messages),
          throwsA(
            isA<ModelException>().having(
              (error) => error.message,
              'message',
              '网络连接失败，请检查网络后重试。',
            ),
          ),
        );
        await tester.pump();
        expect(server.clients.length, 1);
        await tester.pump(const Duration(seconds: 1));
        expect(server.clients.length, 2);
        await tester.pump(const Duration(seconds: 2));
        await expectation;
      }, createHttpClient: server.createClient);
      expect(server.clients.length, 3);
      expect(server.clients.every((client) => client.closed), isTrue);
    },
  );

  testWidgets('malformed JSON produces a friendly error without retry', (
    tester,
  ) async {
    final server = _FakeServer([_Response(200, '<html>not JSON</html>')]);
    await HttpOverrides.runZoned(() async {
      await expectLater(
        _client.complete(_messages),
        throwsA(
          isA<ModelException>().having(
            (error) => error.message,
            'message',
            '模型返回了无法识别的内容。',
          ),
        ),
      );
    }, createHttpClient: server.createClient);
    expect(server.clients.length, 1);
    expect(server.clients.single.closed, isTrue);
  });

  testWidgets('non-HTTPS endpoint is rejected before creating an HTTP client', (
    tester,
  ) async {
    final server = _FakeServer([]);
    const client = ModelClient(
      key: 'test-key',
      baseUrl: 'http://model.invalid',
    );
    await HttpOverrides.runZoned(() async {
      await expectLater(
        client.complete(_messages),
        throwsA(
          isA<ModelException>().having(
            (error) => error.message,
            'message',
            '模型地址配置无效。',
          ),
        ),
      );
    }, createHttpClient: server.createClient);
    expect(server.clients, isEmpty);
  });

  testWidgets(
    'redirect cannot forward credentials or trigger a second request',
    (tester) async {
      final server = _FakeServer([_Response(302, '', redirected: true)]);
      await HttpOverrides.runZoned(() async {
        await expectLater(
          _client.complete(_messages),
          throwsA(
            isA<ModelException>().having(
              (error) => error.message,
              'message',
              '模型地址发生重定向，已停止发送密钥。',
            ),
          ),
        );
      }, createHttpClient: server.createClient);
      expect(server.clients.length, 1);
      expect(server.clients.single.request!.followRedirects, isFalse);
      expect(server.clients.single.closed, isTrue);
    },
  );

  for (final phase in ['response headers', 'response body']) {
    testWidgets('$phase timeout is bounded and retryable', (tester) async {
      final server = _FakeServer(
        List.generate(
          3,
          (_) => _Response(
            200,
            '',
            stallHeaders: phase == 'response headers',
            stallBody: phase == 'response body',
          ),
        ),
      );
      await HttpOverrides.runZoned(() async {
        final expectation = expectLater(
          _client.complete(_messages),
          throwsA(
            isA<ModelException>().having(
              (error) => error.message,
              'message',
              '这次回答超时了，请再试一次。',
            ),
          ),
        );
        await tester.pump();
        for (var attempt = 0; attempt < 3; attempt++) {
          expect(server.clients.length, attempt + 1);
          // Virtual clock: this test does not wait 95 real seconds.
          await tester.pump(const Duration(seconds: 95));
          expect(server.clients[attempt].closed, isTrue);
          if (attempt < 2) {
            await tester.pump(Duration(seconds: 1 << attempt));
          }
        }
        await expectation;
      }, createHttpClient: server.createClient);
      expect(server.clients.length, 3);
    });
  }

  for (final status in [429, 503]) {
    testWidgets('HTTP $status stops after three failed attempts', (
      tester,
    ) async {
      final server = _FakeServer(
        List.generate(3, (_) => _Response(status, '{}')),
      );
      await HttpOverrides.runZoned(() async {
        final expectation = expectLater(
          _client.complete(_messages),
          throwsA(
            isA<ModelException>().having(
              (error) => error.message,
              'message',
              '模型服务暂时不可用（$status）。',
            ),
          ),
        );
        await tester.pump();
        expect(server.clients.length, 1);
        await tester.pump(const Duration(seconds: 1));
        expect(server.clients.length, 2);
        await tester.pump(const Duration(seconds: 2));
        await expectation;
      }, createHttpClient: server.createClient);
      expect(server.clients.length, 3);
      expect(server.clients.every((client) => client.closed), isTrue);
    });
  }

  for (final (description, payload) in [
    ('missing choices', '{}'),
    ('empty choices', '{"choices":[]}'),
    ('missing message', '{"choices":[{}]}'),
    ('null message', '{"choices":[{"message":null}]}'),
    ('non-object choice', '{"choices":[42]}'),
  ]) {
    testWidgets('HTTP 200 with $description is a friendly failure', (
      tester,
    ) async {
      final server = _FakeServer([_Response(200, payload)]);
      await HttpOverrides.runZoned(() async {
        await expectLater(
          _client.complete(_messages),
          throwsA(
            isA<ModelException>().having(
              (error) => error.message,
              'message',
              '模型返回格式无效。',
            ),
          ),
        );
      }, createHttpClient: server.createClient);
      expect(server.clients.length, 1);
      expect(server.clients.single.closed, isTrue);
    });
  }

  testWidgets('blank credentials fail before any network client is created', (
    tester,
  ) async {
    const client = ModelClient(key: ' \t ');
    final server = _FakeServer([]);
    await HttpOverrides.runZoned(() async {
      await expectLater(
        client.complete(_messages),
        throwsA(
          isA<ModelException>().having(
            (error) => error.message,
            'message',
            '这个安装包还没有配置问答密钥。',
          ),
        ),
      );
    }, createHttpClient: server.createClient);
    expect(server.clients, isEmpty);
  });
}

class _FakeServer {
  final List<Object> outcomes;
  final List<_Client> clients = [];
  _FakeServer(this.outcomes);

  HttpClient createClient(SecurityContext? context) {
    if (clients.length >= outcomes.length) {
      throw StateError('Unexpected additional HTTP attempt');
    }
    final client = _Client(outcomes[clients.length]);
    clients.add(client);
    return client;
  }
}

class _Client extends Fake implements HttpClient {
  final Object outcome;
  _Request? request;
  bool closed = false;
  _Client(this.outcome);

  @override
  Duration? connectionTimeout;

  @override
  Future<HttpClientRequest> postUrl(Uri url) async {
    if (url.toString() != 'https://model.invalid/chat/completions') {
      throw StateError('Unexpected model endpoint');
    }
    if (outcome is SocketException) throw outcome;
    return request = _Request(outcome as _Response);
  }

  @override
  void close({bool force = false}) {
    closed = force;
    if (outcome is _Response) (outcome as _Response).finish();
  }
}

class _Headers extends Fake implements HttpHeaders {
  final values = <String, Object>{};
  @override
  ContentType? contentType;

  @override
  void set(String name, Object value, {bool preserveHeaderCase = false}) {
    values[name] = value;
  }
}

class _Request extends Fake implements HttpClientRequest {
  final _Response response;
  String payload = '';
  _Request(this.response);

  @override
  bool followRedirects = true;

  @override
  final HttpHeaders headers = _Headers();

  @override
  void write(Object? object) => payload += object.toString();

  @override
  Future<HttpClientResponse> close() => response.stallHeaders
      ? Completer<HttpClientResponse>().future
      : Future.value(response);
}

class _Response extends Stream<List<int>> implements HttpClientResponse {
  @override
  final int statusCode;
  final String payload;
  final bool redirected;
  final bool stallHeaders;
  final bool stallBody;
  StreamController<List<int>>? _body;
  _Response(
    this.statusCode,
    this.payload, {
    this.redirected = false,
    this.stallHeaders = false,
    this.stallBody = false,
  });

  void finish() {
    if (_body != null) unawaited(_body!.close());
  }

  @override
  bool get isRedirect => redirected;

  @override
  StreamSubscription<List<int>> listen(
    void Function(List<int>)? onData, {
    Function? onError,
    void Function()? onDone,
    bool? cancelOnError,
  }) {
    final stream = stallBody
        ? (_body ??= StreamController<List<int>>()).stream
        : Stream<List<int>>.fromIterable([utf8.encode(payload)]);
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
