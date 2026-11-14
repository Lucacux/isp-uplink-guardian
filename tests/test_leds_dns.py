import asyncio
import struct
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import app
import checks
import config
import state as st
from dashboard import prometheus_metrics
from leds import in_off_window, leds_on_from_page


def _store(tmp):
    with patch.object(config, "DATA_DIR", tmp), patch.object(
        config, "STATE_FILE", str(Path(tmp) / "state.json")
    ):
        s = st.Store()
    s.reboot_lock = asyncio.Lock()
    s.onu_lock = asyncio.Lock()
    return s


class DiagnosisTests(unittest.TestCase):
    def test_classification(self):
        d = checks.diagnose
        self.assertEqual(d(True, True, True, True, True), checks.DIAG_OK)
        self.assertEqual(d(False, True, True, None, None), checks.DIAG_ISP_DOWN)
        self.assertEqual(d(False, False, True, None, None), checks.DIAG_ONU_DOWN)
        self.assertEqual(d(False, False, False, None, None), checks.DIAG_ROUTER_DOWN)
        self.assertEqual(d(True, True, True, False, True), checks.DIAG_ROUTER_DNS)
        self.assertEqual(d(True, True, True, False, False), checks.DIAG_DNS_UPSTREAM)

    def test_unknown_values_do_not_blame_anyone(self):
        self.assertEqual(checks.diagnose(None, None, None, None, None), checks.DIAG_OK)
        # DNS público caído pero el del router anda: no es incidente.
        self.assertEqual(checks.diagnose(True, True, True, True, False), checks.DIAG_OK)


class DnsPacketTests(unittest.TestCase):
    def test_query_layout(self):
        q = checks.build_dns_query("gd1.example.com", 0x1234)
        self.assertEqual(q[:2], b"\x12\x34")
        self.assertIn(b"\x03gd1\x07example\x03com\x00", q)
        self.assertTrue(q.endswith(b"\x00\x01\x00\x01"))

    def test_rcode_handling(self):
        def resp(qid, rcode, qr=True):
            return struct.pack("!HHHHHH", qid, (0x8000 if qr else 0) | 0x0180 | rcode, 1, 0, 0, 0)
        self.assertTrue(checks.dns_answered(resp(7, 0), 7))     # NOERROR
        self.assertTrue(checks.dns_answered(resp(7, 3), 7))     # NXDOMAIN = el resolver anda
        self.assertFalse(checks.dns_answered(resp(7, 2), 7))    # SERVFAIL = roto
        self.assertFalse(checks.dns_answered(resp(7, 5), 7))    # REFUSED
        self.assertFalse(checks.dns_answered(resp(8, 0), 7))    # otro id
        self.assertFalse(checks.dns_answered(resp(7, 0, qr=False), 7))
        self.assertFalse(checks.dns_answered(b"\x00", 7))


class LedWindowTests(unittest.TestCase):
    def at(self, hh, mm):
        return datetime(2026, 11, 14, hh, mm)

    def test_window_crossing_midnight(self):
        f = lambda h, m: in_off_window(self.at(h, m), "23:00", "08:00")  # noqa: E731
        self.assertFalse(f(22, 59))
        self.assertTrue(f(23, 0))
        self.assertTrue(f(3, 30))
        self.assertTrue(f(7, 59))
        self.assertFalse(f(8, 0))
        self.assertFalse(f(14, 0))

    def test_window_same_day_and_disabled(self):
        self.assertTrue(in_off_window(self.at(13, 0), "12:00", "14:00"))
        self.assertFalse(in_off_window(self.at(15, 0), "12:00", "14:00"))
        self.assertFalse(in_off_window(self.at(15, 0), "00:00", "00:00"))

    def test_page_reading(self):
        self.assertTrue(leds_on_from_page({"hidden": "0"}))     # LedControl=0 → encendidos
        self.assertFalse(leds_on_from_page({"hidden": "1"}))
        self.assertFalse(leds_on_from_page({"hidden": None, "checked": True}))
        self.assertIsNone(leds_on_from_page({"hidden": None, "checked": None}))


class LedSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [patch.object(config, "DATA_DIR", self.tmp.name),
                        patch.object(config, "STATE_FILE", str(Path(self.tmp.name) / "s.json")),
                        patch.object(config, "LED_SCHEDULE_ENABLED", True)]
        for p in self.patches:
            p.start()
        self.store = _store(self.tmp.name)
        self.store.wan_up = True
        self.store.onu_up = True

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_never_during_outage_or_reboot(self):
        now = time.time()
        self.assertTrue(app.led_action_needed(self.store, True, now))
        self.store.wan_up = False
        self.assertFalse(app.led_action_needed(self.store, True, now))
        self.store.wan_up = True
        self.store.onu_up = False
        self.assertFalse(app.led_action_needed(self.store, True, now))

    def test_converged_state_waits_for_recheck(self):
        now = time.time()
        self.store.leds_on, self.store.led_dirty, self.store.led_verified_ts = True, False, now
        self.assertFalse(app.led_action_needed(self.store, True, now + 60))
        self.assertTrue(app.led_action_needed(self.store, False, now + 60))    # cambió la franja
        self.assertTrue(app.led_action_needed(self.store, True, now + config.LED_RECHECK_SECS))

    def test_failure_backs_off_and_success_records(self):
        async def failing(_on):
            return {"ok": False, "detail": "login"}

        async def ok(on):
            return {"ok": True, "changed": True, "leds_on": on, "detail": "LEDs apagados."}

        with patch("leds.desired_leds_on", return_value=False):
            asyncio.run(app.led_tick(self.store, apply=failing))
            self.assertGreater(self.store.led_next_try_ts, time.time() + 60)
            self.assertFalse(app.led_action_needed(self.store, False, time.time()))
            self.store.led_next_try_ts = 0
            asyncio.run(app.led_tick(self.store, apply=ok))
        self.assertFalse(self.store.leds_on)
        self.assertFalse(self.store.led_dirty)
        self.assertEqual(self.store.events[-1]["type"], st.LED)

    def test_reboot_marks_leds_dirty_and_skip_is_not_repeated(self):
        app._skip(self.store, "motivo A")
        app._skip(self.store, "motivo A")
        app._skip(self.store, "motivo B")
        skips = [e for e in self.store.events if e["type"] == st.REBOOT_SKIP]
        self.assertEqual([e["msg"] for e in skips], ["motivo A", "motivo B"])


class DiagTickTests(unittest.TestCase):
    def test_dns_needs_consecutive_failures_and_notifies_once(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(config, "DATA_DIR", tmp), \
                patch.object(config, "STATE_FILE", str(Path(tmp) / "s.json")), \
                patch.object(config, "DNS_FAIL_THRESHOLD", 3), \
                patch.object(config, "DNS_CHECK_ENABLED", True):
            store = _store(tmp)
            store.wan_up, store.onu_up = True, True
            pushes = []

            async def fake_push(title, msg, prio="default"):
                pushes.append(msg)

            async def router_dns(server, port=53):
                return server != config.ROUTER_HOST

            async def alive():
                return True

            with patch("app.router_alive", alive), patch("app.dns_ok", router_dns), \
                    patch("notifier.push_ntfy", fake_push):
                for _ in range(2):
                    asyncio.run(app.diag_tick(store))
                self.assertEqual(store.diagnosis, checks.DIAG_OK)
                asyncio.run(app.diag_tick(store))
                self.assertEqual(store.diagnosis, checks.DIAG_ROUTER_DNS)
                asyncio.run(app.diag_tick(store))
            self.assertEqual(len(pushes), 1)
            metrics = prometheus_metrics(store.snapshot())
            self.assertIn('isp_diagnosis{state="router_dns"} 1', metrics)
            self.assertIn('isp_diagnosis{state="ok"} 0', metrics)
            self.assertIn("isp_dns_router_up 0", metrics)


if __name__ == "__main__":
    unittest.main()
