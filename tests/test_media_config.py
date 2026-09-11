"""Protected media configuration never embeds task headers or reads credentials."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from researchops.config import OFFICIAL_IMAGE_HOSTS, load_settings
from researchops.errors import ConfigError
from researchops.runners.research_fetch import FetchPolicy, ResearchFetchError, _target


class TestMediaConfig(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / "settings.yaml"

    def tearDown(self):
        self.temp.cleanup()

    def load(self, media=None):
        self.config.write_text(yaml.safe_dump({"media": media or {}}))
        with patch("researchops.config.find_repo_root", return_value=self.root):
            return load_settings(self.config)

    def provider(self, **kwargs):
        return {"providers": {"cardrag": {"base_url": "http://127.0.0.1:18015", "bearer_token_file": str(self.root / "protected-token"), **kwargs}}}

    def test_disabled_providers_defaults_and_fixed_host_catalog(self):
        media = self.load().media
        self.assertEqual(media.providers, {})
        self.assertEqual(media.official_image_hosts, OFFICIAL_IMAGE_HOSTS)
        self.assertEqual((media.max_total_bytes, media.max_files, media.max_image_bytes), (20_000_000, 64, 4 * 1024 * 1024))
        self.assertEqual((media.file_timeout_seconds, media.phase_timeout_seconds), (20, 120))

    def test_default_catalog_accepts_official_plate_urls_without_network(self):
        policy = FetchPolicy(allowed_hosts=self.load().media.official_image_hosts, require_https=True)
        urls = {
            "kb": "https://img1.kbcard.com/ST/img/cxc/kbcard/upload/img/product/09780_img.png",
            "samsung": "https://static11.samsungcard.com/wcms/home/scard/personal/__icsFiles/afieldfile/2026/04/23/AAP1915_pc.png",
            "hyundai": "https://img.hyundaicard.com/img/com/card/card_ME4_h.png",
            "hana": "https://www.hanacard.co.kr/ATTACH/NEW_HOMEPAGE/images/cardinfo/card_img/14955.gif",
            "bc": "https://www.bccard.com/images/individual/card/renew/list/card_100008_a.png",
            "lotte": "https://image.lottecard.co.kr/UploadFiles/ecenterPath/cdInfo/ecenterCdInfoP14028-A14028_nm1.png",
            "shinhan": "https://www.shinhancard.com/pconts/images/contents/card/plate/cdCreditBNBCRM.png",
            "woori": "https://pc.wooricard.com/webcontent/cdPrdImgFileList/2026/8/6/75a5aba6-ff1f-4ac5-af98-a3f032e90517.png",
        }
        # URL-policy regression only: _target does not resolve DNS or fetch bytes.
        for issuer, url in urls.items():
            with self.subTest(issuer=issuer):
                self.assertEqual(_target(url, policy).url, url)

    def test_default_catalog_still_requires_exact_official_https_hosts(self):
        policy = FetchPolicy(allowed_hosts=self.load().media.official_image_hosts, require_https=True)
        for url, code in (
            ("https://unrelated.example/plate.png", "host_denied"),
            ("https://other.img1.kbcard.com/plate.png", "host_denied"),
            ("https://static11.samsungcard.com.example/plate.png", "host_denied"),
            ("https://other.hyundaicard.com/plate.png", "host_denied"),
            ("http://img1.kbcard.com/plate.png", "https_required"),
            ("http://static11.samsungcard.com/plate.png", "https_required"),
            ("http://img.hyundaicard.com/plate.png", "https_required"),
        ):
            with self.subTest(url=url):
                with self.assertRaises(ResearchFetchError) as error:
                    _target(url, policy)
                self.assertEqual(error.exception.code, code)

    def test_reference_loaded_without_opening_missing_secret(self):
        settings = self.load(self.provider())
        self.assertEqual(settings.media.providers["cardrag"].base_url, "http://127.0.0.1:18015")
        self.assertFalse(settings.media.providers["cardrag"].bearer_token_file.exists())
        settings = self.load(self.provider(base_url="http://[::1]:18015/"))
        self.assertEqual(settings.media.providers["cardrag"].base_url, "http://[::1]:18015")

    def test_unknown_headers_credentials_and_url_routing_not_supported(self):
        for extra in ({"bearer_token": "never-inline"}, {"headers": {"X": "value"}}, {"path": "/other"}):
            with self.subTest(extra=extra), self.assertRaises(ConfigError):
                self.load(self.provider(**extra))

    def test_fixed_loopback_origins_only(self):
        for base in ("http://localhost:18015", "https://127.0.0.1:18015", "http://127.0.0.1:18015/mcp",
                     "http://127.0.0.1:18015?token=secret", "http://127.0.0.1:18015#x", "http://127.0.0.1:0",
                     "http://127.0.0.1:018015", "http://127.0.0.1:18015@evil.example", "http://192.168.50.10:18015",
                     "http://[::ffff:127.0.0.1]:18015", "http://169.254.169.254"):
            with self.subTest(base=base), self.assertRaises(ConfigError):
                self.load(self.provider(base_url=base))

    def test_token_reference_cannot_be_relative_or_in_task_or_archive(self):
        for path in ("relative-secret", str(self.root / "var/task-workspaces/secret"), str(self.root / "var/run-archive/secret"),
                     str(self.root / "tasks/secret")):
            with self.subTest(path=path), self.assertRaises(ConfigError):
                self.load(self.provider(bearer_token_file=path))

    def test_explicit_official_host_list_is_exact_and_can_disable_images(self):
        self.assertEqual(self.load({"official_image_hosts": []}).media.official_image_hosts, ())
        self.assertEqual(self.load({"official_image_hosts": ["images.example.org"]}).media.official_image_hosts, ("images.example.org",))
        for hosts in (["*.example.org"], ["Example.org"], ["example.org/path"], ["http://example.org"],
                      ["example.org", "example.org"], [None], "example.org"):
            with self.subTest(hosts=hosts), self.assertRaises(ConfigError):
                self.load({"official_image_hosts": hosts})

    def test_bounded_adjustable_limits_and_no_retries(self):
        media = self.load({"file_timeout_seconds": 30, "phase_timeout_seconds": 600, "max_files": 10,
                           "max_total_bytes": 1_000_000, "max_image_bytes": 500_000}).media
        self.assertEqual((media.file_timeout_seconds, media.phase_timeout_seconds), (30, 600))
        for item in ({"file_timeout_seconds": 31}, {"phase_timeout_seconds": 601}, {"max_files": 65},
                     {"max_total_bytes": 20_000_001}, {"max_image_bytes": 4_194_305}, {"retries": 1}, {"retries": False},
                     {"mime_reserve_bytes": 1_999_999}, {"mime_part_header_bytes": 4095}, {"max_files": True}):
            with self.subTest(item=item), self.assertRaises(ConfigError):
                self.load(item)

    def test_provider_ids_are_bounded_not_arbitrary_references(self):
        for key in ("../other", "cardrag token", "x" * 65):
            with self.subTest(key=key), self.assertRaises(ConfigError):
                provider = self.provider()["providers"]["cardrag"]
                self.load({"providers": {key: provider}})
