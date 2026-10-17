import asyncio
import json
import random
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

from analysis.auto_config import auto_config
from analysis.model import ORIGINAL, SIMPLIFIED, a_i, run as model_run
from doge.app import create_app
from doge.depth import evaluate
from doge.domain import configuration, parse_dataset
from doge.election import ENGINE_VERSION, elect, execute, stage_votes, final_elect, ranked
from doge.judge import MODEL, PROMPT, JevJudge, Judgment
from doge.service import Runner
from doge.storage import Store


def members(n=6):
    return [{'user_id': f'u{i}', 'posts': [str(i)]*5,
             'truth': {'x': i, 'y': (i % 2), 'group': str(i % 2)}} for i in range(n)]


def dataset(store, n=6):
    return store.add('datasets', {'name': 'Test community', 'language': 'en', 'source': 'test',
                                'members': members(n), 'excluded': [], 'min_posts': 5})


def new_run(store, data, seed=42):
    return store.add('runs', {'dataset_id': data['id'], 'dataset_name': data['name'],
        'config': configuration(len(data['members'])), 'seed': seed,
        'model': MODEL, 'prompt_version': PROMPT, 'engine_version': ENGINE_VERSION,
        'status': 'queued', 'stages': [], 'winner': None, 'current': '待機中',
        'stage_done': 0, 'stage_total': 0, 'error': None})


class FakeJudge:
    model, prompt_version = MODEL, PROMPT

    def __init__(self, fail_after=None, refusal=False):
        self.calls = []
        self.fail_after = fail_after
        self.refusal = refusal

    async def judge(self, voter_posts, candidate_posts, log):
        if self.fail_after is not None and len(self.calls) >= self.fail_after:
            raise RuntimeError('simulated interruption')
        v, c = int(voter_posts[0]), int(candidate_posts[0])
        self.calls.append((f'u{v}', f'u{c}'))
        log({'model': MODEL, 'input_tokens': 100, 'output_tokens': 0})
        return Judgment(None, True) if self.refusal else Judgment(((v*7+c*3) % 10)/10)


class RuleTests(unittest.TestCase):
    def test_stage_median_shared_line_and_refusals(self):
        def row(v, c, value):
            return dict(voter=v, candidate=c, probability=value, refusal=value is None)
        rows = [row('a','c',.1), row('a','d',.2),
                row('b','c',.8), row('b','d',.9), row('b','e',None)]
        approvals, line, rate = stage_votes(['a','b'], rows)
        self.assertEqual(line, .5)
        self.assertEqual(approvals, {'a':set(), 'b':{'c','d'}})
        self.assertEqual(rate, 2/5)
        approvals, line, rate = stage_votes(['a'], [row('a','b',.2), row('a','c',.5), row('a','d',.5)])
        self.assertEqual(line, .5)
        self.assertEqual(approvals['a'], {'c','d'})
        self.assertEqual(rate, 2/3)

    def test_saved_d82b06be_stage_medians(self):
        fixture = json.loads((Path(__file__).parent/'fixtures/d82b06be-scores.json').read_text())
        for stage, expected_line, expected_qualified in zip(fixture['stages'], [.685,.77,.78], [12,13,19]):
            rows = [dict(voter=v, candidate=c, probability=p, refusal=False)
                    for v, scores in zip(stage['voters'],stage['scores'])
                    for c,p in zip(fixture['candidates'],scores) if v != c]
            approvals, line, rate = stage_votes(stage['voters'], rows)
            counts = {c:sum(c in approvals[v] for v in stage['voters']) for c in fixture['candidates']}
            self.assertAlmostEqual(line, expected_line)
            self.assertEqual(sum(n >= stage['minimum'] for n in counts.values()), expected_qualified)
        self.assertEqual({c for c,n in counts.items() if n==12}, {'u010','u039','u046'})
        self.assertEqual(counts['u002'], 10)
        self.assertAlmostEqual(rate, .5, delta=.03)

    def test_final_self_votes_equalize_maximum_and_ties(self):
        voters, candidates = ['a','b'], ['a','b','c']
        approvals = {'a':{'b','c'}, 'b':{'a','c'}}
        rng = random.Random(42)
        result = final_elect(voters, candidates, approvals, 2, rng)
        self.assertEqual(result['counts'], {'a':2,'b':2,'c':2})
        self.assertFalse(result['below_threshold'])
        self.assertTrue(result['self_vote'])
        reference_rng = random.Random(42)
        self.assertEqual(result['selected'], ranked(candidates, result['counts'], reference_rng)[:1])
        self.assertEqual(rng.getstate(), reference_rng.getstate())
        winners = {final_elect(voters,candidates,approvals,2,random.Random(seed))['selected'][0]
                   for seed in range(20)}
        self.assertEqual(winners, set(candidates))
        self.assertEqual(elect(voters,candidates,approvals,1,2,random.Random(42))['counts'],
                         {'a':1,'b':1,'c':2})

    def test_saved_v2_final_self_vote_leaders(self):
        fixtures = json.loads((Path(__file__).parent/'fixtures/v2-final-scores.json').read_text())
        expected = [
            'u010 u013 u017 u026 u046 u047',
            'u006 u010 u012 u017 u019 u023 u029 u036 u046 u047',
            'u010 u012 u013 u016 u026 u029 u039 u046 u047',
            'u006 u010 u012 u013 u017 u046 u047',
            'u010 u012 u013 u017 u019 u026 u029 u046 u047',
        ]
        for fixture, leaders in zip(fixtures,expected):
            with self.subTest(run=fixture['source_run']):
                rows = [dict(voter=v,candidate=c,probability=p,refusal=False)
                        for v,scores in zip(fixture['voters'],fixture['scores'])
                        for c,p in zip(fixture['candidates'],scores) if v!=c]
                approvals,line,rate = stage_votes(fixture['voters'],rows)
                result = final_elect(fixture['voters'],fixture['candidates'],approvals,8,random.Random(42))
                self.assertEqual(line,fixture['line'])
                self.assertEqual(rate,fixture['approval_rate'])
                self.assertEqual(result['counts'], {c:n+int(c in fixture['voters'])
                                                    for c,n in fixture['counts'].items()})
                self.assertEqual({c for c,n in result['counts'].items() if n==12},set(leaders.split()))

    def test_configuration_examples_and_paper(self):
        for n, expected in [(50,(6,12,2)), (25,(4,10,3)), (15,(3,6,2)), (6,(2,4,2))]:
            cfg = auto_config(n)
            self.assertEqual((cfg['l'],cfg['e'],cfg['K']), expected)
        self.assertEqual([round(a_i(i),4) for i in range(3,7)], [.1955,.3929,.6071,.8045])
        for share in [.1,.2,.3,.4,.45]:
            self.assertLess(abs(model_run(ORIGINAL,480,round(480*share))[0]-
                                model_run(SIMPLIFIED,480,round(480*share))[0]), .001)
        for n in range(4,15):
            cfg = configuration(n)
            self.assertTrue(cfg['warning_text'])
        with self.assertRaises(ValueError): configuration(3)
        with self.assertRaises(ValueError): configuration(15, (2,4,2))
        self.assertTrue(configuration(15, (3,7,3))['manual'])

    def test_dataset_validation_and_exclusion(self):
        rows = members()
        rows[0]['posts'] = ['short']
        good, excluded = parse_dataset('\n'.join(json.dumps(p) for p in rows))
        self.assertEqual(len(good),5)
        self.assertEqual(excluded[0]['user_id'],'u0')
        with self.assertRaises(ValueError): parse_dataset(json.dumps(rows[0])+'\n'+json.dumps(rows[0]))
        rows[1]['truth']['x'] = float('nan')
        with self.assertRaises(ValueError): parse_dataset(json.dumps(rows[1]))
        with self.assertRaises(ValueError): parse_dataset('')

    def test_proportional_and_lottery_fallback(self):
        voters, ids = ['a','b','c'], list('abcdefgh')
        result = elect(voters, ids, {v:set() for v in voters}, 7, 3, random.Random(4))
        self.assertTrue(result['proportional'])
        self.assertEqual(len(set(result['selected'])),7)
        self.assertEqual(sorted(a['quota'] for a in result['allocation']), [2,2,3])
        self.assertEqual(sum(len(a['lottery_fallback']) for a in result['allocation']),7)
        approvals = {'a':set('de'), 'b':set('de'), 'c':set('df')}
        result = elect(voters, ids, approvals, 4, 3, random.Random(1))
        self.assertEqual(result['selected'][0], 'd')
        self.assertEqual(len(result['selected']), 4)
        for a in result['allocation']:
            self.assertTrue(set(a['approved']) <= approvals[a['voter']])

    def test_qualified_ranking_self_votes_and_seed(self):
        voters, ids = list('abc'), list('abcdef')
        approvals = {v:set(ids) for v in voters}
        result = elect(voters, ids, approvals, 2, 3, random.Random(8))
        self.assertFalse(result['proportional'])
        self.assertTrue(set(result['selected']) <= set('def'))
        self.assertEqual(result['counts']['a'],2)
        self.assertEqual(result,elect(voters,ids,approvals,2,3,random.Random(8)))

    def test_depth_collinear_coincident_and_square(self):
        def data(points):
            return [{'user_id':str(i), 'truth':{'x':x,'y':y,'group':'A'}} for i,(x,y) in enumerate(points)]
        result = evaluate(data([(-2,0),(-1,0),(0,0),(1,0),(2,0)]),'2')
        self.assertEqual(list(result['depths'].values()),[1,2,3,2,1])
        self.assertEqual(result['rank'],1)
        self.assertEqual(result['gap'],0)
        self.assertEqual(evaluate(data([(0,0),(0,0)]))['depths'],{'0':2,'1':2})
        square = evaluate(data([(-1,-1),(-1,1),(1,-1),(1,1),(0,0)]),'0')
        self.assertEqual(square['depths']['4'],3)
        self.assertEqual(square['centers'],['4'])
        self.assertEqual(square['rank'],2)
        self.assertIsNone(evaluate([{'user_id':'x','truth':None}]))


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name)/'db.sqlite3')
        self.data = dataset(self.store)

    async def test_needed_pairs_only_cache_and_refusal(self):
        r = new_run(self.store,self.data)
        judge = FakeJudge()
        await execute(self.store,r['id'],judge)
        completed = self.store.get('runs',r['id'])
        self.assertEqual(completed['status'],'completed')
        expected = {(v,c['user_id']) for s in completed['stages'] if s['kind'] != 'lot'
                    for v in s['voters'] for c in self.data['members'] if v != c['user_id']}
        self.assertEqual(set(judge.calls), expected)
        self.assertEqual(len(judge.calls),len(expected))
        self.assertTrue(all(v != c for v,c in judge.calls))
        same = new_run(self.store,self.data)
        no_calls = FakeJudge(fail_after=0)
        await execute(self.store,same['id'],no_calls)
        self.assertEqual(completed['stages'],self.store.get('runs',same['id'])['stages'])
        self.assertEqual(self.store.metrics(same['id'])['cost_usd'],0)
        # All refusals have no median and are always no.
        other = dataset(self.store)
        refusal_run = new_run(self.store,other)
        await execute(self.store,refusal_run['id'],FakeJudge(refusal=True))
        result = self.store.get('runs',refusal_run['id'])
        self.assertTrue(result['stages'][-1]['below_threshold'])
        self.assertIsNone(result['stages'][-1]['line'])
        self.assertEqual(result['stages'][-1]['approval_rate'], 0)
        final = result['stages'][-1]
        self.assertEqual(final['counts'], {c: int(c in final['voters']) for c in final['counts']})

    async def test_v2_reuses_v1_judgments_without_changing_old_run(self):
        previous = new_run(self.store, self.data)
        self.store.update_run(previous['id'], engine_version='doge-v1',
                              status='completed', threshold=.5)
        for voter in self.data['members']:
            for candidate in self.data['members']:
                v, c = voter['user_id'], candidate['user_id']
                if v != c:
                    self.store.save_pair((self.data['id'], MODEL, PROMPT, v, c),
                                         Judgment(.8).as_dict(), previous['id'], False)
        current = new_run(self.store, self.data)
        await execute(self.store, current['id'], FakeJudge(fail_after=0))
        self.assertEqual(self.store.get('runs', current['id'])['status'], 'completed')
        self.assertEqual(self.store.metrics(current['id'])['requests'], 0)
        self.assertEqual(self.store.get('runs', previous['id'])['engine_version'], 'doge-v1')
        self.assertEqual(self.store.get('runs', previous['id'])['threshold'], .5)

    async def test_model_upgrade_keeps_old_cache_and_run_separate(self):
        old = FakeJudge()
        old.model = 'jev-1.13.0'
        previous = new_run(self.store, self.data)
        self.store.update_run(previous['id'], model=old.model)
        await execute(self.store, previous['id'], old)
        current = new_run(self.store, self.data)
        judge = FakeJudge()
        await execute(self.store, current['id'], judge)
        self.assertEqual(set(old.calls), set(judge.calls))
        self.assertGreater(len(judge.calls), 0)
        self.assertEqual(self.store.metrics(current['id'])['cached'], 0)
        self.assertEqual(self.store.get('runs', previous['id'])['model'], old.model)
        pending = new_run(self.store, self.data)
        self.store.update_run(pending['id'], model=old.model)
        with self.assertRaisesRegex(ValueError, '版が一致しません'):
            await execute(self.store, pending['id'], judge)

    async def test_self_vote_changes_only_final_counts(self):
        def v2_final(voters,candidates,approvals,minimum,rng):
            counts = {c:sum(c in approvals[v] for v in voters) for c in candidates}
            winner = ranked(candidates,counts,rng)[0]
            return dict(counts=counts,selected=[winner],minimum=minimum,
                        below_threshold=counts[winner]<minimum)
        previous = new_run(self.store,self.data)
        with patch('doge.election.final_elect',side_effect=v2_final):
            await execute(self.store,previous['id'],FakeJudge())
        current = new_run(self.store,self.data)
        await execute(self.store,current['id'],FakeJudge(fail_after=0))
        old = self.store.get('runs',previous['id'])
        new = self.store.get('runs',current['id'])
        self.assertEqual(old['stages'][:-1],new['stages'][:-1])
        self.assertEqual(old['rng_state'],new['rng_state'])
        a,b = old['stages'][-1],new['stages'][-1]
        self.assertEqual(a['line'],b['line'])
        self.assertEqual(a['approval_rate'],b['approval_rate'])
        self.assertEqual(b['counts'],{c:n+int(c in b['voters']) for c,n in a['counts'].items()})
        self.assertTrue(b['self_vote'])

    async def test_partial_stage_resume_matches_uninterrupted(self):
        first = new_run(self.store,self.data)
        interrupted = FakeJudge(fail_after=14)
        with self.assertRaises(RuntimeError):
            await execute(self.store,first['id'],interrupted,concurrency=1)
        checkpoint = self.store.get('runs',first['id'])
        self.assertGreater(len(checkpoint['stages']),1)
        resumed_store = Store(self.store.path)
        resumed = FakeJudge()
        await execute(resumed_store,first['id'],resumed,concurrency=1)
        self.assertTrue(set(interrupted.calls).isdisjoint(resumed.calls))
        fresh_data = dataset(self.store)
        clean = new_run(self.store,fresh_data)
        await execute(self.store,clean['id'],FakeJudge(),concurrency=3)
        self.assertEqual(self.store.get('runs',first['id'])['stages'],
                         self.store.get('runs',clean['id'])['stages'])

    async def test_runner_start_recovers_running_and_prevents_two_workers(self):
        run = new_run(self.store,self.data)
        self.store.update_run(run['id'],status='running')
        runner = Runner(self.store,lambda client:FakeJudge())
        other = Runner(self.store,lambda client:FakeJudge())
        await runner.start()
        try:
            with self.assertRaises(RuntimeError): await other.start()
            await asyncio.wait_for(runner.queue.join(),5)
            self.assertEqual(self.store.get('runs',run['id'])['status'],'completed')
        finally:
            await runner.stop()

    async def test_jev_wire_format_retry_usage_and_no_identifiers(self):
        records, payloads, calls = [], [], []
        def log(record):
            records.append(record)
            self.store.log_request('wire-test', record)
        def handler(request):
            payloads.append(json.loads(request.content))
            calls.append(1)
            status = [429,529,200][len(calls)-1]
            return httpx.Response(status,json={'model':MODEL,'answers':{'agreement':{
                'type':'noul','noul':.75}},'usage':{'input_tokens':125,'output_tokens':5}},
                headers={'x-typesafe-request-id':f'req{len(calls)}'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with patch('doge.judge.asyncio.sleep',new_callable=AsyncMock) as sleep:
                result = await JevJudge('fake-secret',client).judge(['v post'],['c post'],log)
        self.assertEqual(result.probability,.75)
        self.assertEqual(len(records),6)
        self.assertEqual(len({r['attempt_id'] for r in records}),3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list],[1,2])
        self.assertEqual(payloads[0]['state'],{'voter':{'posts':['v post']},'candidate':{'posts':['c post']}})
        self.assertEqual(records[-1]['request_id'],'req3')
        self.assertNotIn('fake-secret',json.dumps(records))
        persisted = self.store.requests('wire-test')
        self.assertEqual(len(persisted),3)
        self.assertTrue(all('timestamp' in r and 'received_at' in r for r in persisted))
        self.assertEqual(self.store.metrics('wire-test')['unknown_usage'],0)


class WebTests(unittest.TestCase):
    def test_eligibility_after_preview_preserves_posts_and_old_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(Path(directory)/'web.sqlite3',lambda client:FakeJudge(),False)
            with TestClient(app) as client:
                home = client.get('/').text
                for field in ('language','source','min_posts'):
                    self.assertNotIn(f'name="{field}"',home)
                people = members()
                people[0]['posts'] = ['short post']
                response = client.post('/datasets',data={'name':'Preview'},
                    files={'file':('data.jsonl','\n'.join(json.dumps(p) for p in people))})
                self.assertEqual(response.status_code,200)
                identifier = str(response.url).split('/')[-1]
                self.assertIn('short post',response.text)
                self.assertIn('name="min_posts"',response.text)
                self.assertEqual(client.get(f'/datasets/{identifier}/new').status_code,400)
                self.assertEqual(client.post(f'/datasets/{identifier}/runs',data={'seed':42}).status_code,400)
                self.assertEqual(client.post(f'/datasets/{identifier}/eligibility',data={'min_posts':0}).status_code,400)
                client.post(f'/datasets/{identifier}/eligibility',data={'min_posts':5})
                store = app.state.store
                self.assertEqual(len(store.get('datasets',identifier)['members']),5)
                client.post(f'/datasets/{identifier}/eligibility',data={'min_posts':1})
                self.assertEqual(len(store.get('datasets',identifier)['members']),6)
                self.assertIn('short post',client.get(f'/datasets/{identifier}').text)
                old = new_run(store,store.get('datasets',identifier))
                store.update_run(old['id'],status='completed')
                response = client.post(f'/datasets/{identifier}/eligibility',data={'min_posts':5})
                new_id = str(response.url).split('/')[-1]
                self.assertNotEqual(identifier,new_id)
                self.assertEqual(len(store.get('datasets',identifier)['members']),6)
                self.assertEqual(len(store.get('datasets',new_id)['members']),5)
                self.assertEqual(store.get('runs',old['id'])['dataset_id'],identifier)

    def test_import_run_results_and_invalid_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(Path(directory)/'web.sqlite3',lambda client:FakeJudge(),False)
            with TestClient(app) as client:
                self.assertEqual(client.get('/').status_code,200)
                data = members()
                data[0]['user_id'] = '<script>alert(1)</script>'
                response = client.post('/datasets',data={'name':'Community','language':'ja'},
                    files={'file':('data.jsonl','\n'.join(json.dumps(p) for p in data),'application/json')})
                self.assertEqual(response.status_code,200)
                self.assertIn('&lt;script&gt;',response.text)
                identifier = str(response.url).split('/')[-1]
                client.post(f'/datasets/{identifier}/eligibility', data={'min_posts':5})
                page = client.get(f'/datasets/{identifier}/new')
                self.assertEqual(page.status_code,200)
                self.assertIn('基準とのずれ',page.text)
                self.assertNotIn('name="threshold"',page.text)
                bad = client.post(f'/datasets/{identifier}/runs',data={'seed':'invalid'})
                self.assertIn(bad.status_code,(400,422))
                bad = client.get(f'/api/config/{identifier}?l=9&e=3&k=1')
                self.assertEqual(bad.status_code,400)
                response = client.post(f'/datasets/{identifier}/runs',data={'seed':42})
                self.assertEqual(response.status_code,200)
                run_id = str(response.url).split('/')[-1]
                import time
                for _ in range(100):
                    status = client.get(f'/api/runs/{run_id}').json()
                    if status['status'] in ('completed','failed'): break
                    time.sleep(.01)
                self.assertEqual(status['status'],'completed',status.get('error'))
                page = client.get(f'/runs/{run_id}')
                self.assertEqual(page.status_code,200)
                self.assertIn('選ばれたドージェ',page.text)
                self.assertNotIn('<script>alert(1)</script>',page.text)
                self.assertEqual(client.get(f'/runs/{run_id}/people',params={'user_id':data[0]['user_id']}).status_code,200)
                records = client.get(f'/runs/{run_id}/records').json()
                self.assertTrue(records['pairs'])
                self.assertNotIn('threshold', records['run'])
                self.assertIn('賛成の割合', page.text)
                self.assertIn('誰が誰に賛成したか（行列）', page.text)
                self.assertIn('○自', page.text)
                self.assertIn('class="vote-matrix-panel"', page.text)
                self.assertTrue(records['run']['stages'][-1]['self_vote'])
                self.assertIn('自己票1を含む', page.text)
                # v2 still renders median lines, without claiming self votes.
                store = app.state.store
                store.update_run(run_id, engine_version='doge-v2',
                                 stages=[{k:v for k,v in stage.items() if k != 'self_vote'}
                                         for stage in records['run']['stages']])
                v2_page = client.get(f'/runs/{run_id}')
                self.assertIn('段階ごとの中央値',v2_page.text)
                self.assertNotIn('自己票1を含む',v2_page.text)
                self.assertNotIn('○自',v2_page.text)
                self.assertIn('誰が誰に賛成したか（行列）',v2_page.text)
                self.assertEqual(client.get(f'/runs/{run_id}/people',
                                           params={'user_id':data[0]['user_id']}).status_code,200)
                # Old completed runs and their member pages retain fixed thresholds.
                store = app.state.store
                store.update_run(run_id, engine_version='doge-v1', threshold=.5,
                                 stages=[{k:v for k,v in stage.items()
                                          if k not in ('line', 'approval_rate', 'self_vote')}
                                         for stage in records['run']['stages']])
                self.assertIn('固定閾値 0.5', client.get(f'/runs/{run_id}').text)
                old_person = client.get(f'/runs/{run_id}/people',params={'user_id':data[0]['user_id']})
                self.assertEqual(old_person.status_code, 200)
                self.assertIn('固定閾値', old_person.text)
                self.assertEqual(client.get('/runs').status_code,200)
                self.assertEqual(client.get('/runs/missing').status_code,404)
                self.assertEqual(client.post(f'/runs/{run_id}/resume').status_code,400)


if __name__ == '__main__':
    unittest.main()
