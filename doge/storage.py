"""Durable datasets, run checkpoints, pair probabilities and request audit records."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def now():
    return datetime.now(timezone.utc).isoformat()


def dumps(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS datasets(id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS judgments(
                    dataset TEXT, model TEXT, prompt TEXT, voter TEXT, candidate TEXT,
                    data TEXT NOT NULL, PRIMARY KEY(dataset,model,prompt,voter,candidate));
                CREATE TABLE IF NOT EXISTS pairs(
                    run TEXT, voter TEXT, candidate TEXT, data TEXT NOT NULL,
                    PRIMARY KEY(run,voter,candidate));
                CREATE TABLE IF NOT EXISTS requests(
                    id INTEGER PRIMARY KEY, run TEXT, data TEXT NOT NULL);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def add(self, table, data):
        assert table in ('datasets', 'runs')
        value = {**data, 'id': uuid4().hex, 'created': now()}
        with self.connect() as db:
            db.execute(f'INSERT INTO {table} VALUES (?,?)', (value['id'], dumps(value)))
        return value

    def get(self, table, identifier):
        assert table in ('datasets', 'runs')
        with self.connect() as db:
            row = db.execute(f'SELECT data FROM {table} WHERE id=?', (identifier,)).fetchone()
        if row is None:
            raise KeyError(identifier)
        return json.loads(row['data'])

    def list(self, table):
        assert table in ('datasets', 'runs')
        with self.connect() as db:
            rows = db.execute(f'SELECT data FROM {table} ORDER BY rowid DESC').fetchall()
        return [json.loads(r['data']) for r in rows]

    def update_run(self, identifier, **changes):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM runs WHERE id=?', (identifier,)).fetchone()
            if row is None:
                raise KeyError(identifier)
            value = {**json.loads(row['data']), **changes, 'updated': now()}
            db.execute('UPDATE runs SET data=? WHERE id=?', (dumps(value), identifier))
        return value

    def configure_dataset(self, identifier, min_posts):
        from .domain import qualify_members
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM datasets WHERE id=?', (identifier,)).fetchone()
            if row is None:
                raise KeyError(identifier)
            data = json.loads(row['data'])
            if 'all_members' not in data:
                raise ValueError('この旧データセットの条件変更には、JSONLを再取り込みしてください')
            if data['min_posts'] == min_posts:
                return data
            members, excluded = qualify_members(data['all_members'], min_posts)
            data.update(min_posts=min_posts, members=members, excluded=excluded)
            used = db.execute("SELECT 1 FROM runs WHERE json_extract(data,'$.dataset_id')=? LIMIT 1",
                              (identifier,)).fetchone()
            if used:
                # Keep every existing run tied to its original electorate.
                data.update(id=uuid4().hex, created=now())
                db.execute('INSERT INTO datasets VALUES (?,?)', (data['id'], dumps(data)))
            else:
                db.execute('UPDATE datasets SET data=? WHERE id=?', (dumps(data), identifier))
            return data

    def cached(self, key):
        with self.connect() as db:
            row = db.execute('''SELECT data FROM judgments WHERE
                dataset=? AND model=? AND prompt=? AND voter=? AND candidate=?''', key).fetchone()
        return json.loads(row['data']) if row else None

    def save_pair(self, key, result, run_id, cached):
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO judgments VALUES (?,?,?,?,?,?)', (*key, dumps(result)))
            db.execute('INSERT OR IGNORE INTO pairs VALUES (?,?,?,?)',
                       (run_id, key[3], key[4], dumps({**result, 'cached': cached})))

    def pairs(self, run_id):
        with self.connect() as db:
            rows = db.execute('SELECT * FROM pairs WHERE run=? ORDER BY voter,candidate',
                              (run_id,)).fetchall()
        return [{'voter': r['voter'], 'candidate': r['candidate'], **json.loads(r['data'])} for r in rows]

    def log_request(self, run_id, record):
        with self.connect() as db:
            previous = db.execute(
                "SELECT id,data FROM requests WHERE run=? AND json_extract(data,'$.attempt_id')=?",
                (run_id, record.get('attempt_id'))).fetchone()
            if previous:
                value = {**json.loads(previous['data']), **record, 'received_at': now()}
                db.execute('UPDATE requests SET data=? WHERE id=?', (dumps(value), previous['id']))
            else:
                db.execute('INSERT INTO requests(run,data) VALUES (?,?)',
                           (run_id, dumps({'timestamp': now(), **record})))

    def requests(self, run_id):
        with self.connect() as db:
            rows = db.execute('SELECT data FROM requests WHERE run=? ORDER BY id', (run_id,)).fetchall()
        return [json.loads(r['data']) for r in rows]

    def metrics(self, run_id):
        pairs, requests = self.pairs(run_id), self.requests(run_id)
        tokens = sum(r.get('input_tokens', 0) for r in requests)
        return {'completed': len(pairs), 'cached': sum(p['cached'] for p in pairs),
                'refusals': sum(p['refusal'] for p in pairs), 'requests': len(requests),
                'input_tokens': tokens, 'cost_usd': tokens * 0.042 / 1e6,
                'unknown_usage': sum('input_tokens' not in r for r in requests)}
