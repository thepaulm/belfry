import 'dart:async';
import 'dart:convert';
import 'dart:math' as math;

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;

import 'auth.dart';
import 'config.dart';

class Detection {
  const Detection({
    required this.x1,
    required this.y1,
    required this.x2,
    required this.y2,
    required this.cls,
    required this.conf,
  });
  // Normalized 0..1 frame coords.
  final double x1, y1, x2, y2;
  final String cls;
  final double conf;

  static Detection? fromArray(List<dynamic> a) {
    if (a.length < 6) return null;
    try {
      return Detection(
        x1: (a[0] as num).toDouble(),
        y1: (a[1] as num).toDouble(),
        x2: (a[2] as num).toDouble(),
        y2: (a[3] as num).toDouble(),
        cls: a[4] as String,
        conf: (a[5] as num).toDouble(),
      );
    } catch (_) {
      return null;
    }
  }
}

/// Subscribes to `/api/inference/live?cam=X` and exposes a stream of
/// detection batches. SSE payloads look like:
///   data: {"ts": 1.0, "boxes": [[x1,y1,x2,y2,cls,conf], ...]}
/// separated by blank lines, with `: ping` comment heartbeats every 15s.
class InferenceLiveClient {
  InferenceLiveClient({required this.auth, required this.cam});
  final AuthService auth;
  final String cam;

  final StreamController<List<Detection>> _ctrl =
      StreamController<List<Detection>>.broadcast();
  Stream<List<Detection>> get stream => _ctrl.stream;

  http.Client? _http;
  bool _disposed = false;
  bool _running = false;
  Timer? _retry;

  static const _retryDelay = Duration(seconds: 2);

  void start() {
    if (_disposed || _running) return;
    _running = true;
    debugPrint('[belfry][inf:$cam] start');
    _connect();
  }

  void stop() {
    if (!_running) return;
    _running = false;
    debugPrint('[belfry][inf:$cam] stop');
    _retry?.cancel();
    _retry = null;
    _http?.close();
    _http = null;
  }

  Future<void> _connect() async {
    final s = auth.session;
    if (s == null) {
      _scheduleRetry();
      return;
    }
    final uri = Uri.parse(
      '${AppConfig.backendBase}/api/inference/live?cam=$cam',
    );
    final req = http.Request('GET', uri);
    req.headers['Authorization'] = 'Bearer ${s.token}';
    req.headers['Accept'] = 'text/event-stream';
    _http = http.Client();
    try {
      final resp = await _http!.send(req);
      debugPrint('[belfry][inf:$cam] connect status=${resp.statusCode}');
      if (resp.statusCode != 200) {
        _scheduleRetry();
        return;
      }
      // Parser state — SSE events end at a blank line. Heartbeat comments
      // (lines starting with ':') are ignored. Multi-line `data:` events
      // would join with '\n' but the server only emits single-line data
      // here, so we just process line by line.
      final buf = StringBuffer();
      await for (final chunk in resp.stream.transform(utf8.decoder)) {
        if (_disposed || !_running) return;
        buf.write(chunk);
        while (true) {
          final s = buf.toString();
          final i = s.indexOf('\n\n');
          if (i < 0) break;
          final event = s.substring(0, i);
          final remaining = s.substring(i + 2);
          buf.clear();
          buf.write(remaining);
          _handleEvent(event);
        }
      }
      // Stream closed cleanly — reconnect unless we were asked to stop.
      _scheduleRetry();
    } catch (_) {
      _scheduleRetry();
    }
  }

  void _handleEvent(String event) {
    for (final line in event.split('\n')) {
      final l = line.trimRight();
      if (l.isEmpty || l.startsWith(':')) continue;
      if (!l.startsWith('data:')) continue;
      final data = l.substring(5).trim();
      Map<String, dynamic> payload;
      try {
        payload = jsonDecode(data) as Map<String, dynamic>;
      } catch (_) {
        continue;
      }
      final raw = payload['boxes'];
      final dets = <Detection>[];
      if (raw is List) {
        for (final b in raw) {
          if (b is List) {
            final d = Detection.fromArray(b);
            if (d != null) dets.add(d);
          }
        }
      }
      if (!_ctrl.isClosed) _ctrl.add(dets);
    }
  }

  void _scheduleRetry() {
    _http?.close();
    _http = null;
    if (_disposed || !_running) return;
    _retry?.cancel();
    _retry = Timer(_retryDelay, _connect);
  }

  void dispose() {
    _disposed = true;
    _running = false;
    _retry?.cancel();
    _http?.close();
    _ctrl.close();
  }
}

/// Per-frame box sample from past-mode re-inference. ts is unix epoch
/// seconds; boxes are normalized 0..1 frame coords.
class _PastSample {
  const _PastSample(this.ts, this.boxes);
  final double ts;
  final List<Detection> boxes;
}

/// Subscribes to `/api/inference/playback?cam=&start=&duration=` for a
/// past mp4 window and accumulates per-frame box samples. Look up the
/// (interpolated) boxes for a playback timestamp with [boxesAt].
///
/// Unlike the live client, this is one-shot: the server side runs the
/// detector over the recorded mp4 at ~1 fps and emits boxes as fast as
/// the GPU can produce them, then closes the stream. No reconnect.
class InferencePlaybackClient {
  InferencePlaybackClient({
    required this.auth,
    required this.cam,
    required this.windowStart,
    required this.duration,
  });
  final AuthService auth;
  final String cam;
  final DateTime windowStart;
  final Duration duration;

  // Sorted by ts. Append-only as samples arrive; on the timescales
  // we care about (a 5-min window = ~300 samples), linear append +
  // binary lookup is fine.
  final List<_PastSample> _samples = [];
  // Notifies on every new sample so the UI can re-look-up the current
  // position's boxes — the player's position keeps advancing
  // independently of arrival timing.
  final StreamController<void> _ctrl = StreamController<void>.broadcast();
  Stream<void> get onUpdate => _ctrl.stream;

  http.Client? _http;
  bool _disposed = false;

  void start() {
    if (_disposed) return;
    debugPrint(
      '[belfry][inf-pb:$cam] start ${windowStart.toIso8601String()} +${duration.inSeconds}s',
    );
    _connect();
  }

  /// Boxes to draw at [tsUnix]. Mirrors dvr/static/overlay.js: take the
  /// latest sample at/before [tsUnix] and, when the next sample is within
  /// [_tolerance], interpolate matched boxes between the two so they glide
  /// instead of stepping at 1 fps. Empty once samples stop (stream end/stall).
  List<Detection> boxesAt(double tsUnix) {
    if (_samples.isEmpty) return const [];
    // First index with ts > tsUnix + slop; the sample before it is `a`. The
    // slop lets a freshly opened window draw its first sample despite mp4
    // startup lag. Picking "closest" instead made boxes lead the object.
    var lo = 0, hi = _samples.length;
    final key = tsUnix + 0.05;
    while (lo < hi) {
      final mid = (lo + hi) >> 1;
      if (_samples[mid].ts <= key) {
        lo = mid + 1;
      } else {
        hi = mid;
      }
    }
    final ai = lo - 1;
    if (ai < 0) return const [];
    final a = _samples[ai];
    if (tsUnix - a.ts > _tolerance) return const [];
    if (ai + 1 >= _samples.length) return a.boxes;
    final b = _samples[ai + 1];
    final span = b.ts - a.ts;
    if (span <= 0 || span > _tolerance) return a.boxes;
    final f = ((tsUnix - a.ts) / span).clamp(0.0, 1.0);
    if (f == 0) return a.boxes;
    return interpolateDetections(a.boxes, b.boxes, f);
  }

  // Hold each 1 fps sample until the next one; must exceed the sample period.
  static const double _tolerance = 1.5;

  Future<void> _connect() async {
    final s = auth.session;
    if (s == null) return;
    final secs = duration.inMicroseconds / 1e6;
    final uri = Uri.parse(
      '${AppConfig.backendBase}/api/inference/playback',
    ).replace(queryParameters: {
      'cam': cam,
      'start': windowStart.toUtc().toIso8601String(),
      'duration': '${secs}s',
    });
    final req = http.Request('GET', uri);
    req.headers['Authorization'] = 'Bearer ${s.token}';
    req.headers['Accept'] = 'text/event-stream';
    _http = http.Client();
    try {
      final resp = await _http!.send(req);
      debugPrint('[belfry][inf-pb:$cam] connect status=${resp.statusCode}');
      if (resp.statusCode != 200) return;
      final buf = StringBuffer();
      await for (final chunk in resp.stream.transform(utf8.decoder)) {
        if (_disposed) return;
        buf.write(chunk);
        while (true) {
          final s = buf.toString();
          final i = s.indexOf('\n\n');
          if (i < 0) break;
          final event = s.substring(0, i);
          final remaining = s.substring(i + 2);
          buf.clear();
          buf.write(remaining);
          _handleEvent(event);
        }
      }
      debugPrint(
        '[belfry][inf-pb:$cam] stream ended (${_samples.length} samples)',
      );
    } catch (e) {
      debugPrint('[belfry][inf-pb:$cam] connect error: $e');
    }
  }

  void _handleEvent(String event) {
    for (final line in event.split('\n')) {
      final l = line.trimRight();
      if (l.isEmpty || l.startsWith(':')) continue;
      if (!l.startsWith('data:')) continue;
      final data = l.substring(5).trim();
      Map<String, dynamic> payload;
      try {
        payload = jsonDecode(data) as Map<String, dynamic>;
      } catch (_) {
        continue;
      }
      final ts = (payload['ts'] as num?)?.toDouble();
      if (ts == null) continue;
      final raw = payload['boxes'];
      final dets = <Detection>[];
      if (raw is List) {
        for (final b in raw) {
          if (b is List) {
            final d = Detection.fromArray(b);
            if (d != null) dets.add(d);
          }
        }
      }
      // Keep _samples sorted. The server emits in order, but be safe.
      if (_samples.isEmpty || _samples.last.ts <= ts) {
        _samples.add(_PastSample(ts, dets));
      } else {
        final insert = _samples.indexWhere((s) => s.ts > ts);
        _samples.insert(insert < 0 ? _samples.length : insert,
            _PastSample(ts, dets));
      }
      if (!_ctrl.isClosed) _ctrl.add(null);
    }
  }

  void dispose() {
    _disposed = true;
    _http?.close();
    _http = null;
    _ctrl.close();
  }
}

double _iou(Detection p, Detection q) {
  final ix = (math.min(p.x2, q.x2) - math.max(p.x1, q.x1)).clamp(0.0, 1.0);
  final iy = (math.min(p.y2, q.y2) - math.max(p.y1, q.y1)).clamp(0.0, 1.0);
  final inter = ix * iy;
  final u = (p.x2 - p.x1) * (p.y2 - p.y1) + (q.x2 - q.x1) * (q.y2 - q.y1) - inter;
  return u > 0 ? inter / u : 0;
}

// Max normalized center travel between two 1 fps samples that still counts
// as the same object; matches _MAX_MATCH_DIST in overlay.js.
const double _maxMatchDist = 0.35;

/// Blend boxes from sample a toward sample b by [f] in [0,1]. Each a-box is
/// greedily paired with the same-class b-box of highest IoU (falling back to
/// nearest center within [_maxMatchDist] for fast movers with no overlap).
/// Unmatched a-boxes hold in place; unmatched b-boxes appear at b's time.
List<Detection> interpolateDetections(
    List<Detection> a, List<Detection> b, double f) {
  final pairs = <(int, int, double)>[];
  for (var i = 0; i < a.length; i++) {
    for (var j = 0; j < b.length; j++) {
      final p = a[i], q = b[j];
      if (p.cls != q.cls) continue;
      final iou = _iou(p, q);
      final dx = (p.x1 + p.x2 - q.x1 - q.x2) / 2;
      final dy = (p.y1 + p.y2 - q.y1 - q.y2) / 2;
      final dist = math.sqrt(dx * dx + dy * dy);
      if (iou <= 0 && dist > _maxMatchDist) continue;
      pairs.add((i, j, iou > 0 ? 1 + iou : 1 - dist));
    }
  }
  pairs.sort((x, y) => y.$3.compareTo(x.$3));
  final usedA = <int>{}, usedB = <int>{};
  final out = <Detection>[];
  double lerp(double x, double y) => x + (y - x) * f;
  for (final (i, j, _) in pairs) {
    if (usedA.contains(i) || usedB.contains(j)) continue;
    usedA.add(i);
    usedB.add(j);
    final p = a[i], q = b[j];
    out.add(Detection(
      x1: lerp(p.x1, q.x1),
      y1: lerp(p.y1, q.y1),
      x2: lerp(p.x2, q.x2),
      y2: lerp(p.y2, q.y2),
      cls: p.cls,
      conf: lerp(p.conf, q.conf),
    ));
  }
  for (var i = 0; i < a.length; i++) {
    if (!usedA.contains(i)) out.add(a[i]);
  }
  return out;
}

// Colors mirror dvr/static/overlay.js's CLASS_COLOR so live boxes look
// identical between web and mobile.
const _classColor = <String, Color>{
  'person': Color(0xff4ea1ff),
  'animal': Color(0xff5ad17c),
  'dog': Color(0xff5ad17c),
  'cat': Color(0xff5ad17c),
  'bird': Color(0xff5ad17c),
  'vehicle': Color(0xffff9b3f),
  'car': Color(0xffff9b3f),
  'truck': Color(0xffff9b3f),
  'motion': Color(0xffe879f9),
};
const _defaultColor = Color(0xffaaaaaa);

Color _colorFor(String cls) => _classColor[cls] ?? _defaultColor;

/// Paints normalized [Detection] boxes over the video frame. Boxes are
/// normalized to the frame, so when the host crops or letterboxes the video
/// pass [videoSize] + [fit] (same as the video's FittedBox) and boxes map
/// onto the painted rect; without them the frame is assumed to fill the host.
/// Pass [listenable] to repaint without rebuilding (per-frame interpolation).
class BoxOverlay extends StatelessWidget {
  const BoxOverlay({
    super.key,
    this.detections = const [],
    this.listenable,
    this.videoSize,
    this.fit = BoxFit.fill,
  });
  final List<Detection> detections;
  final ValueListenable<List<Detection>>? listenable;
  final Size? videoSize;
  final BoxFit fit;

  @override
  Widget build(BuildContext context) {
    return IgnorePointer(
      // Boxes shouldn't swallow taps — the tile is supposed to remain
      // tappable to open the playback screen.
      child: CustomPaint(
        painter: _BoxPainter(detections, listenable, videoSize, fit),
        child: const SizedBox.expand(),
      ),
    );
  }
}

class _BoxPainter extends CustomPainter {
  _BoxPainter(this._detections, this.listenable, this.videoSize, this.fit)
      : super(repaint: listenable);
  final List<Detection> _detections;
  final ValueListenable<List<Detection>>? listenable;
  final Size? videoSize;
  final BoxFit fit;

  List<Detection> get detections => listenable?.value ?? _detections;

  // Rect within [size] where the video frame is painted.
  Rect _frameRect(Size size) {
    final vs = videoSize;
    if (vs == null || vs.isEmpty) return Offset.zero & size;
    final fitted = applyBoxFit(fit, vs, size).destination;
    return Alignment.center.inscribe(fitted, Offset.zero & size);
  }

  @override
  void paint(Canvas canvas, Size size) {
    // Stroke + font scale a bit with tile size so labels stay readable
    // on the small live-grid tiles without blowing up on full-screen
    // playback.
    final scale = (size.shortestSide / 240).clamp(0.7, 1.6);
    final strokeWidth = 2.0 * scale;
    final labelFontSize = 11.0 * scale;
    final stroke = Paint()
      ..style = PaintingStyle.stroke
      ..strokeWidth = strokeWidth;
    final fill = Paint()..style = PaintingStyle.fill;

    final fr = _frameRect(size);
    canvas.save();
    canvas.clipRect(Offset.zero & size);
    for (final d in detections) {
      final r = Rect.fromLTRB(
        fr.left + d.x1 * fr.width,
        fr.top + d.y1 * fr.height,
        fr.left + d.x2 * fr.width,
        fr.top + d.y2 * fr.height,
      );
      final color = _colorFor(d.cls);
      stroke.color = color;
      if (d.cls == 'motion') {
        _drawDashedRect(canvas, r, stroke, dash: 6 * scale, gap: 4 * scale);
        continue;
      }
      canvas.drawRect(r, stroke);

      // Label chip: "<class> 0.NN" above the box (or below if it would
      // clip the top of the canvas).
      final label = '${d.cls} ${d.conf.toStringAsFixed(2)}';
      final tp = TextPainter(
        text: TextSpan(
          text: label,
          style: TextStyle(
            color: const Color(0xff06121f),
            fontSize: labelFontSize,
            fontWeight: FontWeight.w600,
          ),
        ),
        textDirection: TextDirection.ltr,
      )..layout();
      final padX = 4.0 * scale, padY = 2.0 * scale;
      final chipW = tp.width + padX * 2;
      final chipH = tp.height + padY * 2;
      double chipY = r.top - chipH;
      if (chipY < 0) chipY = r.top;
      final chipRect = Rect.fromLTWH(r.left, chipY, chipW, chipH);
      fill.color = color;
      canvas.drawRect(chipRect, fill);
      tp.paint(canvas, Offset(r.left + padX, chipY + padY));
    }
    canvas.restore();
  }

  void _drawDashedRect(Canvas canvas, Rect r, Paint p,
      {required double dash, required double gap}) {
    void seg(Offset a, Offset b) {
      final dx = b.dx - a.dx, dy = b.dy - a.dy;
      final len = (dx * dx + dy * dy).abs();
      final total = (len > 0 ? (dx * dx + dy * dy) : 0).toDouble();
      if (total == 0) return;
      final dist = (dx.abs() + dy.abs());
      final unitX = dx == 0 ? 0.0 : (dx > 0 ? 1.0 : -1.0);
      final unitY = dy == 0 ? 0.0 : (dy > 0 ? 1.0 : -1.0);
      var pos = 0.0;
      while (pos < dist) {
        final next = (pos + dash).clamp(0.0, dist);
        canvas.drawLine(
          Offset(a.dx + unitX * pos, a.dy + unitY * pos),
          Offset(a.dx + unitX * next, a.dy + unitY * next),
          p,
        );
        pos = next + gap;
      }
    }

    seg(r.topLeft, r.topRight);
    seg(r.topRight, r.bottomRight);
    seg(r.bottomLeft, r.bottomRight);
    seg(r.topLeft, r.bottomLeft);
  }

  @override
  bool shouldRepaint(_BoxPainter old) =>
      !identical(old._detections, _detections) ||
      old.listenable != listenable ||
      old.videoSize != videoSize ||
      old.fit != fit;
}
