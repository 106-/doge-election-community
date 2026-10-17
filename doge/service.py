"""One persistent queue worker; inter-process lock prevents duplicate API work."""
import asyncio
import fcntl
import os
from contextlib import suppress
import httpx
from .election import execute
from .judge import JevJudge


class Runner:
    def __init__(self, store, factory=None, concurrency=16):
        self.store, self.factory, self.concurrency = store, factory, concurrency
        self.queue = asyncio.Queue()
        self.enqueued = set()
        self.task = None
        self.lock_file = None

    async def start(self):
        self.lock_file = open(str(self.store.path)+'.worker.lock', 'a')
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock_file.close()
            raise RuntimeError('同じデータベースで別のサーバーが実行中です。workers=1で起動してください') from None
        self.task = asyncio.create_task(self.work())
        for run in reversed(self.store.list('runs')):
            if run['status'] in ('queued', 'running'):
                self.enqueue(run['id'])

    def enqueue(self, identifier):
        if identifier not in self.enqueued:
            self.enqueued.add(identifier)
            self.queue.put_nowait(identifier)

    async def stop(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
        if self.lock_file:
            self.lock_file.close()

    async def work(self):
        async with httpx.AsyncClient(timeout=90) as client:
            while True:
                identifier = await self.queue.get()
                try:
                    judge = self.factory(client) if self.factory else JevJudge(
                        os.getenv('TYPESAFE_API_KEY', ''), client)
                    await execute(self.store, identifier, judge, self.concurrency)
                except (ValueError, RuntimeError) as exc:
                    self.store.update_run(identifier, status='failed', error=str(exc))
                except Exception as exc:
                    # No exception repr: provider exceptions could contain credentials.
                    self.store.update_run(identifier, status='failed',
                        error=f'実行に失敗しました ({type(exc).__name__})。リクエスト記録を確認してください')
                finally:
                    self.enqueued.discard(identifier)
                    self.queue.task_done()
