import 'package:flutter/material.dart';

import 'api.dart';
import 'auth.dart';
import 'live_grid_screen.dart';

class SetsScreen extends StatefulWidget {
  const SetsScreen({super.key, required this.auth});
  final AuthService auth;

  @override
  State<SetsScreen> createState() => _SetsScreenState();
}

class _SetsScreenState extends State<SetsScreen> {
  late final ApiClient _api = ApiClient(widget.auth);
  Future<List<CameraSet>>? _setsFuture;

  @override
  void initState() {
    super.initState();
    _reload();
  }

  void _reload() {
    setState(() {
      _setsFuture = _api.getSets();
    });
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('belfry'),
        actions: [
          IconButton(
            icon: const Icon(Icons.logout),
            tooltip: 'sign out',
            onPressed: widget.auth.signOut,
          ),
        ],
      ),
      body: RefreshIndicator(
        onRefresh: () async => _reload(),
        child: FutureBuilder<List<CameraSet>>(
          future: _setsFuture,
          builder: (context, snap) {
            if (snap.connectionState != ConnectionState.done) {
              return const Center(child: CircularProgressIndicator());
            }
            if (snap.hasError) {
              return _ErrorState(
                error: snap.error.toString(),
                onRetry: _reload,
              );
            }
            final sets = snap.data ?? const [];
            if (sets.isEmpty) {
              return const Center(child: Text('no camera sets configured'));
            }
            return ListView.separated(
              itemCount: sets.length,
              separatorBuilder: (_, __) => const Divider(height: 1),
              itemBuilder: (context, i) {
                final s = sets[i];
                return ListTile(
                  title: Text(s.label),
                  subtitle: Text(
                    '${s.cameraCount} camera${s.cameraCount == 1 ? '' : 's'}',
                  ),
                  trailing: const Icon(Icons.chevron_right),
                  onTap: () => Navigator.of(context).push(
                    MaterialPageRoute(
                      builder: (_) => LiveGridScreen(
                        auth: widget.auth,
                        setId: s.id,
                        setLabel: s.label,
                      ),
                    ),
                  ),
                );
              },
            );
          },
        ),
      ),
    );
  }
}

class _ErrorState extends StatelessWidget {
  const _ErrorState({required this.error, required this.onRetry});
  final String error;
  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Icon(Icons.error_outline, size: 48),
            const SizedBox(height: 12),
            Text(error, textAlign: TextAlign.center),
            const SizedBox(height: 16),
            FilledButton.tonal(onPressed: onRetry, child: const Text('retry')),
          ],
        ),
      ),
    );
  }
}
