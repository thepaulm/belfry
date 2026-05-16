import 'dart:convert';

import 'package:http/http.dart' as http;

import 'auth.dart';
import 'config.dart';

class CameraSet {
  CameraSet({required this.id, required this.label, required this.cameraCount});
  final String id;
  final String label;
  final int cameraCount;

  factory CameraSet.fromJson(Map<String, dynamic> j) => CameraSet(
    id: j['id'] as String,
    label: j['label'] as String,
    cameraCount: j['camera_count'] as int,
  );
}

class Camera {
  Camera({
    required this.name,
    required this.label,
    required this.enabled,
    this.hlsPath,
  });

  final String name;
  final String label;
  final bool enabled;

  // Backend returns hls_url like "/hls/cam5/index.m3u8" (or null if the
  // camera is disabled). We don't store it as a full URL because the
  // bearer header has to be paired with the absolute URL at fetch time,
  // and that pairing happens in LiveGridScreen — see hlsUrl() below.
  final String? hlsPath;

  String? hlsUrl() =>
      hlsPath == null ? null : '${AppConfig.backendBase}$hlsPath';

  factory Camera.fromJson(Map<String, dynamic> j) => Camera(
    name: j['name'] as String,
    label: j['label'] as String,
    enabled: j['enabled'] as bool,
    hlsPath: j['hls_url'] as String?,
  );
}

class ApiException implements Exception {
  ApiException(this.statusCode, this.message);
  final int statusCode;
  final String message;
  @override
  String toString() => 'ApiException($statusCode): $message';
}

class ApiClient {
  ApiClient(this._auth);
  final AuthService _auth;

  Future<List<CameraSet>> getSets() async {
    final body = await _getJson('/api/sets');
    return (body as List).map((e) => CameraSet.fromJson(e)).toList();
  }

  Future<List<Camera>> getSetCameras(String setId) async {
    final body = await _getJson('/api/sets/$setId/cameras');
    return (body as List).map((e) => Camera.fromJson(e)).toList();
  }

  // The bearer headers map that callers (notably the VideoPlayer
  // controllers) inject directly. Exposed so each controller can paste
  // it onto the HLS request without going through this class.
  Map<String, String> bearerHeaders() {
    final s = _auth.session;
    if (s == null) return const {};
    return {'Authorization': 'Bearer ${s.token}'};
  }

  Future<dynamic> _getJson(String path) async {
    final uri = Uri.parse('${AppConfig.backendBase}$path');
    final resp = await http.get(uri, headers: bearerHeaders());
    if (resp.statusCode != 200) {
      throw ApiException(resp.statusCode, resp.body);
    }
    return jsonDecode(resp.body);
  }
}
