"""Local FastAPI app. Run: uv run uvicorn doge.app:app --host 127.0.0.1."""
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode

from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .depth import evaluate
from .domain import configuration, estimate, parse_members
from .election import ENGINE_VERSION
from .judge import MODEL, PROMPT
from .service import Runner
from .storage import Store

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = Jinja2Templates(directory=Path(__file__).parent/'templates')
TEMPLATES.env.globals['person_url'] = lambda run, person: f'/runs/{run}/people?' + urlencode({'user_id': person})
STATUSES = {'queued': '待機中', 'running': '実行中', 'completed': '完了', 'failed': '停止中'}
TEMPLATES.env.globals['statuses'] = STATUSES


def create_app(db_path=None, judge_factory=None, load_environment=True):
    @asynccontextmanager
    async def lifespan(app):
        if load_environment:
            load_dotenv(ROOT/'.env', override=False)
        concurrency = int(os.getenv('DOGE_CONCURRENCY', '16'))
        if not 1 <= concurrency <= 64:
            raise ValueError('DOGE_CONCURRENCYは1〜64で指定してください')
        app.state.store = Store(db_path or os.getenv('DOGE_DB', str(ROOT/'data/doge.sqlite3')))
        app.state.runner = Runner(app.state.store, judge_factory, concurrency)
        await app.state.runner.start()
        try:
            yield
        finally:
            await app.state.runner.stop()

    app = FastAPI(title='コミュニティのためのドージェ選挙', lifespan=lifespan)
    app.mount('/static', StaticFiles(directory=Path(__file__).parent/'static'), name='static')

    def render(request, name, **context):
        return TEMPLATES.TemplateResponse(request=request, name=name, context=context)

    def fetch(request, table, identifier):
        try:
            return request.app.state.store.get(table, identifier)
        except KeyError:
            raise HTTPException(404, '見つかりません') from None

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        if request.url.path.startswith('/api/'):
            return JSONResponse({'error': str(exc)}, status_code=400)
        return TEMPLATES.TemplateResponse(request=request, name='error.html',
                                         context={'error': str(exc)}, status_code=400)

    @app.get('/', response_class=HTMLResponse)
    async def index(request: Request):
        return render(request, 'index.html', datasets=request.app.state.store.list('datasets'))

    @app.post('/datasets')
    async def import_dataset(request: Request, file: UploadFile = File(...),
                             name: str = Form(...)):
        raw = await file.read(5*1024*1024+1)
        await file.close()
        if len(raw) > 5*1024*1024:
            raise ValueError('ファイルは5MiB以内にしてください')
        try:
            content = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            raise ValueError('UTF-8のJSONLファイルを選んでください') from None
        if not name.strip():
            raise ValueError('名前を入力してください')
        members = parse_members(content)
        value = request.app.state.store.add('datasets', {
            'name': name.strip(), 'all_members': members,
            'min_posts': None, 'members': members, 'excluded': []})
        return RedirectResponse(f"/datasets/{value['id']}", status_code=303)

    @app.get('/datasets/{identifier}', response_class=HTMLResponse)
    async def dataset(request: Request, identifier: str):
        data = fetch(request, 'datasets', identifier)
        people = data.get('all_members', data['members'])
        return render(request, 'dataset.html', dataset=data, people=people,
                      posts=sum(len(p['posts']) for p in people),
                      truth_count=sum(p['truth'] is not None for p in people))

    @app.post('/datasets/{identifier}/eligibility')
    async def eligibility(request: Request, identifier: str, min_posts: int = Form(...)):
        fetch(request, 'datasets', identifier)
        data = request.app.state.store.configure_dataset(identifier, min_posts)
        return RedirectResponse(f"/datasets/{data['id']}", status_code=303)

    def require_eligibility(data):
        if data.get('min_posts') is None:
            raise ValueError('データ詳細で最低投稿数を設定してください')

    @app.get('/datasets/{identifier}/new', response_class=HTMLResponse)
    async def new_run(request: Request, identifier: str):
        data = fetch(request, 'datasets', identifier)
        require_eligibility(data)
        cfg = configuration(len(data['members']))
        return render(request, 'new.html', dataset=data, config=cfg,
                      estimate=estimate(len(data['members']), cfg), model=MODEL,
                      seed=secrets.randbits(32), key_ready=bool(os.getenv('TYPESAFE_API_KEY')))

    @app.get('/api/config/{identifier}')
    async def preview(request: Request, identifier: str, l: int | None = None,
                      e: int | None = None, k: int | None = None):
        data = fetch(request, 'datasets', identifier)
        manual = None
        if any(value is not None for value in (l, e, k)):
            if any(value is None for value in (l, e, k)):
                raise ValueError('手動設定はl・e・Kをすべて入力してください')
            manual = (l, e, k)
        require_eligibility(data)
        cfg = configuration(len(data['members']), manual)
        return {'config': cfg, 'estimate': estimate(len(data['members']), cfg)}

    @app.post('/datasets/{identifier}/runs')
    async def start_run(request: Request, identifier: str,
                        seed: int = Form(...), model: str = Form(MODEL),
                        manual: bool = Form(False), l: int = Form(3),
                        e: int = Form(6), k: int = Form(2)):
        data = fetch(request, 'datasets', identifier)
        if model != MODEL:
            raise ValueError(f'この版で利用できる判定器は{MODEL}です')
        if not judge_factory and not os.getenv('TYPESAFE_API_KEY'):
            raise ValueError('TYPESAFE_API_KEYを.envに設定し、サーバーを再起動してください')
        require_eligibility(data)
        cfg = configuration(len(data['members']), (l, e, k) if manual else None)
        run = request.app.state.store.add('runs', {
            'dataset_id': identifier, 'dataset_name': data['name'], 'config': cfg,
            'seed': seed, 'model': model, 'prompt_version': PROMPT,
            'engine_version': ENGINE_VERSION, 'status': 'queued', 'stages': [],
            'current': '待機中', 'stage_done': 0, 'stage_total': 0, 'winner': None,
            'error': None, 'estimate': estimate(len(data['members']), cfg)})
        request.app.state.runner.enqueue(run['id'])
        return RedirectResponse(f"/runs/{run['id']}", status_code=303)

    @app.get('/runs', response_class=HTMLResponse)
    async def runs(request: Request):
        return render(request, 'runs.html', runs=request.app.state.store.list('runs'))

    @app.get('/runs/{identifier}', response_class=HTMLResponse)
    async def run_page(request: Request, identifier: str):
        run = fetch(request, 'runs', identifier)
        data = fetch(request, 'datasets', run['dataset_id'])
        assessment = evaluate(data['members'], run['winner'])
        pair_lookup = {(p['voter'], p['candidate']): p
                       for p in request.app.state.store.pairs(identifier)}
        return render(request, 'run.html', run=run, dataset=data, pair_lookup=pair_lookup,
                      metrics=request.app.state.store.metrics(identifier), assessment=assessment,
                      map_data={'members': [{'user_id': p['user_id'], 'truth': p['truth']}
                                            for p in data['members']],
                                'stages': run['stages'], 'winner': run['winner'],
                                'centers': assessment['centers'] if assessment else []})

    @app.get('/api/runs/{identifier}')
    async def run_status(request: Request, identifier: str):
        run = fetch(request, 'runs', identifier)
        return {k: v for k, v in run.items() if k != 'rng_state'} | {
            'metrics': request.app.state.store.metrics(identifier)}

    @app.post('/runs/{identifier}/resume')
    async def resume(request: Request, identifier: str):
        run = fetch(request, 'runs', identifier)
        if run['status'] != 'failed':
            raise ValueError('停止中の実行だけを再開できます')
        request.app.state.store.update_run(identifier, status='queued', error=None)
        request.app.state.runner.enqueue(identifier)
        return RedirectResponse(f'/runs/{identifier}', status_code=303)

    @app.get('/runs/{identifier}/records')
    async def records(request: Request, identifier: str):
        run = fetch(request, 'runs', identifier)
        return {'run': run, 'pairs': request.app.state.store.pairs(identifier),
                'requests': request.app.state.store.requests(identifier)}

    @app.get('/runs/{identifier}/people', response_class=HTMLResponse)
    async def person(request: Request, identifier: str, user_id: str):
        run = fetch(request, 'runs', identifier)
        data = fetch(request, 'datasets', run['dataset_id'])
        member = next((p for p in data['members'] if p['user_id'] == user_id), None)
        if member is None:
            raise HTTPException(404, '人物が見つかりません')
        pairs = request.app.state.store.pairs(identifier)
        if run['engine_version'] in ('doge-v2', 'doge-v3'):
            # The same cached pair may have different votes in different stages.
            pairs = [{**pair, 'stage_label': stage['label'], 'line': stage['line']}
                     for stage in run['stages'] if stage['kind'] != 'lot'
                     for pair in pairs if pair['voter'] in stage['voters']]
        else:
            pairs = [{**pair, 'stage_label': '固定閾値', 'line': run['threshold']}
                     for pair in pairs]
        def approved(pair):
            return (not pair['refusal'] and pair['line'] is not None
                    and pair['probability'] >= pair['line'])
        return render(request, 'person.html', run=run, member=member,
                      outgoing=[p for p in pairs if p['voter'] == user_id],
                      incoming=[p for p in pairs if p['candidate'] == user_id], approved=approved)

    return app


app = create_app()
