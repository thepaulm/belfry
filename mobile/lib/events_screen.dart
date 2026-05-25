import 'package:flutter/material.dart';

import 'api.dart';
import 'auth.dart';
import 'playback_screen.dart';

class EventsScreen extends StatefulWidget {
  const EventsScreen({super.key, required this.auth});
  final AuthService auth;

  @override
  State<EventsScreen> createState() => _EventsScreenState();
}

class _EventsScreenState extends State<EventsScreen> {
  late final ApiClient _api = ApiClient(widget.auth);
  final List<Event> _events = [];
  Map<String, Camera> _cameraIndex = const {};
  bool _loading = true;
  bool _loadingMore = false;
  bool _atEnd = false;
  String? _error;

  static const _pageSize = 60;

  @override
  void initState() {
    super.initState();
    _loadInitial();
  }

  Future<void> _loadInitial() async {
    setState(() {
      _loading = true;
      _error = null;
      _events.clear();
      _atEnd = false;
    });
    try {
      final cams = await _api.getAllCameras();
      final events = await _api.getEvents(limit: _pageSize);
      if (!mounted) return;
      setState(() {
        _cameraIndex = {for (final c in cams) c.name: c};
        _events.addAll(events);
        _loading = false;
        _atEnd = events.length < _pageSize;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _loading = false;
        _error = e.toString();
      });
    }
  }

  Future<void> _loadMore() async {
    if (_loadingMore || _atEnd || _events.isEmpty) return;
    setState(() => _loadingMore = true);
    try {
      final next = await _api.getEvents(
        beforeId: _events.last.id,
        limit: _pageSize,
      );
      if (!mounted) return;
      setState(() {
        _events.addAll(next);
        _loadingMore = false;
        _atEnd = next.length < _pageSize;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _loadingMore = false;
        _error = e.toString();
      });
    }
  }

  void _openEvent(Event ev) {
    final cam = _cameraIndex[ev.camera];
    if (cam == null) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('camera "${ev.camera}" not in config')),
      );
      return;
    }
    Navigator.of(context).push(
      MaterialPageRoute(
        builder: (_) => PlaybackScreen(
          auth: widget.auth,
          camera: cam,
          initialTs: ev.tsStart,
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Events')),
      body: RefreshIndicator(
        onRefresh: _loadInitial,
        child: _body(),
      ),
    );
  }

  Widget _body() {
    if (_loading) {
      return const Center(child: CircularProgressIndicator());
    }
    if (_error != null && _events.isEmpty) {
      return ListView(
        children: [
          const SizedBox(height: 80),
          Center(
            child: Padding(
              padding: const EdgeInsets.all(24),
              child: Column(
                children: [
                  const Icon(Icons.error_outline, size: 48),
                  const SizedBox(height: 12),
                  Text(_error!, textAlign: TextAlign.center),
                  const SizedBox(height: 16),
                  FilledButton.tonal(
                    onPressed: _loadInitial,
                    child: const Text('retry'),
                  ),
                ],
              ),
            ),
          ),
        ],
      );
    }
    if (_events.isEmpty) {
      return ListView(
        children: const [
          SizedBox(height: 80),
          Center(child: Text('no events yet')),
        ],
      );
    }
    return ListView.separated(
      // +1 row for the "Load more" / end-of-list footer.
      itemCount: _events.length + 1,
      separatorBuilder: (_, __) => const Divider(height: 1),
      itemBuilder: (ctx, i) {
        if (i == _events.length) return _footer();
        return _EventTile(
          event: _events[i],
          camera: _cameraIndex[_events[i].camera],
          authHeaders: _api.bearerHeaders(),
          onTap: () => _openEvent(_events[i]),
        );
      },
    );
  }

  Widget _footer() {
    if (_atEnd) {
      return const Padding(
        padding: EdgeInsets.symmetric(vertical: 20),
        child: Center(
          child: Text(
            'end of events',
            style: TextStyle(color: Colors.white38, fontSize: 12),
          ),
        ),
      );
    }
    if (_loadingMore) {
      return const Padding(
        padding: EdgeInsets.symmetric(vertical: 20),
        child: Center(
          child: SizedBox(
            width: 20,
            height: 20,
            child: CircularProgressIndicator(strokeWidth: 2),
          ),
        ),
      );
    }
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 12),
      child: Center(
        child: TextButton(
          onPressed: _loadMore,
          child: const Text('load more'),
        ),
      ),
    );
  }
}

class _EventTile extends StatelessWidget {
  const _EventTile({
    required this.event,
    required this.camera,
    required this.authHeaders,
    required this.onTap,
  });
  final Event event;
  final Camera? camera;
  final Map<String, String> authHeaders;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final color = eventBucketColor(event.cls);
    return InkWell(
      onTap: onTap,
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.center,
          children: [
            _thumbnail(),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    children: [
                      Container(
                        padding: const EdgeInsets.symmetric(
                          horizontal: 6,
                          vertical: 1,
                        ),
                        decoration: BoxDecoration(
                          color: color.withValues(alpha: 0.22),
                          borderRadius: BorderRadius.circular(3),
                          border: Border.all(color: color),
                        ),
                        child: Text(
                          event.cls,
                          style: TextStyle(
                            fontSize: 11,
                            fontWeight: FontWeight.w600,
                            color: color,
                          ),
                        ),
                      ),
                      const SizedBox(width: 8),
                      Text(
                        camera?.label ?? event.camera,
                        style: const TextStyle(
                          fontSize: 13,
                          fontWeight: FontWeight.w500,
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 4),
                  Text(
                    _formatTimestamp(event.tsStart),
                    style: const TextStyle(
                      fontSize: 12,
                      color: Colors.white60,
                    ),
                  ),
                  Text(
                    'conf ${event.maxConf.toStringAsFixed(2)} · ${_formatDuration(event.duration)}',
                    style: const TextStyle(
                      fontSize: 11,
                      color: Colors.white38,
                    ),
                  ),
                ],
              ),
            ),
            const Icon(Icons.chevron_right, color: Colors.white38),
          ],
        ),
      ),
    );
  }

  Widget _thumbnail() {
    final url = event.thumbUrl();
    return ClipRRect(
      borderRadius: BorderRadius.circular(4),
      child: Container(
        width: 80,
        height: 60,
        color: Colors.black26,
        child: url == null
            ? const Icon(Icons.image_not_supported, color: Colors.white24)
            : Image.network(
                url,
                headers: authHeaders,
                fit: BoxFit.cover,
                errorBuilder: (_, __, ___) => const Icon(
                  Icons.broken_image,
                  color: Colors.white24,
                ),
                loadingBuilder: (ctx, child, prog) {
                  if (prog == null) return child;
                  return const Center(
                    child: SizedBox(
                      width: 16,
                      height: 16,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    ),
                  );
                },
              ),
      ),
    );
  }
}

// Color buckets mirror the web's playback.js PIP_COLOR so the timeline
// and the events list speak the same visual language.
Color eventBucketColor(String cls) {
  switch (cls) {
    case 'person':
      return const Color(0xff4ea1ff);
    case 'vehicle':
    case 'car':
    case 'truck':
      return const Color(0xffff9b3f);
    case 'motion':
      return const Color(0xffe879f9);
    case 'animal':
    case 'dog':
    case 'cat':
    case 'bird':
      return const Color(0xff5ad17c);
    default:
      return const Color(0xffaaaaaa);
  }
}

String _formatTimestamp(DateTime t) {
  final local = t.toLocal();
  final now = DateTime.now();
  final today = DateTime(now.year, now.month, now.day);
  final eventDay = DateTime(local.year, local.month, local.day);
  final hms =
      '${local.hour.toString().padLeft(2, '0')}:${local.minute.toString().padLeft(2, '0')}:${local.second.toString().padLeft(2, '0')}';
  if (eventDay == today) return 'today $hms';
  if (eventDay == today.subtract(const Duration(days: 1))) {
    return 'yesterday $hms';
  }
  final diff = today.difference(eventDay).inDays;
  if (diff < 7) {
    const wd = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
    return '${wd[local.weekday - 1]} $hms';
  }
  final m = local.month.toString().padLeft(2, '0');
  final d = local.day.toString().padLeft(2, '0');
  return '$m/$d $hms';
}

String _formatDuration(Duration d) {
  final s = d.inMilliseconds / 1000.0;
  if (s < 1) return '<1s';
  if (s < 60) return '${s.toStringAsFixed(1)}s';
  final m = (s / 60).floor();
  final rem = (s % 60).round();
  return '${m}m${rem}s';
}
