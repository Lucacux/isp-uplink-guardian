import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import config
from dashboard import make_app, prometheus_metrics
from state import Store


class MetricsTests(unittest.TestCase):
    def test_prometheus_snapshot_contains_expected_types_and_values(self):
        result = prometheus_metrics({
            "wan_up": True,
            "onu_up": False,
            "outage_secs": 12.5,
            "reboots_in_window": 2,
            "cooldown_remaining": 8,
            "uptime_secs": 90,
            "reboots_total": 4,
            "last_reboot_ts": 123,
            "since": 456,
        })

        self.assertIn("# TYPE isp_wan_up gauge", result)
        self.assertIn("isp_wan_up 1", result)
        self.assertIn("isp_onu_up 0", result)
        self.assertIn("isp_outage_seconds 12.5", result)
        self.assertIn("# TYPE isp_reboots_total counter", result)
        self.assertIn("isp_reboots_total 4", result)

    def test_unknown_boolean_is_nan_instead_of_false(self):
        result = prometheus_metrics({"wan_up": None, "onu_up": None})

        self.assertIn("isp_wan_up NaN", result)
        self.assertIn("isp_onu_up NaN", result)

    def test_app_exposes_metrics_route(self):
        store = unittest.mock.Mock()
        routes = {route.resource.canonical for route in make_app(store).router.routes()}

        self.assertIn("/metrics", routes)


class PersistentCounterTests(unittest.TestCase):
    def test_reboot_total_is_migrated_and_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            state_file.write_text(
                json.dumps({"reboot_history": [100.0, 200.0]}),
                encoding="utf-8",
            )
            with patch.object(config, "DATA_DIR", tmp), patch.object(
                config, "STATE_FILE", str(state_file)
            ), patch("state.time.time", return_value=300.0):
                store = Store()
                self.assertEqual(store.reboots_total, 2)
                store.record_reboot()

            persisted = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(persisted["reboots_total"], 3)


if __name__ == "__main__":
    unittest.main()
