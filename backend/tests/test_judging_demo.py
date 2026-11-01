"""No Docker or provider calls. Exercise public boundaries and durable admission."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import uuid
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import pytest

from aura_demo.app import create_demo_app, InstanceLock, COOKIE
from aura_demo.config import DemoSettings
from aura_demo.fixture import LinkdingFixture, LABEL
from aura_demo.ledger import DemoLedger
from aura_demo.runner import BudgetAdapter, DemoRunner, public_observation
from aura_ops.models import OpsError


@pytest.fixture
def config(tmp_path):
    return DemoSettings(root=tmp_path, data=tmp_path / 'state', origin='http://127.0.0.1:8019',
                        access_code='test-only-access-code-not-a-credential', cooldown=0)


def test_public_origin_and_image_constraints(config):
    for origin in ('http://example.com', 'https://example.com/', 'https://x.test/path', 'https://user@x.test'):
        with pytest.raises(ValueError):
            replace(config, origin=origin)
    with pytest.raises(ValueError):
        replace(config, image='linkding:latest')
    with pytest.raises(ValueError):
        replace(config, access_code='short')
    assert replace(config, origin='https://demo.example.com').origin.startswith('https:')


def test_durable_quota_idempotency_and_one_active(config):
    ledger = DemoLedger(replace(config, max_runs=2, session_runs=3))
    token = ledger.new_session(); session = ledger.session(token); key = str(uuid.uuid4())
    first, created = ledger.reserve(session, key)
    assert created and ledger.reserve(session, key) == (first, False)
    with pytest.raises(OpsError, match='Another recovery'):
        ledger.reserve(session, str(uuid.uuid4()))
    ledger.reserve_call(first)
    ledger.save(first, {'phase': 'failed'}, finished=True)
    ledger.close()
    ledger = DemoLedger(replace(config, max_runs=2, session_runs=3))
    assert ledger.session(token) == session
    assert ledger.row(first)['calls'] == 1
    second, _ = ledger.reserve(session, str(uuid.uuid4()))
    ledger.save(second, {'phase': 'cancelled'}, finished=True)
    with pytest.raises(OpsError, match='usage limit'):
        ledger.reserve(session, str(uuid.uuid4()))
    ledger.close()


def test_failed_requests_consume_calls_and_per_run_is_bounded(config):
    ledger = DemoLedger(config)
    run_id, _ = ledger.reserve('test', str(uuid.uuid4()))
    for _ in range(config.calls_per_run):
        ledger.reserve_call(run_id)
    with pytest.raises(OpsError, match='request limit'):
        ledger.reserve_call(run_id)
    assert ledger.row(run_id)['calls'] == 7
    ledger.save(run_id, {}, finished=True)
    with pytest.raises(OpsError):
        ledger.reserve_call(run_id)
    ledger.close()


def test_global_calls_no_new_run_with_insufficient_budget(config):
    ledger = DemoLedger(replace(config, max_calls=7))
    run_id, _ = ledger.reserve('test', str(uuid.uuid4()))
    ledger.reserve_call(run_id)
    ledger.save(run_id, {}, finished=True)
    with pytest.raises(OpsError, match='usage limit'):
        ledger.reserve('different-visit', str(uuid.uuid4()))
    ledger.close()


def test_cleanup_failure_blocks_next_visit(config):
    ledger = DemoLedger(config)
    run_id, _ = ledger.reserve('test', str(uuid.uuid4()))
    ledger.save(run_id, {}, finished=True, blocked=True)
    with pytest.raises(OpsError, match='Another recovery'):
        ledger.reserve('another-visit', str(uuid.uuid4()))
    ledger.close()


def test_single_worker_lock(config):
    lock = InstanceLock(config.data)
    with pytest.raises(RuntimeError):
        InstanceLock(config.data)
    lock.close()
    InstanceLock(config.data).close()


class FakeRunner:
    def __init__(self, settings, ledger):
        self.ledger, self.launched = ledger, []
        self.cancel_requested = False
    def ready(self): return True
    async def startup(self): pass
    async def shutdown(self): pass
    def launch(self, run_id): self.launched.append(run_id)
    async def cancel(self, run_id): self.ledger.save(run_id, {'phase':'cancelled'}, finished=True)


def test_public_routes_auth_csrf_reconnect_and_session_isolation(config):
    async def scenario():
        app = create_demo_app(config, runner_factory=FakeRunner)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url=config.origin) as client:
                headers = {'Origin':config.origin, 'X-Aura-Demo':'1'}
                status = (await client.get('/api/demo/status')).json()
                assert status['run'] is None and not status['authenticated']
                assert (await client.post('/api/demo/runs', json={}, headers=headers)).status_code == 401
                assert (await client.post('/api/demo/session', json={'code':config.access_code})).status_code == 403
                assert (await client.post('/api/demo/session', json={'code':config.access_code}, headers={**headers,'Origin':'https://evil.test'})).status_code == 403
                assert (await client.post('/api/demo/session', content='x'*2048, headers={**headers,'Content-Type':'application/json'})).status_code == 413
                response = await client.post('/api/demo/session', json={'code':config.access_code}, headers=headers)
                assert response.status_code == 200 and 'HttpOnly' in response.headers['set-cookie']
                assert 'SameSite=strict' in response.headers['set-cookie']
                assert config.access_code not in response.text
                for url in ('/api/v2/status', '/api/v2/targets', '/docs', '/openapi.json', '/.env', '/backend/aura_demo/app.py'):
                    assert (await client.get(url)).status_code == 404
                for url in ('/api/v2/targets', '/api/incidents', '/api/v2/proposals/x/approve'):
                    assert (await client.post(url,json={},headers=headers)).status_code in (404,405)
                assert (await client.post('/api/demo/runs',json={'target':'evil'},headers=headers)).status_code == 400
                request_headers = {**headers, 'Idempotency-Key':str(uuid.uuid4())}
                one = (await client.post('/api/demo/runs', json={}, headers=request_headers)).json()
                again = (await client.post('/api/demo/runs', json={}, headers=request_headers)).json()
                assert one['created'] and not again['created'] and one['run_id'] == again['run_id']
                assert len(app.state.runner.launched) == 1
                for _ in range(5):
                    assert (await client.get('/api/demo/status')).status_code == 200
                assert app.state.ledger.row(one['run_id'])['calls'] == 0
                client.cookies.clear()
                await client.post('/api/demo/session',json={'code':config.access_code},headers=headers)
                assert (await client.get('/api/demo/status')).json()['run'] is None
                assert (await client.post('/api/demo/runs/'+one['run_id']+'/cancel',json={},headers=headers)).status_code == 404
                assert (await client.get('/health',headers={'Host':'evil.test'})).status_code == 400
    asyncio.run(scenario())


def test_secure_cookie_for_public_origin(config):
    async def scenario():
        secure = replace(config, origin='https://demo.example.com')
        app = create_demo_app(secure, runner_factory=FakeRunner)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=secure.origin) as client:
                response = await client.post('/api/demo/session',json={'code':secure.access_code},headers={'Origin':secure.origin,'X-Aura-Demo':'1'})
                assert 'Secure' in response.headers['set-cookie']
    asyncio.run(scenario())


def test_login_rate_limit(config):
    ledger = DemoLedger(config)
    for _ in range(10): ledger.login_attempt()
    with pytest.raises(OpsError) as error: ledger.login_attempt()
    assert error.value.status == 429
    ledger.close()


def test_projection_excludes_logs_config_paths_and_credentials():
    observation = {'timestamp':'now','healthy':True,'logs':'private','config':{'secret':'private'},
                   'identity':{'path':'private'},'probes':[{'url':'http://127.0.0.1:999/api/bookmarks/','ok':True,'status_code':200,'duration_ms':3}]}
    public = public_observation(observation)
    assert 'private' not in json.dumps(public) and '127.0.0.1' not in json.dumps(public)
    assert public['probes'][0]['name'] == 'Bookmarks'


def test_budget_adapter_reserves_before_failure_and_rejects_late_call(config):
    class Failing:
        ready = True
        async def complete(self,*args,**kwargs): raise RuntimeError('upstream failed')
        async def aclose(self): pass
    async def scenario():
        ledger = DemoLedger(config); rid,_ = ledger.reserve('test',str(uuid.uuid4()))
        adapter = BudgetAdapter(Failing(),ledger,rid)
        with pytest.raises(RuntimeError): await adapter.complete([{'role':'user','content':'test'}])
        assert ledger.row(rid)['calls'] == 1
        await adapter.aclose()
        with pytest.raises(asyncio.CancelledError): await adapter.complete([])
        assert ledger.row(rid)['calls'] == 1
        ledger.close()
    asyncio.run(scenario())


def test_fixture_ownership_and_static_topology(config):
    fixture = LinkdingFixture(config,'a'*32,'b'*32)
    fixture.image_id = 'sha256:'+'1'*64
    compose = fixture.compose()
    assert 'ports' not in compose['services']['linkding']
    assert compose['services']['gateway']['ports'] == ['127.0.0.1:18097:8080']
    assert compose['services']['gateway']['read_only']
    assert '/var/run/docker.sock' not in json.dumps(compose)
    with pytest.raises(OpsError): fixture.owned('container',{'Config':{'Labels':{LABEL:'another-owner'}}})


def test_interrupted_run_cleaned_not_resumed(config):
    class Fixture:
        cleaned = []
        def __init__(self,settings,owner,run_id): self.run_id=run_id
        async def cleanup(self): self.cleaned.append(self.run_id); return []
    async def scenario():
        ledger = DemoLedger(config); rid,_=ledger.reserve('test',str(uuid.uuid4())); ledger.reserve_call(rid)
        runner = DemoRunner(config,ledger,fixture_factory=Fixture)
        await runner.startup()
        row=ledger.row(rid)
        assert not row['active'] and row['calls']==1
        assert json.loads(row['report'])['phase']=='interrupted' and Fixture.cleaned==[rid]
        assert runner.task is None
        ledger.close()
    asyncio.run(scenario())


def test_cancel_during_preparation_waits_for_cleanup_and_never_calls_provider(config):
    class Adapter:
        ready = True
        calls = 0
        closed = False
        async def complete(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError('No provider request during preparation')
        async def aclose(self): self.closed = True

    async def scenario():
        entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        class Fixture:
            def __init__(self, *args): pass
            async def provision(self):
                entered.set()
                await asyncio.Event().wait()
            async def cleanup(self):
                cleaning.set()
                await release.wait()
                return []
        ledger = DemoLedger(config); rid, _ = ledger.reserve('test', str(uuid.uuid4()))
        adapter = Adapter()
        runner = DemoRunner(config, ledger, adapter_factory=lambda: adapter, fixture_factory=Fixture)
        runner.launch(rid)
        await entered.wait()
        first = asyncio.create_task(runner.cancel(rid))
        await cleaning.wait()
        second = asyncio.create_task(runner.cancel(rid))
        await asyncio.sleep(0)
        assert not runner.task.done() and ledger.active()
        release.set()
        await asyncio.gather(first, second)
        report = json.loads(ledger.row(rid)['report'])
        assert report['phase'] == 'cancelled' and report['verified'] is False
        assert report['cleanup_complete'] and not ledger.active()
        assert adapter.closed and adapter.calls == 0
        ledger.close()
    asyncio.run(scenario())


def test_failed_preparation_cleanup_failure_stays_blocked(config):
    class Adapter:
        ready = True
        async def aclose(self): pass
    class Fixture:
        def __init__(self, *args): pass
        async def provision(self): raise OpsError('test_fixture_error', 'Private details')
        async def cleanup(self): return ['volume']
    async def scenario():
        ledger = DemoLedger(config); rid, _ = ledger.reserve('test', str(uuid.uuid4()))
        runner = DemoRunner(config, ledger, adapter_factory=Adapter, fixture_factory=Fixture)
        await runner.run(rid)
        row = ledger.row(rid); report = json.loads(row['report'])
        assert row['active'] and row['calls'] == 0
        assert report['phase'] == 'failed' and report['verified'] is False
        assert not report['cleanup_complete'] and 'Private details' not in row['report']
        with pytest.raises(OpsError): ledger.reserve('another', str(uuid.uuid4()))
        ledger.close()
    asyncio.run(scenario())


def test_cleanup_will_not_remove_foreign_resources(config):
    async def scenario():
        fixture = LinkdingFixture(config, 'a'*32, 'b'*32)
        commands = []
        async def inspect(kind, name, **kwargs):
            return {'Id': 'foreign', 'Name': name, 'Labels': {LABEL: 'foreign'},
                    'Config': {'Labels': {LABEL: 'foreign'}}}
        async def command(*args, **kwargs): commands.append(args)
        fixture.inspect, fixture.command = inspect, command
        failures = await fixture.cleanup()
        assert len(failures) == 6 and not commands
    asyncio.run(scenario())


def test_input_output_limits_precede_paid_reservation(config):
    class Adapter:
        ready = True
        async def complete(self, *args, **kwargs): raise AssertionError('Must not dispatch')
        async def aclose(self): pass
    async def scenario():
        ledger = DemoLedger(config); rid, _ = ledger.reserve('test', str(uuid.uuid4()))
        adapter = BudgetAdapter(Adapter(), ledger, rid)
        for messages, options in (([{'role':'user','content':'x'*160001}], {}), ([], {'max_tokens':4097})):
            with pytest.raises(OpsError): await adapter.complete(messages, **options)
        assert ledger.row(rid)['calls'] == 0
        ledger.close()
    asyncio.run(scenario())
