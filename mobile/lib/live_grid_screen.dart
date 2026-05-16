import 'package:flutter/material.dart';
import 'package:video_player/video_player.dart';

import 'api.dart';
import 'auth.dart';
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

  @override
  void initState() {
    super.initState();
    _camerasFuture = _api.getSetCameras(widget.setId);
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: Text(widget.setLabel)),
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
            ),
          );
        },
      ),
    );
  }
}

class _LiveTile extends StatefulWidget {
  const _LiveTile({required this.auth, required this.camera});
  final AuthService auth;
  final Camera camera;

  @override
  State<_LiveTile> createState() => _LiveTileState();
}

class _LiveTileState extends State<_LiveTile> {
  VideoPlayerController? _controller;
  String? _error;

  @override
  void initState() {
    super.initState();
    _initController();
  }

  Future<void> _initController() async {
    final hls = widget.camera.hlsUrl();
    if (hls == null) {
      // Camera disabled — no controller, the build() shows "OFFLINE".
      return;
    }
    final c = VideoPlayerController.networkUrl(
      Uri.parse(hls),
      httpHeaders: ApiClient(widget.auth).bearerHeaders(),
    );
    try {
      await c.initialize();
      await c.setLooping(false);
      await c.play();
      if (!mounted) {
        await c.dispose();
        return;
      }
      setState(() => _controller = c);
    } catch (e) {
      await c.dispose();
      if (mounted) setState(() => _error = e.toString());
    }
  }

  @override
  void dispose() {
    _controller?.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: () => Navigator.of(context).push(
        MaterialPageRoute(
          builder: (_) => PlaybackScreen(
            cameraName: widget.camera.name,
            cameraLabel: widget.camera.label,
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
