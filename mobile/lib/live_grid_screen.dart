import 'dart:async';

import 'package:flutter/material.dart';
import 'package:video_player/video_player.dart';

import 'api.dart';
import 'auth.dart';
import 'inference_overlay.dart';
import 'playback_screen.dart';

class LiveGridScreen extends StatefulWidget {
  const LiveGridScreen({
    super.key,
    required this.auth,
    required this.setId,
    required this.setLabel,
  });
  final AuthService auth;
  final String setId;
  final String setLabel;

  @override
  State<LiveGridScreen> createState() => _LiveGridScreenState();
}

class _LiveGridScreenState extends State<LiveGridScreen> {
  late final ApiClient _api = ApiClient(widget.auth);
  Future<List<Camera>>? _camerasFuture;
  // Session-only toggle, matches the web's behavior — labels default OFF
  // each navigation so the user opts in once per session.
  final ValueNotifier<bool> _labelsOn = ValueNotifier<bool>(false);

  @override
  void initState() {
    super.initState();
    _camerasFuture = _api.getSetCameras(widget.setId);
  }

  @override
  void dispose() {
    _labelsOn.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: Text(widget.setLabel),
        actions: [
          ValueListenableBuilder<bool>(
            valueListenable: _labelsOn,
            builder: (ctx, on, _) => IconButton(
              icon: Icon(on
                  ? Icons.visibility
                  : Icons.visibility_off_outlined),
              tooltip: on ? 'Hide labels' : 'Show labels',
              onPressed: () => _labelsOn.value = !on,
            ),
          ),
        ],
      ),
      body: FutureBuilder<List<Camera>>(
        future: _camerasFuture,
        builder: (context, snap) {
          if (snap.connectionState != ConnectionState.done) {
            return const Center(child: CircularProgressIndicator());
          }
          if (snap.hasError) {
            return Center(child: Text(snap.error.toString()));
          }
          final cameras = snap.data ?? const [];
          return GridView.builder(
            padding: const EdgeInsets.all(8),
            gridDelegate:
                const SliverGridDelegateWithFixedCrossAxisCount(
                  crossAxisCount: 2,
                  mainAxisSpacing: 8,
                  crossAxisSpacing: 8,
                  childAspectRatio: 16 / 9,
                ),
            itemCount: cameras.length,
            itemBuilder: (context, i) => _LiveTile(
              auth: widget.auth,
              camera: cameras[i],
              labelsOn: _labelsOn,
            ),
          );
        },
      ),
    );
  }
}

class _LiveTile extends StatefulWidget {
  const _LiveTile({
    required this.auth,
    required this.camera,
    required this.labelsOn,
  });
  final AuthService auth;
  final Camera camera;
  final ValueNotifier<bool> labelsOn;

  @override
  State<_LiveTile> createState() => _LiveTileState();
}

class _LiveTileState extends State<_LiveTile> {
  static const _retryDelays = <Duration>[
    Duration(seconds: 3),
    Duration(seconds: 8),
    Duration(seconds: 20),
  ];

  // Android MediaCodec teardown can outlive the platform-channel dispose
  // return; if a new controller asks for a decoder before that finishes the
  // framework appears to evict another tile's codec. Sleep briefly after
  // dispose to give the hardware decoder time to fully release.
  static const _postDisposeGrace = Duration(milliseconds: 500);

  VideoPlayerController? _controller;
  String? _error;
  int _attempt = 0;
  Timer? _retryTimer;
  bool _disposed = false;

  // Inference overlay state. The SSE client is only created/started while
  // the labels toggle is on; otherwise we hold no socket per tile.
  InferenceLiveClient? _inference;
  StreamSubscription<List<Detection>>? _inferenceSub;
  List<Detection> _detections = const [];

  @override
  void initState() {
    super.initState();
    _initController();
    widget.labelsOn.addListener(_onLabelsToggle);
    if (widget.labelsOn.value && widget.camera.enabled) {
      _startInference();
    }
  }

  void _onLabelsToggle() {
    if (_disposed) return;
    if (widget.labelsOn.value) {
      if (widget.camera.enabled) _startInference();
    } else {
      _stopInference();
    }
  }

  void _startInference() {
    if (_inference != null) return;
    final c = InferenceLiveClient(auth: widget.auth, cam: widget.camera.name);
    _inference = c;
    _inferenceSub = c.stream.listen((dets) {
      if (!mounted) return;
      setState(() => _detections = dets);
    });
    c.start();
  }

  void _stopInference() {
    _inferenceSub?.cancel();
    _inferenceSub = null;
    _inference?.dispose();
    _inference = null;
    // `!_disposed`, not just `mounted`: during dispose() the element is
    // already defunct but `mounted` is still true (it flips false only
    // after dispose returns), so a bare mounted-guarded setState here
    // still throws. _disposed is set at the top of dispose().
    if (mounted && !_disposed) setState(() => _detections = const []);
  }

  Future<void> _initController() async {
    final hls = widget.camera.hlsUrl();
    if (hls == null) {
      // Camera disabled — no controller, the build() shows "OFFLINE".
      return;
    }

    // Tear down any prior controller before re-attaching.
    final prev = _controller;
    _controller = null;
    if (prev != null) {
      prev.removeListener(_onControllerUpdate);
      await _disposeWithLog(prev, 'init-prev');
    }

    final initStart = DateTime.now();
    debugPrint('[belfry][${widget.camera.name}] init start attempt=$_attempt');
    final c = VideoPlayerController.networkUrl(
      Uri.parse(hls),
      httpHeaders: ApiClient(widget.auth).bearerHeaders(),
      // mixWithOthers tells the plugin to skip ExoPlayer's audio-focus
      // handling. Without it, each new controller's focus request fires
      // AUDIOFOCUS_LOSS at every other live tile in the grid, which ExoPlayer
      // responds to by pausing — the other tiles silently freeze. (Cameras
      // are video-only anyway, so there's no real audio to mix.)
      videoPlayerOptions: VideoPlayerOptions(mixWithOthers: true),
    );
    try {
      await c.initialize();
      await c.setLooping(false);
      await c.play();
      if (_disposed) {
        await _disposeWithLog(c, 'init-aborted');
        return;
      }
      c.addListener(_onControllerUpdate);
      final ms = DateTime.now().difference(initStart).inMilliseconds;
      debugPrint('[belfry][${widget.camera.name}] init ok in ${ms}ms');
      if (mounted) {
        setState(() {
          _controller = c;
          _error = null;
          // Successful start — reset retry counter for future failures.
          _attempt = 0;
        });
      }
    } catch (e) {
      final ms = DateTime.now().difference(initStart).inMilliseconds;
      debugPrint('[belfry][${widget.camera.name}] init failed in ${ms}ms: $e');
      await _disposeWithLog(c, 'init-failed');
      _scheduleRetry(e.toString());
    }
  }

  Future<void> _disposeWithLog(VideoPlayerController c, String tag) async {
    final t0 = DateTime.now();
    try {
      await c.dispose();
    } catch (e) {
      debugPrint('[belfry][${widget.camera.name}] dispose($tag) error: $e');
    }
    final ms = DateTime.now().difference(t0).inMilliseconds;
    debugPrint('[belfry][${widget.camera.name}] dispose($tag) done in ${ms}ms');
  }

  void _onControllerUpdate() {
    final c = _controller;
    if (c == null || !c.value.hasError) return;
    final reason = c.value.errorDescription ?? 'playback error';
    _controller = null;
    c.removeListener(_onControllerUpdate);
    debugPrint('[belfry][${widget.camera.name}] runtime error: $reason');
    // Don't arm the retry until the prior controller's MediaCodec is actually
    // released — otherwise the new controller's decoder allocation can evict
    // another tile's codec on Android. _disposeAndScheduleRetry awaits the
    // dispose and then the post-dispose grace before scheduling.
    unawaited(_disposeAndScheduleRetry(c, reason));
  }

  Future<void> _disposeAndScheduleRetry(
    VideoPlayerController c,
    String reason,
  ) async {
    await _disposeWithLog(c, 'runtime-error');
    if (_disposed) return;
    await Future<void>.delayed(_postDisposeGrace);
    if (_disposed) return;
    _scheduleRetry(reason);
  }

  void _scheduleRetry(String reason) {
    if (_disposed) return;
    if (_attempt >= _retryDelays.length) {
      if (mounted) setState(() => _error = reason);
      return;
    }
    final delay = _retryDelays[_attempt];
    _attempt += 1;
    if (mounted) setState(() => _error = reason);
    _retryTimer?.cancel();
    debugPrint('[belfry][${widget.camera.name}] retry in ${delay.inSeconds}s (attempt=$_attempt)');
    _retryTimer = Timer(delay, () {
      if (_disposed) return;
      _initController();
    });
  }

  @override
  void dispose() {
    _disposed = true;
    widget.labelsOn.removeListener(_onLabelsToggle);
    _stopInference();
    _retryTimer?.cancel();
    _controller?.removeListener(_onControllerUpdate);
    _controller?.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: () => Navigator.of(context).push(
        MaterialPageRoute(
          builder: (_) => PlaybackScreen(
            auth: widget.auth,
            camera: widget.camera,
          ),
        ),
      ),
      child: ClipRRect(
        borderRadius: BorderRadius.circular(6),
        child: Container(
          color: Colors.black,
          child: Stack(
            fit: StackFit.expand,
            children: [
              _videoOrPlaceholder(),
              if (_detections.isNotEmpty)
                Positioned.fill(child: BoxOverlay(detections: _detections)),
              Positioned(
                left: 6,
                bottom: 4,
                right: 6,
                child: Text(
                  widget.camera.label,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(
                    color: Colors.white,
                    fontSize: 12,
                    shadows: [
                      Shadow(blurRadius: 4, color: Colors.black),
                    ],
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _videoOrPlaceholder() {
    if (!widget.camera.enabled) {
      return const Center(
        child: Text('OFFLINE', style: TextStyle(color: Colors.white54)),
      );
    }
    if (_error != null) {
      return Center(
        child: Padding(
          padding: const EdgeInsets.all(8),
          child: Text(
            'stream error',
            style: const TextStyle(color: Colors.white54, fontSize: 11),
            textAlign: TextAlign.center,
          ),
        ),
      );
    }
    final c = _controller;
    if (c == null || !c.value.isInitialized) {
      return const Center(
        child: SizedBox(
          width: 24,
          height: 24,
          child: CircularProgressIndicator(strokeWidth: 2),
        ),
      );
    }
    return FittedBox(
      fit: BoxFit.cover,
      child: SizedBox(
        width: c.value.size.width,
        height: c.value.size.height,
        child: VideoPlayer(c),
      ),
    );
  }
}
