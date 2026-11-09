"""Fast checks that need neither ffmpeg nor a window.

Run from the repo root:  py -3 -m unittest discover -s tests
"""

import io
import json
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ClipSqueezer as cs  # noqa: E402


def clip(width=1920, height=1080, duration=60.0, fps=30.0, bitrate=20000.0):
    return cs.MediaInfo(duration=duration, width=width, height=height, fps=fps,
                        acodec="aac", bitrate_kbps=bitrate)


class PlanEncode(unittest.TestCase):
    def test_portrait_clip_keeps_its_height_when_the_budget_allows(self):
        info = clip(width=1080, height=1920, duration=10, bitrate=40000)
        self.assertEqual(cs.plan_encode(info, 100).height, 1920)

    def test_height_outside_the_ladder_is_kept_when_the_budget_allows(self):
        info = clip(width=1600, height=900, duration=10, bitrate=40000)
        self.assertEqual(cs.plan_encode(info, 100).height, 900)

    def test_odd_height_is_rounded_down_to_even(self):
        info = clip(width=1280, height=721, duration=10, bitrate=40000)
        self.assertEqual(cs.plan_encode(info, 100).height, 720)

    def test_4k_portrait_clip_keeps_its_height_when_the_budget_allows(self):
        info = clip(width=2160, height=3840, duration=10, bitrate=100000)
        self.assertEqual(cs.plan_encode(info, 100).height, 3840)

    def test_8k_landscape_clip_is_still_capped_at_4k(self):
        info = clip(width=7680, height=4320, duration=10, bitrate=200000)
        self.assertEqual(cs.plan_encode(info, 1000).height, 2160)

    def test_never_upscales(self):
        info = clip(width=640, height=360, duration=10, bitrate=40000)
        self.assertLessEqual(cs.plan_encode(info, 1000).height, 360)

    def test_resolution_drops_as_the_budget_shrinks(self):
        info = clip()
        self.assertLess(cs.plan_encode(info, 10).height,
                        cs.plan_encode(info, 100).height)

    def test_budget_stays_under_the_target(self):
        info = clip()
        plan = cs.plan_encode(info, 20)
        total_bytes = (plan.video_kbps + plan.audio_kbps) * 1000 / 8 * info.duration
        self.assertLess(total_bytes, 20 * cs.MB)

    def test_high_frame_rate_is_reduced_on_a_small_budget(self):
        self.assertEqual(cs.plan_encode(clip(fps=60), 10).fps, 30.0)


class EncodeToSize(unittest.TestCase):
    def run_encode(self, info, target_mb, output_bytes):
        commands = []

        def fake_ffmpeg(cmd, *_args):
            commands.append(cmd)
            if str(cmd[-1]).endswith(".mp4"):
                Path(cmd[-1]).write_bytes(b"x" * output_bytes)

        settings = cs.Settings(target_mb=target_mb, speed="fast")
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(cs, "_run_ffmpeg", fake_ffmpeg), \
                mock.patch.object(cs, "ffmpeg_bin", lambda: Path("ffmpeg")):
            out = Path(folder) / "out.mp4"
            try:
                result = cs.encode_to_size(Path("in.mp4"), info, settings, out,
                                           lambda _pct: None, threading.Event())
            except cs.CompressError:
                self.assertEqual(list(Path(folder).iterdir()), [],
                                 "a failed encode must leave nothing behind")
                return commands, None
            return commands, result

    def test_result_under_target_is_accepted_first_time(self):
        commands, result = self.run_encode(clip(), 20, 15 * cs.MB)
        self.assertEqual(len(commands), 2)
        self.assertEqual(result[1], 15 * cs.MB)

    def test_two_pass_options_only_apply_to_video(self):
        commands, _ = self.run_encode(clip(), 20, 15 * cs.MB)
        for command in commands:
            self.assertIn("-pass:v", command)
            self.assertIn("-passlogfile:v", command)
            self.assertNotIn("-pass", command)
            self.assertNotIn("-passlogfile", command)

    def test_overshoot_gets_one_tighter_retry(self):
        commands, result = self.run_encode(clip(), 20, 21 * cs.MB)
        self.assertEqual(len(commands), 4)
        self.assertIsNone(result)

    def test_retry_is_skipped_when_it_could_not_change_anything(self):
        too_long = clip(duration=600, bitrate=8000)
        commands, result = self.run_encode(too_long, 1, 3 * cs.MB)
        self.assertEqual(len(commands), 2)
        self.assertIsNone(result)


class FfmpegDownload(unittest.TestCase):
    def test_falls_back_when_the_stable_build_is_gone(self):
        opened = []

        def fake_urlopen(request, timeout):
            opened.append(request.full_url)
            if len(opened) == 1:
                raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)
            return "response"

        with mock.patch.object(cs.urllib.request, "urlopen", fake_urlopen):
            self.assertEqual(cs._open_ffmpeg_download(), "response")
        self.assertEqual(opened, list(cs.FFMPEG_URLS))

    def test_other_download_errors_are_reported(self):
        def fake_urlopen(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 500, "Server Error", {}, None)

        with mock.patch.object(cs.urllib.request, "urlopen", fake_urlopen):
            with self.assertRaises(urllib.error.HTTPError):
                cs._open_ffmpeg_download()


    def test_only_the_two_tools_are_installed(self):
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            for name in ("build/bin/ffmpeg.exe", "build/bin/ffprobe.exe",
                         "build/bin/ffplay.exe", "build/LICENSE.txt"):
                archive.writestr(name, name)
        payload.seek(0)
        response = mock.MagicMock(headers={}, read=payload.read)
        response.__enter__.return_value = response

        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(cs, "_open_ffmpeg_download", lambda: response):
            cs.download_ffmpeg(Path(folder))
            self.assertEqual(sorted(p.name for p in Path(folder).iterdir()),
                             ["ffmpeg.exe", "ffprobe.exe"])


class ProbeMedia(unittest.TestCase):
    def test_rotated_clip_with_cover_art(self):
        data = {
            "format": {"duration": "12.5", "bit_rate": "8000000"},
            "streams": [
                {"index": 0, "codec_type": "video", "codec_name": "mjpeg",
                 "width": 300, "height": 300, "disposition": {"attached_pic": 1}},
                {"index": 1, "codec_type": "video", "codec_name": "h264",
                 "width": 1920, "height": 1080, "avg_frame_rate": "30000/1001",
                 "side_data_list": [{"rotation": -90}]},
                {"index": 2, "codec_type": "audio", "codec_name": "aac", "channels": 6},
            ],
        }
        process = mock.Mock(returncode=0)
        process.communicate.return_value = (json.dumps(data), "")

        with mock.patch.object(cs.subprocess, "Popen", return_value=process), \
                mock.patch.object(cs, "ffprobe_bin", lambda: Path("ffprobe")):
            info = cs.probe_media(Path("clip.mp4"))

        self.assertEqual((info.width, info.height), (1080, 1920))
        self.assertEqual((info.vstream_index, info.astream_index), (1, 2))
        self.assertEqual(info.duration, 12.5)
        self.assertAlmostEqual(info.fps, 29.97, places=2)


class FinishMessage(unittest.TestCase):
    def test_clip_from_an_earlier_run_does_not_hide_a_real_encode(self):
        earlier = cs.Job(uid=1, src=Path("small.mp4"), state="fits")
        squeezed = cs.Job(uid=2, src=Path("big.mp4"), state="done")
        said = []
        app = types.SimpleNamespace(
            jobs=[earlier, squeezed], _run_uids={2}, running=True,
            _update_action=lambda: None, _update_counts=lambda: None,
            bar=mock.Mock(), detail=mock.Mock(),
            _say=lambda text, color=None: said.append(text),
        )

        cs.App._finish(app, {"done": 1, "failed": 0, "saved": 50 * cs.MB,
                             "total": 1, "canceled": False})

        self.assertIn("1 clip ready", said[-1])


class BatchRunnerStaleProbe(unittest.TestCase):
    def test_clip_replaced_after_it_was_added_is_read_again(self):
        old = clip(duration=60)
        old.size = 90 * cs.MB
        new = clip(duration=8)
        new.size = 5 * cs.MB
        job = cs.Job(uid=1, src=Path("clip.mp4"), size=old.size, info=old)

        events = []
        runner = cs.BatchRunner(events.append)
        with mock.patch.object(cs, "probe_media", lambda *_a: new):
            runner._run([job], cs.Settings(target_mb=20))

        self.assertEqual(job.info.duration, 8)
        self.assertEqual(job.state, "fits")


class Helpers(unittest.TestCase):
    def test_sizes_are_decimal(self):
        self.assertEqual(cs.MB, 1_000_000)
        self.assertEqual(cs.human_size(20_000_000), "20.0 MB")

    def test_output_name_never_overwrites(self):
        with tempfile.TemporaryDirectory() as folder:
            src = Path(folder) / "clip.mp4"
            first = cs.output_path_for(src, None)
            self.assertEqual(first.name, "clip (discord).mp4")
            first.write_bytes(b"x")
            self.assertEqual(cs.output_path_for(src, None).name,
                             "clip (discord) 2.mp4")

    def test_output_name_does_not_stack_the_suffix(self):
        src = Path("clip (discord).mp4")
        self.assertEqual(cs.output_path_for(src, Path("nowhere")).name,
                         "clip (discord).mp4")

    def test_frame_rate_fractions(self):
        self.assertAlmostEqual(cs._ratio("30000/1001"), 29.97, places=2)
        self.assertEqual(cs._ratio("0/0"), 0.0)

    def test_friendly_errors(self):
        self.assertEqual(cs.friendly_error("No space left on device"),
                         "Not enough disk space.")


if __name__ == "__main__":
    unittest.main()
