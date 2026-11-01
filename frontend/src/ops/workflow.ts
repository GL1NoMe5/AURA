import type { Investigation, OpsSnapshot, Target } from './useOps';

export function restoreSettingsAvailable(target: Target) {
  return target.configuration?.configuration_recovery.configured === true;
}

export function actionPolicy(policy: Target['policy'], action: 'restart_container' | 'restore_config_snapshot', enabled: boolean) {
  return { ...policy, automatic_actions: enabled
    ? [...new Set([...policy.automatic_actions, action])]
    : policy.automatic_actions.filter(value => value !== action) };
}

export function fixReviewAvailable(run?: Investigation) {
  return !!run && ['fix_proposed', 'needs_human', 'cancelled'].includes(run.phase)
    && run.patch?.status === 'tests_passed';
}

export function fixPreparationAvailable(run: Investigation) {
  return ['needs_human', 'cancelled'].includes(run.phase)
    && !!run.diagnosis?.needs_source_analysis && run.patch?.status !== 'tests_passed';
}

export function checkBlockReason(state: OpsSnapshot | null, connection: string, target?: Target) {
  if (!state || connection !== 'connected') return 'Waiting for AURA to reconnect.';
  if (!target) return 'Connect an app first.';
  if (!target.monitoring) return 'Resume watching this app in My apps first.';
  if (!state.docker.available || target.observation?.error?.code === 'docker_unavailable') return 'Open Docker Desktop and wait for the app to reconnect.';
  if (!state.provider_ready) return 'Run Connect-Baseten.cmd in the AURA folder to add your key, then restart AURA.';
  return '';
}

export function observationLabel(target: Target, connection: string) {
  if (!target.monitoring) return 'Watching paused';
  if (connection !== 'connected') return 'Reconnecting…';
  if (target.observation?.error?.code === 'docker_unavailable') return 'Docker is unavailable';
  if (target.observation?.error) return 'Connection needs attention';
  if (target.observation?.healthy === true) return 'App is reachable';
  if (target.observation?.healthy === false) return 'Needs attention';
  return 'Checking connection';
}

export function connectionSpec(name: string, container: string, address: string, context: string, ids: string[]) {
  name = name.trim(); container = container.trim(); address = address.trim(); context = context.trim();
  if (!name || name.length > 80) throw new Error('Enter an app name, up to 80 characters.');
  if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(container)) throw new Error('Enter the container name shown in Docker Desktop.');
  if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/.test(context)) throw new Error('Enter a valid Docker context, such as default or desktop-linux.');
  let url: URL;
  try { url = new URL(address); } catch { throw new Error('Enter the full app address, starting with http:// or https://.'); }
  if (!['http:', 'https:'].includes(url.protocol) || !url.hostname || url.username || url.password) throw new Error('Use an HTTP or HTTPS address without a username or password.');
  const slug = name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
  const base = `app-${slug || container.toLowerCase().replace(/[^a-z0-9-]/g, '-')}`.slice(0, 42).replace(/-$/, '');
  let id = base, suffix = 2;
  while (ids.includes(id)) id = `${base}-${suffix++}`;
  return { target_id: id, name, container_name: container, docker_context: context, probes: [{ url: url.href }] };
}
