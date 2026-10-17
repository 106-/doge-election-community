"""Dataset validation and the requirements' size constraints."""
import json
import math
from pydantic import BaseModel, ConfigDict, Field, StrictStr
from analysis.auto_config import auto_config, minority_curve, protocol, reference_curve

WARNING = ('くじで選ぶ委員会が、もとの集団とほぼ同じ大きさになっています。'
           '最終的に誰が投票するかを前もって予測しにくくするという、くじの効果が弱くなるため、'
           '論文が示した性質は保証されません。')


class Truth(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    x: float
    y: float
    group: StrictStr


class Member(BaseModel):
    user_id: StrictStr = Field(min_length=1, max_length=200)
    posts: list[StrictStr]
    truth: Truth | None = None


def parse_members(text):
    members, seen = [], set()
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            person = Member.model_validate(json.loads(line))
        except ValueError:
            raise ValueError(f'{number}行目: JSONLの項目・型・座標を確認してください') from None
        if not person.user_id.strip() or any(not post.strip() for post in person.posts):
            raise ValueError(f'{number}行目: IDや投稿本文を空にできません')
        if person.user_id in seen:
            raise ValueError(f'{number}行目: user_idが重複しています')
        seen.add(person.user_id)
        members.append(person.model_dump())
    if not seen:
        raise ValueError('データセットが空です')
    return members


def qualify_members(people, min_posts):
    if not 1 <= min_posts <= 1000:
        raise ValueError('最低投稿数は1〜1000で指定してください')
    members = [p for p in people if len(p['posts']) >= min_posts]
    excluded = [{'user_id': p['user_id'], 'posts': len(p['posts']),
                 'reason': f'投稿数が{min_posts}件未満'}
                for p in people if len(p['posts']) < min_posts]
    if len(members) > 50:
        raise ValueError('このPoCの有資格者は50人までです。最低投稿数を調整してください')
    return members, excluded


def parse_dataset(text, min_posts=5):
    return qualify_members(parse_members(text), min_posts)


def configuration(n, manual=None):
    if n < 4:
        raise ValueError('有資格者が3人以下のため委員会を作れません')
    if n > 50:
        raise ValueError('このPoCは50人までに対応しています')
    if manual is None:
        config = auto_config(n)
    else:
        l, e, k = manual
        valid = (3 <= l <= 9 and 2*l <= e <= n//2 and k in (2, 3)) if n >= 15 else (
            2 <= l <= 9 and l < e < n and 1 <= k <= 3)
        if not valid:
            raise ValueError('手動設定が要件9.2の人数制約を満たしていません')
        curve = minority_curve(n, protocol(l, e, k))
        config = {'l': l, 'e': e, 'K': k, 'm': math.ceil(3*l/4), 'final': math.ceil(3*e/5),
                  'deviation': max(abs(a-b) for a, b in zip(curve, reference_curve())),
                  'warning': n < 15}
    return {**config, 'warning_text': WARNING if n < 15 else '', 'manual': manual is not None}


def estimate(n, cfg):
    pairs = min(n, cfg['l']*cfg['K'] + cfg['e']) * (n-1)
    return {'pairs': pairs, 'tokens': pairs*3150, 'usd': pairs*3150*0.042/1e6}
