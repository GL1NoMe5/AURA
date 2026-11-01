import { test } from 'node:test';
import assert from 'node:assert/strict';
import { sceneSteps } from '../src/ops/sceneSteps.ts';

const target = {target_id:'app-a', name:'My app', monitoring:true, observation:{healthy:true}};
const run = {target_id:'app-a', investigation_id:'run-a', phase:'investigating'};
const labels = (r, p=[], t=target, connected=true) => sceneSteps(t,r,p,connected);

test('an ongoing investigation never fabricates diagnosis, permission or success', () => {
  const nodes=labels(run);
  assert.equal(nodes[2].detail,'Investigating');
  assert.equal(nodes[3].detail,'Not requested');
  assert.equal(nodes[5].detail,'Not verified');
  assert.equal(labels({...run,phase:'verified_fixed'})[5].detail,'Not verified');
});
test('availability and cause repair require distinct verification evidence', () => {
  assert.equal(labels({...run,phase:'mitigated',verification:{verified:true,root_cause_fixed:false}})[5].detail,'Availability restored');
  assert.equal(labels({...run,phase:'verified_fixed',verification:{verified:true,root_cause_fixed:true}})[5].detail,'Cause fixed');
  assert.equal(labels({...run,phase:'verified_fixed',verification:{verified:false,root_cause_fixed:true}})[5].detail,'Not verified');
});
test('approval belongs to this run and distinguishes human permission from policy', () => {
  const r={...run,phase:'awaiting_approval'};
  const p={target_id:'app-a',investigation_id:'run-a',status:'proposed'};
  assert.equal(labels(r,[{...p,target_id:'app-b'}])[3].detail,'Not requested');
  assert.equal(labels(r,[{...p,investigation_id:'old-run'}])[3].detail,'Not requested');
  assert.equal(labels(r,[p])[3].detail,'Waiting for you');
  assert.equal(labels({...run,phase:'executing'},[{...p,status:'executing',automatic:true}])[3].detail,'Allowed by policy');
  assert.equal(labels({...run,phase:'executing'},[{...p,status:'executing',automatic:false}])[3].detail,'Granted');
});
test('disconnect, cancellation, foreign history and unknown action never imply an active successful repair', () => {
  assert.ok(labels(run,[],target,false).every(n=>n.tone!=='active' && n.tone!=='done'));
  assert.equal(labels({...run,phase:'cancelled'})[5].detail,'Check stopped');
  assert.equal(labels({...run,target_id:'app-b',phase:'verified_fixed',verification:{verified:true,root_cause_fixed:true}})[5].detail,'Not verified');
  const p={target_id:'app-a',investigation_id:'run-a',status:'unknown'};
  assert.equal(labels({...run,phase:'needs_human'},[p])[4].detail,'Outcome unknown');
  assert.equal(labels({...run,phase:'needs_human'},[p])[5].detail,'Needs your help');
});
