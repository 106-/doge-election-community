"""Replaceable judge protocol. Jev wire format: https://docs.typesafe.ai/api."""
import asyncio
import math
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4
import httpx

MODEL = 'jev-1.14.0'
PROMPT = 'agreement-v1'
INSTRUCTIONS = ("Based on the voter's posts, would the voter agree with the opinions expressed "
                "in the candidate's posts? Assess opinions, not tone or writing style.")


@dataclass(frozen=True)
class Judgment:
    probability: float | None
    refusal: bool = False

    def __post_init__(self):
        if not self.refusal and (self.probability is None or isinstance(self.probability, bool)
                                or not math.isfinite(self.probability)
                                or not 0 <= self.probability <= 1):
            raise ValueError('判定確率が0〜1の範囲外です')

    def as_dict(self):
        return {'probability': None if self.refusal else self.probability, 'refusal': self.refusal}


class Judge(Protocol):
    model: str
    prompt_version: str
    async def judge(self, voter_posts: list[str], candidate_posts: list[str], log) -> Judgment: ...


class JevJudge:
    model = MODEL
    prompt_version = PROMPT

    def __init__(self, api_key, client):
        if not api_key:
            raise ValueError('TYPESAFE_API_KEYを.envまたは環境変数に設定してください')
        self.key, self.client = api_key, client

    async def judge(self, voter_posts, candidate_posts, log):
        payload = {'model': self.model,
                   'state': {'voter': {'posts': voter_posts}, 'candidate': {'posts': candidate_posts}},
                   'questions': {'agreement': {'type': 'noul', 'instructions': INSTRUCTIONS}}}
        for attempt in range(6):
            record = {'model': self.model, 'prompt_version': self.prompt_version,
                      'request': payload, 'attempt': attempt+1, 'attempt_id': uuid4().hex}
            log(record)
            try:
                response = await self.client.post('https://api.typesafe.ai/v1/systemone',
                    json=payload, headers={'Authorization': f'Bearer {self.key}'})
            except httpx.RequestError as exc:
                log({**record, 'error': type(exc).__name__})
                raise RuntimeError('判定APIに接続できませんでした。保存済み判定は再開時に使います') from None
            try:
                body = response.json()
            except ValueError:
                body = {'raw': response.text}
            usage = body.get('usage', {}) if isinstance(body, dict) else {}
            log({**record, 'status': response.status_code, 'response': body,
                 'request_id': response.headers.get('x-typesafe-request-id'),
                 'input_tokens': usage.get('input_tokens', 0),
                 'output_tokens': usage.get('output_tokens', 0)})
            if response.status_code in (429, 529) and attempt < 5:
                await asyncio.sleep(2**attempt)
                continue
            if not response.is_success:
                raise RuntimeError(f'判定API HTTP {response.status_code}。設定とリクエスト記録を確認してください')
            if body.get('model') != self.model:
                raise ValueError('判定器の応答モデルが固定した版と一致しません')
            answer = body['answers']['agreement']
            if answer.get('refusal') or answer.get('type') == 'refusal':
                return Judgment(None, True)
            if answer.get('type') != 'noul':
                raise ValueError('判定器の応答型がnoulではありません')
            return Judgment(answer['noul'])
        raise RuntimeError('判定APIの再試行上限に達しました')
