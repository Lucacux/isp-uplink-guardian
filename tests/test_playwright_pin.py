"""Playwright (pip) y la imagen base tienen que ser la MISMA versión.

Si difieren, el build pasa pero Chromium no arranca en runtime (el paquete busca
un build de navegador que la imagen no trae) y el reboot del ONU falla justo
durante un corte. Este test lo frena en CI.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class PlaywrightPinTests(unittest.TestCase):
    def test_requirements_and_base_image_match(self):
        req = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        pip_pin = re.search(r"^playwright==([\d.]+)\s*$", req, re.M)
        image = re.search(r"^FROM mcr\.microsoft\.com/playwright/python:v([\d.]+)-(\w+)(@sha256:[0-9a-f]{64})?",
                          docker, re.M)
        self.assertIsNotNone(pip_pin, "requirements.txt debe fijar playwright==X.Y.Z")
        self.assertIsNotNone(image, "Dockerfile debe usar la imagen oficial de Playwright")
        self.assertEqual(pip_pin.group(1), image.group(1))

    def test_base_image_is_lts_and_pinned_by_digest(self):
        docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        image = re.search(r"^FROM \S+:v[\d.]+-(\w+)(@sha256:[0-9a-f]{64})?", docker, re.M)
        self.assertIn(image.group(1), {"noble", "jammy"}, "base Ubuntu LTS")
        self.assertIsNotNone(image.group(2), "fijar la imagen por digest")


if __name__ == "__main__":
    unittest.main()
