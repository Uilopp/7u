class QuestionEvidence {
  final String? msgId;
  final String speaker;
  final String quote;
  final String? date;
  final String sourceType;

  const QuestionEvidence({
    this.msgId,
    required this.speaker,
    required this.quote,
    this.date,
    this.sourceType = 'message_text',
  });

  factory QuestionEvidence.fromJson(Map<String, dynamic> value) =>
      QuestionEvidence(
        msgId: value['msg_id']?.toString(),
        speaker: value['speaker']?.toString() ?? 'unknown',
        quote: value['quote']?.toString() ?? '',
        date: value['date']?.toString(),
        sourceType: value['source_type']?.toString() ?? 'message_text',
      );

  Map<String, dynamic> toJson() => {
    if (msgId != null) 'msg_id': msgId,
    'speaker': speaker,
    'quote': quote,
    if (date != null) 'date': date,
    'source_type': sourceType,
  };
}

class SavedQuestion {
  final String question;
  final String title;
  final String body;
  final List<QuestionEvidence> evidence;
  final DateTime createdAt;
  final String? appVersion;

  const SavedQuestion({
    required this.question,
    required this.title,
    required this.body,
    required this.evidence,
    required this.createdAt,
    this.appVersion,
  });

  factory SavedQuestion.fromResult(
    String question,
    Map<String, dynamic> result, {
    String? appVersion,
  }) {
    final rawAnswer = result['answer'];
    final answer = rawAnswer is Map
        ? Map<String, dynamic>.from(rawAnswer)
        : <String, dynamic>{};
    final rawQuotes = answer['quotes'];
    return SavedQuestion(
      question: question,
      title: answer['title']?.toString().trim().isNotEmpty == true
          ? answer['title'].toString().trim()
          : '7u 想说',
      body: answer['body']?.toString().trim().isNotEmpty == true
          ? answer['body'].toString().trim()
          : result['message']?.toString().trim().isNotEmpty == true
          ? result['message'].toString().trim()
          : '这次的答案还没有完整回来，可以稍后再问一次。',
      evidence: rawQuotes is List
          ? rawQuotes
                .whereType<Map>()
                .map(
                  (item) => QuestionEvidence.fromJson(
                    Map<String, dynamic>.from(item),
                  ),
                )
                .where((item) => item.quote.isNotEmpty)
                .toList()
          : const [],
      createdAt: DateTime.now(),
      appVersion: appVersion,
    );
  }

  factory SavedQuestion.fromJson(Map<String, dynamic> value) => SavedQuestion(
    question: value['question']?.toString() ?? '',
    title: value['title']?.toString() ?? '7u 想说',
    body: value['body']?.toString() ?? '',
    appVersion: value['app_version']?.toString(),
    evidence: (value['evidence'] as List? ?? const [])
        .whereType<Map>()
        .map(
          (item) => QuestionEvidence.fromJson(Map<String, dynamic>.from(item)),
        )
        .toList(),
    createdAt:
        DateTime.tryParse(value['created_at']?.toString() ?? '') ??
        DateTime.now(),
  );

  Map<String, dynamic> toJson() => {
    'question': question,
    'title': title,
    'body': body,
    'evidence': evidence.map((item) => item.toJson()).toList(),
    'created_at': createdAt.toIso8601String(),
    if (appVersion != null) 'app_version': appVersion,
  };
}
