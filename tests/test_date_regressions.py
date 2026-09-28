import unittest
from datetime import datetime, timezone
from foodfinder.events import extract_events, _extract_dates, _extract_times

class DateRegressionTests(unittest.TestCase):
    def setUp(self):
        self.posted = datetime(2026, 8, 22, 17, tzinfo=timezone.utc)
        self.now = datetime(2026, 9, 1, tzinfo=timezone.utc)

    def test_missing_year_uses_post_year_not_current_year(self):
        dates = _extract_dates('September 15th. Sept 15th.', self.posted, datetime(2027, 1, 1, tzinfo=timezone.utc))
        self.assertEqual([str(day) for day, _ in dates], ['2026-09-15'])

    def test_no_rollover_at_year_boundary(self):
        dates = _extract_dates('January 3', datetime(2026, 12, 30, tzinfo=timezone.utc), self.now)
        self.assertEqual(str(dates[0][0]), '2026-01-03')

    def test_durations_are_not_calendar_dates(self):
        self.assertEqual(_extract_dates('4-16 month co-op positions available', self.posted, self.now), [])

    def test_caption_date_wins_over_historical_image_date(self):
        caption = 'Come to our Fall Orientation on September 15th, 6-8pm. Pizza will be provided!'
        events = extract_events(caption + '\nPublished November 29, 2010', posted_at=self.posted,
                                account_name='Climate Justice', now=self.now, date_text=caption)
        self.assertEqual([e.event_date for e in events], ['2026-09-15'])
        self.assertEqual(events[0].event_time, '18:00')

    def test_food_session_not_followup_interviews(self):
        caption = ('Interested in career opportunities at Vale?\n\nJoin the Vale Information Session on '
                   'September 17, 2026. Pizza & drinks provided!\n\nInterviews will follow on September 21 & 22, 2026.')
        events = extract_events(caption + '\nSep 21/22: Interviews\n4-16 month co-op positions',
                                posted_at=self.posted, account_name='CSCE', now=self.now, date_text=caption)
        self.assertEqual([e.event_date for e in events], ['2026-09-17'])

    def test_two_separately_dated_food_events_remain(self):
        events = extract_events('Pizza social September 17, 2026. Free pizza!\n\nCookie meetup September 19, 2026. Free cookies!',
                                posted_at=self.posted, account_name='Club', now=self.now)
        self.assertEqual([e.event_date for e in events], ['2026-09-17','2026-09-19'])

    def test_explicit_year_and_dotted_time(self):
        self.assertEqual(str(_extract_dates('September 17, 2025', self.posted, self.now)[0][0]), '2025-09-17')
        self.assertEqual(_extract_times('5.30pm'), ['17:30'])
