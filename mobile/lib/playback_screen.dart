import 'package:flutter/material.dart';

class PlaybackScreen extends StatelessWidget {
  const PlaybackScreen({
    super.key,
    required this.cameraName,
    required this.cameraLabel,
  });
  final String cameraName;
  final String cameraLabel;

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: Text(cameraLabel)),
      body: Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              const Icon(Icons.history, size: 64, color: Colors.grey),
              const SizedBox(height: 16),
              Text(cameraName, style: const TextStyle(fontSize: 18)),
              const SizedBox(height: 8),
              const Text(
                'scrubback UI (day picker, scrubber, event pips) lands in Phase E',
                style: TextStyle(color: Colors.grey),
                textAlign: TextAlign.center,
              ),
            ],
          ),
        ),
      ),
    );
  }
}
