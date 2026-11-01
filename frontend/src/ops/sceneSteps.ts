import type { Investigation, Proposal, Target } from './useOps';

export type SceneStep = { name: string; detail: string; tone: 'quiet' | 'active' | 'done' | 'attention' };
export const GENERAL_CHECK = 'Investigate the application health, recent failures and resource usage.';

// These are parts of an investigation, not hosts or a count of active agents.
// Completion always comes from recorded evidence, never a phase's position in a list.
export function sceneSteps(target: Target | undefined, run: Investigation | undefined, proposals: Proposal[], connected: boolean): SceneStep[] {
  const step = (name: string, detail: string, tone: SceneStep['tone'] = 'quiet'): SceneStep => ({ name, detail, tone });
  const idle = run ? 'Not recorded' : 'No check yet';
  const nodes = [step('App', 'Not connected'), step('Observations', idle), step('Diagnosis', idle),
    step('Permission', 'Not requested'), step('Change', 'None recorded'), step('Result', 'Not verified')];
  if (target) nodes[0] = step(target.name, !connected ? 'Connection lost' : !target.monitoring ? 'Watching paused'
    : target.observation?.healthy === true ? 'Reachable' : target.observation?.healthy === false ? 'Needs attention' : 'Waiting for data',
    connected && target.monitoring && target.observation?.healthy === false ? 'attention' : 'quiet');
  if (!connected) return nodes.map((node, i) => i === 0 ? node : step(node.name, 'Connection lost'));
  if (!run || run.target_id !== target?.target_id) return nodes;
  if (run.evidence && Object.keys(run.evidence).length) nodes[1] = step('Observations', 'Recorded', 'done');
  if (run.phase === 'investigating') {
    nodes[1] = step('Observations', 'Collecting and reviewing', 'active');
    nodes[2] = step('Diagnosis', 'Investigating', 'active');
  } else if (run.diagnosis) nodes[2] = step('Diagnosis', run.diagnosis.action === 'abstain' ? 'No action recommended' : 'Ready', 'done');
  const proposal = proposals.find(p => p.target_id === run.target_id && p.investigation_id === run.investigation_id);
  if (proposal?.status === 'proposed' && run.phase === 'awaiting_approval') nodes[3] = step('Permission', 'Waiting for you', 'active');
  else if (proposal?.status === 'rejected') nodes[3] = step('Permission', 'Declined');
  else if (proposal && ['executing', 'dispatching', 'executed', 'verifying', 'verified', 'verification_failed', 'rolled_back'].includes(proposal.status)) {
    nodes[3] = step('Permission', proposal.automatic === true ? 'Allowed by policy' : 'Granted', 'done');
  } else if (proposal) nodes[3] = step('Permission', 'Review needed', 'attention');
  if (run.phase === 'preparing_patch') nodes[4] = step('Change', 'Preparing and testing', 'active');
  else if (run.phase === 'fix_proposed') nodes[4] = step('Change', run.patch?.status === 'tests_passed' ? 'Tested · not installed' : 'Awaiting test results');
  else if (run.phase === 'preparing_deployment') nodes[4] = step('Change', 'Preparing installation', 'active');
  else if (run.phase === 'executing') nodes[4] = step('Change', 'Applying', 'active');
  else if (proposal && ['executed', 'verifying', 'verified', 'verification_failed'].includes(proposal.status)) nodes[4] = step('Change', 'Applied · see result', 'done');
  else if (proposal?.status === 'rolled_back') nodes[4] = step('Change', 'Rolled back', 'attention');
  else if (proposal?.status === 'unknown') nodes[4] = step('Change', 'Outcome unknown', 'attention');
  else if (proposal?.status === 'failed') nodes[4] = step('Change', 'Failed', 'attention');
  if (run.phase === 'verifying') nodes[5] = step('Result', 'Checking', 'active');
  else if (run.verification?.verified) nodes[5] = step('Result', run.phase === 'verified_fixed' && run.verification.root_cause_fixed ? 'Cause fixed' : 'Availability restored', 'done');
  else if (run.phase === 'cancelled') nodes[5] = step('Result', 'Check stopped');
  else if (run.phase === 'needs_human') nodes[5] = step('Result', !run.error && run.diagnosis?.action === 'abstain' ? 'No change made' : 'Needs your help', 'attention');
  return nodes;
}
