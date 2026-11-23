import 'package:belfry/inference_overlay.dart';
import 'package:flutter_test/flutter_test.dart';

Detection d(double x1, double y1, double x2, double y2, String cls,
        [double conf = 0.8]) =>
    Detection(x1: x1, y1: y1, x2: x2, y2: y2, cls: cls, conf: conf);

void main() {
  test('lerps matched same-class boxes', () {
    final out = interpolateDetections(
        [d(0.1, 0.1, 0.2, 0.2, 'person', 0.6)],
        [d(0.15, 0.1, 0.25, 0.2, 'person', 0.8)],
        0.5);
    expect(out, hasLength(1));
    expect(out[0].x1, closeTo(0.125, 1e-9));
    expect(out[0].x2, closeTo(0.225, 1e-9));
    expect(out[0].conf, closeTo(0.7, 1e-9));
  });

  test('matches fast mover by center distance, not across classes', () {
    final out = interpolateDetections(
        [d(0.0, 0.0, 0.1, 0.1, 'vehicle'), d(0.5, 0.5, 0.6, 0.6, 'dog')],
        [d(0.3, 0.0, 0.4, 0.1, 'vehicle'), d(0.5, 0.5, 0.6, 0.6, 'cat')],
        0.5);
    expect(out, hasLength(2));
    final v = out.firstWhere((b) => b.cls == 'vehicle');
    expect(v.x1, closeTo(0.15, 1e-9));
    // dog has no same-class partner: holds in place.
    final dog = out.firstWhere((b) => b.cls == 'dog');
    expect(dog.x1, 0.5);
  });

  test('does not pair boxes beyond max travel', () {
    final out = interpolateDetections(
        [d(0.0, 0.0, 0.1, 0.1, 'person')],
        [d(0.8, 0.8, 0.9, 0.9, 'person')],
        0.5);
    expect(out, hasLength(1));
    expect(out[0].x1, 0.0);
  });
}
