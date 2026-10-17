"""The requirements' seeded lottery/approval procedure; independent of truth coordinates."""
import asyncio
import random
from statistics import median

ENGINE_VERSION = 'doge-v3'


def stage_votes(voters, judgments):
    """One shared median; refusals count as no in the approval-rate denominator."""
    values = [p['probability'] for p in judgments if not p['refusal']]
    line = median(values) if values else None
    approvals = {v: set() for v in voters}
    for p in judgments:
        if not p['refusal'] and line is not None and p['probability'] >= line:
            approvals[p['voter']].add(p['candidate'])
    rate = sum(map(len, approvals.values())) / len(judgments) if judgments else 0.0
    return approvals, line, rate


def ranked(candidates, counts, rng):
    result = list(candidates)
    rng.shuffle(result)
    return sorted(result, key=lambda candidate: -counts[candidate])


def elect(voters, candidates, approvals, seats, minimum, rng):
    counts = {c: sum(c in approvals[v] and c != v for v in voters) for c in candidates}
    qualified = [c for c in candidates if counts[c] >= minimum]
    selected = ranked(qualified, counts, rng)[:seats]
    proportional, allocation = len(selected) < seats, []
    if proportional:
        base, extra = divmod(seats-len(selected), len(voters))
        bonus = set(rng.sample(voters, extra))
        for voter in rng.sample(voters, len(voters)):
            quota = base + (voter in bonus)
            available = [c for c in candidates if c not in selected and c != voter
                         and c in approvals[voter]]
            approved = ranked(available, counts, rng)[:quota]
            fallback = rng.sample([c for c in candidates if c not in selected and c not in approved],
                                  quota-len(approved))
            selected.extend(approved + fallback)
            allocation.append({'voter': voter, 'quota': quota, 'approved': approved,
                               'lottery_fallback': fallback})
    return {'counts': counts, 'selected': selected, 'minimum': minimum,
            'proportional': proportional, 'allocation': allocation}


def final_elect(voters, candidates, approvals, minimum, rng):
    counts = {c: sum(c in approvals[v] for v in voters if v != c) + int(c in voters)
              for c in candidates}
    winner = ranked(candidates, counts, rng)[0]
    return {'counts': counts, 'selected': [winner], 'minimum': minimum,
            'below_threshold': counts[winner] < minimum, 'self_vote': True}


def as_tuple(value):
    return tuple(as_tuple(item) for item in value) if isinstance(value, list) else value


async def execute(store, run_id, judge, concurrency=16):
    run = store.get('runs', run_id)
    if run['engine_version'] != ENGINE_VERSION:
        raise ValueError('実行エンジンの版が一致しません')
    if (run['model'], run['prompt_version']) != (judge.model, judge.prompt_version):
        raise ValueError('保存済み実行と判定器・指示文の版が一致しません')
    if run['status'] == 'completed':
        return
    if not 1 <= concurrency <= 64:
        raise ValueError('同時実行数は1〜64で指定してください')
    members = store.get('datasets', run['dataset_id'])['members']
    people = {p['user_id']: p['posts'] for p in members}
    ids, cfg = list(people), run['config']
    rng = random.Random(run['seed'])
    stages = run.get('stages', [])
    if stages:
        rng.setstate(as_tuple(run['rng_state']))
    college = stages[-1]['selected'] if stages else ids
    sequence = ['lot', 'elect']*cfg['K'] + ['final']
    semaphore = asyncio.Semaphore(concurrency)
    store.update_run(run_id, status='running', error=None)

    async def judge_college(voters):
        judgments = []
        done = 0
        store.update_run(run_id, stage_done=0, stage_total=len(voters)*(len(ids)-1))

        async def one(v, c):
            nonlocal done
            async with semaphore:
                key = (run['dataset_id'], judge.model, judge.prompt_version, v, c)
                result = store.cached(key)
                cached = result is not None
                if result is None:
                    answer = await judge.judge(people[v], people[c], lambda record:
                        store.log_request(run_id, {**record, 'voter': v, 'candidate': c}))
                    result = answer.as_dict()
                store.save_pair(key, result, run_id, cached)
                judgments.append({'voter': v, 'candidate': c, **result})
                done += 1
                store.update_run(run_id, stage_done=done)
        tasks = [asyncio.create_task(one(v, c)) for v in voters for c in ids if v != c]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return stage_votes(voters, judgments)

    for index in range(len(stages), len(sequence)):
        kind = sequence[index]
        label = 'ドージェ選出' if kind == 'final' else f'{index//2+1}回目の' + (
            'くじ' if kind == 'lot' else '選出')
        store.update_run(run_id, current=label, stage_done=0, stage_total=0)
        if kind == 'lot':
            result = {'source': list(college), 'selected': rng.sample(college, cfg['l'])}
        else:
            approvals, line, approval_rate = await judge_college(college)
            if kind == 'elect':
                result = elect(college, ids, approvals, cfg['e'], cfg['m'], rng)
            else:
                result = final_elect(college, ids, approvals, cfg['final'], rng)
            result.update(voters=list(college), line=line, approval_rate=approval_rate)
        stages.append({'kind': kind, 'label': label, **result})
        college = result['selected']
        fields = {'stages': stages, 'rng_state': rng.getstate()}
        if kind == 'final':
            fields.update(status='completed', winner=college[0], current='完了')
        store.update_run(run_id, **fields)
