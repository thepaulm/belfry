import 'dart:async';

import 'package:flutter/material.dart';
import 'package:video_player/video_player.dart';

import 'api.dart';
import 'auth.dart';

class PlaybackScreen extends StatefulWidget {
  const PlaybackScreen({
    super.key,
    required this.auth,
    required this.camera,
  });
  final AuthService auth;
  final Camera camera;

  String get cameraName => camera.name;
  String get cameraLabel => camera.label;

  @override
  State<PlaybackScreen> createState() => _PlaybackScreenState();
}

class _PlaybackScreenState extends State<PlaybackScreen> {
  late final ApiClient _api = ApiClient(widget.auth);

  final DateTime _today = _localStartOfDay(DateTime.now());
  late DateTime _selectedDay = _today;

  // Slider value: seconds since local start-of-day, 0..86400.
  late double _sliderSec = _initialSliderSec();

  List<PlaybackRange> _ranges = const [];
  String? _loadError;
  bool _loading = true;

  // Video state. _windowStart marks where the loaded mp4 begins on the
  // absolute timeline; we use it together with the controller's position
  // to drive the cursor while playing back.
  static const _windowDuration = Duration(minutes: 5);
  static const _liveSnapWindow = Duration(seconds: 30);
  VideoPlayerController? _player;
  DateTime? _windowStart;
  bool _playerLoading = false;
  String? _playerError;
  bool _userScrubbing = false;
  bool _isLive = false;
  int _loadGen = 0;
  Timer? _liveTick;

  double _initialSliderSec() {
    final now = DateTime.now();
    return now.difference(_today).inSeconds.toDouble().clamp(0.0, 86399.0);
  }

  @override
  void initState() {
    super.initState();
    _loadList();
    // Default entry is the live stream — the user just tapped a live tile
    // and the past timeline is supporting context, not the primary view.
    _enterLive();
  }

  Future<void> _loadList() async {
    try {
      final r = await _api.getPlaybackList(widget.cameraName);
      if (!mounted) return;
      setState(() {
        _ranges = r;
        _loading = false;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _loadError = e.toString();
        _loading = false;
      });
    }
  }

  bool _dayHasFootage(DateTime day) {
    final start = day;
    final end = day.add(const Duration(days: 1));
    for (final r in _ranges) {
      if (r.end.isAfter(start) && r.start.isBefore(end)) return true;
    }
    return false;
  }

  DateTime _absoluteTime(double sliderSec) =>
      _selectedDay.add(Duration(milliseconds: (sliderSec * 1000).round()));

  bool _absTimeHasFootage(DateTime t) {
    for (final r in _ranges) {
      if (!t.isBefore(r.start) && t.isBefore(r.end)) return true;
    }
    return false;
  }

  Future<void> _loadWindowAt(double sliderSec) async {
    final start = _absoluteTime(sliderSec);
    final now = DateTime.now();

    // Snap to live whenever the 5-min mp4 window would extend past now.
    // MediaMTX can't serve frames it hasn't recorded yet, and a window that
    // straddles the live edge errors instead of returning a partial mp4.
    if (start.add(_windowDuration).isAfter(now.subtract(_liveSnapWindow))) {
      await _enterLive();
      return;
    }

    if (!_absTimeHasFootage(start)) {
      // Slider landed in a gap or before retention. Skip the fetch — the
      // server's 404 surfaces in video_player as an opaque PlatformException.
      await _stopPlayer();
      setState(() => _playerError = 'no footage at this time');
      return;
    }

    // If the new time falls inside the loaded window, seek instead of
    // refetching — saves a roundtrip and the iOS-Safari cache step on the
    // server side.
    final cur = _player;
    final wStart = _windowStart;
    if (!_isLive &&
        cur != null &&
        cur.value.isInitialized &&
        wStart != null &&
        !start.isBefore(wStart) &&
        start.isBefore(wStart.add(_windowDuration))) {
      await cur.seekTo(start.difference(wStart));
      await cur.play();
      return;
    }

    final gen = ++_loadGen;
    setState(() {
      _playerLoading = true;
      _playerError = null;
      _isLive = false;
    });
    _liveTick?.cancel();
    _liveTick = null;

    final uri = _api.playbackMp4Uri(widget.cameraName, start, _windowDuration);
    final headers = _api.bearerHeaders();
    final c = VideoPlayerController.networkUrl(uri, httpHeaders: headers);
    try {
      await c.initialize();
      if (!mounted || gen != _loadGen) {
        await c.dispose();
        return;
      }
      await c.setLooping(false);
      await c.play();
      c.addListener(_onPlayerUpdate);

      final old = _player;
      _player = c;
      _windowStart = start;
      _playerLoading = false;
      _playerError = null;
      if (mounted) setState(() {});
      if (old != null) {
        old.removeListener(_onPlayerUpdate);
        unawaited(old.dispose());
      }
    } catch (e) {
      await c.dispose();
      if (!mounted || gen != _loadGen) return;
      setState(() {
        _playerLoading = false;
        _playerError = e.toString();
      });
    }
  }

  Future<void> _stopPlayer() async {
    _liveTick?.cancel();
    _liveTick = null;
    final old = _player;
    _player = null;
    _windowStart = null;
    _isLive = false;
    _playerLoading = false;
    if (old != null) {
      old.removeListener(_onPlayerUpdate);
      await old.dispose();
    }
  }

  Future<void> _enterLive({int attempt = 0}) async {
    // Already streaming live and healthy — don't tear down the working
    // controller just to re-init it.
    final cur = _player;
    if (_isLive &&
        cur != null &&
        cur.value.isInitialized &&
        !cur.value.hasError) {
      // Make sure the cursor is pinned to now even on a no-op call.
      final now = DateTime.now();
      setState(() {
        _selectedDay = _today;
        _sliderSec =
            now.difference(_today).inSeconds.toDouble().clamp(0.0, 86399.0);
      });
      return;
    }

    final hls = widget.camera.hlsUrl();
    if (hls == null) {
      setState(() => _playerError = 'camera is disabled');
      return;
    }

    final gen = ++_loadGen;
    setState(() {
      _playerLoading = true;
      _playerError = null;
      _selectedDay = _today;
      _sliderSec = DateTime.now().difference(_today).inSeconds.toDouble().clamp(
            0.0,
            86399.0,
          );
    });

    final c = VideoPlayerController.networkUrl(
      Uri.parse(hls),
      httpHeaders: _api.bearerHeaders(),
    );
    try {
      await c.initialize();
      if (!mounted || gen != _loadGen) {
        await c.dispose();
        return;
      }
      await c.setLooping(false);
      await c.play();

      final old = _player;
      _player = c;
      _windowStart = null;
      _playerLoading = false;
      _isLive = true;
      if (mounted) setState(() {});
      if (old != null) {
        old.removeListener(_onPlayerUpdate);
        unawaited(old.dispose());
      }

      _liveTick?.cancel();
      _liveTick = Timer.periodic(const Duration(seconds: 1), (_) {
        if (!mounted || !_isLive || _userScrubbing) return;
        final now = DateTime.now();
        final today = _localStartOfDay(now);
        setState(() {
          if (!today.isAtSameMomentAs(_selectedDay)) _selectedDay = today;
          _sliderSec =
              now.difference(today).inSeconds.toDouble().clamp(0.0, 86399.0);
        });
      });
    } catch (e) {
      await c.dispose();
      if (!mounted || gen != _loadGen) return;
      // Live HLS init can flake on first try — MediaMTX takes a beat to
      // mux a playlist after the path comes up. One quick retry covers it.
      if (attempt == 0) {
        await Future<void>.delayed(const Duration(seconds: 1));
        if (!mounted || gen != _loadGen) return;
        return _enterLive(attempt: 1);
      }
      setState(() {
        _playerLoading = false;
        _playerError = e.toString();
      });
    }
  }

  void _onPlayerUpdate() {
    final c = _player;
    final wStart = _windowStart;
    if (c == null || wStart == null || !c.value.isInitialized) return;
    if (c.value.hasError) {
      setState(() {
        _playerError = c.value.errorDescription ?? 'playback error';
      });
      return;
    }
    if (_userScrubbing) return;
    // Cursor follows the playing video. Clamp to the selected day so the
    // slider stays valid when a window straddles midnight.
    final absolute = wStart.add(c.value.position);
    final sec = absolute.difference(_selectedDay).inMilliseconds / 1000.0;
    if (sec < 0 || sec > 86400) return;
    if ((sec - _sliderSec).abs() < 0.05) return;
    setState(() => _sliderSec = sec);
  }

  @override
  void dispose() {
    _liveTick?.cancel();
    _player?.removeListener(_onPlayerUpdate);
    _player?.dispose();
    super.dispose();
  }

  List<_Segment> _segmentsForSelectedDay() {
    final dayStart = _selectedDay;
    final dayEnd = _selectedDay.add(const Duration(days: 1));
    final out = <_Segment>[];
    for (final r in _ranges) {
      if (!r.end.isAfter(dayStart) || !r.start.isBefore(dayEnd)) continue;
      final s = r.start.isBefore(dayStart) ? dayStart : r.start;
      final e = r.end.isAfter(dayEnd) ? dayEnd : r.end;
      out.add(_Segment(
        startFrac: s.difference(dayStart).inMilliseconds / 1000 / 86400,
        endFrac: e.difference(dayStart).inMilliseconds / 1000 / 86400,
      ));
    }
    return out;
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: Text(widget.cameraLabel),
        actions: [
          IconButton(
            icon: const Icon(Icons.sensors),
            tooltip: 'Go Live',
            onPressed: _isLive ? null : () => _enterLive(),
          ),
        ],
      ),
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : _loadError != null
              ? Center(
                  child: Padding(
                    padding: const EdgeInsets.all(24),
                    child: Text('failed to load: $_loadError'),
                  ),
                )
              : Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    const SizedBox(height: 8),
                    _DayStrip(
                      today: _today,
                      selectedDay: _selectedDay,
                      hasFootage: _dayHasFootage,
                      onSelect: (d) => setState(() => _selectedDay = d),
                    ),
                    const SizedBox(height: 8),
                    _Timeline(
                      sliderSec: _sliderSec,
                      segments: _segmentsForSelectedDay(),
                      timeLabel: _formatHms(_sliderSec),
                      isLive: _isLive,
                      onChangeStart: (_) => _userScrubbing = true,
                      onChanged: (v) => setState(() => _sliderSec = v),
                      onChangeEnd: (v) {
                        _userScrubbing = false;
                        _loadWindowAt(v);
                      },
                    ),
                    Expanded(child: _videoArea()),
                  ],
                ),
    );
  }

  Widget _videoArea() {
    return ColoredBox(
      color: Colors.black,
      child: Center(child: _videoContent()),
    );
  }

  Widget _videoContent() {
    if (_playerError != null) {
      return Padding(
        padding: const EdgeInsets.all(12),
        child: Text(
          _playerError!,
          textAlign: TextAlign.center,
          style: const TextStyle(color: Colors.white54, fontSize: 12),
        ),
      );
    }
    if (_playerLoading) {
      return const SizedBox(
        width: 28,
        height: 28,
        child: CircularProgressIndicator(strokeWidth: 2),
      );
    }
    final c = _player;
    if (c == null || !c.value.isInitialized) {
      return const Text(
        'drag the scrubber to load footage',
        style: TextStyle(color: Colors.white38, fontSize: 12),
      );
    }
    return AspectRatio(
      aspectRatio: c.value.aspectRatio == 0 ? 16 / 9 : c.value.aspectRatio,
      child: VideoPlayer(c),
    );
  }
}

DateTime _localStartOfDay(DateTime d) {
  final local = d.toLocal();
  return DateTime(local.year, local.month, local.day);
}

String _formatHms(double sec) {
  final s = sec.round();
  final h = (s ~/ 3600).toString().padLeft(2, '0');
  final m = ((s % 3600) ~/ 60).toString().padLeft(2, '0');
  final ss = (s % 60).toString().padLeft(2, '0');
  return '$h:$m:$ss';
}

class _Segment {
  const _Segment({required this.startFrac, required this.endFrac});
  final double startFrac;
  final double endFrac;
}

class _DayStrip extends StatelessWidget {
  const _DayStrip({
    required this.today,
    required this.selectedDay,
    required this.hasFootage,
    required this.onSelect,
  });
  final DateTime today;
  final DateTime selectedDay;
  final bool Function(DateTime) hasFootage;
  final ValueChanged<DateTime> onSelect;

  static const _weekdays = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      height: 64,
      child: ListView.builder(
        scrollDirection: Axis.horizontal,
        reverse: true, // today (i=0) lands on the right
        padding: const EdgeInsets.symmetric(horizontal: 8),
        itemCount: 14,
        itemBuilder: (ctx, i) {
          final date = today.subtract(Duration(days: i));
          final has = hasFootage(date);
          final selected = date.isAtSameMomentAs(selectedDay);
          return GestureDetector(
            onTap: () => onSelect(date),
            child: Container(
              width: 52,
              margin: const EdgeInsets.symmetric(horizontal: 3, vertical: 4),
              decoration: BoxDecoration(
                color: selected
                    ? Colors.orange.withValues(alpha: 0.18)
                    : null,
                border: Border.all(
                  color: selected ? Colors.orange : Colors.white24,
                ),
                borderRadius: BorderRadius.circular(6),
              ),
              child: Column(
                mainAxisAlignment: MainAxisAlignment.center,
                children: [
                  Text(
                    _weekdays[date.weekday - 1],
                    style: const TextStyle(
                      fontSize: 11,
                      color: Colors.white70,
                    ),
                  ),
                  const SizedBox(height: 2),
                  Text(
                    '${date.day}',
                    style: const TextStyle(fontSize: 18),
                  ),
                  const SizedBox(height: 2),
                  Text(
                    has ? '●' : '○',
                    style: TextStyle(
                      fontSize: 10,
                      color: has
                          ? const Color(0xff9fb3c8)
                          : Colors.white24,
                    ),
                  ),
                ],
              ),
            ),
          );
        },
      ),
    );
  }
}

class _Timeline extends StatelessWidget {
  const _Timeline({
    required this.sliderSec,
    required this.segments,
    required this.timeLabel,
    required this.isLive,
    required this.onChangeStart,
    required this.onChanged,
    required this.onChangeEnd,
  });
  final double sliderSec;
  final List<_Segment> segments;
  final String timeLabel;
  final bool isLive;
  final ValueChanged<double> onChangeStart;
  final ValueChanged<double> onChanged;
  final ValueChanged<double> onChangeEnd;

  static const _dayLen = 86400.0;
  static const _horizontalInset = 20.0;
  static const _thumbRadius = 10.0;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 24),
          child: Row(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              if (isLive) ...[
                Container(
                  padding: const EdgeInsets.symmetric(
                    horizontal: 6,
                    vertical: 1,
                  ),
                  decoration: BoxDecoration(
                    color: Colors.red,
                    borderRadius: BorderRadius.circular(3),
                  ),
                  child: const Text(
                    'LIVE',
                    style: TextStyle(
                      fontSize: 10,
                      fontWeight: FontWeight.bold,
                      letterSpacing: 0.5,
                      color: Colors.white,
                    ),
                  ),
                ),
                const SizedBox(width: 8),
              ],
              Text(
                timeLabel,
                style: const TextStyle(
                  fontFamily: 'monospace',
                  fontSize: 14,
                  color: Colors.white70,
                ),
              ),
            ],
          ),
        ),
        const SizedBox(height: 4),
        LayoutBuilder(builder: (ctx, c) {
          final width = c.maxWidth;
          final usable = width - 2 * (_horizontalInset + _thumbRadius);
          final cursorX =
              _horizontalInset + _thumbRadius + (sliderSec / _dayLen) * usable;
          return SizedBox(
            height: 52,
            child: Stack(
              children: [
                // Availability bar — slate background with lighter segments
                // where footage exists. Inset matches the slider's thumb so
                // the cursor aligns with the bar at the extremes.
                Positioned(
                  left: _horizontalInset + _thumbRadius,
                  right: _horizontalInset + _thumbRadius,
                  top: 22,
                  child: SizedBox(
                    height: 8,
                    child: CustomPaint(
                      painter: _AvailabilityPainter(segments),
                    ),
                  ),
                ),
                // Vertical orange cursor line.
                Positioned(
                  left: cursorX - 1,
                  top: 6,
                  bottom: 6,
                  child: Container(width: 2, color: Colors.orange),
                ),
                Positioned.fill(
                  child: Padding(
                    padding: const EdgeInsets.symmetric(
                      horizontal: _horizontalInset,
                    ),
                    child: SliderTheme(
                      data: SliderTheme.of(ctx).copyWith(
                        activeTrackColor: Colors.transparent,
                        inactiveTrackColor: Colors.transparent,
                        thumbColor: Colors.orange,
                        overlayColor:
                            Colors.orange.withValues(alpha: 0.2),
                        thumbShape: const RoundSliderThumbShape(
                          enabledThumbRadius: _thumbRadius,
                        ),
                        trackHeight: 0,
                      ),
                      child: Slider(
                        min: 0,
                        max: _dayLen,
                        value: sliderSec.clamp(0.0, _dayLen),
                        onChangeStart: onChangeStart,
                        onChanged: onChanged,
                        onChangeEnd: onChangeEnd,
                      ),
                    ),
                  ),
                ),
              ],
            ),
          );
        }),
      ],
    );
  }
}

class _AvailabilityPainter extends CustomPainter {
  _AvailabilityPainter(this.segments);
  final List<_Segment> segments;

  static const _trackColor = Color(0xff4a525c);
  static const _segmentColor = Color(0xff9fb3c8);

  @override
  void paint(Canvas canvas, Size size) {
    final radius = const Radius.circular(2);
    canvas.drawRRect(
      RRect.fromRectAndRadius(Offset.zero & size, radius),
      Paint()..color = _trackColor,
    );
    final paint = Paint()..color = _segmentColor;
    for (final s in segments) {
      final left = s.startFrac.clamp(0.0, 1.0) * size.width;
      final right = s.endFrac.clamp(0.0, 1.0) * size.width;
      final w = (right - left).clamp(1.0, size.width);
      canvas.drawRRect(
        RRect.fromRectAndRadius(
          Rect.fromLTWH(left, 0, w, size.height),
          radius,
        ),
        paint,
      );
    }
  }

  @override
  bool shouldRepaint(_AvailabilityPainter old) => true;
}
