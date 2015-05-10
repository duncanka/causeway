from __future__ import print_function
from collections import defaultdict
import colorama
colorama.init()
colorama.deinit()
from copy import copy
from StringIO import StringIO
from gflags import DEFINE_list, DEFINE_float, DEFINE_bool, DEFINE_string, DuplicateFlagError, FLAGS
import logging
import operator
import os
import sys
from textwrap import wrap

from data import CausationInstance, ParsedSentence
from util import Enum, print_indented, truncated_string, get_terminal_size
from util.diff import SequenceDiff
from util.metrics import ClassificationMetrics, ConfusionMatrix

try:
    DEFINE_list(
        'iaa_given_connective_ids', [], "Annotation IDs for connectives"
        " that were given as gold and should be treated separately for IAA.")
    DEFINE_float(
        'iaa_min_partial_overlap', 0.5, "Minimum fraction of the larger of two"
        " annotations that must be overlapping for the two annotations to be"
        " considered a partial match.")
    DEFINE_bool('iaa_log_confusion', False, "Log confusion matrices for IAA.")
    DEFINE_bool('iaa_log_stats', True, 'Log IAA statistics.')
    DEFINE_bool('iaa_log_differences', False,
                'Log differing annotations during IAA comparison.')
    DEFINE_string('iaa_cause_color', 'Blue',
                  'ANSI color to use for formatting cause words in IAA'
                  ' comparison output')
    DEFINE_string('iaa_effect_color', 'Red',
                  'ANSI color to use for formatting cause words in IAA'
                  ' comparison output')
    DEFINE_bool('iaa_force_color', False,
                "Force ANSI color in IAA comparisons even when we're not"
                " outputting to a TTY")
except DuplicateFlagError as e:
    logging.warn('Ignoring redefinition of flag %s' % e.flagname)


def make_annotation_comparator(allow_partial):
    min_partial_overlap = [1.0, FLAGS.iaa_min_partial_overlap][allow_partial]

    def match_annotations(token_list_1, token_list_2):
        offsets_1 = [(token.start_offset, token.end_offset)
                     for token in token_list_1]
        offsets_2 = [(token.start_offset, token.end_offset)
                     for token in token_list_2]
        if offsets_1 == offsets_2:
            return True

        # No partial matching allowed
        if min_partial_overlap == 1.0:
            return False

        a1_length = sum([end - start for start, end in offsets_1])
        a2_length = sum([end - start for start, end in offsets_2])
        # Larger length is converted to float below to avoid having to do that
        # repeatedly when computing fractions.
        if a1_length > a2_length:
            larger_offsets, smaller_offsets, larger_length = (
                offsets_1, offsets_2, float(a1_length))
        else:
            larger_offsets, smaller_offsets, larger_length = (
                offsets_2, offsets_1, float(a2_length))

        # This algorithm assumes no fragments ever overlap with each other.
        # Thus, each token of the smaller annotation can only ever overlap with
        # a single fragment from the larger annotation. This means we can safely
        # add a separate fraction to the total percent overlap each time we
        # detect any overlap at all.
        fraction_of_larger_overlapping = 0.0
        for larger_offset in larger_offsets:
            for smaller_offset in smaller_offsets:
                overlap_start = max(larger_offset[0], smaller_offset[0])
                overlap_end = min(larger_offset[1], smaller_offset[1])
                overlap_size = overlap_end - overlap_start
                if overlap_size > 0:
                    fraction_of_larger_overlapping += (
                        overlap_size / larger_length)

        return fraction_of_larger_overlapping > min_partial_overlap

    return match_annotations


def get_truncated_sentence(instance):
    return truncated_string(
        instance.source_sentence.original_text.replace('\n', ' '))

class CausalityMetrics(object):
    IDsConsidered = Enum(['GivenOnly', 'NonGivenOnly', 'Both'])
    ArgTypes = Enum(['Cause', 'Effect'])

    def __init__(self, gold, predicted, allow_partial,
                 save_differences=False, ids_considered=None,
                 compare_degrees=True, compare_types=True):
        assert len(gold) == len(predicted), (
            "Cannot compute IAA for different-sized datasets")

        if ids_considered is None:
            ids_considered = CausalityMetrics.IDsConsidered.Both
        self.allow_partial = allow_partial
        self._annotation_comparator = make_annotation_comparator(allow_partial)
        self.ids_considered = ids_considered
        self.save_differences = save_differences
        self.gold_only_instances = []
        self.predicted_only_instances = []
        self.argument_differences = []
        self.property_differences = []

        # Compute attributes that take a little more work.
        self.connective_metrics, matches = self._match_connectives(
            gold, predicted)
        if compare_degrees:
            self.degree_matrix = self._compute_agreement_matrix(
                matches, CausationInstance.Degrees, 'degree', gold)
        else:
            self.degree_matrix = None

        if compare_types:
            self.causation_type_matrix = self._compute_agreement_matrix(
                matches, CausationInstance.CausationTypes, 'type', gold)
        else:
            self.causation_type_matrix = None
        self.arg_metrics, self.arg_label_matrix = self._match_arguments(
            matches, gold)

    def __add__(self, other):
        if (self.allow_partial != other.allow_partial or
            [self.degree_matrix, other.degree_matrix].count(None) == 1 or
            [self.causation_type_matrix,
             other.causation_type_matrix].count(None) == 1):
            raise ValueError("Can't add causality metrics with different"
                             " comparison criteria")

        sum_metrics = copy(self)
        # Add recorded instances/differences
        sum_metrics.gold_only_instances.extend(other.gold_only_instances)
        sum_metrics.predicted_only_instances.extend(
            other.predicted_only_instances)
        sum_metrics.property_differences.extend(other.property_differences)
        # Add together submetrics, if they exist
        sum_metrics.connective_metrics += other.connective_metrics
        if sum_metrics.degree_matrix is not None:
            sum_metrics.degree_matrix += other.degree_matrix
        if sum_metrics.causation_type_matrix is not None:
            sum_metrics.causation_type_matrix += other.causation_type_matrix
        sum_metrics.arg_metrics += other.arg_metrics
        sum_metrics.arg_label_matrix += other.arg_label_matrix

        return sum_metrics

    def __get_causations(self, sentence):
        causations = []
        for instance in sentence.causation_instances:
            is_given_id = instance.id in FLAGS.iaa_given_connective_ids
            if (self.ids_considered == self.IDsConsidered.Both or
                (is_given_id and
                 self.ids_considered == self.IDsConsidered.GivenOnly) or
                (not is_given_id and
                 self.ids_considered == self.IDsConsidered.NonGivenOnly)):
                causations.append(instance)
        return causations

    def _match_connectives(self, gold, predicted):
        matching_instances = []
        gold_only_instances = []
        predicted_only_instances = []
        def compare_connectives(instance_1, instance_2):
            return self._annotation_comparator(instance_1.connective,
                                               instance_2.connective)
            
        if self.allow_partial:
            def compare_connectives_exact(instance_1, instance_2):
                comparator = make_annotation_comparator(False)
                return comparator(instance_1.connective, instance_2.connective)

        for gold_sentence, predicted_sentence in zip(gold, predicted):
            assert (gold_sentence.original_text ==
                    predicted_sentence.original_text), (
                        "Can't compare annotations on non-identical sentences")
            gold_causations = self.__get_causations(gold_sentence)
            predicted_causations = self.__get_causations(predicted_sentence)
            sort_key = lambda inst: inst.connective[0].start_offset
            
            # If we're allowing partial matches, we don't want any partial
            # matches to override full matches. So we first do an exact match,
            # and remove the ones that matched from the partial matching.
            if self.allow_partial:
                diff = SequenceDiff(gold_causations, predicted_causations,
                                    compare_connectives_exact, sort_key)
                matching_pairs = diff.get_matching_pairs()
                matching_instances.extend(matching_pairs)

                matched_golds = [gold_causation for gold_causation, _
                                 in matching_pairs]
                gold_causations = [c for c in gold_causations
                                   if c not in matched_golds]

                matched_predicteds = [predicted_causation for
                                      _, predicted_causation in matching_pairs]
                predicted_causations = [c for c in predicted_causations
                                        if c not in matched_predicteds]
            
            diff = SequenceDiff(gold_causations, predicted_causations,
                                compare_connectives, sort_key)
            matching_instances.extend(diff.get_matching_pairs())
            gold_only_instances.extend(diff.get_a_only_elements())
            predicted_only_instances.extend(diff.get_b_only_elements())

        if self.ids_considered == CausalityMetrics.IDsConsidered.GivenOnly:
            assert len(matching_instances) == len(
                FLAGS.iaa_given_connective_ids), (
                    "Didn't find all expected given connectives! Perhaps"
                    " annotators re-annotated spans with different IDs?")
            # Leave connective_metrics as None to indicate that there aren't
            # any interesting values here. (Everything should be perfect.)
            connective_metrics = None
        # "Both" will only affect the connective stats if there are actually
        # some given connectives.
        elif (self.ids_considered == CausalityMetrics.IDsConsidered.Both
              and FLAGS.iaa_given_connective_ids):
            connective_metrics = None
        else:
            connective_metrics = ClassificationMetrics(
                len(matching_instances), len(predicted_only_instances),
                len(gold_only_instances))

        if self.save_differences:
            def sentences_by_file(sentences):
                by_file = defaultdict(list)
                for sentence in sentences:
                    filename = os.path.split(sentence.source_file_path)[-1]
                    by_file[filename].append(sentence)
                return by_file
            gold_by_file = sentences_by_file(gold)
            predicted_by_file = sentences_by_file(predicted)

            self.gold_only_instances = [
                (gold_by_file[os.path.split(
                    i.source_sentence.source_file_path)[-1]]
                 .index(i.source_sentence) + 1, i)
                for i in gold_only_instances]
            self.predicted_only_instances = [
                (predicted_by_file[os.path.split(
                    i.source_sentence.source_file_path)[-1]]
                 .index(i.source_sentence) + 1, i)
                for i in predicted_only_instances]

        return (connective_metrics, matching_instances)

    def _compute_agreement_matrix(self, matches, labels_enum, property_name,
                                  gold_sentences):
        labels_1 = []
        labels_2 = []

        def log_missing(instance, number):
            print(property_type_name,
                  ('property not set in Annotation %d;' % number),
                  'not including in analysis (sentence: "',
                  get_truncated_sentence(instance_1).encode('utf-8') + '")',
                  file=sys.stderr)

        for instance_1, instance_2 in matches:
            property_1 = getattr(instance_1, property_name)
            property_2 = getattr(instance_2, property_name)

            property_type_name = (["Degree", "Causation type"]
                                  [property_name == 'type'])
            if property_1 >= len(labels_enum):
                log_missing(instance_1, 1)
            elif property_2 >= len(labels_enum):
                log_missing(instance_2, 2)
            else:
                labels_1.append(labels_enum[property_1])
                labels_2.append(labels_enum[property_2])
                sentence_num = (
                    gold_sentences.index(instance_1.source_sentence) + 1)
                if property_1 != property_2 and self.save_differences:
                    self.property_differences.append(
                        (instance_1, instance_2, labels_enum, sentence_num))

        return ConfusionMatrix(labels_1, labels_2)

    def _match_arguments(self, matches, gold):
        # Initially, we assume every argument was unique. We'll update this
        # incrementally as we find matches.
        gold_only_args = predicted_only_args = 2 * len(matches)
        null_args = 0

        gold_labels = []
        predicted_labels = []

        for instance_1, instance_2 in matches:
            gold_args = (instance_1.cause, instance_1.effect)
            predicted_args = (instance_2.cause, instance_2.effect)
            sentence_num = gold.index(instance_1.source_sentence) + 1

            predicted_args_matched = [False, False]
            for i in range(len(gold_args)):
                if gold_args[i] is None:
                    gold_only_args -= 1
                    null_args += 1
                    continue
                for j in range(len(predicted_args)):
                    if predicted_args[j] is None:
                        # Only update arg counts on the first round to avoid
                        # double-counting
                        if i == 0:
                            predicted_only_args -= 1
                            null_args += 1
                        continue
                    elif predicted_args_matched[j]:
                        continue
                    elif self._annotation_comparator(gold_args[i],
                                                     predicted_args[j]):
                        gold_labels.append(self.ArgTypes[i])
                        predicted_labels.append(self.ArgTypes[j])
                        if self.save_differences and i != j:
                            self.property_differences.append(
                                (instance_1, instance_2, self.ArgTypes,
                                 sentence_num))
                        predicted_args_matched[j] = True
                        gold_only_args -= 1
                        predicted_only_args -= 1
                        # We're done matching this gold arg; move on to the next
                        break
            
            if predicted_args_matched != [True, True] and self.save_differences:
                self.argument_differences.append((instance_1, instance_2,
                                                  sentence_num))

        total_matches = len(gold_labels)
        #assert 4 * len(matches) == (2 * total_matches + gold_only_args +
        #                            predicted_only_args + null_args)
        arg_metrics = ClassificationMetrics(total_matches, predicted_only_args,
                                            gold_only_args)
        arg_label_matrix = ConfusionMatrix(gold_labels, predicted_labels)
        return arg_metrics, arg_label_matrix

    def pp(self, log_confusion=None, log_stats=None, log_differences=None,
           indent=0, file=sys.stdout):
        # Flags aren't available as defaults when the function is created, so
        # set the defaults here.
        if log_confusion is None:
            log_confusion = FLAGS.iaa_log_confusion
        if log_stats is None:
            log_stats = FLAGS.iaa_log_stats
        if log_differences is None:
            log_differences = FLAGS.iaa_log_differences

        if log_differences:
            colorama.reinit()

        if log_differences and (
            self.gold_only_instances or self.predicted_only_instances
            or self.property_differences):
            print_indented(indent, 'Annotation differences:', file=file)
            for sentence_num, instance in self.gold_only_instances:
                self._log_unique_instance(instance, sentence_num, 1,
                                          indent + 1, file)
            for sentence_num, instance in self.predicted_only_instances:
                self._log_unique_instance(instance, sentence_num, 2,
                                           indent + 1, file)
            self._log_property_differences(CausationInstance.CausationTypes,
                                           indent + 1, file)
            self._log_property_differences(CausationInstance.Degrees,
                                           indent + 1, file)
            self._log_arg_label_differences(indent + 1, file)

        # Ignore connective-related metrics if we have nothing interesting to
        # show there.
        printing_connective_metrics = (log_stats and self.connective_metrics)
        if printing_connective_metrics or log_confusion:
            print_indented(indent, 'Connectives:', file=file)
        if printing_connective_metrics:
            print_indented(indent + 1, self.connective_metrics, file=file)
        if log_stats or log_confusion:
            if self.degree_matrix is not None:
                self._log_property_metrics(
                    'Degrees', self.degree_matrix, indent + 1, log_confusion,
                    log_stats, file)
            if self.causation_type_matrix is not None:
                self._log_property_metrics(
                    'Causation types', self.causation_type_matrix, indent + 1,
                    log_confusion, log_stats, file)

        if log_stats:
            print_indented(indent, 'Arguments:', file=file)
            print_indented(indent + 1, self.arg_metrics, file=file)

        if log_stats or log_confusion:
            self._log_property_metrics('Argument labels', self.arg_label_matrix,
                                       indent + 1, log_confusion, log_stats, file)
            
        if log_differences:
            colorama.deinit()

    def __repr__(self):
        '''
        This is a dumb hack, but it's easier than trying to rewrite all of pp to
        operate on strings, and possibly faster too (since then we'd have to
        keep copying strings over to concatenate them).
        '''
        string_buffer = StringIO()
        self.pp(None, None, None, 0, string_buffer)
        return string_buffer.getvalue()

    @staticmethod
    def aggregate(metrics_list):
        '''
        Aggregates IAA statistics. Classification metrics are averaged;
        confusion matrices are summed.
        '''
        assert metrics_list, "Can't aggregate empty list of causality metrics!"
        # Copying gets us a CausalityMetrics object to work with, without having
        # to worry about what to pass to __init__.
        aggregated = copy(metrics_list[0])
        # For an aggregated, it won't make sense to list all the individual
        # sets of instances/properties processed in the individual computations.
        for attr_name in [
            'ids_considered', 'gold_only_instances',
            'predicted_only_instances', 'property_differences']:
            setattr(aggregated, attr_name, [])
        aggregated.save_differences = None

        aggregated.connective_metrics = ClassificationMetrics.average(
            [m.connective_metrics for m in metrics_list])
        degrees = [m.degree_matrix for m in metrics_list]
        if None not in degrees:
            aggregated.degree_matrix = reduce(operator.add, degrees)
        else:
            aggregated.degree_matrix = None
        causation_types = [m.causation_type_matrix for m in metrics_list]
        if None not in causation_types:
            aggregated.causation_type_matrix = reduce(operator.add,
                                                      causation_types)
        else:
            aggregated.causation_type_matrix = None
        aggregated.arg_metrics = ClassificationMetrics.average(
            [m.arg_metrics for m in metrics_list])
        aggregated.arg_label_matrix = reduce(
            operator.add, [m.arg_label_matrix for m in metrics_list])

        return aggregated

    def _log_property_metrics(self, name, matrix, indent, log_confusion,
                              log_stats, file):
        print_indented(indent, name, ':', sep='', file=file)
        if log_confusion:
            print_indented(indent + 1, matrix.pretty_format(metrics=log_stats),
                           file=file)
        else: # we must be logging just stats
            print_indented(indent + 1, matrix.pretty_format_metrics(),
                           file=file)


    @staticmethod
    def _log_unique_instance(instance, sentence_num, annotator_num, indent,
                             file):
        connective_text = ParsedSentence.get_annotation_text(
            instance.connective)
        filename = os.path.split(instance.source_sentence.source_file_path)[-1]
        print_indented(
            indent, "Annotation", annotator_num,
            'only: "%s"' % connective_text.encode('utf-8'),
            '(%s:%d: "%s")' % (filename, sentence_num, get_truncated_sentence(
                                instance).encode('utf-8')),
            file=file)
        
    @staticmethod
    def _print_with_labeled_args(instance, indent, file, cause_start, cause_end,
                                 effect_start, effect_end):
        '''
        Prints sentences annotated according to a particular CausationInstance.
        Connectives are printed in ALL CAPS. If run from a TTY, arguments are
        printed in color; otherwise, they're indicated as '/cause/' and
        '*effect*'. 
        '''
        def get_printable_word(token):
            word = token.get_unnormalized_original_text()
            if token in instance.connective:
                word = word.upper()

            if instance.cause and token in instance.cause:
                word = cause_start + word + cause_end
            elif instance.effect and token in instance.effect:
                word = effect_start + word + effect_end
            return word
            
        sentence = instance.source_sentence 
        tokens = sentence.tokens[1:] # skip ROOT
        if sys.stdout.isatty() or FLAGS.iaa_force_color:
            words = [token.get_unnormalized_original_text()
                     for token in tokens]
            # -10 allows viewing later in a slightly smaller terminal/editor.
            available_term_width = get_terminal_size()[0] - indent * 4 - 10
        else:
            words = [get_printable_word(token) for token in tokens]
            available_term_width = 75 - indent * 4 # 75 to allow for long words
        lines = wrap(' '.join(words), available_term_width,
                     subsequent_indent='    ', break_long_words=False)

        # For TTY, we now have to re-process the lines to add in color and
        # capitalizations.
        if sys.stdout.isatty() or FLAGS.iaa_force_color:
            tokens_processed = 0
            for i, line in enumerate(lines):
                # NOTE: This assumes no tokens with spaces in them.
                words = line.split()
                zipped = zip(words, tokens[tokens_processed:])
                printable_line = ' '.join([get_printable_word(token)
                                           for _, token in zipped])
                print_indented(indent, printable_line.encode('utf-8'))
                tokens_processed += len(words)
                if i == 0:
                    indent += 1 # future lines should be printed more indented
        else: # non-TTY: we're ready to print
            print_indented(indent, *[line.encode('utf-8') for line in lines],
                           sep='\n')

    def _log_arg_label_differences(self, indent, file):
        if sys.stdout.isatty() or FLAGS.iaa_force_color:
            cause_start = getattr(colorama.Fore, FLAGS.iaa_cause_color.upper())
            cause_end = colorama.Fore.RESET
            effect_start = getattr(colorama.Fore, FLAGS.iaa_effect_color.upper())
            effect_end = colorama.Fore.RESET
        else:
            cause_start = '/'
            cause_end = '/'
            effect_start = '*'
            effect_end = '*'
        
        for instance_1, instance_2, sentence_num in self.argument_differences:
            filename = os.path.split(
                instance_1.source_sentence.source_file_path)[-1]
            connective_text = ParsedSentence.get_annotation_text(
                    instance_1.connective).encode('utf-8)')
            print_indented(
                indent,
                'Arguments differ for connective "', connective_text,
                '" (', filename, ':', sentence_num, ')',
                ' with ', cause_start, 'cause', cause_end, ' and ',
                effect_start, 'effect', effect_end, ':',
                sep='', file=file)
            self._print_with_labeled_args(
                instance_1, indent + 1, file, cause_start, cause_end,
                effect_start, effect_end)
            # print_indented(indent + 1, "vs.", file=file)
            self._print_with_labeled_args(
                instance_2, indent + 1, file, cause_start, cause_end,
                effect_start, effect_end)

    def _log_property_differences(self, property_enum, indent, file):
        filtered_differences = [x for x in self.property_differences
                                if x[2] is property_enum]

        if property_enum is CausationInstance.Degrees:
            property_name = 'Degree'
            value_extractor = lambda instance: instance.Degrees[instance.degree]
        elif property_enum is CausationInstance.CausationTypes:
            property_name = 'Causation type'
            value_extractor = lambda instance: (
                instance.CausationTypes[instance.type])

        for instance_1, instance_2, _, sentence_num in filtered_differences:
            if value_extractor:
                values = (value_extractor(instance_1),
                          value_extractor(instance_2))
            filename = os.path.split(
                instance_1.source_sentence.source_file_path)[-1]
            print_indented(
                indent, property_name, 's for connective "',
                ParsedSentence.get_annotation_text(
                    instance_1.connective).encode('utf-8)'),
                '" differ: ', values[0], ' vs. ', values[1],
                ' (', filename, ':', sentence_num, ': "',
                get_truncated_sentence(instance_1), '")',
                sep='', file=file)
