import { test } from 'node:test';
import assert from 'node:assert/strict';
import { connectionSpec, checkBlockReason, observationLabel, fixReviewAvailable, fixPreparationAvailable, actionPolicy, restoreSettingsAvailable } from '../src/ops/workflow.ts';

test('each automatic action is an explicit opt-in and preserves the other permissions and cooldown', () => {
  const original = { auto_investigate: false, automatic_actions: ['rollback_image'], restart_cooldown_seconds: 900 };
  const on = actionPolicy(original, 'restore_config_snapshot', true);
  assert.deepEqual(original.automatic_actions, ['rollback_image']);
  assert.deepEqual(on, { ...original, automatic_actions: ['rollback_image', 'restore_config_snapshot'] });
  assert.deepEqual(actionPolicy(on, 'restore_config_snapshot', true), on);
  const both = actionPolicy(on, 'restart_container', true);
  assert.deepEqual(actionPolicy(both, 'restore_config_snapshot', false).automatic_actions, ['rollback_image', 'restart_container']);
  assert.deepEqual(actionPolicy(on, 'restore_config_snapshot', false), original);
});

test('restore settings is offered only with a server-confirmed recovery setup', () => {
  assert.equal(restoreSettingsAvailable({}), false);
  assert.equal(restoreSettingsAvailable({ configuration: { configuration_recovery: { configured: false } } }), false);
  assert.equal(restoreSettingsAvailable({ configuration: { configuration_recovery: { configured: true } } }), true);
});

test('retained tested fixes remain reviewable after build failure/cancel without regeneration', () => {
  for (const phase of ['fix_proposed', 'needs_human', 'cancelled']) {
    const run = {phase, patch:{status:'tests_passed'}, diagnosis:{needs_source_analysis:true}};
    assert.equal(fixReviewAvailable(run), true);
    assert.equal(fixPreparationAvailable(run), false);
  }
  for (const phase of ['verified_fixed', 'mitigated', 'preparing_deployment', 'awaiting_approval']) {
    const run = {phase, patch:{status:'tests_passed'}, diagnosis:{needs_source_analysis:true}};
    assert.equal(fixReviewAvailable(run), false);
    assert.equal(fixPreparationAvailable(run), false);
  }
  assert.equal(fixReviewAvailable(), false);
  assert.equal(fixReviewAvailable({phase:'needs_human'}), false);
  assert.equal(fixPreparationAvailable({phase:'needs_human',diagnosis:{needs_source_analysis:true}}), true);
});

test('connection accepts non-Latin and number-first names without invalid IDs', () => {
  for (const name of ['Мой сервер', '123 App', 'Hello']) {
    const spec = connectionSpec(name, 'my-api', 'http://127.0.0.1:8080/health', 'desktop-linux', []);
    assert.match(spec.target_id, /^[a-z][a-z0-9-]{0,47}$/);
    assert.equal(spec.name, name); assert.equal(spec.docker_context, 'desktop-linux');
    assert.equal(spec.policy, undefined); // New targets retain observation-only server defaults.
  }
});
test('connection avoids existing IDs and does not accept credentials or other URL schemes', () => {
  assert.equal(connectionSpec('Test', 'api', 'https://example.com/health', 'default', ['app-test', 'app-test-2']).target_id, 'app-test-3');
  for (const url of ['ftp://example.com', 'http://name:password@example.com', 'localhost:8080', '']) {
    assert.throws(() => connectionSpec('Test', 'api', url, 'default', []));
  }
  assert.throws(() => connectionSpec('   ', 'api', 'http://localhost', 'default', []));
  assert.throws(() => connectionSpec('Test', 'bad/name', 'http://localhost', 'default', []));
});
test('check prerequisites explain missing connection, target, monitoring and provider', () => {
  const state = { provider_ready: true, docker: {available:true} }, target = { monitoring: true };
  assert.match(checkBlockReason(null, 'connecting', target), /reconnect/);
  assert.match(checkBlockReason(state, 'disconnected', target), /reconnect/);
  assert.match(checkBlockReason(state, 'connected'), /Connect an app/);
  assert.match(checkBlockReason(state, 'connected', { monitoring: false }), /Resume watching/);
  assert.match(checkBlockReason({...state, provider_ready:false}, 'connected', target), /Baseten/);
  assert.match(checkBlockReason({...state, docker:{available:false}}, 'connected', target), /Docker Desktop/);
  assert.match(checkBlockReason(state, 'connected', {...target, observation:{error:{code:'docker_unavailable'}}}), /Docker Desktop/);
  assert.equal(checkBlockReason(state, 'connected', target), '');
});

test('unavailable Docker is not presented as an endless connection attempt', () => {
  const target = {monitoring:true, observation:{healthy:null,error:{code:'docker_unavailable'}}};
  assert.equal(observationLabel(target, 'connected'), 'Docker is unavailable');
  assert.equal(observationLabel(target, 'disconnected'), 'Reconnecting…');
  assert.equal(observationLabel({...target, monitoring:false}, 'connected'), 'Watching paused');
  assert.equal(observationLabel({monitoring:true,observation:{healthy:true}}, 'connected'), 'App is reachable');
});
