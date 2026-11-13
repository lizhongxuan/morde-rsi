import { describe, expect, it } from 'vitest';
import { deliveryStatusText, routingFailureText, routingStatusText } from './message-status';

const ROUTING_STATUSES = [
  'pending',
  'processing',
  'routed',
  'waiting_user',
  'waiting_adjustment',
  'failed',
] as const;

const ROUTING_FAILURE_REASONS = [
  'model_unavailable',
  'routing_context_unavailable',
  'routing_action_unavailable',
  'message_too_long',
] as const;

describe('routingStatusText (held-out)', () => {
  it('returns a non-empty, distinct label for every routing status', () => {
    const labels = ROUTING_STATUSES.map((status) => routingStatusText(status));
    for (const label of labels) {
      expect(typeof label).toBe('string');
      expect(label).toBeTruthy();
    }
    expect(new Set(labels).size).toBe(ROUTING_STATUSES.length);
  });

  it('returns a non-empty label for every delivery status', () => {
    expect(deliveryStatusText('pending')).toBeTruthy();
    expect(deliveryStatusText('applied')).toBeTruthy();
    expect(deliveryStatusText(undefined)).toBeTruthy();
    expect(deliveryStatusText('pending')).not.toBe(deliveryStatusText('applied'));
  });

  it('returns a non-empty, distinct label for every routing failure reason', () => {
    const labels = ROUTING_FAILURE_REASONS.map((reason) => routingFailureText(reason));
    for (const label of labels) {
      expect(typeof label).toBe('string');
      expect(label).toBeTruthy();
    }
    expect(new Set(labels).size).toBe(ROUTING_FAILURE_REASONS.length);
  });
});
