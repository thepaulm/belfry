import 'package:flutter/material.dart';

import 'api.dart';
import 'auth.dart';
import 'events_screen.dart' show eventBucketColor;
import 'playback_screen.dart';
import 'push_service.dart';

/// ROI alert history — the in-app visual record of zone firings. New alerts
/// arriving via push bump PushService.ping, which reloads the top of the
/// list. Tapping an alert deep-links to that moment in playback.
class AlertsScreen extends StatefulWidget {
  const AlertsScreen({super.key, required this.auth, required this.push});
  final AuthService auth;
  final PushService push;

  @override
  State<AlertsScreen> createState() => _AlertsScreenState();
}

class _AlertsScreenState extends State<AlertsScreen> {
  late final ApiClient _api = ApiClient(widget.auth);
  final List<Alert> _alerts = [];
  Map<String, Camera> _cameraIndex = const {};
  bool _loading = true;
  bool _loadingMore = false;
  bool _atEnd = false;
  String? _error;

  static const _pageSize = 60;

  @override
  void initState() {
    super.initState();
    widget.push.ping.addListener(_onPing);
    _loadInitial();
  }

  @override
  void dispose() {
    widget.push.ping.removeListener(_onPing);
    super.dispose();
  }

  // A push landed: refresh from the top and clear the unread badge if this
  // screen is on-screen.
  void _onPing() {
    _loadInitial();
  }

  Future<void> _loadInitial() async {
    setState(() {
      _loading = _alerts.isEmpty;
      _error = null;
    });
    try {
      final cams = await _api.getAllCameras();
      final alerts = await _api.getAlerts(limit: _pageSize);
      if (!mounted) return;
      setState(() {
        _cameraIndex = {for (final c in cams) c.name: c};
        _alerts
          ..clear()
          ..addAll(alerts);
        _loading = false;
        _atEnd = alerts.length < _pageSize;
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
    if (_loadingMore || _atEnd || _alerts.isEmpty) return;
    setState(() => _loadingMore = true);
    try {
      final next = await _api.getAlerts(
        beforeId: _alerts.last.id,
        limit: _pageSize,
      );
      if (!mounted) return;
      setState(() {
        _alerts.addAll(next);
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

  void _openAlert(Alert a) {
    final cam = _cameraIndex[a.camera];
    if (cam == null) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('camera "${a.camera}" not in config')),
      );
      return;
    }
    Navigator.of(context).push(
      MaterialPageRoute(
        builder: (_) =>
            PlaybackScreen(auth: widget.auth, camera: cam, initialTs: a.ts),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Alerts')),
      body: RefreshIndicator(onRefresh: _loadInitial, child: _body()),
    );
  }

  Widget _body() {
    if (_loading) {
      return const Center(child: CircularProgressIndicator());
    }
    if (_error != null && _alerts.isEmpty) {
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
    if (_alerts.isEmpty) {
      return ListView(
        children: const [
          SizedBox(height: 80),
          Center(child: Text('no alerts yet')),
        ],
      );
    }
    return ListView.separated(
      itemCount: _alerts.length + 1,
      separatorBuilder: (_, __) => const Divider(height: 1),
      itemBuilder: (ctx, i) {
        if (i == _alerts.length) return _footer();
        return _AlertTile(
          alert: _alerts[i],
          camera: _cameraIndex[_alerts[i].camera],
          authHeaders: _api.bearerHeaders(),
          onTap: () => _openAlert(_alerts[i]),
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
            'end of alerts',
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

class _AlertTile extends StatelessWidget {
  const _AlertTile({
    required this.alert,
    required this.camera,
    required this.authHeaders,
    required this.onTap,
  });
  final Alert alert;
  final Camera? camera;
  final Map<String, String> authHeaders;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final color = eventBucketColor(alert.cls);
    return InkWell(
      onTap: onTap,
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
        child: Row(
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
                          alert.cls,
                          style: TextStyle(
                            fontSize: 11,
                            fontWeight: FontWeight.w600,
                            color: color,
                          ),
                        ),
                      ),
                      const SizedBox(width: 8),
                      Flexible(
                        child: Text(
                          alert.roiName,
                          overflow: TextOverflow.ellipsis,
                          style: const TextStyle(
                            fontSize: 13,
                            fontWeight: FontWeight.w600,
                          ),
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 4),
                  Text(
                    '${camera?.label ?? alert.camera} · ${_formatTimestamp(alert.ts)}',
                    style: const TextStyle(fontSize: 12, color: Colors.white60),
                  ),
                  Text(
                    'conf ${alert.conf.toStringAsFixed(2)}',
                    style: const TextStyle(fontSize: 11, color: Colors.white38),
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
    final url = alert.thumbUrl();
    return ClipRRect(
      borderRadius: BorderRadius.circular(4),
      child: Container(
        width: 80,
        height: 60,
        color: Colors.black26,
        child: url == null
            ? const Icon(Icons.notifications_active, color: Colors.white24)
            : Image.network(
                url,
                headers: authHeaders,
                fit: BoxFit.cover,
                errorBuilder: (_, __, ___) =>
                    const Icon(Icons.broken_image, color: Colors.white24),
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

String _formatTimestamp(DateTime t) {
  final local = t.toLocal();
  final now = DateTime.now();
  final today = DateTime(now.year, now.month, now.day);
  final day = DateTime(local.year, local.month, local.day);
  final hms =
      '${local.hour.toString().padLeft(2, '0')}:${local.minute.toString().padLeft(2, '0')}:${local.second.toString().padLeft(2, '0')}';
  if (day == today) return 'today $hms';
  if (day == today.subtract(const Duration(days: 1))) return 'yesterday $hms';
  final m = local.month.toString().padLeft(2, '0');
  final d = local.day.toString().padLeft(2, '0');
  return '$m/$d $hms';
}
