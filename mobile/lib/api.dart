import 'package:http/http.dart' as http;

import 'auth.dart';
import 'config.dart';

class ApiClient {
  ApiClient(this._auth);
  final AuthService _auth;

  Future<http.Response> get(String path, [Map<String, String>? query]) {
    final uri = Uri.parse(
      '${AppConfig.backendBase}$path',
    ).replace(queryParameters: query);
    return http.get(uri, headers: _bearerHeaders());
  }

  Map<String, String> _bearerHeaders() {
    final s = _auth.session;
    if (s == null) return const {};
    return {'Authorization': 'Bearer ${s.token}'};
  }
}
