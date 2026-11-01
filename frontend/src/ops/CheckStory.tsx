import type { Investigation } from './useOps';
import { phaseLabel } from './useOps';

export function Mark({ kind = 'check' }: { kind?: 'check' | 'search' | 'repair' | 'shield' | 'pause' | 'file' | 'arrow' | 'pulse' }) {
  const paths = {
    pulse: 'M3 12h4l3-7 4 14 3-7h4',
    check: 'M5 12l4 4L19 6', search: 'M10.5 17a6.5 6.5 0 1 0 0-13 6.5 6.5 0 0 0 0 13m5-1 5 5',
    repair: 'M14 5a5 5 0 0 0-6 6L3 16a3 3 0 0 0 5 5l5-5a5 5 0 0 0 6-6l-4 4-4-4 4-4',
    shield: 'M12 3l8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3m-4 9 3 3 5-6',
    pause: 'M8 5v14M16 5v14', file: 'M14 3H5v18h14V8l-5-5v5h5M8 13h8M8 17h6',
    arrow: 'M4 12h16m-6-6 6 6-6 6',
  };
  return <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><path d={paths[kind]}/></svg>;
}

export function resultLabel(run: Investigation) {
  if (run.verification?.verified && run.verification.root_cause_fixed && run.phase === 'verified_fixed') return 'Problem fixed';
  if (run.verification?.verified) return 'App responding again';
  if (['verified_fixed', 'mitigated'].includes(run.phase)) return 'Result not confirmed';
  if (run.phase === 'needs_human' && !run.error && run.diagnosis?.action === 'abstain') return 'No change made';
  return phaseLabel[run.phase] ?? 'Check in progress';
}

export default function CheckStory({ run }: { run: Investigation }) {
  const verified = run.verification?.verified === true;
  const fixed = verified && run.verification?.root_cause_fixed === true && run.phase === 'verified_fixed';
  const stopped = ['needs_human', 'cancelled'].includes(run.phase);
  const noChange = run.phase === 'needs_human' && !run.error && run.diagnosis?.action === 'abstain';
  const title = resultLabel(run);
  const tests = run.patch?.attempts?.filter(a => a.status === 'tests_passed').at(-1)?.tests ?? [];
  const before = tests.some(t => t.stage.endsWith('-before') && t.gate_passed && t.exit_code === 1);
  const after = tests.some(t => t.stage.endsWith('-after') && t.gate_passed && t.passed && t.exit_code === 0);
  const checks = run.verification?.functional?.checks ?? [];
  const delivered = checks.filter(c => c.ok && c.expected_bytes > 0 && c.bytes_received === c.expected_bytes && !!c.expected_sha256 && c.sha256 === c.expected_sha256).length;
  const complete = run.verification?.functional?.verified && checks.length > 0 && delivered === checks.length;
  const description = fixed ? 'Cause fixed and tested.' : verified ? 'Availability restored. The cause may remain.' : noChange ? 'AURA has findings for you to review.' : ({
    queued: 'This check will start when AURA is free.', investigating: 'Checking what went wrong.',
    awaiting_approval: 'Review the change before AURA applies it.', executing: 'Applying the change you allowed.',
    verifying: 'Checking that the app works.', fix_proposed: 'Tests pass. Your app has not changed yet.',
    preparing_patch: 'Trying a correction in a separate test copy.', preparing_deployment: 'Preparing the version for your review.',
    needs_human: 'AURA could not confirm a safe next step.', cancelled: 'No more steps will be started.',
  }[run.phase] ?? 'See the findings for the next step.');
  return <div className="check-story">
    <div className={`check-outcome ${fixed ? 'fixed' : verified ? 'restored' : stopped ? 'attention' : 'working'}`}>
      <span className="outcome-symbol"><Mark kind={fixed ? 'shield' : verified ? 'pulse' : stopped ? 'pause' : 'search'}/></span>
      <div><h3>{fixed && complete ? 'Files download again' : title}</h3><p>{description}</p></div>
    </div>
    <dl className="result-facts">
      <div><dt>Problem</dt><dd>{before ? 'The problem reproduced in a test copy.' : run.diagnosis ? (run.diagnosis.summary.length > 180 ? run.diagnosis.summary.slice(0, 177) + '…' : run.diagnosis.summary) : stopped ? 'No cause confirmed.' : 'Still being investigated.'}</dd></div>
      <div><dt>Change</dt><dd>{fixed ? 'The tested code fix is installed.' : verified ? ({ restart_container: 'The app was restarted.', restore_config_snapshot: 'Saved settings were restored.', rollback_image: 'The previous version was restored.' }[run.diagnosis?.action ?? ''] ?? 'Availability was restored.') : run.phase === 'fix_proposed' ? 'A fix is ready. Your app is unchanged.' : run.phase === 'awaiting_approval' ? 'Waiting for your permission.' : 'No verified change yet.'}</dd></div>
      <div><dt>Checked</dt><dd>{before && after && <span className="result-test"><span>Test failed</span><span aria-hidden="true"> → </span><strong>Test passed</strong></span>}{checks.length > 0 ? <span>{delivered} of {checks.length} files arrived complete with matching contents.</span> : verified ? <span>The app responds to its checks.</span> : !before || !after ? <span>Results are not confirmed yet.</span> : null}</dd></div>
    </dl>
  </div>;
}
