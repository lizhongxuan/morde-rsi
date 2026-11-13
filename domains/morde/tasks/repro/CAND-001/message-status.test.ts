import { describe, expect, it } from 'vitest';
import { routingStatusText } from './message-status';

describe('routingStatusText', () => {
  it('returns a status label for routed messages', () => {
    expect(routingStatusText('routed')).toBeTruthy();
  });
});
