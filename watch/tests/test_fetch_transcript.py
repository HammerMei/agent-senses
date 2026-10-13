import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import watch  # noqa: E402

VTT = "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nhello world\n"
VTT_ZH = "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\n你好世界\n"
M429 = "ERROR: Unable to download video subtitles: HTTP Error 429: Too Many Requests"


def proc(returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout="", stderr=stderr)


def lang_of(call):
    cmd = call.args[0]
    return cmd[cmd.index("--sub-lang") + 1]


class FetchTranscriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        sleep = mock.patch.object(watch.time, "sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)
        self.addCleanup(self.tmp.cleanup)

    def run_with(self, results, meta=None):
        """results: list of (CompletedProcess, vtt_text_or_None), one per yt-dlp call.

        A vtt is written to sub.<requested lang>.vtt, like yt-dlp does.
        """
        calls = iter(results)

        def fake_run(cmd, timeout, input_text=None):
            p, vtt = next(calls)
            if vtt is not None:
                lang = cmd[cmd.index("--sub-lang") + 1]
                (self.dir / f"sub.{lang}.vtt").write_text(vtt)
            return p

        with mock.patch.object(watch, "_run", side_effect=fake_run) as run:
            out = watch._fetch_transcript("u", self.dir, 10, meta)
        return out, run

    def test_manual_subs_found(self):
        out, run = self.run_with([(proc(), VTT)])
        self.assertEqual(out, ("hello world", "manual", "en", None))
        self.assertEqual(run.call_count, 1)

    def test_clean_run_without_subs_is_none_without_error(self):
        out, _ = self.run_with([(proc(), None)] * 3)
        self.assertEqual(out, ("", "none", "", None))

    def test_failure_is_reported_not_called_no_subtitles(self):
        msg = "ERROR: HTTP Error 403: Forbidden"
        (text, source, lang, err), _ = self.run_with([(proc(1, msg), None)] * 3)
        self.assertEqual(source, "none")
        self.assertEqual(err["type"], "subtitle_fetch_failed")
        self.assertIn("403", err["message"])

    def test_429_waits_then_retries_and_succeeds(self):
        out, run = self.run_with([(proc(1, M429), None), (proc(), VTT)])
        self.assertEqual(out, ("hello world", "manual", "en", None))
        self.sleep.assert_called_once_with(watch.SUBTITLE_RETRY_WAIT_SECONDS)
        self.assertEqual(run.call_count, 2)

    def test_429_exhausted_reports_rate_limited_with_original_message(self):
        n = (watch.SUBTITLE_RETRIES + 1) * 3  # manual + en-orig + en, each retried
        (text, source, lang, err), run = self.run_with([(proc(1, M429), None)] * n)
        self.assertEqual(source, "none")
        self.assertEqual(err["type"], "subtitle_rate_limited")
        self.assertEqual(err["message"], M429)
        self.assertEqual(run.call_count, n)

    def test_prefers_en_orig_and_skips_translated_en_track(self):
        out, run = self.run_with([(proc(), None), (proc(), VTT)])
        self.assertEqual(out, ("hello world", "auto", "en-orig", None))
        self.assertEqual([lang_of(c) for c in run.call_args_list], ["en", "en-orig"])

    def test_manual_fails_but_auto_succeeds_returns_transcript_without_error(self):
        out, _ = self.run_with([(proc(1, "ERROR: boom"), None), (proc(), VTT)])
        self.assertEqual(out, ("hello world", "auto", "en-orig", None))

    def test_non_english_video_falls_back_to_its_manual_track(self):
        meta = {"subtitles": {"zh-TW": [{}]}, "automatic_captions": {}}
        out, run = self.run_with([(proc(), None)] * 3 + [(proc(), VTT_ZH)], meta)
        self.assertEqual(out, ("你好世界", "manual", "zh-TW", None))
        self.assertEqual(lang_of(run.call_args_list[-1]), "zh-TW")

    def test_non_english_video_falls_back_to_orig_auto_caption(self):
        meta = {"subtitles": {}, "automatic_captions": {"ja-orig": [{}], "ja": [{}], "en": [{}]}}
        out, run = self.run_with([(proc(), None)] * 3 + [(proc(), VTT_ZH)], meta)
        self.assertEqual(out, ("你好世界", "auto", "ja-orig", None))
        self.assertEqual([lang_of(c) for c in run.call_args_list][3:], ["ja-orig"])

    def test_empty_vtt_from_one_track_does_not_hide_the_next_track(self):
        empty = "WEBVTT\n\n"
        out, _ = self.run_with([(proc(), empty), (proc(), VTT)])
        self.assertEqual(out, ("hello world", "auto", "en-orig", None))

    def test_empty_en_orig_vtt_does_not_hide_the_en_track(self):
        empty = "WEBVTT\n\n"
        out, _ = self.run_with([(proc(), None), (proc(), empty), (proc(), VTT)])
        self.assertEqual(out, ("hello world", "auto", "en", None))

    def test_fallback_never_requests_machine_translated_tracks(self):
        meta = {"subtitles": {}, "automatic_captions": {"ab-zh-TW": [{}], "af-zh-TW": [{}]}}
        self.assertEqual(watch._fallback_tracks(meta), [])

    def test_fallback_uses_declared_language_auto_caption(self):
        meta = {"subtitles": {}, "automatic_captions": {"ko": [{}]}, "language": "ko"}
        self.assertEqual(watch._fallback_tracks(meta), [("--write-auto-sub", "ko", "auto")])

    def test_english_video_never_triggers_fallback(self):
        meta = {"subtitles": {"en": [{}]}, "automatic_captions": {"en-orig": [{}]}, "language": "en"}
        self.assertEqual(watch._fallback_tracks(meta), [])

    def test_fallback_error_is_kept_when_no_track_yields_text(self):
        meta = {"subtitles": {"zh-TW": [{}]}}
        results = [(proc(), None)] * 3 + [(proc(1, "ERROR: HTTP Error 403: Forbidden"), None)]
        (text, source, lang, err), _ = self.run_with(results, meta)
        self.assertEqual((text, source, lang), ("", "none", ""))
        self.assertEqual(err["type"], "subtitle_fetch_failed")


if __name__ == "__main__":
    unittest.main()
