import csv
import json
from pathlib import Path
import unittest

from cci_chop_timeframe_comparison_v1 import runner


class GuardedGridIntegrity(unittest.TestCase):
    def setUp(self):
        self.here=Path(runner.__file__).parent
        self.output=self.here/'guarded_grid_results'
        self.summary=json.loads((self.output/'summary.json').read_text())

    def test_guarded_matrix_is_complete_and_recipe_hashes_match(self):
        with (self.output/'comparison_all.csv').open() as f:rows=list(csv.DictReader(f))
        self.assertEqual(len(rows),3456)
        self.assertEqual(len({(r['period'],r['variant'],r['fee'],r['cost'],r['scope']) for r in rows}),3456)
        frozen=json.loads((self.output/'frozen_protocol.json').read_text())
        self.assertEqual(runner.base.sha(self.output/'frozen_protocol.json'),self.summary['protocol_sha256'])
        for name,digest in frozen['source_sha256'].items():self.assertEqual(runner.base.sha(self.here/name),digest)

    def test_guarded_selection_is_training_only_and_remains_unapproved(self):
        train=self.summary['periods']['train_2023_2024']['scenarios']
        selected,ranking=runner.choose(train,runner.variants())
        self.assertEqual(selected,self.summary['guarded_training_selected_rule'])
        self.assertEqual(ranking,self.summary['guarded_training_ranking'])
        self.assertEqual(selected['id'],'cci_m30_chop_h6')
        self.assertEqual(self.summary['necessary_condition_pass_both_periods'],[])
        self.assertIs(self.summary['deployment_approved'],False)

    def test_guarded_paired_bounds_are_frozen_and_lower_quantiles_are_ordered(self):
        protocol=json.loads((self.output/'bootstrap_protocol.json').read_text())
        bounds=json.loads((self.output/'bootstrap_bounds.json').read_text())
        self.assertEqual(runner.base.sha(self.output/'bootstrap_protocol.json'),bounds['protocol_sha256'])
        self.assertEqual(runner.base.sha(self.here/'guarded_bootstrap.py'),protocol['source_sha256'])
        for period,p in bounds['periods'].items():
            for unit,data in p.items():
                for key,b in data.items():
                    self.assertLessEqual(b['net_lower_familywise_usdt'],b['net_lower_5pct_usdt'])
                    self.assertLessEqual(b['incremental_lower_familywise_usdt'],b['incremental_lower_5pct_usdt'])
                    if key.startswith('baseline|'):
                        self.assertEqual(b['incremental_lower_5pct_usdt'],0)
                        self.assertEqual(b['incremental_lower_familywise_usdt'],0)


if __name__=='__main__':unittest.main()
