import 'local_store.dart';
import 'model_client.dart';
import 'question_agent.dart';
import 'question_cancellation.dart';
import 'question_history.dart';

class Api {
  final LocalStore local;
  final QuestionAgent agent;

  Api({LocalStore? local, QuestionAgent? agent})
    : local = local ?? LocalStore(),
      agent = agent ?? QuestionAgent();

  Future<Map<String, dynamic>> askQuestion(
    String question, {
    List<SavedQuestion> history = const [],
    QuestionCancellation? cancellation,
  }) => agent.ask(question, history: history, cancellation: cancellation);
}

typedef ApiException = ModelException;
