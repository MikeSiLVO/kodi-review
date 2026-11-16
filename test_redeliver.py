"""Offline tests for replaying the App's webhook deliveries that never got through."""

import base64
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import redeliver

NOW = datetime(2026, 11, 16, 18, 0, tzinfo=timezone.utc)


def attempt(guid, minutes_ago, code, redelivery=False, number=0):
    """Return a delivery attempt as the App's delivery list gives it."""
    at = (NOW - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")
    return {"id": number or minutes_ago, "guid": guid, "delivered_at": at, "status_code": code,
            "status": "OK" if code == 202 else "failed", "redelivery": redelivery,
            "event": "pull_request", "action": "opened"}


class UndeliveredTests(unittest.TestCase):
    """Events a run replays and those it leaves alone."""

    def ids(self, *attempts):
        """Return the attempt ids undelivered picks, sorted."""
        return sorted(a["id"] for a in redeliver.undelivered(list(attempts)))

    def test_a_failed_event_is_replayed_from_its_newest_attempt(self):
        """An event whose attempts all failed is replayed once, from its newest attempt."""
        self.assertEqual(self.ids(attempt("a", 30, 0)), [30])
        self.assertEqual(self.ids(attempt("a", 20, 502, redelivery=True), attempt("a", 30, 0)),
                         [20])

    def test_a_delivered_event_is_left_alone(self):
        """An event with any 2xx attempt is not replayed."""
        self.assertEqual(self.ids(attempt("a", 30, 202)), [])
        self.assertEqual(self.ids(attempt("a", 20, 202, redelivery=True), attempt("a", 30, 0)),
                         [])

    def test_replays_stop_at_the_attempt_limit(self):
        """Three failed attempts end the replays."""
        tries = [attempt("a", m, 0, redelivery=m < 30) for m in (10, 20, 30)]
        self.assertEqual(self.ids(*tries), [])

    def test_an_event_first_sent_before_the_window_is_left_alone(self):
        """Replays alone in the list mean the event was first sent before the window."""
        self.assertEqual(self.ids(attempt("a", 20, 0, redelivery=True)), [])

    def test_events_are_judged_apart(self):
        """Each event is judged by its own attempts."""
        self.assertEqual(self.ids(attempt("a", 30, 0), attempt("b", 40, 202),
                                  attempt("c", 50, 401)), [30, 50])


class RecentTests(unittest.TestCase):
    """Reading the delivery list back to the window's start."""

    def test_pages_are_read_until_one_reaches_past_the_window(self):
        """Paging stops at the first page ending before the window and drops older attempts."""
        pages = {"/app/hook/deliveries?per_page=100": ([attempt("a", 10, 202),
                                                        attempt("b", 20, 0)], "page2"),
                 "page2": ([attempt("c", 100, 0), attempt("d", 200, 0)], "page3")}
        with mock.patch.object(redeliver, "call", side_effect=lambda *args: pages[args[1]]):
            found = redeliver.recent("token", NOW - timedelta(hours=2))
        self.assertEqual([a["guid"] for a in found], ["a", "b", "c"])


class JwtTests(unittest.TestCase):
    """The App JWT that openssl signs."""

    def test_the_jwt_carries_the_claims_and_a_valid_signature(self):
        """The claims name the App and the signature checks out against the key."""
        with tempfile.TemporaryDirectory() as folder:
            key, public = Path(folder, "key.pem"), Path(folder, "public.pem")
            subprocess.run(["openssl", "genrsa", "-out", str(key), "2048"], check=True,
                           capture_output=True)
            subprocess.run(["openssl", "rsa", "-in", str(key), "-pubout", "-out", str(public)],
                           check=True, capture_output=True)
            token = redeliver.app_jwt("Iv23test", key.read_text(), 1_000_000)
            head, claims, signature = token.split(".")

            def decoded(text):
                return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))

            self.assertEqual(json.loads(decoded(head)), {"alg": "RS256", "typ": "JWT"})
            self.assertEqual(json.loads(decoded(claims)),
                             {"iat": 999_940, "exp": 1_000_540, "iss": "Iv23test"})
            Path(folder, "signature").write_bytes(decoded(signature))
            check = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(public),
                                    "-signature", str(Path(folder, "signature"))],
                                   input=f"{head}.{claims}".encode(), capture_output=True)
            self.assertEqual(check.returncode, 0, check.stdout + check.stderr)


if __name__ == "__main__":
    unittest.main()
