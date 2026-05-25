import 'dart:async';

import 'package:flutter/material.dart';
import 'package:video_player/video_player.dart';

import 'api.dart';
import 'auth.dart';
import 'events_screen.dart' show eventBucketColor;

class PlaybackScreen extends StatefulWidget {
  const PlaybackScreen({
    super.key,
    required this.auth,
    required this.camera,
    this.initialTs,
  });
  final AuthService auth;
  final Camera camera;
  // Absolute timestamp to land on. Set when navigating from the Events
  // tab so the user lands on the event's moment instead of the live edge.
  final DateTime? initialTs;

  String get cameraName => camera.name;
  String get cameraLabel => camera.label;

  @override
  State<PlaybackScreen> createState() => _PlaybackScreenState();
}

class _PlaybackScreenState extends State<PlaybackScreen> {
  late final ApiClient _api = ApiClient(widget.auth);

  // Visible-window model — mirrors the web playback page. _viewScale picks
  // the visible window size; _viewStartSec is its left edge, snapped to a
  // multiple of the span so neighboring scale steps line up.
  static const _scaleSpans = <String, double>{
    'day': 86400.0,
    'hour': 3600.0,
    '5min': 300.0,
  };
  String _viewScale = 'day';
  double _viewStartSec = 0.0;

  final DateTime _today = _localStartOfDay(DateTime.now());
  late DateTime _selectedDay;
  late double _sliderSec;

  List<PlaybackRange> _ranges = const [];
  List<Event> _dayEvents = const [];
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

  @override
  void initState() {
    super.initState();
    final initial = widget.initialTs;
    if (initial != null) {
      final local = initial.toLocal();
      _selectedDay = _localStartOfDay(local);
      _sliderSec =
          local.difference(_selectedDay).inSeconds.toDouble().clamp(0.0, 86399.0);
    } else {
      _selectedDay = _today;
      _sliderSec =
          DateTime.now().difference(_today).inSeconds.toDouble().clamp(0.0, 86399.0);
    }
    _viewStartSec = _snappedStart(_sliderSec);
    _loadList();
    _refreshDayEvents();
    if (initial != null) {
      _loadWindowAt(_sliderSec);
    } else {
      _enterLive();
    }
  }

  double _snappedStart(double sec) {
    final span = _scaleSpans[_viewScale]!;
    final clamped = sec.clamp(0.0, 86399.0);
    return (clamped / span).floor() * span;
  }

  // Sec-of-day of the live edge of _selectedDay, or null if the selected
  // day isn't today (past days are fully available).
  double? _liveEdgeOfSelectedDay() {
    if (!_selectedDay.isAtSameMomentAs(_today)) return null;
    final now = DateTime.now();
    return now.difference(_selectedDay).inSeconds.toDouble().clamp(0.0, 86399.0);
  }

  double _scrubMinSec() {
    final live = _liveEdgeOfSelectedDay();
    final dayMax = live ?? 86399.0;
    return _viewStartSec.clamp(0.0, dayMax);
  }

  double _scrubMaxSec() {
    final span = _scaleSpans[_viewScale]!;
    final live = _liveEdgeOfSelectedDay();
    final dayMax = live ?? 86399.0;
    final mn = _scrubMinSec();
    return (_viewStartSec + span - 1).clamp(mn, dayMax);
  }

  void _setScale(String scale) {
    if (!_scaleSpans.containsKey(scale) || scale == _viewScale) return;
    setState(() {
      _viewScale = scale;
      _viewStartSec = _snappedStart(_sliderSec);
    });
  }

  // Slide the visible window forward so it still contains `sec`. Called
  // on live tick / playback advancement in Hour and 5-min scales.
  void _ensureVisible(double sec) {
    final span = _scaleSpans[_viewScale]!;
    if (sec < _viewStartSec || sec >= _viewStartSec + span) {
      _viewStartSec = _snappedStart(sec);
    }
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

  Future<void> _refreshDayEvents() async {
    final dayStart = _selectedDay;
    final dayEnd = _selectedDay.add(const Duration(days: 1));
    try {
      final events = await _api.getEvents(
        cam: widget.cameraName,
        since: dayStart,
        until: dayEnd,
        limit: 500,
      );
      if (!mounted) return;
      // Drop stale results if the day changed between request and reply.
      if (!_selectedDay.isAtSameMomentAs(dayStart)) return;
      setState(() => _dayEvents = events);
    } catch (_) {
      // Soft-fail: events are an enhancement, not load-bearing for playback.
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

    if (start.add(_windowDuration).isAfter(now.subtract(_liveSnapWindow))) {
      await _enterLive();
      return;
    }

    if (!_absTimeHasFootage(start)) {
      await _stopPlayer();
      setState(() => _playerError = 'no footage at this time');
      return;
    }

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
    final c = VideoPlayerController.networkUrl(
      uri,
      httpHeaders: headers,
      videoPlayerOptions: VideoPlayerOptions(mixWithOthers: true),
    );
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
    final cur = _player;
    if (_isLive &&
        cur != null &&
        cur.value.isInitialized &&
        !cur.value.hasError) {
      final now = DateTime.now();
      setState(() {
        _selectedDay = _today;
        _sliderSec =
            now.difference(_today).inSeconds.toDouble().clamp(0.0, 86399.0);
        _ensureVisible(_sliderSec);
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
      _sliderSec = DateTime.now()
          .difference(_today)
          .inSeconds
          .toDouble()
          .clamp(0.0, 86399.0);
      _ensureVisible(_sliderSec);
    });

    final c = VideoPlayerController.networkUrl(
      Uri.parse(hls),
      httpHeaders: _api.bearerHeaders(),
      videoPlayerOptions: VideoPlayerOptions(mixWithOthers: true),
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
        final crossedMidnight = !today.isAtSameMomentAs(_selectedDay);
        setState(() {
          if (crossedMidnight) _selectedDay = today;
          _sliderSec =
              now.difference(today).inSeconds.toDouble().clamp(0.0, 86399.0);
          _ensureVisible(_sliderSec);
        });
        if (crossedMidnight) _refreshDayEvents();
      });
    } catch (e) {
      await c.dispose();
      if (!mounted || gen != _loadGen) return;
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
    final absolute = wStart.add(c.value.position);
    final sec = absolute.difference(_selectedDay).inMilliseconds / 1000.0;
    if (sec < 0 || sec > 86400) return;
    if ((sec - _sliderSec).abs() < 0.05) return;
    setState(() {
      _sliderSec = sec;
      _ensureVisible(sec);
    });
  }

  void _selectDay(DateTime d) {
    if (d.isAtSameMomentAs(_selectedDay)) return;
    setState(() {
      _selectedDay = d;
      // Center the visible window — for past days that means start-of-day;
      // user can scrub to whatever they want from there.
      _viewStartSec = 0.0;
      _sliderSec = 0.0;
      _dayEvents = const [];
    });
    _refreshDayEvents();
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
        startSec: s.difference(dayStart).inMilliseconds / 1000.0,
        endSec: e.difference(dayStart).inMilliseconds / 1000.0,
      ));
    }
    return out;
  }

  @override
  Widget build(BuildContext context) {
    final dayStartUnix = _selectedDay.millisecondsSinceEpoch / 1000.0;
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
                      onSelect: _selectDay,
                    ),
                    const SizedBox(height: 4),
                    _ScaleBar(
                      scale: _viewScale,
                      onScale: _setScale,
                    ),
                    const SizedBox(height: 4),
                    _Timeline(
                      sliderSec: _sliderSec,
                      minSec: _scrubMinSec(),
                      maxSec: _scrubMaxSec(),
                      segments: _segmentsForSelectedDay(),
                      events: _dayEvents,
                      dayStartUnix: dayStartUnix,
                      timeLabel: _formatHms(_sliderSec),
                      isLive: _isLive,
                      onPipTap: (ts) {
                        final sec = ts.difference(_selectedDay).inMilliseconds /
                            1000.0;
                        if (sec < 0 || sec > 86399) return;
                        setState(() {
                          _sliderSec = sec;
                          _ensureVisible(sec);
                        });
                        _loadWindowAt(sec);
                      },
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
  const _Segment({required this.startSec, required this.endSec});
  final double startSec;
  final double endSec;
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
        reverse: true,
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

class _ScaleBar extends StatelessWidget {
  const _ScaleBar({required this.scale, required this.onScale});
  final String scale;
  final ValueChanged<String> onScale;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 24),
      child: SegmentedButton<String>(
        showSelectedIcon: false,
        style: const ButtonStyle(
          visualDensity: VisualDensity.compact,
          tapTargetSize: MaterialTapTargetSize.shrinkWrap,
        ),
        segments: const [
          ButtonSegment(value: 'day', label: Text('Day')),
          ButtonSegment(value: 'hour', label: Text('Hour')),
          ButtonSegment(value: '5min', label: Text('5 min')),
        ],
        selected: {scale},
        onSelectionChanged: (s) => onScale(s.first),
      ),
    );
  }
}

class _Timeline extends StatelessWidget {
  const _Timeline({
    required this.sliderSec,
    required this.minSec,
    required this.maxSec,
    required this.segments,
    required this.events,
    required this.dayStartUnix,
    required this.timeLabel,
    required this.isLive,
    required this.onPipTap,
    required this.onChangeStart,
    required this.onChanged,
    required this.onChangeEnd,
  });
  final double sliderSec;
  final double minSec;
  final double maxSec;
  final List<_Segment> segments;
  final List<Event> events;
  final double dayStartUnix;
  final String timeLabel;
  final bool isLive;
  final ValueChanged<DateTime> onPipTap;
  final ValueChanged<double> onChangeStart;
  final ValueChanged<double> onChanged;
  final ValueChanged<double> onChangeEnd;

  static const _horizontalInset = 20.0;
  static const _thumbRadius = 10.0;

  @override
  Widget build(BuildContext context) {
    final range = (maxSec - minSec).clamp(1.0, 86400.0);
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
          final clampedSlider = sliderSec.clamp(minSec, maxSec);
          final cursorX = _horizontalInset +
              _thumbRadius +
              ((clampedSlider - minSec) / range) * usable;
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
                      painter: _AvailabilityPainter(
                        segments: segments,
                        minSec: minSec,
                        maxSec: maxSec,
                      ),
                    ),
                  ),
                ),
                // Event pips — one tappable colored bar per overlapping
                // event, drawn over the availability strip. Tap → seek.
                ..._buildPips(usable: usable, range: range),
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
                        min: minSec,
                        max: maxSec <= minSec ? minSec + 1 : maxSec,
                        value: clampedSlider,
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

  List<Widget> _buildPips({required double usable, required double range}) {
    final out = <Widget>[];
    for (final ev in events) {
      final startSec = ev.tsStart.millisecondsSinceEpoch / 1000.0 - dayStartUnix;
      final endSec = ev.tsEnd.millisecondsSinceEpoch / 1000.0 - dayStartUnix;
      if (endSec < minSec || startSec > maxSec) continue;
      final clampedStart = startSec.clamp(minSec, maxSec);
      final clampedEnd = endSec.clamp(minSec, maxSec);
      final left = _horizontalInset +
          _thumbRadius +
          ((clampedStart - minSec) / range) * usable;
      var w = ((clampedEnd - clampedStart) / range) * usable;
      // Single-frame events would otherwise be 0 wide; floor at a hairline
      // so they're still tappable.
      if (w < 3) w = 3;
      out.add(Positioned(
        left: left,
        top: 18,
        width: w,
        height: 16,
        child: GestureDetector(
          behavior: HitTestBehavior.opaque,
          onTap: () => onPipTap(ev.tsStart),
          child: Container(
            decoration: BoxDecoration(
              color: eventBucketColor(ev.cls).withValues(alpha: 0.85),
              borderRadius: BorderRadius.circular(1.5),
            ),
          ),
        ),
      ));
    }
    return out;
  }
}

class _AvailabilityPainter extends CustomPainter {
  _AvailabilityPainter({
    required this.segments,
    required this.minSec,
    required this.maxSec,
  });
  final List<_Segment> segments;
  final double minSec;
  final double maxSec;

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
    final range = (maxSec - minSec).clamp(1.0, 86400.0);
    for (final s in segments) {
      if (s.endSec < minSec || s.startSec > maxSec) continue;
      final left =
          ((s.startSec.clamp(minSec, maxSec) - minSec) / range) * size.width;
      final right =
          ((s.endSec.clamp(minSec, maxSec) - minSec) / range) * size.width;
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
