import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import watch  # noqa: E402

VTT = "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nhello world\n"


def proc(returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout="", stderr=stderr)


class FetchTranscriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        sleep = mock.patch.object(watch.time, "sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)
        self.addCleanup(self.tmp.cleanup)

    def run_with(self, results):
        """results: list of (CompletedProcess, vtt_text_or_None) per yt-dlp call."""
        calls = iter(results)

        def fake_run(cmd, timeout, input_text=None):
            p, vtt = next(calls)
            if vtt is not None:
                (self.dir / "sub.en.vtt").write_text(vtt)
            return p

        with mock.patch.object(watch, "_run", side_effect=fake_run) as run:
            out = watch._fetch_transcript("u", self.dir, 10)
        return out, run

    def test_manual_subs_found(self):
        (text, source, err), run = self.run_with([(proc(), VTT)])
        self.assertEqual((text, source, err), ("hello world", "manual", None))
        self.assertEqual(run.call_count, 1)

    def test_clean_run_without_subs_is_none_without_error(self):
        (text, source, err), _ = self.run_with([(proc(), None), (proc(), None)])
        self.assertEqual((text, source, err), ("", "none", None))

    def test_failure_is_reported_not_called_no_subtitles(self):
        msg = "ERROR: HTTP Error 403: Forbidden"
        (text, source, err), _ = self.run_with([(proc(1, msg), None), (proc(1, msg), None)])
        self.assertEqual(source, "none")
        self.assertEqual(err["type"], "subtitle_fetch_failed")
        self.assertIn("403", err["message"])

    def test_429_waits_then_retries_and_succeeds(self):
        msg = "ERROR: Unable to download video subtitles: HTTP Error 429: Too Many Requests"
        (text, source, err), run = self.run_with([(proc(1, msg), None), (proc(), VTT)])
        self.assertEqual((text, source, err), ("hello world", "manual", None))
        self.sleep.assert_called_once_with(watch.SUBTITLE_RETRY_WAIT_SECONDS)
        self.assertEqual(run.call_count, 2)

    def test_429_exhausted_reports_rate_limited_with_original_message(self):
        msg = "ERROR: Unable to download video subtitles: HTTP Error 429: Too Many Requests"
        n = (watch.SUBTITLE_RETRIES + 1) * 2  # manual + auto, each retried
        (text, source, err), run = self.run_with([(proc(1, msg), None)] * n)
        self.assertEqual(source, "none")
        self.assertEqual(err["type"], "subtitle_rate_limited")
        self.assertEqual(err["message"], msg)
        self.assertEqual(run.call_count, n)

    def test_manual_fails_but_auto_succeeds_returns_transcript_without_error(self):
        (text, source, err), _ = self.run_with([(proc(1, "ERROR: boom"), None), (proc(), VTT)])
        self.assertEqual((text, source, err), ("hello world", "auto", None))


if __name__ == "__main__":
    unittest.main()
