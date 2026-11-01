import type { Investigation, Target } from './useOps';

// Only enrollment fields may cross back into the strict TargetSpec endpoint.
const fields = ['target_id', 'name', 'docker_context', 'container_name', 'probes',
  'source_dir', 'source_revision', 'source_manifest_sha256', 'source_image_id',
  'deployment_root', 'deployment_launch', 'compose_file', 'compose_project',
  'compose_service', 'monitoring', 'policy', 'patch_profile', 'recovery_checks'];

export function editableProfile(target: Target): Record<string, unknown> {
  const record = target as unknown as Record<string, unknown>;
  return JSON.parse(JSON.stringify(Object.fromEntries(fields.filter(k => k in record).map(k => [k, record[k]]))));
}

export function importProfile(text: string, target: Target): Record<string, unknown> {
  const data: unknown = JSON.parse(text);
  if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error('Choose a connection file containing an app profile.');
  const profile = data as Record<string, unknown>;
  for (const field of ['target_id', 'docker_context', 'container_name'] as const) {
    if (profile[field] !== target[field]) throw new Error('This file belongs to a different app connection. Its ID, container and Docker context must match.');
  }
  if (Object.keys(profile).some(k => !fields.includes(k))) throw new Error('Use a connection profile, without observation, history or revision fields.');
  if (!Array.isArray(profile.probes) || profile.probes.length < 1 || profile.probes.length > 3) throw new Error('The connection file needs one to three check addresses.');
  return JSON.parse(JSON.stringify(profile));
}

export function profileUpdate(target: Target, draft: Record<string, unknown>, name: string, address: string) {
  const cleanName = name.trim();
  if (!cleanName || cleanName.length > 80) throw new Error('Enter an app name, up to 80 characters.');
  let url: URL;
  try { url = new URL(address.trim()); } catch { throw new Error('Enter a full HTTP or HTTPS check address.'); }
  if (!['http:', 'https:'].includes(url.protocol) || !url.hostname || url.username || url.password || url.search || url.hash) throw new Error('Use a check address without credentials, query parameters or a fragment.');
  const profile = JSON.parse(JSON.stringify(draft)) as Record<string, unknown>;
  for (const field of ['target_id', 'docker_context', 'container_name'] as const) {
    if (profile[field] !== target[field]) throw new Error('The container and Docker context cannot change here.');
  }
  const probes = profile.probes as Record<string, unknown>[] | undefined;
  if (!Array.isArray(probes) || probes.length < 1 || !probes[0] || typeof probes[0] !== 'object') throw new Error('Add a check address to the connection file.');
  profile.probes = [{ ...probes[0], url: url.href }, ...probes.slice(1)];
  profile.name = cleanName;
  profile.monitoring = false;
  profile.policy = { automatic_actions: [], auto_investigate: false, restart_cooldown_seconds: target.policy.restart_cooldown_seconds };
  return { expected_revision: target.connection_revision ?? 0, spec: profile };
}

export function currentConnection(run: Investigation, target?: Target) {
  return !!target && run.target_id === target.target_id && (run.connection_revision ?? 0) === (target.connection_revision ?? 0);
}

export function newConnectionId(base: string) {
  // IDs remain unique across removal, browser reload and retained tombstones.
  return `${base.slice(0, 34).replace(/-$/, '')}-${crypto.randomUUID().replaceAll('-', '').slice(0, 12)}`;
}

export const setupNames: Record<string, string> = {
  probes: 'a check address', container_identity: 'a Docker container', compose_profile: 'saved deployment settings',
  known_good_snapshot: 'a saved working version', verified_source: 'verified source code', source_image: 'the matching app image',
  patch_profile: 'an isolated test setup', deployment_root: 'the code location in the image',
  deployment_launch: 'a reviewed startup command', recovery_checks: 'a complete-operation check',
};
