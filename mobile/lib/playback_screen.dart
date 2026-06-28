import 'dart:async';

import 'package:flutter/material.dart';
import 'package:video_player/video_player.dart';

import 'api.dart';
import 'auth.dart';
import 'events_screen.dart' show eventBucketColor;
import 'inference_overlay.dart';

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
  // Drives pinch-to-zoom on the video. Reset to identity whenever new
  // footage loads (window change / go-live) so a stale zoom doesn't carry
  // over into an unrelated frame, and on double-tap.
  final TransformationController _zoomController = TransformationController();
  bool _playerLoading = false;
  String? _playerError;
  bool _userScrubbing = false;
  bool _isLive = false;
  int _loadGen = 0;
  Timer? _liveTick;

  // Inference overlay. _inferenceLive handles live mode (continuous SSE),
  // _inferencePast handles past-mode re-inference (one-shot SSE per window
  // with ts→frame lookup). Only one is active at a time.
  bool _labelsOn = false;
  // How far before an event/alert moment a deep-link lands, so labels can
  // connect and draw boxes before the subject enters the frame.
  static const Duration _deepLinkLeadIn = Duration(seconds: 7);
  InferenceLiveClient? _inferenceLive;
  StreamSubscription<List<Detection>>? _inferenceLiveSub;
  InferencePlaybackClient? _inferencePast;
  StreamSubscription<void>? _inferencePastSub;
  List<Detection> _detections = const [];

  @override
  void initState() {
    super.initState();
    final initial = widget.initialTs;
    if (initial != null) {
      // Deep-link from an event/alert tap: back up a few seconds before the
      // moment so the label overlay has time to connect and start drawing
      // boxes before the subject actually enters the frame.
      final local = initial.toLocal().subtract(_deepLinkLeadIn);
      _selectedDay = _localStartOfDay(local);
      _sliderSec =
          local.difference(_selectedDay).inSeconds.toDouble().clamp(0.0, 86399.0);
      // Start with labels on so the user immediately sees what was detected.
      _labelsOn = true;
      // Land zoomed to the 5-min window so the event is easy to scrub around,
      // instead of a thumb lost across the full-day timeline.
      _viewScale = '5min';
    } else {
      _selectedDay = _today;
      _sliderSec =
          DateTime.now().difference(_today).inSeconds.toDouble().clamp(0.0, 86399.0);
    }
    _viewStartSec = _snappedStart(_sliderSec);
    _refreshDayEvents();
    if (initial != null) {
      // Deep-link (alert/event tap): the availability ranges must be
      // loaded before we probe for footage, or _absTimeHasFootage runs
      // against an empty _ranges and falsely reports "no footage".
      _loadListThenWindow(_sliderSec);
    } else {
      _loadList();
      _enterLive();
    }
  }

  Future<void> _loadListThenWindow(double sliderSec) async {
    await _loadList();
    if (!mounted) return;
    await _loadWindowContaining(sliderSec);
  }

  // Load a past window that *contains* `targetSec` and seek playback to it.
  // Used by deep-links (event/alert tap). A plain _loadWindowAt(targetSec)
  // would, for a moment within ~5 min of live, see the 5-min window overrun
  // the live edge and snap to the live stream — stranding the user at live
  // instead of on the event. Here we slide the window start back far enough
  // that the whole window sits before live, then seek to the target inside it.
  Future<void> _loadWindowContaining(double targetSec) async {
    final live = _liveEdgeOfSelectedDay();
    if (live == null) {
      // Selected day isn't today — no live edge to collide with.
      await _loadWindowAt(targetSec, seekToSec: targetSec);
      return;
    }
    // Within the live-snap window of the edge the live stream already shows
    // this moment (and the footage may not be on disk yet) — just go live.
    if (targetSec >= live - _liveSnapWindow.inSeconds) {
      await _enterLive();
      return;
    }
    // Latest window start that keeps the whole window off the live edge, so
    // _loadWindowAt's own live-snap guard doesn't fire and bounce us to live.
    final latestStart =
        (live - _windowDuration.inSeconds - _liveSnapWindow.inSeconds)
            .clamp(0.0, 86399.0);
    final startSec = targetSec.clamp(0.0, latestStart);
    await _loadWindowAt(startSec, seekToSec: targetSec);
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

  // Oldest day reachable from the day strip (14 days incl. today).
  DateTime get _oldestDay => _today.subtract(const Duration(days: 13));

  // Whether prev/next-window stepping is possible. direction: -1 earlier,
  // +1 later. In Day scale this steps the day; in Hour/5-min it shifts the
  // visible window by one span.
  bool _canShift(int direction) {
    if (_viewScale == 'day') {
      return direction > 0
          ? _selectedDay.isBefore(_today)
          : _selectedDay.isAfter(_oldestDay);
    }
    final span = _scaleSpans[_viewScale]!;
    final live = _liveEdgeOfSelectedDay();
    final dayMax = live ?? 86399.0;
    final newStart = _viewStartSec + direction * span;
    return newStart >= 0 && newStart <= dayMax;
  }

  // Step the visible window. Mirrors web playback's shiftSlice: Day scale
  // steps the day picker; Hour/5-min shift the window by one span and load
  // footage at the new window start.
  void _shiftSlice(int direction) {
    if (!_canShift(direction)) return;
    if (_viewScale == 'day') {
      _selectDay(_selectedDay.add(Duration(days: direction)));
      return;
    }
    final span = _scaleSpans[_viewScale]!;
    final newStart = _viewStartSec + direction * span;
    setState(() {
      _viewStartSec = newStart;
      _sliderSec = newStart;
    });
    _loadWindowAt(newStart);
  }

  // Label for the current visible window, e.g. "14:00 – 15:00". Empty in
  // Day scale (the day strip already names the window).
  String _sliceLabel() {
    if (_viewScale == 'day') return '';
    final span = _scaleSpans[_viewScale]!;
    final end = (_viewStartSec + span).clamp(0.0, 86400.0);
    return '${_formatClock(_viewStartSec)} – ${_formatClock(end)}';
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

  // `seekToSec` (sec-of-day) is where playback should land inside the loaded
  // window; defaults to the window start. Deep-links pass a later target so
  // the window can begin earlier (off the live edge) yet play from the event.
  Future<void> _loadWindowAt(double sliderSec, {double? seekToSec}) async {
    debugPrint(
      '[belfry][pb:${widget.cameraName}] loadWindowAt sliderSec=${sliderSec.toStringAsFixed(1)}',
    );
    final start = _absoluteTime(sliderSec);
    final seekTarget = _absoluteTime(seekToSec ?? sliderSec);
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
      await cur.seekTo(seekTarget.difference(wStart));
      await cur.play();
      return;
    }

    final gen = ++_loadGen;
    setState(() {
      _playerLoading = true;
      _playerError = null;
      _isLive = false;
      _detections = const [];
    });
    _liveTick?.cancel();
    _liveTick = null;
    // Switching to a past window — tear down the live inference stream;
    // the past client for the new window starts after init succeeds.
    _stopLiveInference();

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
      if (seekToSec != null) {
        await c.seekTo(seekTarget.difference(start));
      }
      await c.play();
      c.addListener(_onPlayerUpdate);

      final old = _player;
      _player = c;
      _windowStart = start;
      _playerLoading = false;
      _playerError = null;
      _zoomController.value = Matrix4.identity();
      if (mounted) setState(() {});
      if (old != null) {
        old.removeListener(_onPlayerUpdate);
        unawaited(old.dispose());
      }
      // Past mp4 is ready — kick off re-inference for this window if
      // labels are on. _refreshInference is idempotent and picks the
      // past client for this _windowStart.
      _refreshInference();
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
      _zoomController.value = Matrix4.identity();
      if (mounted) setState(() {});
      if (old != null) {
        old.removeListener(_onPlayerUpdate);
        unawaited(old.dispose());
      }

      _refreshInference();
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
      debugPrint(
        '[belfry][pb:${widget.cameraName}] player error: ${c.value.errorDescription}',
      );
      setState(() {
        _playerError = c.value.errorDescription ?? 'playback error';
      });
      return;
    }
    if (_userScrubbing) return;
    final absolute = wStart.add(c.value.position);
    // Past-mode overlay tracks the playhead: every position tick we look
    // up the box sample closest to the absolute play time. This is cheap
    // (binary search over a few hundred samples per window).
    _updatePastDetections();
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

  void _setLabelsOn(bool on) {
    if (on == _labelsOn) return;
    debugPrint('[belfry][pb:${widget.cameraName}] labels=$on isLive=$_isLive');
    setState(() {
      _labelsOn = on;
      if (!on) _detections = const [];
    });
    if (on) {
      _refreshInference();
    } else {
      _stopAllInference();
    }
  }

  /// Idempotent: spin up the right inference client for the current state
  /// (or none). Called whenever _isLive, _windowStart, or _labelsOn change.
  void _refreshInference() {
    if (!_labelsOn || !widget.camera.enabled) {
      _stopAllInference();
      return;
    }
    if (_isLive) {
      _stopPastInference();
      _startLiveInference();
    } else if (_windowStart != null) {
      _stopLiveInference();
      _startPastInference(_windowStart!);
    } else {
      _stopAllInference();
    }
  }

  void _startLiveInference() {
    if (_inferenceLive != null) return;
    final c = InferenceLiveClient(auth: widget.auth, cam: widget.cameraName);
    _inferenceLive = c;
    _inferenceLiveSub = c.stream.listen((dets) {
      if (!mounted) return;
      setState(() => _detections = dets);
    });
    c.start();
  }

  void _startPastInference(DateTime start) {
    // If the existing past client already covers this window, leave it
    // alone — re-creating would drop already-buffered samples.
    if (_inferencePast != null &&
        _inferencePast!.windowStart.isAtSameMomentAs(start) &&
        _inferencePast!.duration == _windowDuration) {
      return;
    }
    _stopPastInference();
    final c = InferencePlaybackClient(
      auth: widget.auth,
      cam: widget.cameraName,
      windowStart: start,
      duration: _windowDuration,
    );
    _inferencePast = c;
    _inferencePastSub = c.onUpdate.listen((_) {
      // New samples arrived — refresh the overlay to whatever sample is
      // closest to the player's current position.
      _updatePastDetections();
    });
    c.start();
  }

  void _updatePastDetections() {
    final c = _player;
    final wStart = _windowStart;
    final inf = _inferencePast;
    if (!_labelsOn || _isLive || c == null || wStart == null || inf == null) {
      return;
    }
    final absUnix =
        (wStart.add(c.value.position).millisecondsSinceEpoch) / 1000.0;
    final boxes = inf.boxesNear(absUnix);
    if (!_detectionsEqual(boxes, _detections)) {
      setState(() => _detections = boxes);
    }
  }

  void _stopLiveInference() {
    _inferenceLiveSub?.cancel();
    _inferenceLiveSub = null;
    _inferenceLive?.dispose();
    _inferenceLive = null;
  }

  void _stopPastInference() {
    _inferencePastSub?.cancel();
    _inferencePastSub = null;
    _inferencePast?.dispose();
    _inferencePast = null;
  }

  void _stopAllInference() {
    _stopLiveInference();
    _stopPastInference();
    if (_detections.isNotEmpty && mounted) {
      setState(() => _detections = const []);
    }
  }

  // The past client returns the same List instance for repeated lookups
  // in the same sample window, so reference equality is enough to avoid
  // pointless setState churn. When both are empty we also skip the
  // rebuild (an empty const list match would compare unequal by identity).
  bool _detectionsEqual(List<Detection> a, List<Detection> b) {
    if (identical(a, b)) return true;
    return a.isEmpty && b.isEmpty;
  }

  @override
  void dispose() {
    _liveTick?.cancel();
    _stopAllInference();
    _player?.removeListener(_onPlayerUpdate);
    _player?.dispose();
    _zoomController.dispose();
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
    // Landscape = "cinema" mode: hide the app bar and day strip, fold the
    // remaining controls (back / time / scale / labels / live) into one
    // compact row, and shrink the timeline so the video gets nearly the
    // whole screen. Day selection is still reachable by rotating back.
    final landscape =
        MediaQuery.of(context).orientation == Orientation.landscape;

    final timeline = _Timeline(
      sliderSec: _sliderSec,
      minSec: _scrubMinSec(),
      maxSec: _scrubMaxSec(),
      segments: _segmentsForSelectedDay(),
      events: _dayEvents,
      dayStartUnix: dayStartUnix,
      timeLabel: _formatHms(_sliderSec),
      isLive: _isLive,
      compact: landscape,
      // In landscape the time label lives in the combined control row.
      showLabel: !landscape,
      onPipTap: (ts) {
        final sec = ts.difference(_selectedDay).inMilliseconds / 1000.0;
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
    );

    final List<Widget> children;
    if (landscape) {
      children = [
        _landscapeControlBar(),
        timeline,
        Expanded(child: _videoArea()),
      ];
    } else {
      children = [
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
          sliceLabel: _sliceLabel(),
          canPrev: _canShift(-1),
          canNext: _canShift(1),
          onShift: _shiftSlice,
        ),
        const SizedBox(height: 4),
        timeline,
        Expanded(child: _videoArea()),
      ];
    }

    return Scaffold(
      appBar: landscape
          ? null
          : AppBar(
              title: Text(widget.cameraLabel),
              actions: [
                IconButton(
                  icon: Icon(_labelsOn
                      ? Icons.visibility
                      : Icons.visibility_off_outlined),
                  tooltip: _labelsOn ? 'Hide labels' : 'Show labels',
                  onPressed: () => _setLabelsOn(!_labelsOn),
                ),
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
              : SafeArea(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: children,
                  ),
                ),
    );
  }

  // Landscape-only: every control on one short row so the video keeps the
  // rest of the height. Back / LIVE+time on the left, scale stepper in the
  // middle, labels + Go-Live on the right.
  Widget _landscapeControlBar() {
    Widget iconBtn(IconData icon, String tip, VoidCallback? onTap) =>
        IconButton(
          icon: Icon(icon, size: 20),
          visualDensity: VisualDensity.compact,
          padding: EdgeInsets.zero,
          constraints: const BoxConstraints(minWidth: 36, minHeight: 36),
          tooltip: tip,
          onPressed: onTap,
        );
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 4),
      child: Row(
        children: [
          iconBtn(Icons.arrow_back, 'Back',
              () => Navigator.of(context).maybePop()),
          if (_isLive) ...[
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 5, vertical: 1),
              decoration: BoxDecoration(
                color: Colors.red,
                borderRadius: BorderRadius.circular(3),
              ),
              child: const Text(
                'LIVE',
                style: TextStyle(
                  fontSize: 9,
                  fontWeight: FontWeight.bold,
                  letterSpacing: 0.5,
                  color: Colors.white,
                ),
              ),
            ),
            const SizedBox(width: 6),
          ],
          Text(
            _formatHms(_sliderSec),
            style: const TextStyle(
              fontFamily: 'monospace',
              fontSize: 13,
              color: Colors.white70,
            ),
          ),
          const Spacer(),
          iconBtn(Icons.chevron_left, 'Previous',
              _canShift(-1) ? () => _shiftSlice(-1) : null),
          SegmentedButton<String>(
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
            selected: {_viewScale},
            onSelectionChanged: (s) => _setScale(s.first),
          ),
          iconBtn(Icons.chevron_right, 'Next',
              _canShift(1) ? () => _shiftSlice(1) : null),
          const Spacer(),
          iconBtn(
            _labelsOn ? Icons.visibility : Icons.visibility_off_outlined,
            _labelsOn ? 'Hide labels' : 'Show labels',
            () => _setLabelsOn(!_labelsOn),
          ),
          iconBtn(Icons.sensors, 'Go Live',
              _isLive ? null : () => _enterLive()),
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
    final ar = c.value.aspectRatio == 0 ? 16 / 9 : c.value.aspectRatio;
    // Labels render in both live and past modes now — past mode uses the
    // playback re-inference SSE with ts→frame lookup.
    final showOverlay = _labelsOn && _detections.isNotEmpty;
    // Keep the original AspectRatio → VideoPlayer tree when no overlay
    // would render. Wrapping in a Stack with StackFit.expand even when
    // empty appears to perturb the video_player texture sizing path on
    // Android — saw maxImages buffer exhaustion in logcat under that
    // tree shape.
    if (!showOverlay) {
      return _zoomable(AspectRatio(aspectRatio: ar, child: VideoPlayer(c)));
    }
    return _zoomable(AspectRatio(
      aspectRatio: ar,
      child: Stack(
        fit: StackFit.expand,
        children: [
          VideoPlayer(c),
          BoxOverlay(detections: _detections),
        ],
      ),
    ));
  }

  // Pinch-to-zoom + pan wrapper for the video. Double-tap snaps back to
  // 1x. The overlay (when present) lives inside the same AspectRatio so the
  // detection boxes scale and pan together with the frame.
  Widget _zoomable(Widget child) {
    return GestureDetector(
      onDoubleTap: () => _zoomController.value = Matrix4.identity(),
      child: InteractiveViewer(
        transformationController: _zoomController,
        minScale: 1.0,
        maxScale: 6.0,
        child: child,
      ),
    );
  }
}

DateTime _localStartOfDay(DateTime d) {
  final local = d.toLocal();
  return DateTime(local.year, local.month, local.day);
}

String _formatClock(double sec) {
  final s = sec.round();
  final h = (s ~/ 3600).toString().padLeft(2, '0');
  final m = ((s % 3600) ~/ 60).toString().padLeft(2, '0');
  return '$h:$m';
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
      // 72, not 64: each cell's Column (Mon / 18 / ●, two 2px gaps) plus the
      // Container's 4px vertical margin needs ~70px at default text scale;
      // 64 left only 56px of inner space and overflowed ~6px on the bottom.
      height: 72,
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
  const _ScaleBar({
    required this.scale,
    required this.onScale,
    required this.sliceLabel,
    required this.canPrev,
    required this.canNext,
    required this.onShift,
  });
  final String scale;
  final ValueChanged<String> onScale;
  final String sliceLabel;
  final bool canPrev;
  final bool canNext;
  final ValueChanged<int> onShift;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 16),
      child: Column(
        children: [
          Row(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              IconButton(
                icon: const Icon(Icons.chevron_left),
                visualDensity: VisualDensity.compact,
                tooltip: 'Previous',
                onPressed: canPrev ? () => onShift(-1) : null,
              ),
              SegmentedButton<String>(
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
              IconButton(
                icon: const Icon(Icons.chevron_right),
                visualDensity: VisualDensity.compact,
                tooltip: 'Next',
                onPressed: canNext ? () => onShift(1) : null,
              ),
            ],
          ),
          if (sliceLabel.isNotEmpty)
            Text(
              sliceLabel,
              style: const TextStyle(
                fontFamily: 'monospace',
                fontSize: 11,
                color: Colors.white54,
              ),
            ),
        ],
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
    required this.compact,
    required this.showLabel,
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
  // Landscape "cinema" mode: shorter track + pips so the video gets the
  // vertical space.
  final bool compact;
  // When false the internal time-label row is dropped — landscape hoists it
  // into the single combined control row instead.
  final bool showLabel;
  final ValueChanged<DateTime> onPipTap;
  final ValueChanged<double> onChangeStart;
  final ValueChanged<double> onChanged;
  final ValueChanged<double> onChangeEnd;

  static const _horizontalInset = 20.0;
  static const _thumbRadius = 10.0;

  @override
  Widget build(BuildContext context) {
    final range = (maxSec - minSec).clamp(1.0, 86400.0);
    // Track geometry derives from one height so the bar, pips and cursor
    // stay centered at either size.
    final trackH = compact ? 34.0 : 52.0;
    const barH = 8.0;
    final pipH = compact ? 12.0 : 16.0;
    final barTop = (trackH - barH) / 2;
    final pipTop = (trackH - pipH) / 2;
    final cursorInset = compact ? 4.0 : 6.0;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        if (showLabel) ...[
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
          SizedBox(height: compact ? 2 : 4),
        ],
        LayoutBuilder(builder: (ctx, c) {
          final width = c.maxWidth;
          final usable = width - 2 * (_horizontalInset + _thumbRadius);
          final clampedSlider = sliderSec.clamp(minSec, maxSec);
          final cursorX = _horizontalInset +
              _thumbRadius +
              ((clampedSlider - minSec) / range) * usable;
          return SizedBox(
            height: trackH,
            child: Stack(
              children: [
                // Availability bar — slate background with lighter segments
                // where footage exists. Inset matches the slider's thumb so
                // the cursor aligns with the bar at the extremes.
                Positioned(
                  left: _horizontalInset + _thumbRadius,
                  right: _horizontalInset + _thumbRadius,
                  top: barTop,
                  child: SizedBox(
                    height: barH,
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
                ..._buildPips(
                  usable: usable,
                  range: range,
                  top: pipTop,
                  height: pipH,
                ),
                // Vertical orange cursor line.
                Positioned(
                  left: cursorX - 1,
                  top: cursorInset,
                  bottom: cursorInset,
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

  List<Widget> _buildPips({
    required double usable,
    required double range,
    required double top,
    required double height,
  }) {
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
        top: top,
        width: w,
        height: height,
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
