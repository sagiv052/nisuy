import asyncio
import tempfile
import unittest
from pathlib import Path

asyncio.set_event_loop(asyncio.new_event_loop())

from catalog import Catalog
from main import parse_cloudinary_config, parse_media_caption
from stream_utils import RangeNotSatisfiable, content_disposition_filename, parse_range


class StreamUtilsTests(unittest.TestCase):
    def test_full_and_open_ended_ranges(self):
        self.assertEqual(parse_range("bytes=0-99", 1000), (0, 99))
        self.assertEqual(parse_range("bytes=100-", 1000), (100, 999))
        self.assertEqual(parse_range("bytes=-50", 1000), (950, 999))
        self.assertEqual(parse_range("bytes=0-5000", 1000), (0, 999))

    def test_invalid_ranges(self):
        for value in ("bytes=1000-", "bytes=10-9", "bytes=-0", "bytes=1-2,4-5", "items=0-1"):
            with self.subTest(value=value):
                with self.assertRaises(RangeNotSatisfiable):
                    parse_range(value, 1000)

    def test_filename_header_is_safe_and_utf8_capable(self):
        header = content_disposition_filename('סרט\r\n"/פרק.mp4')
        self.assertNotIn("\r", header)
        self.assertNotIn("\n", header)
        self.assertIn("filename*=UTF-8''", header)

    def test_parse_media_caption_extracts_episode_summary_and_genre(self):
        caption = (
            "חיים של קוקו - עונה 1 פרק 2\n"
            "זאנר: סדרה ישראלית | תרגום מובנה 🇮🇱\n"
            "שנת יציאה: 2022\n"
            "איכות 1080p WEB-DL x265 ❤\n"
            "תקציר:\n"
            "> הלחץ על המשפחה גובר. מטי לוקחת את גד ואלה לעורך דין\n"
            "**הועלה וקודד ע\"י אחלה בנאדם\n"
            "עבור הקרטל בטלגרם** 👍"
        )

        metadata = parse_media_caption(caption)

        self.assertIsNotNone(metadata)
        metadata = metadata if metadata is not None else {}
        self.assertEqual(metadata["kind"], "episode")
        self.assertEqual(metadata["season"], 1)
        self.assertEqual(metadata["episode"], 2)
        self.assertEqual(metadata["quality"], "1080p WEB-DL x265")
        self.assertEqual(metadata["genre"], "סדרה ישראלית")
        self.assertEqual(metadata["year"], 2022)
        summary = metadata["summary"] if metadata.get("summary") else ""
        self.assertIn("הלחץ על המשפחה גובר", summary)

    def test_parse_cloudinary_config(self):
        config = parse_cloudinary_config("cloudinary://123456789:test-api-secret@demo-cloud")
        self.assertEqual(config["cloud_name"], "demo-cloud")
        self.assertEqual(config["api_key"], "123456789")
        self.assertEqual(config["api_secret"], "test-api-secret")


class CatalogTests(unittest.TestCase):
    def test_upload_is_idempotent_and_cascades_series_data(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = Catalog(str(Path(directory) / "catalog.db"))
            upload_one = catalog.save_upload("a.mp4", 10, "video/mp4", "url", 7, 8)
            upload_two = catalog.save_upload("a.mp4", 10, "video/mp4", "url", 7, 8)
            self.assertEqual(upload_one, upload_two)
            series_id = catalog.add_item("series", "Test")
            catalog.add_episode(series_id, 1, 1, "Episode 1", "url")
            self.assertEqual(len(catalog.list_episodes(series_id)), 1)
            catalog.delete_item(series_id)
            self.assertEqual(catalog.list_episodes(series_id), [])

    def test_integrity_report_finds_duplicates_and_episode_gaps(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = Catalog(str(Path(directory) / "catalog.db"))
            catalog.add_item("movie", "Same Movie")
            duplicate_id = catalog.add_item("movie", " same movie ")
            series_id = catalog.add_item("series", "Test Series")
            catalog.add_episode(series_id, 1, 1, "Episode 1")
            catalog.add_episode(series_id, 1, 3, "Episode 3")

            report = catalog.integrity_report()

            self.assertEqual(report["duplicates"][0]["item_ids"], [1, duplicate_id])
            self.assertEqual(report["missing_episodes"], [{
                "series_id": series_id,
                "series_title": "Test Series",
                "season_number": 1,
                "missing_episodes": [2],
            }])

    def test_episode_upload_fills_gap_and_updates_existing_episode(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = Catalog(str(Path(directory) / "catalog.db"))
            series_id = catalog.add_item("series", "Test Series")
            catalog.add_episode(series_id, 1, 1, "Episode 1", "url-1")
            catalog.add_episode(series_id, 1, 2, "Episode 2", "url-2")
            catalog.add_episode(series_id, 1, 4, "Episode 4", "url-4", "720p")

            existing_id = catalog.add_episode(series_id, 1, 2, "Episode 2 updated", "url-2-new")
            missing_id = catalog.add_episode(series_id, 1, 3, "Episode 3", "url-3")

            episodes = catalog.list_episodes(series_id, 1)
            self.assertEqual([episode["episode_number"] for episode in episodes], [1, 2, 3, 4])
            self.assertEqual(episodes[1]["id"], existing_id)
            self.assertEqual(episodes[1]["title"], "Episode 2 updated")
            self.assertEqual(episodes[1]["stream_url"], "url-2-new")
            self.assertEqual(episodes[2]["id"], missing_id)
            self.assertEqual(episodes[3]["quality"], "720p")

    def test_upload_stream_url_can_be_updated_after_cloudinary_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = Catalog(str(Path(directory) / "catalog.db"))
            upload_id = catalog.save_upload("a.mp4", 10, "video/mp4", "telegram-url", 7, 8)
            series_id = catalog.add_item("series", "Test Series")
            episode_id = catalog.add_episode(series_id, 1, 1, "Episode 1", "telegram-url")
            catalog.attach_upload(upload_id, episode_id)

            catalog.update_upload_stream_url(upload_id, "cloudinary-url")

            episodes = catalog.list_episodes(series_id, 1)
            self.assertEqual(episodes[0]["stream_url"], "cloudinary-url")
            self.assertEqual(catalog.list_uploads()[0]["stream_url"], "cloudinary-url")

    def test_admin_and_chat_management_is_persistent(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = Catalog(str(Path(directory) / "catalog.db"))
            catalog.add_admin(5699704187)
            catalog.add_admin(12345)
            catalog.add_user(98765, 5699704187)
            catalog.register_chat(-100123, "Test Group", "supergroup", 5699704187)

            self.assertEqual(catalog.list_admins(), [12345, 5699704187])
            self.assertTrue(catalog.is_user_approved(98765))
            self.assertEqual(catalog.list_bot_chats()[0]["chat_id"], -100123)
            self.assertTrue(catalog.claim_access_notice("user:555"))
            self.assertFalse(catalog.claim_access_notice("user:555"))

            reopened = Catalog(str(Path(directory) / "catalog.db"))
            self.assertEqual(reopened.list_admins(), [12345, 5699704187])
            self.assertTrue(reopened.is_user_approved(98765))
            self.assertEqual(reopened.list_bot_chats()[0]["title"], "Test Group")

    def test_quality_metadata_is_persistent(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "catalog.db")
            catalog = Catalog(database_path)
            movie_id = catalog.add_item("movie", "Die My Love", quality="1080P BluRay remux")

            item = catalog.get_item(movie_id)
            reopened_item = Catalog(database_path).get_item(movie_id)
            if item is None or reopened_item is None:
                self.fail("Movie was not persisted")
            self.assertEqual(item["quality"], "1080P BluRay remux")
            self.assertEqual(reopened_item["quality"], "1080P BluRay remux")

    def test_integrity_report_tracks_missing_posters_and_empty_summaries(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = Catalog(str(Path(directory) / "catalog.db"))
            catalog.add_item("movie", "Missing Poster", poster_url="")
            catalog.add_item("movie", "Empty Summary", summary="")
            catalog.add_item("series", "Series With Poster", poster_url="https://example.com/poster.jpg", summary="Good summary")

            report = catalog.integrity_report()

            self.assertIn(
                {"item_id": 1, "title": "Missing Poster", "kind": "movie"},
                report["missing_posters"],
            )
            self.assertIn(
                {"item_id": 2, "title": "Empty Summary", "kind": "movie"},
                report["empty_summaries"],
            )


if __name__ == "__main__":
    unittest.main()
