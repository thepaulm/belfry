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

class Event {
  Event({
    required this.id,
    required this.camera,
    required this.setId,
    required this.cls,
    required this.tsStart,
    required this.tsEnd,
    required this.maxConf,
    required this.hasThumb,
  });

  final int id;
  final String camera;
  final String? setId;
  final String cls;
  final DateTime tsStart;
  final DateTime tsEnd;
  final double maxConf;
  final bool hasThumb;

  Duration get duration => tsEnd.difference(tsStart);

  String? thumbUrl() =>
      hasThumb ? '${AppConfig.backendBase}/api/events/thumb/$id' : null;

  factory Event.fromJson(Map<String, dynamic> j) => Event(
    id: j['id'] as int,
    camera: j['camera'] as String,
    setId: j['set_id'] as String?,
    cls: j['class'] as String,
    tsStart: DateTime.fromMillisecondsSinceEpoch(
      ((j['ts_start'] as num).toDouble() * 1000).round(),
    ),
    tsEnd: DateTime.fromMillisecondsSinceEpoch(
      ((j['ts_end'] as num).toDouble() * 1000).round(),
    ),
    maxConf: (j['max_conf'] as num).toDouble(),
    hasThumb: j['thumb_url'] != null,
  );
}

class PlaybackRange {
  PlaybackRange({required this.start, required this.duration});

  // start is an ISO-8601 timestamp from MediaMTX. duration is seconds as a
  // JSON number (MediaMTX emits fractional float seconds).
  final DateTime start;
  final Duration duration;

  DateTime get end => start.add(duration);

  factory PlaybackRange.fromJson(Map<String, dynamic> j) => PlaybackRange(
    start: DateTime.parse(j['start'] as String),
    duration: Duration(
      microseconds: ((j['duration'] as num).toDouble() * 1e6).round(),
    ),
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

  Future<List<Camera>> getAllCameras() async {
    final body = await _getJson('/api/cameras');
    return (body as List).map((e) => Camera.fromJson(e)).toList();
  }

  Future<List<PlaybackRange>> getPlaybackList(String cam) async {
    final body = await _getJson('/api/playback/list?cam=$cam');
    return (body as List).map((e) => PlaybackRange.fromJson(e)).toList();
  }

  Future<List<Event>> getEvents({
    String? cam,
    String? cls,
    DateTime? since,
    DateTime? until,
    int? beforeId,
    int limit = 100,
  }) async {
    final p = <String, String>{'limit': '$limit'};
    if (cam != null) p['cam'] = cam;
    if (cls != null) p['class'] = cls;
    if (since != null) {
      p['since'] = (since.millisecondsSinceEpoch / 1000).toString();
    }
    if (until != null) {
      p['until'] = (until.millisecondsSinceEpoch / 1000).toString();
    }
    if (beforeId != null) p['before_id'] = '$beforeId';
    final uri = Uri.parse(
      '${AppConfig.backendBase}/api/events',
    ).replace(queryParameters: p);
    final resp = await http.get(uri, headers: bearerHeaders());
    if (resp.statusCode != 200) {
      throw ApiException(resp.statusCode, resp.body);
    }
    final body = jsonDecode(resp.body);
    return (body as List).map((e) => Event.fromJson(e)).toList();
  }

  // The video_player package handles the mp4 fetch itself — we just hand it
  // the URL and the bearer header. Duration is rendered the same way MediaMTX
  // expects it on the wire: "<seconds>s".
  Uri playbackMp4Uri(String cam, DateTime start, Duration duration) {
    final secs = (duration.inMicroseconds / 1e6);
    return Uri.parse('${AppConfig.backendBase}/api/playback/get').replace(
      queryParameters: {
        'cam': cam,
        'start': start.toUtc().toIso8601String(),
        'duration': '${secs}s',
      },
    );
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
