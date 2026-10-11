"""Public, synthetic safety contract tests.

These tests deliberately use no preserved run data, private credentials, or
local browser profiles, so the release checkout exercises the same boundaries
as the development checkout.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import orchestrator  # noqa: E402
from core import safe_url  # noqa: E402


class PublicSafetyContract(unittest.TestCase):
    def test_no_outreach_requests_are_refused(self):
        parsed = orchestrator.parse(
            "Find 5 florists in Melbourne, AU and message them on WhatsApp."
        )
        self.assertTrue(parsed["flags"]["outreach_requested"])
        self.assertFalse(parsed["intent"]["dm_probe"])
        result = orchestrator.spec_command(
            "Find 5 florists in Melbourne, AU and message them on WhatsApp."
        )
        self.assertEqual(result["status"], "refused")

    def test_messaging_urls_are_denied(self):
        for url in (
            "https://www.instagram.com/direct/inbox/",
            "https://web.whatsapp.com/",
            "https://example.com/messages/send",
        ):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "messaging URL denied"):
                safe_url(url, "read-site", resolve=False)

    def test_login_urls_are_denied(self):
        for url in (
            "https://accounts.google.com/login",
            "https://www.instagram.com/accounts/login/",
            "https://example.com/signin",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                safe_url(url, "read-site", resolve=False)

    def test_dm_probe_is_disabled(self):
        intent = orchestrator.parse(
            "Find 5 florists in Melbourne, AU. No outreach."
        )["intent"]
        self.assertFalse(intent["dm_probe"])
        with self.assertRaisesRegex(ValueError, "dm_probe must be false"):
            orchestrator.validate_intent({**intent, "dm_probe": True})

    def test_instagram_role_isolation(self):
        profile = "https://www.instagram.com/example_business/"
        self.assertEqual(safe_url(profile, "read-ig", resolve=False), profile)
        with self.assertRaisesRegex(ValueError, "dedicated reader context"):
            safe_url(profile, "read-site", resolve=False)
        with self.assertRaisesRegex(ValueError, "Instagram-only scope"):
            safe_url("https://example.com/", "read-ig", resolve=False)

    def test_instagram_hostname_matching_uses_label_boundaries(self):
        # 'instagram.com' tested as a bare substring also matches hosts that
        # merely contain the text: 'evil-instagram.com' and
        # 'instagram.com.evil.example'. Those are not Instagram, so they may
        # be misclassified in either direction -- wrongly allowed as a
        # lookalike by the read-ig allowlist's neighbour, and wrongly reported
        # as Instagram to every other role. Both directions are pinned here.
        real_instagram = (
            "https://instagram.com/example/",
            "https://www.instagram.com/example/",
            "https://m.instagram.com/example/",
        )
        for url in real_instagram:
            with self.subTest(url=url), self.assertRaisesRegex(
                ValueError, "dedicated reader context"
            ):
                safe_url(url, "read-site", resolve=False)

        lookalikes = (
            "https://evil-instagram.com/",
            "https://instagram.com.evil.example/",
        )
        for url in lookalikes:
            with self.subTest(url=url):
                self.assertEqual(safe_url(url, "read-site", resolve=False), url)
                with self.assertRaisesRegex(ValueError, "Instagram-only scope"):
                    safe_url(url, "read-ig", resolve=False)

    def test_private_and_local_addresses_are_denied(self):
        for url in (
            "https://localhost/",
            "https://127.0.0.1/",
            "https://10.0.0.1/",
        ):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "Private/local URL denied|Non-public address denied"):
                safe_url(url, "read-site", resolve=True)


if __name__ == "__main__":
    unittest.main()
