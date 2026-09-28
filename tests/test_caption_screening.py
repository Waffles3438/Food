import unittest
from unittest.mock import patch

from fixed_clock import EventFixtureDateTime
from foodfinder.events import caption_needs_ocr, extract_events
from foodfinder.food_keywords import FOOD_KEYWORDS


POSTED = "2026-09-23T12:00:00-04:00"


class CaptionScreeningTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("foodfinder.events.datetime", EventFixtureDateTime))

    def candidates(self, text):
        return extract_events(text, posted_at=POSTED, account_name="club")

    def test_free_food_keywords_and_plural_variants_are_detected(self):
        for word in FOOD_KEYWORDS:
            with self.subTest(word=word):
                events = self.candidates(f"Free {word}! September 27, 2026 at 6 PM.")
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0].food.lower(), f"free {word}")
                self.assertEqual(events[0].food_confidence, "high")

    def test_hashtag_and_reverse_complimentary_claims(self):
        for offer in ("#FreeCookies", "FREE HAMBURGERS", "Complimentary BBQ", "Cookies are free", "Pizza included with admission"):
            with self.subTest(offer=offer):
                events = self.candidates(offer + "! September 27, 2026.")
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0].food_confidence, "high")

    def test_free_cookies_without_date_survives_for_review_and_requests_ocr(self):
        events = self.candidates("Free cookies!")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].food, "Free cookies")
        self.assertEqual(events[0].event_date, "")
        self.assertIn("date", events[0].review_reason)
        self.assertTrue(caption_needs_ocr("Free cookies!", posted_at=POSTED))

    def test_complete_caption_skips_ocr_but_missing_fields_request_it(self):
        full = "Free cookies! September 27, 2026 at 6 PM.\nLocation: Student Centre"
        self.assertFalse(caption_needs_ocr(full, posted_at=POSTED))
        for text in (full.replace("September 27, 2026", ""), full.replace("at 6 PM", ""), full.split("\n")[0]):
            with self.subTest(text=text):
                self.assertTrue(caption_needs_ocr(text, posted_at=POSTED))

    def test_empty_event_and_food_captions_request_ocr(self):
        for caption in ("", "Join us Thursday! Details above.", "Pizza night", "Our event has been cancelled"):
            with self.subTest(caption=caption):
                self.assertTrue(caption_needs_ocr(caption, posted_at=POSTED))
        self.assertFalse(caption_needs_ocr("Meet our new executive team!", posted_at=POSTED))

    def test_food_mentions_dietary_free_and_unrelated_free_are_not_offers(self):
        for text in ("Free play", "Gluten-free pizza", "Sugar-free cookies", "Pizza costs $5", "Free entry, hamburgers cost $5"):
            with self.subTest(text=text):
                self.assertEqual(self.candidates(text + ". Social on September 27, 2026."), [])

    def test_negated_new_food_words_and_available_food_are_not_confident_offers(self):
        for text in ("No free hamburgers", "No free cookies", "No complimentary BBQ", "Burgers available"):
            with self.subTest(text=text):
                events = self.candidates(text + ". Social on September 27, 2026.")
                self.assertTrue(all(event.review_reason for event in events))
                self.assertTrue(all(event.food_confidence != "high" for event in events))

    def test_membership_sentence_does_not_negate_free_food(self):
        events = self.candidates("No membership required. Free cookies! September 27, 2026.")
        self.assertEqual(events[0].food_confidence, "high")
