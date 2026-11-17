"""Smoke tests: imports, state, heuristics, earnings math, read-only routes.

Run from the project root:
    ./.venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class TestModules(unittest.TestCase):
    def test_imports(self):
        import agents  # noqa: F401
        import ai  # noqa: F401
        import app  # noqa: F401
        import campaigns  # noqa: F401
        import captioner  # noqa: F401
        import chat  # noqa: F401
        import clipper  # noqa: F401
        import config  # noqa: F401
        import detector  # noqa: F401
        import monitor  # noqa: F401
        import publisher  # noqa: F401
        import recorder  # noqa: F401
        import state  # noqa: F401
        import transcriber  # noqa: F401
        import vod_mode  # noqa: F401

    def test_agent_count(self):
        import agents

        self.assertEqual(len(agents.AGENTS), 11)
        ids = {a["id"] for a in agents.AGENTS}
        self.assertIn("viral-hook-doctor", ids)
        self.assertIn("repurpose-engine", ids)


class TestState(unittest.TestCase):
    def test_snapshot_shape(self):
        from state import get_state

        snap = get_state().snapshot()
        for key in ("now", "streamers", "active", "clips", "campaigns",
                    "earnings", "agent_outputs", "settings", "stats"):
            self.assertIn(key, snap)

    def test_settings_are_sanitized(self):
        from state import get_state

        snap = get_state().snapshot()
        self.assertNotIn("api_key", snap["ai"])
        self.assertNotIn("api_key", snap["settings"])


class TestDetector(unittest.TestCase):
    def test_signal_moments_finds_spike(self):
        import detector

        points = [(float(t), -42.0) for t in range(120)]
        points += [(float(t), -18.0) for t in range(50, 66)]
        moments = detector.signal_moments(
            loud_points=points, start=0.0, end=120.0, top=5
        )
        self.assertTrue(moments)
        self.assertTrue(any(m["start"] <= 55 <= m["end"] for m in moments))

    def test_signal_moments_empty_input(self):
        import detector

        self.assertEqual(detector.signal_moments(loud_points=[], start=0, end=10), [])


class TestEarnings(unittest.TestCase):
    def test_earning_math(self):
        import campaigns

        row = campaigns._earning({
            "campaign_id": "",
            "views": 2500,
            "rate": 2,
            "per": 1000,
            "date": "2026-11-16",
        })
        self.assertEqual(row["revenue"], 5.0)

    def test_override_wins(self):
        import campaigns

        row = campaigns._earning({
            "campaign_id": "",
            "views": 1000,
            "rate": 2,
            "revenue_override": 12.5,
        })
        self.assertEqual(row["revenue"], 12.5)


class TestCaptions(unittest.TestCase):
    def test_cues_from_words(self):
        import captioner

        words = captioner.words_from_text("this is a test of the caption engine", 4.0)
        cues = captioner.build_cues(words, max_words=2)
        self.assertTrue(cues)
        self.assertLessEqual(len(cues[0]["words"]), 2)

    def test_ass_has_events(self):
        import captioner

        words = captioner.words_from_text("big play right here", 3.0)
        ass = captioner.build_ass(words, duration=3.0, hook="wait for it")
        self.assertIn("[Events]", ass)
        self.assertIn("Dialogue:", ass)


class TestRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as app_module

        app_module.app.config.update(TESTING=True)
        cls.client = app_module.app.test_client()

    def test_index(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Clipping Studio", res.data)

    def test_health(self):
        res = self.client.get("/api/health")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["ok"])

    def test_state(self):
        res = self.client.get("/api/state")
        self.assertEqual(res.status_code, 200)
        self.assertIn("clips", res.get_json())

    def test_agents(self):
        res = self.client.get("/api/agents")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.get_json()["agents"]), 11)


if __name__ == "__main__":
    unittest.main()
