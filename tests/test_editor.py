"""Native editor contracts. Synthetic transcript/bridge only; no user data."""
import os
import sys
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, 'mcp'), os.path.join(ROOT, 'extensions/transcript-ui')]
import editor_service as E

WORDS = [dict(i=i, w=w, f_start=10+i, f_end=10+i+0.8)
         for i, w in enumerate(['One', 'two.', 'Three', 'four.'])]
TRANSCRIPT = dict(media_path=__file__, clip='Test', words=WORDS)


def clip(start, trim, duration):
    return dict(id=str(trim), lane='primary', media_path=__file__,
                timeline_start_s=start, trim_start_s=trim, duration_s=duration)


class EditorTests(unittest.TestCase):
    def setUp(self):
        self.state = dict(duration_s=4, clips=[clip(0, 10, 4)])
        self.writes = []
        self.patches = [patch.object(E.core, '_fresh_transcript', return_value=TRANSCRIPT),
                        patch.object(E.core, 'logged', side_effect=lambda name, body, fn: fn(self.rpc)),
                        patch.object(E.core, '_settle', return_value=(4, True))]
        for p in self.patches: p.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(self.patches)])

    def rpc(self, method, params=None):
        if method == 'timeline.clips': return self.state
        self.writes.append((method, params))
        return {}

    def request(self, action, **extra):
        story = E.snapshot(self.rpc)
        return dict(revision=story['revision'], id=story['slices'][0]['id'], action=action, **extra)

    def test_playhead_selection_and_bridge_ids_do_not_stale_revision(self):
        revision = E.snapshot(self.rpc)['revision']
        self.state['playhead_s'] = 100
        self.state['clips'][0]['selected'] = True
        self.state['clips'][0]['id'] = 'new-bridge-handle'
        self.assertEqual(E.snapshot(self.rpc)['revision'], revision)
        self.state['clips'][0]['trim_start_s'] = 11
        self.assertNotEqual(E.snapshot(self.rpc)['revision'], revision)

    def test_stale_intent_rebases_if_source_words_still_match(self):
        body = self.request('trim', words=[0], source_words=[0, 1], dry_run=True)
        self.state['clips'].append(clip(4, 20, 2))
        result = E.edit(body)
        self.assertTrue(result['ok'])
        self.assertTrue(result['dry_run'])
        self.assertEqual(self.writes, [])

    def test_changed_source_cannot_be_rebased(self):
        body = self.request('delete', source_words=[0, 1])
        self.state['clips'] = [clip(0, 11, 3)]
        result = E.edit(body)
        self.assertEqual(result['code'], 'stale')
        self.assertEqual(self.writes, [])

    def test_changed_move_anchor_cannot_be_rebased(self):
        body = self.request('move', before_id='S2-3', source_words=[0, 1], anchor_words=[2, 3])
        self.state['clips'] = [clip(0, 10, 2), clip(2, 13, 1)]
        result = E.edit(body)
        self.assertEqual(result['code'], 'stale')
        self.assertEqual(self.writes, [])

    def test_other_project_cannot_be_rebased(self):
        body = self.request('delete', source_words=[0, 1])
        self.state['sequence_name'] = 'Different project'
        self.assertEqual(E.edit(body)['code'], 'stale')
        self.assertEqual(self.writes, [])

    def test_refresh_uses_timeline_order(self):
        self.state['clips'] = [clip(0, 12, 2), clip(2, 10, 2)]
        self.assertEqual(E.word_order(E.snapshot(self.rpc)), [2, 3, 0, 1])

    def test_split_creates_independent_slices_without_metadata(self):
        self.state['clips'] = [clip(0, 10, .9), clip(.9, 10.9, 3.1)]
        rows = E.snapshot(self.rpc)['slices']
        self.assertEqual([[w['i'] for w in s['words']] for s in rows], [[0], [1], [2, 3]])
        self.assertAlmostEqual(rows[0]['end'], rows[1]['start'])

    def test_trimmed_words_do_not_reappear(self):
        self.state['clips'] = [clip(0, 11, 3)]
        self.assertEqual(E.word_order(E.snapshot(self.rpc)), [1, 2, 3])

    def test_stale_revision_cannot_write(self):
        body = self.request('delete')
        self.state['clips'] = [clip(0, 11, 3)]
        self.assertFalse(E.edit(body)['ok'])
        self.assertEqual(self.writes, [])

    def test_move_returns_actual_order(self):
        body = self.request('move')
        def move(rpc, span, dest):
            self.state['clips'] = [clip(0, 11.9, 2.1), clip(2.1, 10, 1.9)]
        with patch.object(E.core, '_move_live_span', side_effect=move):
            result = E.edit(body)
        self.assertTrue(result['ok'])
        self.assertEqual(E.word_order(result['story']), [2, 3, 0, 1])

    def test_unverified_move_does_not_invent_rollback_or_retry(self):
        with patch.object(E.core, '_move_live_span') as move:
            result = E.edit(self.request('move'))
        self.assertFalse(result['ok'])
        self.assertEqual(E.word_order(result['story']), [0, 1, 2, 3])
        move.assert_called_once()

    def test_split_verified_by_boundary_not_only_duration(self):
        result = E.edit(self.request('split', words=[0]))
        self.assertFalse(result['ok'])
        self.assertEqual(len(self.writes), 2)

    def test_dry_run_is_read_only_and_word_bounded(self):
        result = E.edit(self.request('trim', words=[0], dry_run=True))
        self.assertTrue(result['ok'])
        self.assertEqual(result['plan']['text'], 'One')
        self.assertAlmostEqual(result['plan']['span'][1], .9)
        self.assertEqual(self.writes, [])

    def test_duplicate_source_disables_mutations(self):
        self.state['clips'].append(clip(4, 10, 4))
        story = E.snapshot(self.rpc)
        self.assertIsNotNone(story['edit_error'])
        self.assertEqual(len({s['id'] for s in story['slices']}), len(story['slices']))
        self.assertFalse(E.edit(self.request('delete'))['ok'])
        self.assertEqual(self.writes, [])

    def test_partial_cut_does_not_autocontinue(self):
        with patch.object(E.core, '_cut_spans', return_value={'ok': False, 'remaining': 1, 'error': 'failed'}) as cut:
            result = E.edit(self.request('trim', words=[0]))
        self.assertFalse(result['ok'])
        self.assertIn('story', result)
        cut.assert_called_once()


if __name__ == '__main__': unittest.main()
