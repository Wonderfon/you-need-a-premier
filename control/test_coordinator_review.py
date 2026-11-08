import unittest
from datetime import datetime, timezone, timedelta
from coordinator_review import evaluate, plan


class CoordinatorReviewTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 29, 5, tzinfo=timezone.utc)
        self.policy = dict(anchor_at_utc='2026-09-29T01:39:59Z', anchor_used_percent=17,
                           ceiling_percent=75, shrink_percent=70, tolerance_pp=2,
                           window_minutes=10080, resets_at_unix=1791246275)
        self.usage = dict(used_percent=24, window_minutes=10080, resets_at_unix=1791246275,
                          observed_at_utc=self.now.isoformat())
        self.org = dict(jobs=[dict(id='j', status='reviewing')], incidents=[])
        self.voice = dict(trial_window=dict(ends_at='2026-09-29T13:09:02Z'),
                          tasks=dict(details=[dict(id='v', review_needed=True, receipt='receipt')]))

    def test_modes_and_thresholds(self):
        for used, mode, count in [(17,'normal',3),(24,'paced',1),(69.9,'paced',1),
                                  (70,'reserve',1),(74.9,'reserve',1),(75,'hard_stop',0)]:
            actual = evaluate(dict(self.usage, used_percent=used), self.policy, self.now)
            self.assertEqual((actual['mode'],actual['max_items']), (mode,count))

    def test_unknown_and_stale_fail_closed(self):
        cases = [{},dict(self.usage,used_percent=float('nan')),
                 dict(self.usage,resets_at_unix=1791246276),
                 dict(self.usage,observed_at_utc=(self.now-timedelta(minutes=16)).isoformat()),
                 dict(self.usage,observed_at_utc=(self.now+timedelta(minutes=2)).isoformat())]
        for usage in cases:
            self.assertEqual(evaluate(usage,self.policy,self.now)['max_items'],0)

    def test_paced_review_unblocks_queue(self):
        result = plan(self.usage,self.policy,self.org,self.voice,self.now)
        self.assertEqual([x['id'] for x in result['selected']],['voice:v'])
        self.assertEqual([x['id'] for x in result['deferred']],['org:j'])
        self.assertFalse(result['automatic_acceptance'])

    def test_incident_priority_and_dedup(self):
        self.org['incidents']=[dict(id='i',status='needs_coordinator')]*2
        self.org['jobs'][0]['incident_id']='i'
        result=plan(self.usage,self.policy,self.org,self.voice,self.now)
        self.assertEqual([x['id'] for x in result['selected']],['incident:i'])
        self.assertEqual([x['id'] for x in result['deferred']],['voice:v'])

    def test_reserve_only_critical_and_expiry(self):
        self.voice['tasks']['details']=[]
        result=plan(dict(self.usage,used_percent=70),self.policy,self.org,self.voice,self.now)
        self.assertEqual(result['selected'],[])
        self.voice['trial_window']['ends_at']=self.now.isoformat()
        result=plan(self.usage,self.policy,self.org,self.voice,self.now)
        self.assertEqual(result['budget']['mode'],'expired')
        self.assertEqual(result['selected'],[])


if __name__ == '__main__':
    unittest.main()
