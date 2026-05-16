import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:google_sign_in/google_sign_in.dart';
import 'package:http/http.dart' as http;

import 'config.dart';

class AuthSession {
  AuthSession({
    required this.email,
    required this.token,
    required this.expiresAt,
  });

  final String email;
  final String token;
  final DateTime expiresAt;

  Map<String, dynamic> toJson() => {
    'email': email,
    'token': token,
    'expires_at': expiresAt.toIso8601String(),
  };

  factory AuthSession.fromJson(Map<String, dynamic> j) => AuthSession(
    email: j['email'] as String,
    token: j['token'] as String,
    expiresAt: DateTime.parse(j['expires_at'] as String),
  );
}

class AuthService extends ChangeNotifier {
  static const _storageKey = 'belfry-session-v1';
  final FlutterSecureStorage _storage = const FlutterSecureStorage();

  AuthSession? _session;
  AuthSession? get session => _session;

  String? _error;
  String? get error => _error;

  bool _busy = false;
  bool get busy => _busy;

  bool _initialized = false;
  bool get initialized => _initialized;

  // First-launch sequence: configure google_sign_in, then try to rehydrate
  // a stored JWT. If the stored JWT is past (now + slack), drop it — the
  // user will re-sign-in. We don't try to silently re-authenticate Google
  // here; the 30-day JWT window means it rarely matters, and silent
  // Google auth on Android can pop UI in some edge cases.
  Future<void> bootstrap() async {
    if (AppConfig.webClientId.isEmpty) {
      _error =
          'BELFRY_WEB_CLIENT_ID not set; build with '
          '--dart-define=BELFRY_WEB_CLIENT_ID=...';
      _initialized = true;
      notifyListeners();
      return;
    }
    try {
      await GoogleSignIn.instance.initialize(
        serverClientId: AppConfig.webClientId,
      );
    } catch (e) {
      _error = 'google_sign_in init failed: $e';
      _initialized = true;
      notifyListeners();
      return;
    }
    final stored = await _storage.read(key: _storageKey);
    if (stored != null) {
      try {
        final s = AuthSession.fromJson(
          jsonDecode(stored) as Map<String, dynamic>,
        );
        if (s.expiresAt.isAfter(
          DateTime.now().add(AppConfig.tokenRefreshSlack),
        )) {
          _session = s;
        } else {
          await _storage.delete(key: _storageKey);
        }
      } catch (_) {
        // Corrupt stored blob — just nuke it.
        await _storage.delete(key: _storageKey);
      }
    }
    _initialized = true;
    notifyListeners();
  }

  Future<void> signIn() async {
    _busy = true;
    _error = null;
    notifyListeners();
    try {
      final account = await GoogleSignIn.instance.authenticate();
      final idToken = account.authentication.idToken;
      if (idToken == null || idToken.isEmpty) {
        throw StateError('Google returned no ID token');
      }
      final resp = await http.post(
        Uri.parse('${AppConfig.backendBase}/auth/exchange'),
        headers: const {'Content-Type': 'application/json'},
        body: jsonEncode({'id_token': idToken}),
      );
      if (resp.statusCode != 200) {
        throw StateError(
          '/auth/exchange ${resp.statusCode}: ${resp.body}',
        );
      }
      final body = jsonDecode(resp.body) as Map<String, dynamic>;
      final session = AuthSession(
        email: body['email'] as String,
        token: body['token'] as String,
        expiresAt: DateTime.fromMillisecondsSinceEpoch(
          (body['expires_at'] as int) * 1000,
        ),
      );
      await _storage.write(
        key: _storageKey,
        value: jsonEncode(session.toJson()),
      );
      _session = session;
    } catch (e) {
      _error = e.toString();
    } finally {
      _busy = false;
      notifyListeners();
    }
  }

  Future<void> signOut() async {
    // Clear our session first — even if the Google side errors, the user
    // is signed out of belfry the moment we drop the JWT.
    await _storage.delete(key: _storageKey);
    _session = null;
    notifyListeners();
    try {
      await GoogleSignIn.instance.signOut();
    } catch (_) {
      // Best-effort; Credential Manager handles sign-out semantics
      // differently across versions and this is rarely the user's
      // problem.
    }
  }
}
