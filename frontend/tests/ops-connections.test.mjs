import { test } from 'node:test';
import assert from 'node:assert/strict';
import { editableProfile, profileUpdate, importProfile, currentConnection, newConnectionId } from '../src/ops/connectionProfile.ts';

const target = {
  target_id: 'studio', name: 'Studio', container_name: 'studio-api', docker_context: 'default',
  monitoring: false, connection_revision: 3,
  policy: { auto_investigate: true, automatic_actions: ['restart_container'], restart_cooldown_seconds: 3600 },
  probes: [{ url: 'https://example.com/health', credential_env: 'APP_AUTH', auth_scheme: 'Bearer', expected_status: 204 },
    { url: 'https://example.com/ready', expected_status: 200 }],
  source_dir: 'D:/apps/studio', source_revision: 'a'.repeat(40), patch_profile: { image: 'sha256:' + 'b'.repeat(64), test_paths: ['tests'] },
  recovery_checks: [{ url: 'https://example.com/file', expected_bytes: 42, expected_sha256: 'c'.repeat(64) }],
  observation: { healthy: true }, configuration: {}, verified_deployment: { proposal_id: 'old' },
};

test('editing preserves all checks and repair setup, binds revision, and never enables automation', () => {
  const draft = editableProfile(target);
  for (const key of ['connection_revision', 'observation', 'configuration', 'verified_deployment']) assert.equal(key in draft, false);
  const body = profileUpdate(target, draft, 'Renamed', 'https://example.com/health2');
  assert.equal(body.expected_revision, 3);
  assert.equal(body.spec.name, 'Renamed');
  assert.deepEqual(body.spec.probes, [{ ...target.probes[0], url: 'https://example.com/health2' }, target.probes[1]]);
  assert.deepEqual(body.spec.patch_profile, target.patch_profile);
  assert.deepEqual(body.spec.recovery_checks, target.recovery_checks);
  assert.deepEqual(body.spec.policy, { auto_investigate: false, automatic_actions: [], restart_cooldown_seconds: 3600 });
  assert.equal(body.spec.monitoring, false);
  assert.equal(target.probes[0].url, 'https://example.com/health');
});

test('profile imports are explicit, identity-bound and cannot hide server metadata', () => {
  const profile = editableProfile(target);
  assert.deepEqual(importProfile(JSON.stringify(profile), target), profile);
  for (const key of ['target_id', 'docker_context', 'container_name']) {
    assert.throws(() => importProfile(JSON.stringify({ ...profile, [key]: 'other' }), target), /different app/);
  }
  assert.throws(() => importProfile(JSON.stringify(target), target), /without observation/);
  for (const data of [null, [], true, { ...profile, probes: [] }]) assert.throws(() => importProfile(JSON.stringify(data), target));
});

test('check addresses do not carry credentials or unsupported query/fragment data', () => {
  for (const address of ['https://a:b@example.com', 'http://example.com/?token=a', 'http://example.com/#x', 'file:///test', 'bad']) {
    assert.throws(() => profileUpdate(target, editableProfile(target), 'Studio', address));
  }
  assert.throws(() => profileUpdate(target, { ...editableProfile(target), container_name: 'other' }, 'Studio', 'https://example.com'));
});

test('historical and removed connections never offer current mutation controls', () => {
  const run = { target_id: 'studio', connection_revision: 2 };
  assert.equal(currentConnection(run, target), false);
  assert.equal(currentConnection({ ...run, connection_revision: 3 }, target), true);
  assert.equal(currentConnection({ ...run, target_id: 'other', connection_revision: 3 }, target), false);
  assert.equal(currentConnection(run, undefined), false);
  assert.equal(currentConnection({ target_id: 'old' }, { target_id: 'old' }), true);
});

test('new registrations avoid retired IDs even after a browser reload', () => {
  const ids = Array.from({ length: 30 }, () => newConnectionId('app-studio'));
  assert.equal(new Set(ids).size, 30);
  for (const id of ids) assert.match(id, /^[a-z][a-z0-9-]{0,47}$/);
  assert.ok(newConnectionId('app-' + 'long-name'.repeat(8)).length <= 48);
});
