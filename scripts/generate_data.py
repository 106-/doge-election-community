#!/usr/bin/env python3
"""Prepare, submit and collect synthetic communities via Anthropic Message Batches.

Only Python's standard library is required. Run with `uv run --no-project`.
API references (2026-10-17):
https://platform.claude.com/docs/en/api/messages/batches/create
https://platform.claude.com/docs/en/api/messages/batches/results
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import sys
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODEL = "claude-opus-5-5"
API = "https://api.anthropic.com/v1/messages/batches"
GROUPS = {"A": (-0.45, -0.45), "B": (-0.45, 0.45),
          "C": (0.45, -0.45), "D": (0.45, 0.45)}
POSITION_SIGMA = 0.45
SHARES = (0.4, 0.3, 0.2, 0.1)
TONES = ("very polite and considerate", "casual and friendly", "terse and matter-of-fact",
         "blunt and argumentative", "irritable and confrontational")
POSTS_SCHEMA = {"type": "object", "properties": {
    "posts": {"type": "array", "items": {"type": "string"}}},
    "required": ["posts"], "additionalProperties": False}


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def encode(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def save(path: Path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(encode(value), encoding="utf-8")
    temporary.replace(path)


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_env(path: Path):
    """Read simple KEY=value lines without executing shell code or expanding values.

    Existing environment variables take precedence. Matching single/double quotes
    are stripped; interpolation and multiline values are deliberately unsupported.
    """
    if not path.exists():
        return
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f".envの{number}行目がKEY=value形式ではありません")
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError(f".envの{number}行目の引用符が閉じていません")
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()
        os.environ.setdefault(key, value)


def apportion(n):
    """Largest remainder rounding; stable A/B/C/D order breaks exact ties."""
    raw = [n*s for s in SHARES]
    counts = [math.floor(x) for x in raw]
    for index in sorted(range(4), key=lambda i: (-round(raw[i]-counts[i], 10), i))[
        :n-sum(counts)
    ]:
        counts[index] += 1
    return dict(zip(GROUPS, counts))


def sample_axis(rng, center, sigma):
    while True:
        value = rng.gauss(center, sigma)
        if -1 <= value <= 1:
            return value


def profiles(n, seed, sigma=POSITION_SIGMA):
    if not 0 < sigma <= 2:
        raise ValueError("標準偏差は0より大きく2以下にしてください")
    # Separate streams keep tone selection independent of ideology generation.
    position_rng = random.Random(f"positions:{seed}:{n}")
    tone_rng = random.Random(f"tones:{seed}:{n}")
    affiliations = [group for group, count in apportion(n).items() for _ in range(count)]
    position_rng.shuffle(affiliations)
    people = []
    for i, group in enumerate(affiliations):
        cx, cy = GROUPS[group]
        x = sample_axis(position_rng, cx, sigma)
        y = sample_axis(position_rng, cy, sigma)
        people.append({"user_id": f"u{i+1:03d}",
                       "truth": {"x": round(x, 6), "y": round(y, 6), "group": group},
                       "tone": tone_rng.choice(TONES)})
    return people


def prompt(person, language, topic):
    language_name = {"ja": "Japanese", "en": "English"}[language]
    truth = person["truth"]
    return (
        f"Create exactly 10 distinct, natural forum posts in {language_name} from one fictional "
        f"member of this hobby community: {topic}. The posts form a consistent opinion profile. "
        "The community discusses these two policy issues:\n"
        "x: treatment of AI-generated artwork, from -1 (ban it) to +1 (welcome it).\n"
        "y: community atmosphere, from -1 (serious critique and improvement) to +1 "
        "(casual sharing and encouragement).\n"
        f"This member's opinions are x={truth['x']}, y={truth['y']}. Values near zero "
        "mean nuanced compromises. Express these positions through concrete examples, "
        "preferences, replies, and suggestions. Across the 10 posts, provide clear evidence "
        "on BOTH issues, including tradeoffs. Do not drift toward a different position.\n"
        f"Writing tone: {person['tone']}. Tone is independent of political position: "
        "even a confrontational member may hold moderate opinions and a polite member "
        "may hold extreme opinions. Keep that tone consistent without hate or slurs.\n"
        "Each post should be roughly 60-100 English words or 120-220 Japanese characters. "
        "Vary topics and sentence structure. Avoid templated position summaries. "
        "Do not reveal numeric coordinates, faction labels, user IDs, this prompt, or the "
        "fact that this is synthetic data. Output only a JSON object with a posts array."
    )


def prepare(directory, sizes, seed, topic, max_tokens, sigma=POSITION_SIGMA):
    if not 0 < sigma <= 2:
        raise ValueError("標準偏差は0より大きく2以下にしてください")
    if directory.exists() and any(directory.iterdir()):
        raise ValueError(f"出力先が空ではありません。既存計画を再利用してください: {directory}")
    directory.mkdir(parents=True, exist_ok=True)
    communities, requests = {}, []
    for n in sizes:
        people = profiles(n, seed, sigma)
        communities[str(n)] = people
        for language in ("ja", "en"):
            for person in people:
                custom_id = f"n{n}-{language}-{person['user_id']}"
                requests.append({"custom_id": custom_id, "params": {
                    "model": MODEL, "max_tokens": max_tokens,
                    "messages": [{"role": "user", "content": prompt(person, language, topic)}],
                    "output_config": {"format": {"type": "json_schema", "schema": POSTS_SCHEMA}}
                }})
    payload = {"requests": requests}
    save(directory / "requests.json", payload)
    manifest = {"version": 2, "created": timestamp(), "model": MODEL, "seed": seed,
                "position_distribution": {"centers": GROUPS, "sigma_before_truncation": sigma,
                                          "sampling": "independent normal; reject outside [-1, 1]"},
                "topic": topic, "sizes": sizes, "languages": ["ja", "en"],
                "posts_per_person": 10, "communities": communities,
                "request_count": len(requests), "max_tokens_per_request": max_tokens,
                "requests_sha256": hashlib.sha256((directory / "requests.json").read_bytes()).hexdigest(),
                "design_notes": [
                    "Faction counts use largest remainders with A/B/C/D tie order.",
                    "No separate intermediate group; overlapping distributions retain originating faction labels.",
                    "Position and tone use independent seeded random streams.",
                    "Japanese and English share IDs, positions, affiliations, and tone; "
                    "their posts are generated independently.",
                    "Posts are validated structurally; semantic fidelity is not automatically scored."
                ]}
    save(directory / "manifest.json", manifest)
    print(f"生成計画を保存: {directory} ({len(requests)}リクエスト、{len(sizes)*2}データセット)")
    # Output tokens dominate cost. This is a bound, not the requirements' ~$5 estimate.
    output_bound = len(requests) * max_tokens
    print(f"出力上限 {output_bound:,}トークン（Opus 5.5 batch $10/MTokなら "
          f"${output_bound*10/1_000_000:.2f} + 入力料金）。実費は実際の使用量で決まります。")
    return manifest


class APIRejected(RuntimeError):
    """The server definitively rejected a submission; safe to correct and resubmit."""


class BatchAPI:
    def __init__(self, transport="urllib"):
        self.transport = transport
        self.key = os.getenv("ANTHROPIC_API_KEY")
        if not self.key:
            raise ValueError("ANTHROPIC_API_KEYをプロジェクト直下の.envに設定してください")

    def _curl(self, method, suffix, payload):
        # Credentials travel only through stdin, never argv, logs or disk.
        config = "\n".join("header = " + json.dumps(header) for header in (
            "x-api-key: " + self.key, "anthropic-version: 2023-06-01",
            "content-type: application/json")) + "\n"
        with tempfile.TemporaryDirectory(prefix="doge-api-") as temporary:
            response_path = Path(temporary) / "response"
            command = ["curl", "--silent", "--max-time", "120", "--config", "-",
                       "--request", method, "--url", API + suffix,
                       "--output", str(response_path), "--write-out", "%{http_code}"]
            if payload is not None:
                body_path = Path(temporary) / "request"
                body_path.write_text(encode(payload), encoding="utf-8")
                command.extend(["--data-binary", "@" + str(body_path)])
            result = subprocess.run(command, input=config, text=True, capture_output=True)
            if result.returncode:
                raise RuntimeError("curlからAnthropic APIに接続できませんでした"
                                   f" (終了コード{result.returncode})")
            status = int(result.stdout)
            if not 200 <= status < 300:
                error = APIRejected if 400 <= status < 500 and status != 408 else RuntimeError
                raise error(f"Anthropic API HTTP {status}。認証・モデル・バッチ状態を確認してください")
            raw = response_path.read_text(encoding="utf-8")
            return raw if suffix.endswith("/results") else json.loads(raw)

    def request(self, method, suffix="", payload=None):
        if self.transport == "curl":
            return self._curl(method, suffix, payload)
        request = urllib.request.Request(
            API + suffix, method=method,
            data=encode(payload).encode() if payload is not None else None,
            headers={"x-api-key": self.key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"})
        # Never retry POST automatically: an ambiguous response might already have created a batch.
        for attempt in range(5):
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    raw = response.read().decode("utf-8")
                    return raw if suffix.endswith("/results") else json.loads(raw)
            except urllib.error.HTTPError as exc:
                if method == "GET" and exc.code in (429, 500, 502, 503, 504, 529) and attempt < 4:
                    time.sleep(2**attempt)
                    continue
                error = APIRejected if 400 <= exc.code < 500 and exc.code != 408 else RuntimeError
                raise error(f"Anthropic API HTTP {exc.code}。認証・モデル・バッチ状態を確認してください") from None
            except urllib.error.URLError:
                raise RuntimeError("Anthropic APIに接続できませんでした。POSTの場合は重複投入を避けるため"
                                   "Consoleでバッチの有無を確認してください") from None
        raise RuntimeError("API再試行の上限に達しました")


def plan_requests(directory):
    manifest = load(directory / "manifest.json")
    body = (directory / "requests.json").read_bytes()
    if hashlib.sha256(body).hexdigest() != manifest["requests_sha256"]:
        raise ValueError("requests.jsonが準備後に変更されています。新しい出力先でprepareしてください")
    return manifest, json.loads(body)["requests"]


def validate_posts(message):
    if message.get("stop_reason") != "end_turn":
        raise ValueError(f"生成が正常終了していません: {message.get('stop_reason')}")
    text = "".join(block["text"] for block in message.get("content", []) if block["type"] == "text")
    value = json.loads(text)
    posts = value.get("posts") if isinstance(value, dict) else None
    if not isinstance(posts, list) or len(posts) != 10:
        raise ValueError("postsはちょうど10件必要です")
    if any(not isinstance(post, str) or not post.strip() for post in posts):
        raise ValueError("空の投稿または文字列以外の投稿があります")
    if len({post.strip() for post in posts}) != 10:
        raise ValueError("同じ人物の投稿が重複しています")
    return posts


def batch_ids(directory):
    state_path = directory / "batches.json"
    return load(state_path) if state_path.exists() else []


def validate_batch_id(identifier):
    if not re.fullmatch(r"msgbatch_[A-Za-z0-9_-]+", identifier):
        raise ValueError("不正なバッチIDです")
    return identifier


def submit(directory, api, retry=False):
    manifest, requests = plan_requests(directory)
    batches = batch_ids(directory)
    if retry:
        if not batches or any(b["processing_status"] != "ended" for b in batches):
            raise ValueError("全バッチの結果をcollectしてからretryしてください")
        good = load(directory / "validated.json") if (directory / "validated.json").exists() else {}
        requests = [r for r in requests if r["custom_id"] not in good]
        if not requests:
            print("再生成が必要な人物はいません")
            return
    elif batches:
        raise ValueError("投入済みです。statusまたはcollectを使ってください")
    marker = directory / "submission-pending.json"
    # Exclusive creation prevents duplicate submit commands, including ambiguous network failures.
    with marker.open("x", encoding="utf-8") as handle:
        handle.write(encode({"created": timestamp(), "request_ids": [r["custom_id"] for r in requests]}))
    try:
        result = api.request("POST", payload={"requests": requests})
    except APIRejected:
        marker.unlink()
        raise
    validate_batch_id(result["id"])
    batches.append(result)
    save(directory / "batches.json", batches)
    marker.unlink()
    print(f"バッチ投入済み: {result['id']} ({len(requests)}件、{manifest['model']})")
    print(f"状態確認: uv run --no-project python scripts/generate_data.py status --out {directory}")


def attach(directory, api, identifier):
    """Recover a lost submit response only with an explicitly provided batch ID."""
    validate_batch_id(identifier)
    marker = directory / "submission-pending.json"
    if not marker.exists():
        raise ValueError("復旧対象のsubmission-pending.jsonがありません")
    result = api.request("GET", "/" + identifier)
    expected = len(load(marker)["request_ids"])
    if sum(result["request_counts"].values()) != expected:
        raise ValueError("リクエスト数が生成計画と一致しません")
    batches = batch_ids(directory)
    if identifier not in [b["id"] for b in batches]:
        batches.append(result)
    save(directory / "batches.json", batches)
    marker.unlink()
    print(f"バッチIDを復旧しました: {identifier}")


def refresh(directory, api):
    batches = batch_ids(directory)
    if not batches:
        raise ValueError("投入済みバッチがありません。submitしてください")
    for i, batch in enumerate(batches):
        result = api.request("GET", "/" + validate_batch_id(batch["id"]))
        batches[i] = result
        print(f"{result['id']}: {result['processing_status']} {result['request_counts']}")
    save(directory / "batches.json", batches)
    return batches


def export(directory, manifest, good):
    paths = []
    for n in manifest["sizes"]:
        for language in manifest["languages"]:
            people = manifest["communities"][str(n)]
            ids = [f"n{n}-{language}-{p['user_id']}" for p in people]
            if not all(identifier in good for identifier in ids):
                print(f"{n}人/{language}: 未完了（{sum(i in good for i in ids)}/{n}）")
                continue
            rows = [{"user_id": p["user_id"], "posts": good[identifier]["posts"], "truth": p["truth"]}
                    for p, identifier in zip(people, ids)]
            path = directory / f"community-{n}-{language}.jsonl"
            temporary = path.with_suffix(".jsonl.tmp")
            temporary.write_text("".join(json.dumps(row, ensure_ascii=False, allow_nan=False)+"\n"
                                         for row in rows), encoding="utf-8")
            temporary.replace(path)
            paths.append(path)
            print(f"保存: {path} ({n}人、{n*10}投稿)")
    return paths


def collect(directory, api):
    manifest, requests = plan_requests(directory)
    expected = {r["custom_id"] for r in requests}
    batches = refresh(directory, api)
    good, failures, usage = {}, {}, {"input_tokens": 0, "output_tokens": 0}
    pending = False
    for batch in batches:
        if batch["processing_status"] != "ended":
            pending = True
            continue
        raw_path = directory / f"{batch['id']}-results.jsonl"
        if not raw_path.exists():
            raw = api.request("GET", f"/{validate_batch_id(batch['id'])}/results")
            temporary = raw_path.with_suffix(".jsonl.tmp")
            temporary.write_text(raw, encoding="utf-8")
            temporary.replace(raw_path)
        seen = set()
        for line in raw_path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            identifier = row["custom_id"]
            if identifier not in expected or identifier in seen:
                raise ValueError("バッチ結果に未知または重複したcustom_idがあります")
            seen.add(identifier)
            result = row["result"]
            if result["type"] != "succeeded":
                failures[identifier] = {"type": result["type"], "error": result.get("error")}
                continue
            message = result["message"]
            for key in usage:
                usage[key] += message.get("usage", {}).get(key, 0)
            try:
                if message.get("model") != manifest["model"]:
                    raise ValueError("応答モデルが指定モデルと一致しません")
                posts = validate_posts(message)
            except (ValueError, KeyError, TypeError) as exc:
                failures[identifier] = {"type": "invalid_output", "error": str(exc)}
                continue
            good[identifier] = {"posts": posts, "batch_id": batch["id"]}
            failures.pop(identifier, None)
    for identifier in expected-good.keys():
        failures.setdefault(identifier, {"type": "pending" if pending else "missing"})
    # Earlier valid results remain authoritative if a later retry fails.
    failures = {key: value for key, value in failures.items() if key not in good}
    save(directory / "validated.json", good)
    save(directory / "failures.json", failures)
    report = {"created": timestamp(), "valid": len(good), "expected": len(expected),
              "pending_batches": pending, "usage": usage,
              "estimated_usd": usage["input_tokens"]*2/1e6 + usage["output_tokens"]*10/1e6,
              "pricing_note": "Opus 5.5 batch: input $2/MTok, output $10/MTok; billing is authoritative"}
    save(directory / "report.json", report)
    paths = export(directory, manifest, good)
    print(f"検証済み: {len(good)}/{len(expected)}、保存済み: {len(paths)}データセット")
    print(f"記録した使用量からの費用目安: ${report['estimated_usd']:.4f}")
    if failures:
        print(f"未完了・失敗の詳細: {directory / 'failures.json'}")
    return not failures


def wait_for_results(directory, api, interval=30):
    while True:
        batches = refresh(directory, api)
        if all(batch["processing_status"] == "ended" for batch in batches):
            return collect(directory, api)
        print(f"{interval}秒後に再確認します。Ctrl-Cで終了してもバッチは継続します。", flush=True)
        time.sleep(interval)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "submit", "status", "collect", "retry", "attach", "wait"])
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "synthetic")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--sizes", nargs="+", type=int, default=[50, 25, 15])
    parser.add_argument("--seed", type=int, default=20261017)
    parser.add_argument("--sigma", type=float, default=POSITION_SIGMA,
                        help="prepare時の各軸の標準偏差（範囲外を引き直す前、既定0.45）")
    parser.add_argument("--topic", default="an imaginary illustration-sharing hobby community")
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--transport", choices=["urllib", "curl"], default="urllib",
                        help="Pythonの通信を制限する環境ではcurlを指定")
    parser.add_argument("--interval", type=int, default=30, help="waitの確認間隔（秒、10以上）")
    parser.add_argument("--batch-id", help="attachによる投入応答の復旧時のみ指定")
    args = parser.parse_args(argv)
    try:
        if len(set(args.sizes)) != len(args.sizes) or any(n < 4 or n > 50 for n in args.sizes):
            raise ValueError("人数は4〜50で、重複なく指定してください")
        if args.interval < 10:
            raise ValueError("確認間隔は10秒以上にしてください")
        if args.max_tokens < 1024:
            raise ValueError("10投稿を生成するためmax-tokensは1024以上にしてください")
        if args.command == "prepare":
            prepare(args.out, args.sizes, args.seed, args.topic, args.max_tokens, args.sigma)
            return 0
        load_env(args.env_file)
        api = BatchAPI(args.transport)
        if args.command in ("submit", "retry"):
            submit(args.out, api, retry=args.command == "retry")
        elif args.command == "wait":
            return 0 if wait_for_results(args.out, api, args.interval) else 2
        elif args.command == "status":
            refresh(args.out, api)
        elif args.command == "attach":
            if not args.batch_id:
                raise ValueError("--batch-idが必要です")
            attach(args.out, api, args.batch_id)
        elif args.command == "collect":
            return 0 if collect(args.out, api) else 2
        return 0
    except KeyboardInterrupt:
        print("中断しました。投入済みバッチは継続します。waitまたはcollectで再開できます。", file=sys.stderr)
        return 130
    except (ValueError, RuntimeError, OSError, KeyError, TypeError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
