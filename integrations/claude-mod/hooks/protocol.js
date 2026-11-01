export const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
export const LIMITATION = 'Avoid clearing, switching or resuming this conversation until queued WATCH work settles; reconnect the intended chat afterward. Disconnect does not cancel native queued input.';
export const TRANSITIONS = new Set(['clear', 'reset', 'new', 'resume', 'branch', 'fork', 'logout', 'exit']);

export function pairing(value) {
  const match = /^(\d{1,5}):([A-Za-z0-9_-]{16,256})$/.exec(value.trim());
  if (!match || Number(match[1]) < 1 || Number(match[1]) > 65535) return null;
  return {port:Number(match[1]), code:match[2]};
}

export function sessionResponse(value, nativeId) {
  return value?.version === 1 && value.native_id === nativeId && UUID.test(value.generation) &&
    value.heartbeat_interval_ms === 2000 && value.lease_seconds === 12;
}

export function validClaim(value, state, now) {
  return value && UUID.test(value.request_id) && UUID.test(value.attempt_id) &&
    value.native_id === state.native_id && value.generation === state.generation &&
    typeof value.envelope === 'string' && [...value.envelope].length <= 4000 &&
    value.envelope.startsWith('WATCH_REQUEST ' + value.request_id + '\n') &&
    Number.isFinite(value.expires_at) && value.expires_at > now / 1000;
}

// Claude Code 2.1.309 adds this exact attribution around between-turn plugin input.
// Accept only the complete original envelope or this complete host representation.
export function matchingPrompt(text, envelope) {
  if (typeof envelope !== 'string' || typeof text !== 'string') return false;
  return text === envelope || text === 'The watch-owner plugin sent a message:\n' + envelope +
    "\n\nThis is how Claude Code surfaces a prompt a plugin submits between turns — it starts this turn in the user's place. Address the message above.";
}

export function matchingAnswer(answer, id) {
  if (typeof answer !== 'string' || [...answer].length > 16000) return false;
  const markers = answer.split(/\r?\n/).filter(line => /^WATCH_REPLY\s/.test(line));
  return markers.length === 1 && markers[0] === 'WATCH_REPLY ' + id;
}

export function publicStatus(state) {
  return {connected:state.connected, native_id:state.data?.native_id || null,
    pending:state.flight?.request_id || null, error:state.error || '', limitation:LIMITATION};
}
