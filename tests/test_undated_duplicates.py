import tempfile
import unittest
from pathlib import Path

from foodfinder.database import connect, initialize, link_club_account, upsert_club
from foodfinder.scanner import event_counts, get_event, list_events, patch_event


class UndatedDuplicateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'events.sqlite3'
        initialize(self.path)

    def add_event(self, ident, title, day='', time='', status='upcoming'):
        with connect(self.path) as db:
            club = upsert_club(db, source_key=ident, name=ident)
            account = link_club_account(db, club, ident)
            db.execute("INSERT INTO sources(id,account_id,source_key,kind,url,checked_at) VALUES (?,?,?,'story',?,'2026-09-28')",
                       (ident, account, ident, f'https://www.instagram.com/stories/{ident}/1/'))
            db.execute("""INSERT INTO events(id,source_id,event_key,title,event_date,event_time,
                       food_confidence,review_reason,status,updated_at)
                       VALUES (?,?,?,?,?,?,'high',?,?,'2026-09-28')""",
                       (ident, ident, ident, title, day, time,
                        '' if day else 'Could not confidently read an event date.', status))
            db.execute("INSERT INTO evidence(event_id,source_id,created_at) VALUES (?,?,'2026-09-28')", (ident, ident))
        return club

    def test_undated_repost_joins_dated_card_across_tabs_and_filters(self):
        self.add_event('known', 'APSA MOVIE NIGHT:', '2099-10-01', '17:00')
        club = self.add_event('unknown', 'APSA MOVE NIGHT!')
        events = list_events(db_path=self.path)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['event_date'], '2099-10-01')
        self.assertEqual(events[0]['event_time'], '17:00')
        self.assertEqual(len(events[0]['supporting_sources']), 2)
        self.assertEqual(list_events(view='review', db_path=self.path), [])
        self.assertEqual(event_counts(self.path), {'upcoming': 1, 'today': 0, 'review': 0})
        self.assertEqual(len(list_events(club=club, from_date='2099-10-01', to_date='2099-10-01', db_path=self.path)), 1)
        self.assertEqual(get_event('unknown', self.path)['event_date'], '2099-10-01')
        patch_event('unknown', {'title': 'APSA film screening'}, self.path)
        self.assertEqual(len(list_events(db_path=self.path)[0]['supporting_sources']), 2)
        patch_event('known', {'status': 'dismissed'}, self.path)
        self.assertEqual(list_events(view='review', db_path=self.path), [])
        self.assertEqual(list_events(db_path=self.path), [])

    def test_two_possible_dates_leave_unknown_in_review(self):
        self.add_event('first', 'APSA MOVIE NIGHT', '2099-10-01')
        self.add_event('second', 'APSA MOVIE NIGHT', '2099-10-08')
        self.add_event('unknown', 'APSA MOVIE NIGHT')
        self.assertEqual(len(list_events(db_path=self.path)), 2)
        self.assertEqual(len(list_events(view='review', db_path=self.path)), 1)

    def test_similar_but_not_near_identical_titles_stay_separate(self):
        self.add_event('known', 'APSA MOVIE NIGHT', '2099-10-01')
        self.add_event('unknown', 'APSA MUSIC NIGHT')
        self.assertEqual(len(list_events(view='review', db_path=self.path)), 1)

    def test_conflicting_times_stay_separate(self):
        self.add_event('known', 'APSA MOVIE NIGHT', '2099-10-01', '17:00')
        self.add_event('unknown', 'APSA MOVIE NIGHT', time='19:00')
        self.assertEqual(len(list_events(view='review', db_path=self.path)), 1)

    def test_multiple_undated_sources_can_join_one_dated_card(self):
        self.add_event('known', 'APSA MOVIE NIGHT', '2099-10-01')
        for ident in ('a', 'z'):
            self.add_event(ident, 'APSA MOVIE NIGHT!')
        self.assertEqual(len(list_events(db_path=self.path)[0]['supporting_sources']), 3)
        self.assertEqual(event_counts(self.path)['review'], 0)

    def test_undated_only_and_dismissed_matches_do_not_supply_dates(self):
        self.add_event('known', 'APSA MOVIE NIGHT', '2099-10-01', status='dismissed')
        self.add_event('unknown', 'APSA MOVIE NIGHT')
        self.add_event('other', 'APSA MOVIE NIGHT')
        self.assertEqual(len(list_events(view='review', db_path=self.path)), 2)
