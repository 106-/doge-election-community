"""Offline tests: no API credentials and no paid requests."""
import json
import os
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from scripts.generate_data import (
    APIRejected, BatchAPI, MODEL, apportion, attach, collect, load, load_env, plan_requests,
    prepare, profiles, save, submit, validate_posts,
)


def message(label="post"):
    return {"model": MODEL, "stop_reason": "end_turn", "content": [
        {"type": "text", "text": json.dumps({"posts": [f"{label} {i}" for i in range(10)]})}],
        "usage": {"input_tokens": 100, "output_tokens": 200}}


class FakeAPI:
    def __init__(self):
        self.batches = []
        self.results = {}

    def request(self, method, suffix="", payload=None):
        if method == "POST":
            identifier = f"msgbatch_test{len(self.batches)}"
            batch = {"id": identifier, "processing_status": "ended", "request_counts": {
                "processing": 0, "succeeded": len(payload["requests"]),
                "errored": 0, "canceled": 0, "expired": 0}}
            self.batches.append(batch)
            rows = [{"custom_id": r["custom_id"], "result": {"type": "succeeded",
                     "message": message(r["custom_id"])}} for r in payload["requests"]]
            # API results need not arrive in request order.
            self.results[identifier] = "\n".join(json.dumps(row) for row in reversed(rows))
            return batch
        identifier = suffix.split("/")[1]
        if suffix.endswith("/results"):
            return self.results[identifier]
        return next(batch for batch in self.batches if batch["id"] == identifier)


class GeneratorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name) / "synthetic"

    def prepare(self, sizes=None):
        return prepare(self.out, sizes or [4], 42, "illustration forum", 4096)

    def test_rounding_and_reproducibility(self):
        self.assertEqual(apportion(50), {"A": 20, "B": 15, "C": 10, "D": 5})
        self.assertEqual(apportion(25), {"A": 10, "B": 8, "C": 5, "D": 2})
        self.assertEqual(apportion(15), {"A": 6, "B": 5, "C": 3, "D": 1})
        for n in (15, 25, 50):
            people = profiles(n, 42)
            self.assertEqual(people, profiles(n, 42))
            self.assertEqual(Counter(p["truth"]["group"] for p in people), apportion(n))
            self.assertTrue(all("intermediate" not in p for p in people))
            self.assertTrue(all(-1 <= p["truth"][axis] <= 1 for p in people for axis in ("x", "y")))

    def test_overlapping_layout_and_sigma(self):
        people = profiles(50, 20261017)
        self.assertEqual(sum(abs(p["truth"]["x"]) < .3 and abs(p["truth"]["y"]) < .3
                             for p in people), 12)
        wider = profiles(50, 20261017, .6)
        self.assertNotEqual([p["truth"] for p in people], [p["truth"] for p in wider])
        self.assertEqual([p["tone"] for p in people], [p["tone"] for p in wider])
        for sigma in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                profiles(50, 20261017, sigma)
        manifest = prepare(self.out, [50], 20261017, "forum", 4096)
        self.assertEqual(manifest["communities"]["50"], people)
        self.assertEqual(manifest["position_distribution"]["sigma_before_truncation"], .45)

    def test_prepare_all_sizes_and_languages(self):
        manifest = self.prepare([50, 25, 15])
        _, requests = plan_requests(self.out)
        self.assertEqual(manifest["request_count"], 180)
        self.assertEqual(len({r["custom_id"] for r in requests}), 180)
        self.assertEqual({r["params"]["model"] for r in requests}, {MODEL})
        self.assertEqual(len(requests), 180)
        for n in (50, 25, 15):
            person = manifest["communities"][str(n)][0]
            by_id = {r["custom_id"]: r for r in requests}
            for language in ("ja", "en"):
                text = by_id[f"n{n}-{language}-{person['user_id']}"]["params"]["messages"][0]["content"]
                self.assertIn(str(person["truth"]["x"]), text)
                self.assertIn(person["tone"], text)
        with self.assertRaises(ValueError):
            self.prepare()
        (self.out / "requests.json").write_text("{}")
        with self.assertRaises(ValueError):
            plan_requests(self.out)

    def test_submit_collect_and_bilingual_truth(self):
        self.prepare()
        api = FakeAPI()
        submit(self.out, api)
        with self.assertRaises(ValueError):
            submit(self.out, api)
        self.assertTrue(collect(self.out, api))
        ja = [json.loads(s) for s in (self.out / "community-4-ja.jsonl").read_text().splitlines()]
        en = [json.loads(s) for s in (self.out / "community-4-en.jsonl").read_text().splitlines()]
        self.assertEqual(len(ja), 4)
        self.assertEqual([p["truth"] for p in ja], [p["truth"] for p in en])
        self.assertEqual([p["user_id"] for p in ja], [p["user_id"] for p in en])
        self.assertTrue(all(len(p["posts"]) == 10 for p in ja+en))
        self.assertNotIn("tone", ja[0])
        self.assertTrue(collect(self.out, api))
        self.assertEqual(load(self.out / "report.json")["usage"]["input_tokens"], 800)

    def test_failed_member_retry_only_and_no_partial_export(self):
        self.prepare()
        api = FakeAPI()
        submit(self.out, api)
        rows = [json.loads(line) for line in api.results["msgbatch_test0"].splitlines()]
        failed = rows[0]["custom_id"]
        rows[0]["result"] = {"type": "expired"}
        api.results["msgbatch_test0"] = "\n".join(json.dumps(r) for r in rows)
        self.assertFalse(collect(self.out, api))
        self.assertEqual(len(list(self.out.glob("community-*.jsonl"))), 1)
        submit(self.out, api, retry=True)
        self.assertEqual(len(api.results["msgbatch_test1"].splitlines()), 1)
        self.assertIn(failed, api.results["msgbatch_test1"])
        self.assertTrue(collect(self.out, api))
        self.assertEqual(len(list(self.out.glob("community-*.jsonl"))), 2)

    def test_ambiguous_submission_blocks_duplicate_and_attach_recovers(self):
        self.prepare()
        api = FakeAPI()
        original = api.request

        def lost_response(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("lost response")

        with patch.object(api, "request", side_effect=lost_response):
            with self.assertRaises(RuntimeError):
                submit(self.out, api)
        with self.assertRaises(FileExistsError):
            submit(self.out, api)
        self.assertEqual(len(api.batches), 1)
        attach(self.out, api, "msgbatch_test0")
        self.assertFalse((self.out / "submission-pending.json").exists())
        self.assertTrue(collect(self.out, api))

    def test_definitive_rejection_can_be_resubmitted(self):
        self.prepare()
        api = FakeAPI()
        with patch.object(api, "request", side_effect=APIRejected("HTTP 401")):
            with self.assertRaises(APIRejected):
                submit(self.out, api)
        self.assertFalse((self.out / "submission-pending.json").exists())
        submit(self.out, api)
        self.assertEqual(len(api.batches), 1)

    def test_curl_keeps_credentials_out_of_arguments_and_logs(self):
        import subprocess
        def response(command, **kwargs):
            self.assertNotIn("test-secret", " ".join(command))
            self.assertIn("test-secret", kwargs["input"])
            output = Path(command[command.index("--output") + 1])
            output.write_text('{"data": []}')
            return subprocess.CompletedProcess(command, 0, "200", "")
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-secret"}):
            with patch("scripts.generate_data.subprocess.run", side_effect=response):
                self.assertEqual(BatchAPI("curl").request("GET"), {"data": []})
            failed = subprocess.CompletedProcess([], 6, "000", "test-secret")
            with patch("scripts.generate_data.subprocess.run", return_value=failed):
                with self.assertRaises(RuntimeError) as error:
                    BatchAPI("curl").request("GET")
                self.assertNotIn("test-secret", str(error.exception))

    def test_invalid_outputs(self):
        self.assertEqual(len(validate_posts(message())), 10)
        for invalid in (None, ["one"]*10, ["one"], list(range(10)), [" "]*10):
            value = message()
            value["content"][0]["text"] = json.dumps({"posts": invalid})
            with self.assertRaises(ValueError):
                validate_posts(value)
        value = message()
        value["stop_reason"] = "max_tokens"
        with self.assertRaises(ValueError):
            validate_posts(value)

    def test_env_file_no_execution_and_environment_wins(self):
        path = Path(self.temp.name) / ".env"
        path.write_text('# comment\nexport ANTHROPIC_API_KEY="from-file"\nLOCAL_TEST=\'$(never-run)\'\n')
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "existing"}, clear=True):
            load_env(path)
            self.assertEqual(os.environ["ANTHROPIC_API_KEY"], "existing")
            self.assertEqual(os.environ["LOCAL_TEST"], "$(never-run)")
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                BatchAPI()


if __name__ == "__main__":
    unittest.main()
