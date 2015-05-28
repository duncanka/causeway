from __future__ import absolute_import

from copy import deepcopy
import gflags
import os
import unittest

from data.readers import StandoffReader
from iaa import CausalityMetrics
from tests import get_resources_dir
from util.metrics import ClassificationMetrics, AccuracyMetrics

class CausalityMetricsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        gflags.FLAGS.metrics_log_raw_counts = True
        
    @staticmethod
    def _get_sentences_from_file(filename):
        reader = StandoffReader()
        reader.open(os.path.join(get_resources_dir(), 'IAATest', filename))
        sentences = reader.get_all()
        reader.close()
        return sentences
    
    @staticmethod
    def _get_sentences_with_swapped_args(sentences):
        swapped_sentences = []
        for sentence in sentences:
            swapped_sentence = deepcopy(sentence)
            for instance in swapped_sentence.causation_instances:
                instance.cause, instance.effect = (
                    instance.effect, instance.cause)
            swapped_sentences.append(swapped_sentence)
        return swapped_sentences

    def _test_arg_metrics(
        self, metrics, correct_cause_span_metrics, correct_effect_span_metrics,
        correct_cause_head_metrics, correct_effect_head_metrics,
        correct_cause_jaccard, correct_effect_jaccard):

        self.assertEqual(metrics.cause_span_metrics,
                         correct_cause_span_metrics)
        self.assertEqual(metrics.effect_span_metrics,
                         correct_effect_span_metrics)
        self.assertEqual(metrics.cause_head_metrics,
                         correct_cause_head_metrics)
        self.assertEqual(metrics.effect_head_metrics,
                         correct_effect_head_metrics)

        self.assertEqual(metrics.cause_jaccard, correct_cause_jaccard)
        self.assertEqual(metrics.effect_jaccard, correct_effect_jaccard)

        # TODO: verify type and degree matrices

    def setUp(self):
        self.sentences = self._get_sentences_from_file('standoff_test.ann')
        # We have 4 unmodified connectives; 1 connective with an added fragment
        # (still qualifies for partial overlap, so 1 FN + 1 FP if matching
        # without partial overlap); 2 missing connectives (FN), 1
        # with two arguments and one with only one; and 1 added connective (FP).
        #
        # We also have 1 cause adjusted to partially overlap; 1 cause deleted;
        # and 1 cause changed to a completely different span.
        self.modified_sentences = self._get_sentences_from_file(
            'standoff_test_modified.ann')

    def test_same_annotations_metrics(self):
        swapped = self._get_sentences_with_swapped_args(self.sentences)
        correct_connective_metrics = ClassificationMetrics(7, 0, 0)
        correct_arg_metrics = AccuracyMetrics(7, 0)
        for sentences in [self.sentences, swapped]:
            metrics = CausalityMetrics(sentences, sentences, False)
            self.assertEqual(metrics.connective_metrics,
                             correct_connective_metrics)
            self._test_arg_metrics(metrics, correct_connective_metrics,
                                   *([correct_arg_metrics] * 4 + [1.0] * 2))

    def test_modified_annotations_metrics(self):
        metrics = CausalityMetrics(self.sentences, self.modified_sentences,
                                   False)
        # For non-partial matching, the partial overlap counts as 1 FP + 1 FN.
        correct_connective_metrics = ClassificationMetrics(4, 2, 3)
        self.assertEqual(metrics.connective_metrics, correct_connective_metrics)

if __name__ == "__main__":
    # import sys;sys.argv = ['', 'Test.testName']
    unittest.main()
