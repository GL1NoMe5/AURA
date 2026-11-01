import type { Target } from './useOps';
import { actionPolicy, restoreSettingsAvailable } from './workflow';

type Props = {
  target: Target;
  pending: boolean;
  command: (path: string, body?: unknown) => Promise<unknown>;
};

export default function TargetPolicy({ target, pending, command }: Props) {
  const { policy } = target;
  const restoring = policy.automatic_actions.includes('restore_config_snapshot');
  const ready = restoreSettingsAvailable(target);
  const save = (next: Target['policy']) => void command(`targets/${target.target_id}/policy`, next);
  const minutes = Math.ceil(policy.restart_cooldown_seconds / 60);
  return <>
    <label className="ops-check ops-policy">
      <input className="quiet-switch" role="switch" type="checkbox" checked={policy.auto_investigate} disabled={pending}
        onChange={e => save({ ...policy, auto_investigate: e.target.checked })}/>
      <span>Check problems automatically</span>
    </label>
    <label className="ops-check ops-policy">
      <input className="quiet-switch" role="switch" type="checkbox" checked={policy.automatic_actions.includes('restart_container')} disabled={pending}
        onChange={e => save(actionPolicy(policy, 'restart_container', e.target.checked))}/>
      <span>Allow a restart<small>Restart without asking when a check recommends it</small></span>
    </label>
    <label className="ops-check ops-policy">
      <input className="quiet-switch" role="switch" type="checkbox" checked={restoring} disabled={pending || (!ready && !restoring)}
        onChange={e => save(actionPolicy(policy, 'restore_config_snapshot', e.target.checked))}/>
      <span>Restore saved settings<small>{ready || restoring
        ? 'Restore without asking when a check recommends it'
        : 'Add a Compose connection and save working settings first'}</small></span>
    </label>
    <p className="ops-small">Automatic changes share a {minutes}-minute waiting period. Checks use Baseten credits. Installing a code fix always needs your permission.</p>
  </>;
}
