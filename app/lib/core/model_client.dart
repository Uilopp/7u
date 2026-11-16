import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'question_cancellation.dart';

class ModelException implements Exception {
  final String message;
  const ModelException(this.message);
  @override
  String toString() => message;
}

class ModelClient {
  static const defaultKey = String.fromEnvironment('DEEPSEEK_API_KEY');
  static const defaultBaseUrl = String.fromEnvironment(
    'DEEPSEEK_BASE_URL',
    defaultValue: 'https://api.deepseek.com',
  );
  static const defaultModel = String.fromEnvironment(
    'DEEPSEEK_MODEL',
    defaultValue: 'deepseek-flash',
  );

  final String key;
  final String baseUrl;
  final String model;
  final Future<Map<String, Object?>> Function(Map<String, Object?> body)?
  transport;

  const ModelClient({
    this.key = defaultKey,
    this.baseUrl = defaultBaseUrl,
    this.model = defaultModel,
    this.transport,
  });

  Future<Map<String, Object?>> complete(
    List<Map<String, Object?>> messages, {
    List<Map<String, Object?>>? tools,
    QuestionCancellation? cancellation,
  }) async {
    cancellation?.throwIfCancelled();
    if (key.trim().isEmpty && transport == null) {
      throw const ModelException('这个安装包还没有配置问答密钥。');
    }
    final body = <String, Object?>{
      'model': model,
      'messages': messages,
      'temperature': .3,
      'max_tokens': 8000,
      'response_format': {'type': 'json_object'},
      if (tools != null && tools.isNotEmpty) ...{
        'tools': tools,
        'tool_choice': 'auto',
      },
    };
    if (transport != null) {
      return cancellation == null
          ? transport!(body)
          : cancellation.run(() => transport!(body));
    }

    final endpoint = Uri.tryParse(
      '${baseUrl.replaceFirst(RegExp(r'/$'), '')}/chat/completions',
    );
    if (endpoint == null ||
        endpoint.scheme != 'https' ||
        endpoint.host.isEmpty) {
      throw const ModelException('模型地址配置无效。');
    }
    for (var attempt = 0; attempt < 3; attempt++) {
      cancellation?.throwIfCancelled();
      final client = HttpClient()
        ..connectionTimeout = const Duration(seconds: 12);
      final unsubscribe = cancellation?.onCancel(
        () => client.close(force: true),
      );
      try {
        final request = await _run(
          cancellation,
          () => client.postUrl(endpoint),
        );
        cancellation?.throwIfCancelled();
        request.followRedirects = false;
        request.headers.contentType = ContentType.json;
        request.headers.set(HttpHeaders.authorizationHeader, 'Bearer $key');
        request.write(jsonEncode(body));
        final response = await _run(
          cancellation,
          () => request.close().timeout(const Duration(seconds: 95)),
        );
        final payload = await _run(
          cancellation,
          () => response
              .transform(utf8.decoder)
              .join()
              .timeout(const Duration(seconds: 95)),
        );
        cancellation?.throwIfCancelled();
        if (response.isRedirect) {
          throw const ModelException('模型地址发生重定向，已停止发送密钥。');
        }
        if (response.statusCode == 401 || response.statusCode == 403) {
          throw const ModelException('问答密钥不可用，请重新打包。');
        }
        if (response.statusCode == 402) {
          throw const ModelException('模型账户余额不足。');
        }
        if (response.statusCode != 200) {
          if (_transient(response.statusCode) && attempt < 2) {
            await _run(
              cancellation,
              () => Future<void>.delayed(Duration(seconds: 1 << attempt)),
            );
            continue;
          }
          throw ModelException('模型服务暂时不可用（${response.statusCode}）。');
        }
        final decoded = jsonDecode(payload);
        if (decoded is! Map ||
            decoded['choices'] is! List ||
            (decoded['choices'] as List).isEmpty) {
          throw const ModelException('模型返回格式无效。');
        }
        final choice = (decoded['choices'] as List).first;
        if (choice is! Map || choice['message'] is! Map) {
          throw const ModelException('模型返回格式无效。');
        }
        return Map<String, Object?>.from(choice['message'] as Map);
      } on ModelException {
        rethrow;
      } on TimeoutException catch (_) {
        cancellation?.throwIfCancelled();
        if (attempt == 2) throw const ModelException('这次回答超时了，请再试一次。');
      } on SocketException catch (_) {
        cancellation?.throwIfCancelled();
        if (attempt == 2) throw const ModelException('网络连接失败，请检查网络后重试。');
      } on FormatException catch (_) {
        throw const ModelException('模型返回了无法识别的内容。');
      } finally {
        unsubscribe?.call();
        client.close(force: true);
      }
      await _run(
        cancellation,
        () => Future<void>.delayed(Duration(seconds: 1 << attempt)),
      );
    }
    throw const ModelException('这次没有完成回答，请再试一次。');
  }

  Future<T> _run<T>(
    QuestionCancellation? cancellation,
    Future<T> Function() operation,
  ) => cancellation == null ? operation() : cancellation.run(operation);

  bool _transient(int status) =>
      const {408, 429, 500, 502, 503, 504}.contains(status);
}
