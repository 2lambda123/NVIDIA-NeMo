# Copyright (c) 2021, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import time
import copy
import csv
import json
import os
import math
import copy
from collections import OrderedDict as od
from datetime import datetime
from typing import Dict, List, Tuple
import gc
import numpy as np
import librosa
import soundfile as sf
from pyannote.metrics.diarization import DiarizationErrorRate
from nemo.collections.asr.metrics.wer import word_error_rate
from nemo.collections.asr.models import ClusteringDiarizer
from nemo.collections.asr.parts.utils.speaker_utils import (
    audio_rttm_map,
    get_uniqname_from_filepath,
    labels_to_rttmfile,
    rttm_to_labels,
    write_rttm2manifest,
)
from nemo.utils import logging

from nemo.collections.asr.parts.utils.decoder_timestamps_utils import FrameBatchASR_Logits, WERBPE_TS, ASR_TIMESTAMPS, WER_TS
from nemo.collections.asr.parts.utils.speaker_utils import get_contiguous_stamps, merge_stamps, labels_to_pyannote_object, rttm_to_labels, labels_to_rttmfile, get_uniqname_from_filepath, get_embs_and_timestamps, get_subsegments, isOverlap, getOverlapRange, getMergedRanges, getSubRangeList, fl2int, int2fl, combine_int_overlaps
import torch
from nemo.collections.asr.parts.utils.vad_utils import (
    generate_overlap_vad_seq,
    generate_vad_segment_table,
    get_vad_stream_status,
    prepare_manifest,
)
from nemo.collections.asr.models import OnlineDiarizer
from nemo.collections import nlp as nemo_nlp
import nemo.collections.asr as nemo_asr
from typing import Dict, List, Tuple, Type, Union
from nemo.collections.asr.models import ClusteringDiarizer
from nemo.collections.asr.models.ctc_bpe_models import EncDecCTCModelBPE
from nemo.collections.asr.models.ctc_models import EncDecCTCModel
from sklearn.preprocessing import OneHotEncoder
from nemo.collections.asr.parts.utils.streaming_utils import AudioFeatureIterator, FrameBatchASR
# , FrameBatchVAD
from omegaconf import OmegaConf

try:
    import arpa

    ARPA = True
except ImportError:
    ARPA = False

try:
    import diff_match_patch

    DIFF_MATCH_PATCH = True
except ImportError:
    DIFF_MATCH_PATCH = False

__all__ = ['ASR_DIAR_OFFLINE', 'ASR_DIAR_ONLINE']


def dump_json_to_file(file_path, riva_dict):
    """
    Write a json file from the riva_dict dictionary.
    """
    with open(file_path, "w") as outfile:
        json.dump(riva_dict, outfile, indent=4)


def write_txt(w_path, val):
    """
    Write a text file from the string input.
    """
    with open(w_path, "w") as output:
        output.write(val + '\n')
    return None


def get_diff_text(text1: List[str], text2: List[str]) -> List[Tuple[int, str]]:
    """
    Take the alignment between two lists and get the difference.
    """
    orig_words = '\n'.join(text1.split()) + '\n'
    pred_words = '\n'.join(text2.split()) + '\n'

    diff = diff_match_patch.diff_match_patch()
    diff.Diff_Timeout = 0
    orig_enc, pred_enc, enc = diff.diff_linesToChars(orig_words, pred_words)
    diffs = diff.diff_main(orig_enc, pred_enc, False)
    diff.diff_charsToLines(diffs, enc)
    return diffs

def get_speaker_error_mismatch(ctm_error_dict, error_buffer, w_range_buffer, pred_rttm_eval):
    """
    Calculate the diarization confusion error using the reference CTM file.
    """
    correct_count, error_count, align_error = 0, 0, []
    for k, _d in enumerate(error_buffer):
        if _d[0] == 1:
            stt, end = w_range_buffer[k]
            bool_list = [_bool for _bool in pred_rttm_eval[stt:end]]
            error_count = len(bool_list) - sum(bool_list)

    ctm_error_dict['diar_confuse_count'] += error_count

def get_speaker_error_match(ctm_error_dict, w_range, ctm_info_list, pred_info_list, mapping_dict):
    """
    Count the words with wrong speaker assignments.
    """
    error_count, align_error_list = 0, []

    for ref, prd in zip(range(w_range[0][0], w_range[0][1]), range(w_range[1][0], w_range[1][1])):
        ref_spk, ref_start, ref_end = ctm_info_list[ref]
        pred_spk, pred_start, pred_end = pred_info_list[prd]
        if pred_spk in mapping_dict:
            error_count += 1 if ref_spk != mapping_dict[pred_spk] else 0
        else:
            error_count += 1
        align_error_list.append(ref_start - pred_start)
    ctm_error_dict['diar_confuse_count'] += error_count
    return error_count, align_error_list


class ASR_DIAR_OFFLINE(object):
    """
    A class designed for performing ASR and diarization together.
    """
    def __init__(self, **cfg_diarizer):
        self.offline_mode = True
        self.manifest_filepath = cfg_diarizer['manifest_filepath']
        self.params = cfg_diarizer['asr']['parameters']
        self.ctc_decoder_params = cfg_diarizer['asr']['ctc_decoder_parameters']
        self.realigning_lm_params = cfg_diarizer['asr']['realigning_lm_parameters']
        self.nonspeech_threshold = self.params['asr_based_vad_threshold']
        self.fix_word_ts_with_VAD = self.params['fix_word_ts_with_VAD']
        self.root_path = cfg_diarizer['out_dir']
        self.cfg_diarizer = cfg_diarizer
        self.load_punctuation_model()

        self.vad_threshold_for_word_ts = 0.7
        self.max_word_ts_length_in_sec = 0.6
        self.word_ts_anchor_offset = 0.0
        self.run_ASR = None
        self.realigning_lm = None
        self.capitalize_first_word = False
        self.ctm_exists = {}
        self.frame_VAD = {}
        self.align_error_list = []
        self.AUDIO_RTTM_MAP = audio_rttm_map(self.manifest_filepath)
        self.audio_file_list = [value['audio_filepath'] for _, value in self.AUDIO_RTTM_MAP.items()]
        self.color_palette = {
                              'speaker_0': '\033[1;30m',
                              'speaker_1': '\033[1;34m',
                              'speaker_2': '\033[1;32m',
                              'speaker_3': '\033[1;35m',
                              'speaker_4': '\033[1;31m',
                              'speaker_5': '\033[1;36m',
                              'speaker_6': '\033[1;37m',
                              'speaker_7': '\033[1;30m',
                              'speaker_8': '\033[1;33m',
                              'speaker_9': '\033[0;34m',
                              'white': '\033[0;37m'}

    def load_realigning_LM(self):
        self.N_range = (
            self.realigning_lm_params['min_number_of_words'],
            self.realigning_lm_params['max_number_of_words'],
        )
        self.stt_end_tokens = ['</s>', '<s>']
        logging.info(f"Loading LM for realigning: {self.realigning_lm_params['arpa_language_model']}")
        return arpa.loadf(self.realigning_lm_params['arpa_language_model'])[0]
    
    def load_punctuation_model(self):
        """
        Load punctuation model in .nemo format or model name on NGC.
        """
        self.punctuation_model_path = self.cfg_diarizer['asr']['parameters']['punctuation_model_path']
        if self.punctuation_model_path is not None:
            if '.nemo' in self.punctuation_model_path.lower():
                self.punctuation_model = nemo_nlp.models.PunctuationCapitalizationModel.restore_from(self.punctuation_model_path)
            else:
                self.punctuation_model = nemo_nlp.models.PunctuationCapitalizationModel.from_pretrained(self.punctuation_model_path)
        else:
            self.punctuation_model = None

    def save_VAD_labels_list(self, word_ts_dict):
        """
        Take the non_speech labels from logit output. The logit output is obtained from
        run_ASR() function.

        Args:
            word_ts_dict (dict):
                List containing word timestamps.
        """
        self.VAD_RTTM_MAP = {}
        for idx, (uniq_id, word_timestamps) in enumerate(word_ts_dict.items()):
            speech_labels_float = self.get_speech_labels_from_decoded_prediction(word_timestamps)
            speech_labels = self.get_str_speech_labels(speech_labels_float)
            output_path = os.path.join(self.root_path, 'pred_rttms')
            if not os.path.exists(output_path):
                os.makedirs(output_path)
            filename = labels_to_rttmfile(speech_labels, uniq_id, output_path)
            self.VAD_RTTM_MAP[uniq_id] = {'audio_filepath': self.audio_file_list[idx], 'rttm_filepath': filename}

    def get_speech_labels_from_decoded_prediction(self, input_word_ts):
        """
        Extract speech labels from the ASR output (decoded predictions)

        Args:
            input_word_ts (list):
                List containing word timestamps.

        Returns:
            word_ts (list):
                The ranges of the speech segments, which are merged ranges of input_word_ts.
        """
        speech_labels = []
        word_ts = copy.deepcopy(input_word_ts)
        if word_ts == []:
            return speech_labels
        else:
            count = len(word_ts) - 1
            while count > 0:
                if len(word_ts) > 1:
                    if word_ts[count][0] - word_ts[count - 1][1] <= self.nonspeech_threshold:
                        trangeB = word_ts.pop(count)
                        trangeA = word_ts.pop(count - 1)
                        word_ts.insert(count - 1, [trangeA[0], trangeB[1]])
                count -= 1
        return word_ts

    def run_diarization(
        self, diar_model_config, word_timestamps,
    ):
        """
        Launch the diarization process using the given VAD timestamp (oracle_manifest).

        Args:
            word_and_timestamps (list):
                List containing words and word timestamps

        Returns:
            diar_hyp (dict):
                A dictionary containing rttm results which are indexed by a unique ID.
            score Tuple[pyannote object, dict]:
                A tuple containing pyannote metric instance and mapping dictionary between
                speakers in hypotheses and speakers in reference RTTM files.
        """

        if diar_model_config.diarizer.asr.parameters.asr_based_vad:
            self.save_VAD_labels_list(word_timestamps)
            oracle_manifest = os.path.join(self.root_path, 'asr_vad_manifest.json')
            oracle_manifest = write_rttm2manifest(self.VAD_RTTM_MAP, oracle_manifest)
            diar_model_config.diarizer.vad.model_path = None
            diar_model_config.diarizer.vad.external_vad_manifest = oracle_manifest

        diar_model = ClusteringDiarizer(cfg=diar_model_config)
        score = diar_model.diarize()
        if diar_model_config.diarizer.vad.model_path is not None and not diar_model_config.diarizer.oracle_vad:
            self.get_frame_level_VAD(vad_processing_dir=diar_model.vad_pred_dir)

        diar_hyp = {}
        for k, audio_file_path in enumerate(self.audio_file_list):
            uniq_id = get_uniqname_from_filepath(audio_file_path)
            pred_rttm = os.path.join(self.root_path, 'pred_rttms', uniq_id + '.rttm')
            diar_hyp[uniq_id] = rttm_to_labels(pred_rttm)
        return diar_hyp, score

    def get_frame_level_VAD(self, vad_processing_dir):
        """
        Read frame-level VAD outputs.

        Args:
            vad_processing_dir (str):
                The path where VAD results are saved.
        """
        for uniq_id in self.AUDIO_RTTM_MAP:
            frame_vad = os.path.join(vad_processing_dir, uniq_id + '.median')
            frame_vad_float_list = []
            with open(frame_vad, 'r') as fp:
                for line in fp.readlines():
                    frame_vad_float_list.append(float(line.strip()))
            self.frame_VAD[uniq_id] = frame_vad_float_list

    def gather_eval_results(self, metric, mapping_dict, total_riva_dict, pred_labels=None):
        """
        Gather diarization evaluation results from pyannote DiarizationErrorRate metric object.

        Args:
            metric (DiarizationErrorRate metric): DiarizationErrorRate metric pyannote object
            mapping_dict (dict): A dictionary containing speaker mapping labels for each audio file with key as unique name

        Returns:
            DER_result_dict (dict): A dictionary containing scores for each audio file along with aggregated results
        """
        results = metric.results_
        DER_result_dict = {}
        count_correct_spk_counting = 0
        for result in results:
            key, score = result
            if pred_labels is None:
                pred_rttm = os.path.join(self.root_path, 'pred_rttms', key + '.rttm')
                pred_labels = rttm_to_labels(pred_rttm)

            est_n_spk = self.get_num_of_spk_from_labels(pred_labels)
            ref_rttm = self.AUDIO_RTTM_MAP[key]['rttm_filepath']
            ref_labels = rttm_to_labels(ref_rttm)
            ref_n_spk = self.get_num_of_spk_from_labels(ref_labels)

            if self.cfg_diarizer['oracle_vad']:
                score['missed detection'] = 0
                score['false alarm'] = 0

            _DER, _CER, _FA, _MISS = (
                (score['confusion'] + score['false alarm'] + score['missed detection']) / score['total'],
                score['confusion'] / score['total'],
                score['false alarm'] / score['total'],
                score['missed detection'] / score['total'],
            )

            DER_result_dict[key] = {
                "DER": round(_DER, 4),
                "CER": round(_CER, 4),
                "FA": round(_FA, 4),
                "MISS": round(_MISS, 4),
                "est_n_spk": est_n_spk,
                "mapping": mapping_dict[key],
                "is_spk_count_correct": (est_n_spk == ref_n_spk),
            }
            count_correct_spk_counting += int(est_n_spk == ref_n_spk)
        
        if metric['total'] > 0.01: 
            DER, CER, FA, MISS = (
                abs(metric),
                metric['confusion'] / metric['total'],
                metric['false alarm'] / metric['total'],
                metric['missed detection'] / metric['total'],
            )
            speaker_counting_acc = count_correct_spk_counting / len(metric.results_)
        else:
            DER, CER, FA, MISS = 100, 100, 100, 100
            speaker_counting_acc = 0.0

        DER_result_dict["total"] = {
            "DER": DER,
            "CER": CER,
            "FA": FA,
            "MISS": MISS,
            "spk_counting_acc": speaker_counting_acc,
        }

        return DER_result_dict

    def get_the_closest_silence_start(self, vad_index_word_end, vad_frames, params, offset=10):
        """
        Find the closest silence frame from the given starting position.

        Args:
            vad_index_word_end (float):
                The timestamp of the end of the current word.
            vad_frames (numpy.array):
                The numpy array containing  frame-level VAD probability.
            params (dict):
                Contains the parameters for diarization and ASR decoding.

        Returns:
            c (float):
                A timestamp of the earliest start of a silence region from
                the given time point, vad_index_word_end.
        """

        c = vad_index_word_end + offset
        limit = int(100 * self.max_word_ts_length_in_sec + vad_index_word_end)
        while c < len(vad_frames):
            if vad_frames[c] < self.vad_threshold_for_word_ts:
                break
            else:
                c += 1
                if c > limit:
                    break
        c = min(len(vad_frames) - 1, c)
        c = round(c / 100.0, 2)
        return c

    def compensate_word_ts_list(self, audio_file_list, word_ts_dict, params):
        """
        Compensate the word timestamps based on the VAD output.
        The length of each word is capped by self.max_word_ts_length_in_sec.

        Args:
            audio_file_list (list):
                List containing audio file paths.
            word_ts_dict (dict):
                Dictionary containing timestamps of words.
            params (dict):
                The parameter dictionary for diarization and ASR decoding.

        Returns:
            enhanced_word_ts_dict (list):
                List of the enhanced word timestamp values.
        """
        enhanced_word_ts_dict = {}
        for idx, (uniq_id, word_ts_seq_list) in enumerate(word_ts_dict.items()):
            N = len(word_ts_seq_list)
            enhanced_word_ts_buffer = []
            for k, word_ts in enumerate(word_ts_seq_list):
                if k < N - 1:
                    word_len = round(word_ts[1] - word_ts[0], 2)
                    len_to_next_word = round(word_ts_seq_list[k + 1][0] - word_ts[0] - 0.01, 2)
                    if uniq_id in self.frame_VAD:
                        vad_index_word_end = int(100 * word_ts[1])
                        closest_sil_stt = self.get_the_closest_silence_start(
                            vad_index_word_end, self.frame_VAD[uniq_id], params
                        )
                        vad_est_len = round(closest_sil_stt - word_ts[0], 2)
                    else:
                        vad_est_len = len_to_next_word
                    min_candidate = min(vad_est_len, len_to_next_word)
                    fixed_word_len = max(min(self.max_word_ts_length_in_sec, min_candidate), word_len)
                    enhanced_word_ts_buffer.append([word_ts[0], word_ts[0] + fixed_word_len])
                else:
                    enhanced_word_ts_buffer.append([word_ts[0], word_ts[1]])

            enhanced_word_ts_dict[uniq_id] = enhanced_word_ts_buffer
        return enhanced_word_ts_dict
    
    def get_transcript_with_speaker_labels(self, diar_hyp, word_hyp, word_ts_hyp, write_files=True):
        """
        Match the diarization result with the ASR output.
        The words and the timestamps for the corresponding words are matched
        in a for loop.

        Args:
            diar_labels (dict):
                Dictionary of the Diarization output labels in str.
            word_hyp (dict):
                Dictionary of words from ASR inference.
            word_ts_hyp (dict):
                Dictionary containing the start time and the end time of each word.

        Returns:
            total_riva_dict (dict):
                A dictionary containing word timestamps, speaker labels and words.

        """
        total_riva_dict = {}
        if self.fix_word_ts_with_VAD:
            if self.frame_VAD == {}:
                logging.info(
                    f"VAD timestamps are not provided. Fixing word timestamps without VAD. Please check the hydra configurations."
                )
            word_ts_refined = self.compensate_word_ts_list(self.audio_file_list, word_ts_hyp, self.params)
        else:
            word_ts_refined = word_ts_hyp

        if self.realigning_lm_params['arpa_language_model']:
            if not ARPA:
                raise ImportError(
                    'LM for realigning is provided but arpa is not installed. Install arpa using PyPI: pip install arpa'
                )
            else:
                self.realigning_lm = self.load_realigning_LM()

        for k, audio_file_path in enumerate(self.audio_file_list):
            uniq_id = get_uniqname_from_filepath(audio_file_path)
            word_dict_seq_list = self.get_word_dict_seq_list(diar_hyp=diar_hyp[uniq_id], 
                                                             word_hyp=word_hyp[uniq_id], 
                                                             word_ts_hyp=word_ts_hyp[uniq_id], 
                                                             word_ts_refined=word_ts_refined[uniq_id])
            if self.realigning_lm:
                word_dict_seq_list = self.realign_words_with_lm(word_dict_seq_list)
            self.make_json_output(uniq_id, diar_hyp[uniq_id], word_dict_seq_list, total_riva_dict, write_files)
        logging.info(f"Diarization with ASR output files are saved in: {self.root_path}/pred_rttms")
        return total_riva_dict

    def get_word_dict_seq_list(self, diar_hyp, word_hyp, word_ts_hyp, word_ts_refined):
        """
        Save the hypothesis words and speaker labels to a dictionary variable for future use.
        """
        words, labels = word_hyp, diar_hyp
        start_point, end_point, speaker = labels[0].split()
        word_pos, idx = 0, 0
        word_dict_seq_list = []
        for j, word_ts_stt_end in enumerate(word_ts_hyp):
            word_pos = self.get_word_timestamp_anchor(word_ts_stt_end)
            if word_pos > float(end_point):
                idx += 1
                idx = min(idx, len(labels) - 1)
                start_point, end_point, speaker = labels[idx].split()
            refined_word_ts_stt_end = word_ts_refined[j]
            stt_sec, end_sec = round(refined_word_ts_stt_end[0], 2), round(refined_word_ts_stt_end[1], 2)
            word_dict_seq_list.append(
                {'word': words[j], 'start_time': stt_sec, 'end_time': end_sec, 'speaker_label': speaker}
            )
        return word_dict_seq_list

    def make_json_output(self, uniq_id, diar_hyp, word_dict_seq_list, total_riva_dict, write_files):
        """
        Generate json output files and transcripts from the ASR and diarization results.

        Args:
            uniq_id (str):
                A unique ID (key) that identifies each input audio file.
            diar_hyp (list):
                Dictionary containing the word sequence from ASR output.
            word_dict_seq_list (list):
                List containing words and corresponding word timestamps in dictionary format.
            total_riva_dict (dict):
                Dictionary containing the final transcription, alignment and speaker labels.

        Returns:
            total_riva_dict (dict):
                A dictionary containing overall results of diarization and ASR inference.
        """
        word_seq_list, audacity_label_words = [], []
        labels = diar_hyp
        n_spk = self.get_num_of_spk_from_labels(labels)
        riva_dict = od(
            {
                'status': 'Success',
                'session_id': uniq_id,
                'transcription': '',
                'speaker_count': n_spk,
                'words': [],
                'sentences': [],
            }
        )
        gecko_dict = od({'schemaVersion': 2.0, 'monologues': []})
        start_point, end_point, speaker = labels[0].split()
        prev_speaker = speaker
        terms_list = []

        sentences = []
        sentence = {'speaker': speaker, 'start_point': float(start_point), 'end_point': float(end_point), 'text': ''}

        logging.info(f"Creating results for Session: {uniq_id} n_spk: {n_spk} ")
        for k, line_dict in enumerate(word_dict_seq_list):
            word, speaker = line_dict['word'], line_dict['speaker_label']
            word_seq_list.append(word)
            start_point, end_point = line_dict['start_time'], line_dict['end_time']
            if speaker != prev_speaker:
                if len(terms_list) != 0:
                    gecko_dict['monologues'].append(
                        {'speaker': {'name': None, 'id': prev_speaker}, 'terms': terms_list}
                    )
                    terms_list = []

                # remove trailing space in text
                sentence['text'] = sentence['text'].strip()

                if self.punctuation_model and self.offline_mode:
                    sentence['text'] = ' '.join(self.punctuate_words(sentence['text'].split()))
                
                # store the current sentence
                if len(sentence['text']) > 0:
                    sentences.append(sentence)

                # start construction of a new sentence
                sentence = {'speaker': speaker, 'start_point': start_point, 'end_point': end_point, 'text': ''}
            else:
                # correct the ending time
                sentence['end_point'] = end_point

            stt_sec, end_sec = start_point, end_point
            terms_list.append({'start': stt_sec, 'end': end_sec, 'text': word, 'type': 'WORD'})

            # add current word to sentence
            if self.capitalize_first_word and sentence['text'] == '':
                word = word.capitalize()

            sentence['text'] += word.strip() + ' '

            self.add_json_to_dict(riva_dict, word, stt_sec, end_sec, speaker)
            audacity_label_words.append(self.get_audacity_label(word, stt_sec, end_sec, speaker))
            total_riva_dict[uniq_id] = riva_dict
            prev_speaker = speaker

        # note that we need to add the very last sentence.
        sentence['text'] = sentence['text'].strip()
        sentences.append(sentence)
        gecko_dict['monologues'].append({'speaker': {'name': None, 'id': speaker}, 'terms': terms_list})

        riva_dict['transcription'] = ' '.join(word_seq_list)
        if write_files:
            self.write_and_log(uniq_id, riva_dict, audacity_label_words, gecko_dict, sentences)
        return sentences

    def get_realignment_ranges(self, k, word_seq_len):
        """
        Calculate word ranges for realignment operation.
        N1, N2 are calculated to not exceed the start and end of the input word sequence.
        """
        if k < self.N_range[1]:
            N1 = max(k, self.N_range[0])
            N2 = min(word_seq_len - k, self.N_range[1])
        elif k > (word_seq_len - self.N_range[1]):
            N1 = min(k, self.N_range[1])
            N2 = max(word_seq_len - k, self.N_range[0])
        else:
            N1, N2 = self.N_range[1], self.N_range[1]
        return N1, N2

    def get_word_timestamp_anchor(self, word_ts_stt_end: List[float]) -> float:
        """
        Determine a reference point to match a word with the diarization results.
        word_ts_anchor_pos determines the position of a word in relation to the given diarization labels:
            - 'start' uses the beginning of the word
            - 'end' uses the end of the word
            - 'mid' uses the mean of start and end of the word

        word_ts_anchor_offset determines how much offset we want to add to the anchor position.
        It is recommended to use the default value.
        """
        if self.params['word_ts_anchor_pos'] == 'start':
            word_pos = word_ts_stt_end[0]
        elif self.params['word_ts_anchor_pos'] == 'end':
            word_pos = word_ts_stt_end[1]
        elif self.params['word_ts_anchor_pos'] == 'mid':
            word_pos = (word_ts_stt_end[0] + word_ts_stt_end[1]) / 2
        else:
            logging.info(
                f"word_ts_anchor_pos: {self.params['word_ts_anchor']} is not a supported option. Using the default 'start' option."
            )
            word_pos = word_ts_stt_end[0]
        word_pos = word_pos + self.word_ts_anchor_offset
        return word_pos
    

    def realign_words_with_lm(self, word_dict_seq_list: List[Dict[str, float]]):
        """
        Realign the mapping between speaker labels and words using a language model.
        The realigning process calculates the probability of the certain range around the words,
        especially at the boundary between two hypothetical sentences spoken by different speakers.

        <Example> k-th word: "but"
            hyp_former:
                since i think like tuesday </s> <s>  but he's coming back to albuquerque
            hyp_latter:
                since i think like tuesday but </s> <s>  he's coming back to albuquerque

        The joint probabilities of words in the sentence are computed for these two hypotheses. In addition,
        logprob_diff_threshold parameter is used for reducing the false positive realigning.
        """
        word_seq_len = len(word_dict_seq_list)
        hyp_w_dict_list, spk_list = [], []
        for k, line_dict in enumerate(word_dict_seq_list):
            word, spk_label = line_dict['word'], line_dict['speaker_label']
            hyp_w_dict_list.append(word)
            spk_list.append(spk_label)

        realigned_list = []
        org_spk_list = copy.deepcopy(spk_list)
        for k, line_dict in enumerate(word_dict_seq_list):
            if self.N_range[0] < k < (word_seq_len - self.N_range[0]) and (
                spk_list[k] != org_spk_list[k + 1] or spk_list[k] != org_spk_list[k - 1]
            ):
                N1, N2 = self.get_realignment_ranges(k, word_seq_len)
                hyp_former = self.realigning_lm.log_s(
                    ' '.join(hyp_w_dict_list[k - N1 : k] + self.stt_end_tokens + hyp_w_dict_list[k : k + N2])
                )
                hyp_latter = self.realigning_lm.log_s(
                    ' '.join(hyp_w_dict_list[k - N1 : k + 1] + self.stt_end_tokens + hyp_w_dict_list[k + 1 : k + N2])
                )
                log_p = [hyp_former, hyp_latter]
                p_order = np.argsort(log_p)[::-1]
                if log_p[p_order[0]] > log_p[p_order[1]] + self.realigning_lm_params['logprob_diff_threshold']:
                    if p_order[0] == 0:
                        spk_list[k] = org_spk_list[k + 1]
                line_dict['speaker_label'] = spk_list[k]
            realigned_list.append(line_dict)
        return realigned_list

    def get_alignment_errors(self, ctm_content, hyp_w_dict_list, mapping_dict):
        """
        Compute various types of errors using the provided CTM file and RTTM file.

        The variables computed for CTM file based evaluation:
            error_count : Number of words that have wrong speaker labels
            align_error : (reference word timestamp - hypothesis word timestamp)

        The error metrics in ctm_error_dict variable:
            ref_word_count: The number of words in the reference transcript
            hyp_word_count: The number of words in the hypothesis
            diar_confuse_count: Number of incorrectly diarized words
            all_correct_count: Count the word if both hypothesis word and speaker label are correct.
            hyp_based_wder: The number of incorrectly diarized words divided by the number of words in the hypothesis
            ref_based_wder: The number of incorrectly diarized words divided by the number of words in the reference transcript
        """

        ctm_ref_word_seq, ctm_info_list = [], []
        pred_word_seq, pred_info_list, pred_rttm_eval = [], [], []

        for ctm_line in ctm_content:
            spl = ctm_line.split()
            ctm_ref_word_seq.append(spl[4])
            ctm_info_list.append([spl[1], float(spl[2]), float(spl[3])])

        for w_dict in hyp_w_dict_list:
            pred_rttm_eval.append(w_dict['diar_correct'])
            pred_word_seq.append(w_dict['word'])
            pred_info_list.append([w_dict['speaker_label'], w_dict['start_time'], w_dict['end_time']])

        ctm_text = ' '.join(ctm_ref_word_seq)
        pred_text = ' '.join(pred_word_seq)
        diff = get_diff_text(ctm_text, pred_text)

        ref_word_count, hyp_word_count, all_correct_count, wder_count = 0, 0, 0, 0
        ctm_error_dict = {
            'ref_word_count': 0,
            'hyp_word_count': 0,
            'diar_confuse_count': 0,
            'all_correct_count': 0,
            'hyp_based_wder': 0,
            'ref_based_wder': 0,
        }

        error_buffer, w_range_buffer, cumul_align_error = [], [], []
        for k, d in enumerate(diff):
            word_seq = d[1].strip().split('\n')
            if d[0] == 0:
                if error_buffer != []:
                    get_speaker_error_mismatch(ctm_error_dict, error_buffer, w_range_buffer, pred_rttm_eval)
                    error_buffer, w_range_buffer = [], []
                w_range = [
                    (ctm_error_dict['ref_word_count'], ctm_error_dict['ref_word_count'] + len(word_seq)),
                    (ctm_error_dict['hyp_word_count'], ctm_error_dict['hyp_word_count'] + len(word_seq)),
                ]
                error_count, align_error = get_speaker_error_match(
                    ctm_error_dict, w_range, ctm_info_list, pred_info_list, mapping_dict
                )
                ctm_error_dict['all_correct_count'] += len(word_seq) - error_count
                ctm_error_dict['ref_word_count'] += len(word_seq)
                ctm_error_dict['hyp_word_count'] += len(word_seq)
                cumul_align_error += align_error
            elif d[0] == -1:
                error_buffer.append(d)
                w_range_buffer.append((ref_word_count, ref_word_count + len(word_seq)))
                ctm_error_dict['ref_word_count'] += len(word_seq)
            elif d[0] == 1:
                error_buffer.append(d)
                w_range_buffer.append((hyp_word_count, hyp_word_count + len(word_seq)))
                ctm_error_dict['hyp_word_count'] += len(word_seq)

        if error_buffer != []:
            get_speaker_error_mismatch(ctm_error_dict, error_buffer, w_range_buffer, pred_rttm_eval)

        ctm_error_dict['hyp_based_wder'] = round(
            ctm_error_dict['diar_confuse_count'] / ctm_error_dict['hyp_word_count'], 4
        )
        ctm_error_dict['ref_based_wder'] = round(
            ctm_error_dict['diar_confuse_count'] / ctm_error_dict['ref_word_count'], 4
        )
        ctm_error_dict['diar_trans_acc'] = round(
            ctm_error_dict['all_correct_count'] / ctm_error_dict['ref_word_count'], 4
        )
        return cumul_align_error, ctm_error_dict

    def get_WDER(self, total_riva_dict, DER_result_dict):
        """
        Calculate word-level diarization error rate (WDER). WDER is calculated by
        counting the wrongly diarized words and divided by the total number of words
        recognized by the ASR model.

        Args:
            total_riva_dict (dict):
                Dictionary that stores riva_dict(dict) which is indexed by uniq_id variable.
            DER_result_dict (dict):
                Dictionary that stores DER, FA, Miss, CER, mapping, the estimated
                number of speakers and speaker counting accuracy.

        Returns:
            wder_dict (dict):
                A dictionary containing  WDER value for each session and total WDER.
        """
        wder_dict, count_dict = {'session_level': {}}, {}
        asr_eval_dict = {'hypotheses_list': [], 'references_list': []}
        align_error_list = []

        count_dict['total_ctm_wder_count'], count_dict['total_asr_and_spk_correct_words'] = 0, 0
        (
            count_dict['grand_total_ctm_word_count'],
            count_dict['grand_total_pred_word_count'],
            count_dict['grand_total_correct_word_count'],
        ) = (0, 0, 0)

        if any([self.AUDIO_RTTM_MAP[uniq_id]['ctm_filepath'] != None for uniq_id in self.AUDIO_RTTM_MAP.keys()]):
            if not DIFF_MATCH_PATCH:
                raise ImportError(
                    'CTM file is provided but diff_match_patch is not installed. Install diff_match_patch using PyPI: pip install diff_match_patch'
                )

        for k, audio_file_path in enumerate(self.audio_file_list):

            uniq_id = get_uniqname_from_filepath(audio_file_path)
            error_dict = {'uniq_id': uniq_id}
            ref_rttm = self.AUDIO_RTTM_MAP[uniq_id]['rttm_filepath']
            ref_labels = rttm_to_labels(ref_rttm)
            mapping_dict = DER_result_dict[uniq_id]['mapping']
            hyp_w_dict_list = total_riva_dict[uniq_id]['words']
            hyp_w_dict_list, word_seq_list, correct_word_count, rttm_wder = self.calculate_WDER_from_RTTM(
                hyp_w_dict_list, ref_labels, mapping_dict
            )
            error_dict['rttm_based_wder'] = rttm_wder
            error_dict.update(DER_result_dict[uniq_id])

            # If CTM files are provided, evaluate word-level diarization and WER with the CTM files.
            if self.AUDIO_RTTM_MAP[uniq_id]['ctm_filepath']:
                self.ctm_exists[uniq_id] = True
                ctm_content = open(self.AUDIO_RTTM_MAP[uniq_id]['ctm_filepath']).readlines()
                self.get_ctm_based_eval(ctm_content, error_dict, count_dict, hyp_w_dict_list, mapping_dict)
            else:
                self.ctm_exists[uniq_id] = False

            wder_dict['session_level'][uniq_id] = error_dict
            asr_eval_dict['hypotheses_list'].append(' '.join(word_seq_list))
            asr_eval_dict['references_list'].append(self.AUDIO_RTTM_MAP[uniq_id]['text'])

            count_dict['grand_total_pred_word_count'] += len(hyp_w_dict_list)
            count_dict['grand_total_correct_word_count'] += correct_word_count

        wder_dict = self.get_wder_dict_values(asr_eval_dict, wder_dict, count_dict, align_error_list)
        return wder_dict

    def calculate_WDER_from_RTTM(self, hyp_w_dict_list, ref_labels, mapping_dict):
        """
        Calculate word-level diarization error rate (WDER) using the provided RTTM files.
        If lenient_overlap_WDER is True, the words are considered to be correctly diarized
        if the words fall into overlapped regions that include the correct speaker labels.
        Note that WDER values computed from RTTM may not be accurate if the word timestamps
        have limited accuracy. It is recommended to use CTM files to compute an accurate
        evaluation result.
        """
        correct_word_count = 0
        ref_label_list = [[float(x.split()[0]), float(x.split()[1])] for x in ref_labels]
        ref_label_array = np.array(ref_label_list)
        word_seq_list = []
        for w_idx in range(len(hyp_w_dict_list)):
            wdict = hyp_w_dict_list[w_idx]
            wdict['diar_correct'] = False
            speaker_label = wdict['speaker_label']
            if speaker_label in mapping_dict:
                est_spk_label = mapping_dict[speaker_label]
            else:
                continue
            word_range = np.array(
                [wdict['start_time'] + self.word_ts_anchor_offset, wdict['end_time'] + self.word_ts_anchor_offset]
            )
            word_seq_list.append(wdict['word'])
            word_range_tile = np.tile(word_range, (ref_label_array.shape[0], 1))
            ovl_bool = self.isOverlapArray(ref_label_array, word_range_tile)
            if np.any(ovl_bool) == False:
                continue
            ovl_length = self.getOverlapRangeArray(ref_label_array, word_range_tile)
            if self.params['lenient_overlap_WDER']:
                ovl_length_list = list(ovl_length[ovl_bool])
                max_ovl_sub_idx = np.where(ovl_length_list == np.max(ovl_length_list))[0]
                max_ovl_idx = np.where(ovl_bool == True)[0][max_ovl_sub_idx]
                ref_spk_labels = [x.split()[-1] for x in list(np.array(ref_labels)[max_ovl_idx])]
                if est_spk_label in ref_spk_labels:
                    correct_word_count += 1
                    wdict['diar_correct'] = True
            else:
                max_ovl_sub_idx = np.argmax(ovl_length[ovl_bool])
                max_ovl_idx = np.where(ovl_bool == True)[0][max_ovl_sub_idx]
                _, _, ref_spk_label = ref_labels[max_ovl_idx].split()
                if est_spk_label == ref_spk_labels:
                    correct_word_count += 1
                    wdict['diar_correct'] = True
            hyp_w_dict_list[w_idx] = wdict
        rttm_wder = round(1 - (correct_word_count / len(hyp_w_dict_list)), 4)
        return hyp_w_dict_list, word_seq_list, correct_word_count, rttm_wder

    def get_ctm_based_eval(self, ctm_content, error_dict, count_dict, hyp_w_dict_list, mapping_dict):
        """
        Calculate errors using the given CTM files.
        """
        count_dict['grand_total_ctm_word_count'] += len(ctm_content)
        align_errors, ctm_error_dict = self.get_alignment_errors(ctm_content, hyp_w_dict_list, mapping_dict)
        count_dict['total_asr_and_spk_correct_words'] += ctm_error_dict['all_correct_count']
        count_dict['total_ctm_wder_count'] += ctm_error_dict['diar_confuse_count']
        self.align_error_list += align_errors
        error_dict.update(ctm_error_dict)

    def get_wder_dict_values(self, asr_eval_dict, wder_dict, count_dict, align_error_list):
        """
        Calculate the total error rates for WDER, WER and alignment error.
        """
        if '-' in asr_eval_dict['references_list'] or None in asr_eval_dict['references_list']:
            wer = -1
        else:
            wer = word_error_rate(
                hypotheses=asr_eval_dict['hypotheses_list'], references=asr_eval_dict['references_list']
            )

        wder_dict['total_WER'] = wer
        wder_dict['total_wder_rttm'] = 1 - (
            count_dict['grand_total_correct_word_count'] / count_dict['grand_total_pred_word_count']
        )

        if all(x for x in self.ctm_exists.values()) == True:
            wder_dict['total_wder_ctm_ref_trans'] = (
                count_dict['total_ctm_wder_count'] / count_dict['grand_total_ctm_word_count']
                if count_dict['grand_total_ctm_word_count'] > 0
                else -1
            )
            wder_dict['total_wder_ctm_pred_asr'] = (
                count_dict['total_ctm_wder_count'] / count_dict['grand_total_pred_word_count']
                if count_dict['grand_total_pred_word_count'] > 0
                else -1
            )
            wder_dict['total_diar_trans_acc'] = (
                count_dict['total_asr_and_spk_correct_words'] / count_dict['grand_total_ctm_word_count']
                if count_dict['grand_total_ctm_word_count'] > 0
                else -1
            )
            wder_dict['total_alignment_error_mean'] = (
                np.mean(self.align_error_list).round(4) if self.align_error_list != [] else -1
            )
            wder_dict['total_alignment_error_std'] = (
                np.std(self.align_error_list).round(4) if self.align_error_list != [] else -1
            )
        return wder_dict

    def get_str_speech_labels(self, speech_labels_float):
        """
        Convert speech_labels_float to a list that contains string values.
        """
        speech_labels = []
        for start, end in speech_labels_float:
            speech_labels.append("{:.3f} {:.3f} speech".format(start, end))
        return speech_labels

    def write_result_in_csv(self, args, WDER_dict, DER_result_dict, effective_WDER):
        """
        This function is for development use.
        Saves the diarization result into a csv file.
        """
        row = [
            args.asr_based_vad_threshold,
            WDER_dict['total'],
            DER_result_dict['total']['DER'],
            DER_result_dict['total']['FA'],
            DER_result_dict['total']['MISS'],
            DER_result_dict['total']['CER'],
            DER_result_dict['total']['spk_counting_acc'],
            effective_WDER,
        ]

        with open(os.path.join(self.root_path, args.csv), 'a') as csvfile:
            csvwriter = csv.writer(csvfile)
            csvwriter.writerow(row)

    def write_session_level_result_in_csv(self, WDER_dict):
        """
        This function is for development use when a CTM file is provided.
        Saves the session-level diarization and ASR result into a csv file.
        """
        target_path = f"{self.root_path}/pred_rttms/ctm_eval.csv"
        logging.info(f"Writing {target_path}")
        csv_columns = [
            'uniq_id',
            'DER',
            'CER',
            'FA',
            'MISS',
            'est_n_spk',
            'is_spk_count_correct',
            'ref_word_count',
            'hyp_word_count',
            'diar_confuse_count',
            'all_correct_count',
            'diar_trans_acc',
            'hyp_based_wder',
            'ref_based_wder',
            'rttm_based_wder',
            'mapping',
        ]
        dict_data = [x for k, x in WDER_dict['session_level'].items()]
        try:
            with open(target_path, 'w') as csvfile:
                writer = csv.DictWriter(csvfile, fieldnames=csv_columns)
                writer.writeheader()
                for data in dict_data:
                    writer.writerow(data)
        except IOError:
            logging.info("I/O error has occurred while writing a csv file.")

    def break_lines(self, string_out, max_chars_in_line=90):
        """
        Break the lines in the transcript.
        """
        color_str_len = len('\033[1;00m') if self.params['colored_text'] else 0
        split_string_out = string_out.split('\n')
        return_string_out = []
        for org_chunk in split_string_out:
            buffer = []
            if len(org_chunk) - color_str_len > max_chars_in_line:
                color_str = org_chunk[:color_str_len] if color_str_len > 0 else ''
                for i in range(color_str_len, len(org_chunk), max_chars_in_line):
                    trans_str = org_chunk[i : i + max_chars_in_line]
                    if len(trans_str.strip()) > 0:
                        c_trans_str = color_str + trans_str
                        buffer.append(c_trans_str)
                return_string_out.extend(buffer)
            else:
                return_string_out.append(org_chunk)
        return '\n'.join(return_string_out)

    def write_and_log(self, uniq_id, riva_dict, audacity_label_words, gecko_dict, sentences):
        """
        Write output files and display logging messages.
        """
        # print the sentences in the .txt output
        string_out = self.print_sentences(sentences, self.params)
        if self.params['break_lines']:
            string_out = self.break_lines(string_out)

        # add sentences to the json array
        self.add_sentences_to_dict(riva_dict, sentences)

        ROOT = self.root_path
        dump_json_to_file(f'{ROOT}/pred_rttms/{uniq_id}.json', riva_dict)
        dump_json_to_file(f'{ROOT}/pred_rttms/{uniq_id}_gecko.json', gecko_dict)
        write_txt(f'{ROOT}/pred_rttms/{uniq_id}.txt', string_out.strip())
        write_txt(f'{ROOT}/pred_rttms/{uniq_id}.w.label', '\n'.join(audacity_label_words))

    def print_errors(self, DER_result_dict, WDER_dict):
        """
        Print a slew of error metrics for ASR and Diarization.
        """
        if all(x for x in self.ctm_exists.values()) == True:
            self.write_session_level_result_in_csv(WDER_dict)
            logging.info(
                f"\nDER                : {DER_result_dict['total']['DER']:.4f} \
                \nFA                 : {DER_result_dict['total']['FA']:.4f} \
                \nMISS               : {DER_result_dict['total']['MISS']:.4f} \
                \nCER                : {DER_result_dict['total']['CER']:.4f} \
                \nrttm WDER          : {WDER_dict['total_wder_rttm']:.4f} \
                \nCTM WDER Ref.      : {WDER_dict['total_wder_ctm_ref_trans']:.4f} \
                \nCTM WDER ASR Hyp.  : {WDER_dict['total_wder_ctm_pred_asr']:.4f} \
                \nCTM diar-trans Acc.: {WDER_dict['total_diar_trans_acc']:.4f} \
                \nmanifest text WER  : {WDER_dict['total_WER']:.4f} \
                \nalignment Err.     : Mean: {WDER_dict['total_alignment_error_mean']:.4f} STD:{WDER_dict['total_alignment_error_std']:.4f} \
                \nSpk. counting Acc. : {DER_result_dict['total']['spk_counting_acc']:.4f}"
            )
        else:
            logging.info(
                f"\nDER      : {DER_result_dict['total']['DER']:.4f} \
                \nFA       : {DER_result_dict['total']['FA']:.4f} \
                \nMISS     : {DER_result_dict['total']['MISS']:.4f} \
                \nCER      : {DER_result_dict['total']['CER']:.4f} \
                \nWDER     : {WDER_dict['total_wder_rttm']:.4f} \
                \nWER      : {WDER_dict['total_WER']:.4f} \
                \nSpk. counting acc.: {DER_result_dict['total']['spk_counting_acc']:.4f}"
            )

    def print_sentences(self, sentences, params):
        """
        Print a transcript with speaker labels and timestamps.
        """
        # init output
        string_out = ''

        for sentence in sentences:
            # extract info
            speaker = sentence['speaker']
            start_point = sentence['start_point']
            end_point = sentence['end_point']
            text = sentence['text']

            if params['colored_text']:
                color = self.color_palette.get(speaker, '\033[0;37m')
            else:
                color = ''

            # cast timestamp to the correct format
            datetime_offset = 16 * 3600
            if float(start_point) > 3600:
                time_str = '%H:%M:%S.%f'
            else:
                time_str = '%M:%S.%f'
            start_point, end_point = max(float(start_point), 0), max(float(end_point), 0)
            start_point_str = datetime.fromtimestamp(start_point - datetime_offset).strftime(time_str)[:-4]
            end_point_str = datetime.fromtimestamp(end_point - datetime_offset).strftime(time_str)[:-4]
            
            if params['print_time']:
                time_str = f'[{start_point_str} - {end_point_str}] '
            else:
                time_str = ''

            # string out concatenation
            string_out += f'{color}{time_str}{speaker}: {text}\n'

        return string_out

    def add_sentences_to_dict(self, riva_dict, sentences):
        # iterate over sentences
        for sentence in sentences:
            # extract info
            speaker = sentence['speaker']
            start_point = sentence['start_point']
            end_point = sentence['end_point']
            text = sentence['text']

            # save to riva_dict
            riva_dict['sentences'].append(
                {'sentence': text, 'start_time': start_point, 'end_time': end_point, 'speaker_label': speaker}
            )

    @staticmethod
    def isOverlapArray(rangeA, rangeB):
        startA, endA = rangeA[:, 0], rangeA[:, 1]
        startB, endB = rangeB[:, 0], rangeB[:, 1]
        return (endA > startB) & (endB > startA)

    @staticmethod
    def getOverlapRangeArray(rangeA, rangeB):
        left = np.max(np.vstack((rangeA[:, 0], rangeB[:, 0])), axis=0)
        right = np.min(np.vstack((rangeA[:, 1], rangeB[:, 1])), axis=0)
        return right - left

    @staticmethod
    def get_audacity_label(word, stt_sec, end_sec, speaker):
        spk = speaker.split('_')[-1]
        return f'{stt_sec}\t{end_sec}\t[{spk}] {word}'

    @staticmethod
    def get_num_of_spk_from_labels(labels):
        spk_set = [x.split(' ')[-1].strip() for x in labels]
        return len(set(spk_set))

    @staticmethod
    def add_json_to_dict(riva_dict, word, stt, end, speaker):
        riva_dict['words'].append({'word': word, 'start_time': stt, 'end_time': end, 'speaker_label': speaker})


def timeit(method):
    def timed(*args, **kw):
        ts = time.time()
        result = method(*args, **kw)
        te = time.time()
        if 'log_time' in kw:
            name = kw.get('log_name', method.__name__.upper())
            kw['log_time'][name] = int((te - ts) * 1000)
        else:
            logging.info('%2.2fms %r'%((te - ts) * 1000, method.__name__))
            # pass
        return result
    return timed

def process_audio_file(input_data, orig_sr=48000, target_sr=16000, MAX_INT32=2147483647):
    """
    This function is designed to process the streaming input from Gradio instance.

    Args:
        input_data (str or numpy.ndarray):
            If data type is temporary wav file, input data is temporary file path or `input_data`
            could be `numpy.ndarray` containing timeseries in floating point numbers.
        orig_sr (int):
            Sampling rate of the Input audio file.
        target_sr (int):
            The expected sampling rate of the converted sampling rate.
        MAX_INT32 (int):
            The maximum value of `int32` type for normalizing the timeseries data.
    Returns:
        data (numpy.ndarray)
            Numpy array containing timeseries data of the input audio stream.
    """
    if type(input_data) == tuple:
        data = (input_data[1]/MAX_INT32).astype(np.float32)
        data = librosa.resample(data, orig_sr=orig_sr, target_sr=target_sr)
    else:
        raise ValueError(f"The streaming input has unknown input_data type {type(input_data)}")
    return data

def add_timestamp_offset(words, word_ts, offset): 
    words_adj, word_ts_adj = [], []
    for w, x in zip(words, word_ts):
        word_range = [round(x[0] + offset,2), round(x[1] + offset,2)] 
        if word_range[1] >  0.0:
            word_ts_adj.append(word_range)
            words_adj.append(w)
    return words_adj, word_ts_adj


def score_labels(AUDIO_RTTM_MAP, all_reference, all_hypothesis, collar=0.25, ignore_overlap=True):
    """
    >>> [This function should be merged into speaker_utils.py]
    """
    metric = None
    if len(all_reference) == len(all_hypothesis):
        metric = DiarizationErrorRate(collar=2 * collar, skip_overlap=ignore_overlap)

        mapping_dict = {}
        for (reference, hypothesis) in zip(all_reference, all_hypothesis):
            ref_key, ref_labels = reference
            _, hyp_labels = hypothesis
            uem = AUDIO_RTTM_MAP[ref_key].get('uem_filepath', None)
            if uem is not None:
                uem = uem_timeline_from_file(uem_file=uem, uniq_name=ref_key)
            metric(ref_labels, hyp_labels, uem=uem, detailed=True)
            mapping_dict[ref_key] = metric.optimal_mapping(ref_labels, hyp_labels)

        DER = abs(metric)
        CER = metric['confusion'] / metric['total']
        FA = metric['false alarm'] / metric['total']
        MISS = metric['missed detection'] / metric['total']
        itemized_errors = (DER, CER, FA, MISS)

        logging.info(
            "Cumulative Results for collar {} sec and ignore_overlap {}: \n FA: {:.4f}\t MISS {:.4f}\t \
                Diarization ER: {:.4f}\t, Confusion ER:{:.4f}".format(
                collar, ignore_overlap, FA, MISS, DER, CER
            )
        )

        return metric, mapping_dict, itemized_errors
    else:
        logging.warning(
            "check if each ground truth RTTMs were present in provided manifest file. Skipping calculation of Diariazation Error Rate"
        )
        return None
def get_partial_ref_labels(pred_labels, ref_labels):
    last_pred_time = float(pred_labels[-1].split()[1])
    ref_labels_out = []
    for label in ref_labels:
        start, end, speaker = label.split()
        start, end = float(start), float(end)
        if last_pred_time <= start:
            pass
        elif start < last_pred_time <= end:
            label = f"{start} {last_pred_time} {speaker}"
            ref_labels_out.append(label)
        elif end < last_pred_time:
            ref_labels_out.append(label)
    return ref_labels_out

def get_online_DER_stats(DER, CER, FA, MISS, diar_eval_count, der_stat_dict, deci=3):
    der_dict = {
        "DER": round(100 * DER, deci),
        "CER": round(100 * CER, deci),
        "FA": round(100 * FA, deci),
        "MISS": round(100 * MISS, deci),
    }
    der_stat_dict['cum_DER'] += DER
    der_stat_dict['cum_CER'] += CER
    der_stat_dict['avg_DER'] = round(100 * der_stat_dict['cum_DER'] / diar_eval_count, deci)
    der_stat_dict['avg_CER'] = round(100 * der_stat_dict['cum_CER'] / diar_eval_count, deci)
    der_stat_dict['max_DER'] = round(max(der_dict['DER'], der_stat_dict['max_DER']), deci)
    der_stat_dict['max_CER'] = round(max(der_dict['CER'], der_stat_dict['max_CER']), deci)
    return der_dict, der_stat_dict
@timeit
def get_wer_feat_logit_single(samples, frame_asr, frame_len, tokens_per_chunk, delay, model_stride_in_secs, frame_mask):
    """
    Create a preprocessor to convert audio samples into raw features,
    Normalization will be done per buffer in frame_bufferer.
    """
    hyps = []
    tokens_list = []
    frame_asr.reset()
    feature_frame_shape = frame_asr.read_audio_samples(samples, delay, model_stride_in_secs, frame_mask)
    hyp, tokens, log_prob  = frame_asr.transcribe_with_ts(tokens_per_chunk, delay)
    hyps.append(hyp)
    tokens_list.append(tokens)
    return hyps, tokens_list, feature_frame_shape, log_prob

class MaskedFeatureIterator(AudioFeatureIterator):
    def __init__(self, samples, frame_len, preprocessor, device, frame_mask=None):
        super().__init__(samples, frame_len, preprocessor, device)
        if frame_mask is not None:
            self._features = torch.log(torch.mul(np.exp(1) ** self._features, frame_mask.to(device)))

class FrameBatchASR_Logits_Sample(FrameBatchASR_Logits):
    """
    A class for streaming frame-based ASR.
    Inherits from FrameBatchASR and adds new capability of returning the logit output.
    Please refer to FrameBatchASR for more detailed information.
    """

    def __init__(self, asr_model, frame_len=1.0, total_buffer=4.0, batch_size=4):
        super().__init__(asr_model, frame_len, total_buffer, batch_size)
        self.frame_len = frame_len
        self.total_buffer = total_buffer
        self.batch_size = batch_size
    
    @timeit
    def read_audio_samples(self, samples, delay: float, model_stride_in_secs: float, frame_mask):
        self.device = self.asr_model.device
        # samples = np.pad(samples, (0, int(delay * model_stride_in_secs * self.asr_model._cfg.sample_rate)))
        frame_reader = MaskedFeatureIterator(samples, self.frame_len, self.raw_preprocessor, self.device, frame_mask)
        self.set_frame_reader(frame_reader)
        return frame_reader._features.shape

    def buffer_reset(self):
        self.clear_buffer()
        self.reset()

class ASR_DIAR_ONLINE(ASR_DIAR_OFFLINE, ASR_TIMESTAMPS):
    def __init__(self, cfg):
        super().__init__(**cfg.diarizer)
        '''
        Args:
            frame_len (int):
                duration of each frame in second.
            frame_overlap (int)
                duration of overlaps before and after current frame, seconds
        '''
        self.diar = OnlineDiarizer(cfg)
        self.offline_mode = False
        self._cfg_diarizer = cfg.diarizer
        self.audio_queue_buffer = np.array([])
        self.ASR_model_name = self._cfg_diarizer['asr']['model_path']
        self.params = dict(self._cfg_diarizer['asr']['parameters'])
        self.params['round_float'] = 2
        self.params['use_cuda'] = True
        self.params['color'] = True
        self.use_cuda = self.params['use_cuda']
        self.rttm_file_path = None
        self.sample_rate = self.diar.sample_rate
        if self.use_cuda:
            self.device = torch.device("cuda:0")
            self.cuda = True
        else: 
            self.device = torch.device("cpu")
            self.cuda = False

        self.metric = None

        # ASR parameters
        self.frame_len = float(self._cfg_diarizer.asr.parameters.frame_len)
        self.frame_overlap = float(self._cfg_diarizer.asr.parameters.frame_overlap)
        self.word_ts_anchor_offset = float(self._cfg_diarizer.asr.parameters.word_ts_anchor_offset)
        self.max_word_ts_length_in_sec = self._cfg_diarizer.asr.parameters.max_word_ts_length_in_sec
        self.asr_based_vad_threshold = self._cfg_diarizer.asr.parameters.asr_based_vad_threshold
        self.asr_batch_size = 1 # Streaming mode requires only one batch
        self.word_update_margin = 0.0  
        self.ROUND = 2

        # Model initializations
        self.load_online_VAD_model(self.params)
        self.asr_model = self.set_asr_model()
        self._init_FrameBatchASR()
        self.load_punctuation_model()
        self._init_diar_eval_variables()

        # Streaming buffer parameters
        self.CHUNK_SIZE = int(self.frame_len*self.sample_rate)
        self.n_frame_len = int(self.frame_len * self.sample_rate)
        self.n_frame_overlap = int(self.frame_overlap * self.sample_rate)
        self.audio_buffer = np.zeros(shape=2*self.n_frame_overlap + self.n_frame_len,
                               dtype=np.float32)
        self.audio_buffer_length = self.audio_buffer.shape[0]
        self.overlap_frames_count = int(self.n_frame_overlap/self.sample_rate)

        
        self.launcher_end_time = None
        self.streaming_reset_pause_sec = 3.0
        self.reset()

    
    def get_audio_rttm_map(self, uniq_id):
        self.uniq_id = uniq_id
        self.AUDIO_RTTM_MAP = {self.uniq_id: self.AUDIO_RTTM_MAP[uniq_id]}
        self.audio_file_list = [value['audio_filepath'] for _, value in self.AUDIO_RTTM_MAP.items()]

    def load_online_VAD_model(self, params):
        if self._cfg_diarizer.vad.model_path is not None:
            self.vad_model = nemo_asr.models.EncDecClassificationModel.restore_from(self._cfg_diarizer.vad.model_path)
            self.vad_model = self.vad_model.to(self.device)
            self.vad_model.eval()
    
    def _init_diar_eval_variables(self):
        self.diar_eval_count = 0
        self.der_dict = {}
        self.DER_csv_list = []
        self.der_stat_dict = {"avg_DER": 0, "avg_CER": 0, "max_DER": 0, "max_CER": 0, "cum_DER": 0, "cum_CER": 0}

    def update_word_and_word_ts(self, words, word_timetamps):
        """
        Stitch the existing word sequence in the buffer with the new word sequence.
        """
        update_margin =  -1* float(self.frame_len * self.word_update_margin)
        if len(self.word_seq) == 0:
            self.word_seq.extend(words)
            self.word_ts_seq.extend(word_timetamps)

        elif len(words) > 0:
            # Find the first word that starts after frame_start point in state buffer self.word_seq
            before_frame_start_old = torch.tensor(self.word_ts_seq)[:,0] < self.frame_start + update_margin
            if all(before_frame_start_old):
                old_end = len(self.word_seq)
            else:
                old_end = torch.where(before_frame_start_old == False)[0][0].item()
            
            # Find the first word that starts after frame_start point in incoming words
            before_frame_start_new = torch.tensor(word_timetamps)[:,0] < self.frame_start + update_margin
            if all(before_frame_start_new):
                new_stt = len(word_timetamps)
            else:
                new_stt = torch.where(before_frame_start_new == False)[0][0].item()
            
            del self.word_seq[old_end:]
            del self.word_ts_seq[old_end:]
            self.word_seq.extend(words[new_stt:])
            self.word_ts_seq.extend(word_timetamps[new_stt:])
    
    def create_single_session(self, uniq_id, diar_hyp_list):
        """
        Create single session dictionaries to make the streaming online output compatible with offline diarization.

        Args:
            uniq_id (str):
                Unique ID (key) that identifies each input audio file.
            diar_hyp_list (list):
                List containing diarization hypothesis.

        Returns:
            diar_hyp (dict):
                Dictionary containing diarization hypothesis list indexed by unique IDs.
            word_hyp (dict):
                Dictionary containing word sequence from ASR decoding indexed by unique IDs.
            word_ts_hyp (dict):
                Dictionary containing word timestamps  from ASR decoding indexed by unique IDs.
            metric_results  (list):
                Metric result from Pyannote package for online diarization evaluation.
        Returns:
        """
        diar_hyp = {uniq_id: diar_hyp_list}
        word_hyp = {uniq_id: self.word_seq}
        word_ts_hyp = {uniq_id: self.word_ts_seq}
        metric_results = [(uniq_id, self.metric.results_[0][1])]
        return diar_hyp, word_hyp, word_ts_hyp, metric_results
    
    def online_eval_diarization(self, pred_labels, rttm_file):
        pred_diar_labels, ref_labels_list = [], []
        all_hypotheses, all_references = [], []

        if os.path.exists(rttm_file):
            ref_labels_total = rttm_to_labels(rttm_file)
            ref_labels = get_partial_ref_labels(pred_labels, ref_labels_total)
            reference = labels_to_pyannote_object(ref_labels)
            all_references.append([self.uniq_id, reference])
        else:
            raise ValueError("No reference RTTM file provided.")

        pred_diar_labels.append(pred_labels)

        self.der_stat_dict['ref_n_spk'] = self.get_num_of_spk_from_labels(ref_labels)
        self.der_stat_dict['est_n_spk'] = self.get_num_of_spk_from_labels(pred_labels)
        hypothesis = labels_to_pyannote_object(pred_labels)
        self.diar_eval_count += 1
        if ref_labels == [] and pred_labels != []:
            logging.info("Streaming Diar [{}][frame-  {}th  ]:".format(self.uniq_id, self.frame_index))
            DER, CER, FA, MISS = 100.0, 0.0, 0.0, 100.0
            der_dict, self.der_stat_dict = get_online_DER_stats(DER, CER, FA, MISS, self.diar_eval_count, self.der_stat_dict)
            metric, mapping_dict = None, None
        else:
            all_hypotheses.append([self.uniq_id, hypothesis])
            metric, mapping_dict, itemized_errors = score_labels(
                self.AUDIO_RTTM_MAP, all_references, all_hypotheses, collar=0.25, ignore_overlap=True
            )
            DER, CER, FA, MISS = itemized_errors
            logging.info(
                "Streaming Diar [{}][frame-    {}th    ]: DER:{:.4f} MISS:{:.4f} FA:{:.4f}, CER:{:.4f}".format(
                    self.uniq_id, self.frame_index, DER, MISS, FA, CER
                )
            )

            der_dict, self.der_stat_dict = get_online_DER_stats(DER, CER, FA, MISS, self.diar_eval_count, self.der_stat_dict)
        return der_dict, self.der_stat_dict, metric, mapping_dict

    @timeit  
    def print_online_DER_info(self, uniq_id, string_out, diar_hyp_list, params):
        """
        Display online diarization error rate while transcribing the input audio stream.

        Args:
            uniq_id (str):
                Unique ID (key) that identifies each input audio file.
            string_out (str):
                String output containing time, speaker and transcription.
            diar_hyp_list (list):
                List containing diarization hypothesis.

        Returns:
            string_out (str):
                String output containing time, speaker and transcription followed by online DER information
                if RTTM file is provided in simulation mode.

        """
        der_dict, der_stat_dict, self.metric, self.mapping_dict = self.online_eval_diarization(diar_hyp_list, self.rttm_file_path)

        if len(self.metric.results_) > 0:
            diar_hyp, word_hyp, word_ts_hyp, self.metric.results_ = self.create_single_session(uniq_id, diar_hyp_list)
            total_riva_dict = self.get_transcript_with_speaker_labels(diar_hyp, word_hyp, word_ts_hyp, write_files=False)
            DER_result_dict = self.gather_eval_results(self.metric, self.mapping_dict, total_riva_dict, pred_labels=diar_hyp)
            WDER_dict = self.get_WDER(total_riva_dict, DER_result_dict)
            wder = WDER_dict['session_level'][uniq_id]['ref_based_wder']
            string_out += self.DER_to_str(der_dict, der_stat_dict, wder)
        logging.info(
            "Streaming Diar [{}][frame-  {}th  ]:".format(
            self.diar.uniq_id, self.frame_index
            )
        )
        write_txt(f"{self.diar._out_dir}/{uniq_id}.csv", ''.join(self.DER_csv_list))
        return string_out

    def DER_to_str(self, der_dict, der_stat_dict, wder):
        if self.params['color']:
            color = self.color_palette['white']
        else:
            color = ''
        DER, FA, MISS, CER = der_dict['DER'], der_dict['FA'], der_dict['MISS'], der_dict['CER']
        der_strings_list = [f'\n{color}=============================================================================',
                            f'\n{color}[Session: {self.uniq_id}, DER:{DER:.2f}%, FA:{FA:.2f}% MISS:{MISS:.2f}% CER:{CER:.2f}%]',
                            f'\n{color}[Num of Speakers (Est/Ref): {der_stat_dict["est_n_spk"]}/{der_stat_dict["ref_n_spk"]}]',
                            f'\n{color}[WDER : {wder}]']
        self.DER_csv_list.append(f"{self.frame_index}, {DER}, {FA}, {MISS}, {CER}\n")
        return ''.join(der_strings_list)
    
    def update_audio_frame_input(self, frame, buffer): 
        if frame is None:
            frame = np.zeros(shape=self.n_frame_len, dtype=np.float32)
        if len(frame) < self.n_frame_len:
            frame = np.pad(frame, [0, self.n_frame_len - len(frame)], 'constant')
        if len(frame) != self.n_frame_len:
            raise ValueError(f"Frame length {len(frame)} is not a correct frame length {self.n_frame_len}")
        self.buffer_start = round(float((self.frame_index+1)*self.frame_len - (2*self.overlap_frames_count+self.frame_len)), 2)
        buffer[:-self.n_frame_len] = buffer[self.n_frame_len:]
        buffer[-self.n_frame_len:] = frame
        return buffer

    def fix_word_ts(self, word_ts_seq_list):
        """
        [This function should be merged into diarization_utils.py  compensate_word_ts_list()]

        """
        N = len(word_ts_seq_list)
        enhanced_word_ts_buffer = []
        for k, word_ts in enumerate(word_ts_seq_list):
            if k < N - 1:
                word_len = round(word_ts[1] - word_ts[0], self.ROUND)
                len_to_next_word = round(word_ts_seq_list[k + 1][0] - word_ts[0] - 0.01, self.ROUND)
                vad_est_len = len_to_next_word
                min_candidate = min(vad_est_len, len_to_next_word)
                fixed_word_len = max(min(self.max_word_ts_length_in_sec, min_candidate), word_len)
                enhanced_word_ts_buffer.append([word_ts[0], round(word_ts[0] + fixed_word_len, self.ROUND)])
            else:
                enhanced_word_ts_buffer.append([word_ts[0], word_ts[1]])
        return enhanced_word_ts_buffer
        
    @timeit
    def get_VAD_from_ASR(self, word_ts):
        speech_labels = []
        word_ts = copy.deepcopy(word_ts)
        if word_ts == []:
            return speech_labels
        else:
            count = len(word_ts)-1
            while count > 0:
                if len(word_ts) > 1: 
                    if word_ts[count][0] - word_ts[count-1][1] <= self.asr_based_vad_threshold:
                        trangeB = word_ts.pop(count)
                        trangeA = word_ts.pop(count-1)
                        word_ts.insert(count-1, [trangeA[0], trangeB[1]])
                count -= 1

        word_ts = self.fix_word_ts(word_ts)
        return word_ts 
    
    def _init_FrameBatchASR(self):
        torch.manual_seed(0)
        torch.set_grad_enabled(False)

        self.chunk_len_in_sec = self.frame_len
        context_len_in_secs = self.frame_overlap
        self.total_buffer_in_secs = 2*context_len_in_secs + self.chunk_len_in_sec
        self.model_stride_in_secs = 0.04

        self.werbpe_ts = WERBPE_TS(
            tokenizer=self.asr_model.tokenizer,
            batch_dim_index=0,
            use_cer=self.asr_model._cfg.get('use_cer', False),
            ctc_decode=True,
            dist_sync_on_step=True,
            log_prediction=self.asr_model._cfg.get("log_prediction", False),
        )
            
        self.frame_asr = FrameBatchASR_Logits_Sample(
            asr_model=self.asr_model,
            frame_len=self.chunk_len_in_sec,
            total_buffer=self.total_buffer_in_secs,
            batch_size=self.asr_batch_size,
        )

        self.set_buffered_infer_params(self.asr_model)
        self.onset_delay_in_sec = round(self.onset_delay * self.model_stride_in_secs, 2)
    
    @timeit
    def run_VAD_decoder_step(self, buffer):
        """
        Place holder for VAD integration. This function returns vad_mask that is identical for ASR feature matrix for
        the current buffer.
        Streaming VAD infer Example:
            vad_logits, speech_segments, feats_shape = get_vad_feat_logit_single(buffer,
                                    self.frame_vad,
                                    self.chunk_len_in_sec,
                                    self.tokens_per_chunk,
                                    self.mid_delay,
                                    self.model_stride_in_secs,
                                    threshold=0.05,
                                )
        """
        vad_mask, vad_timestamps = None, None
        return vad_mask, vad_timestamps
    
    @timeit
    def run_ASR_decoder_step(self, buffer, frame_mask):
        hyps, tokens_list, feats_shape, log_prob = get_wer_feat_logit_single(buffer,
                                                    self.frame_asr,
                                                    self.chunk_len_in_sec,
                                                    self.tokens_per_chunk,
                                                    self.mid_delay,
                                                    self.model_stride_in_secs,
                                                    frame_mask,
                                                )
        self.frame_asr.buffer_reset()
        logits_len = torch.from_numpy(np.array([len(tokens_list[0])]))
        greedy_predictions = torch.from_numpy(np.array(tokens_list[0])).unsqueeze(0)
        text, char_ts, _word_ts = self.werbpe_ts.ctc_decoder_predictions_tensor_with_ts(
            self.model_stride_in_secs, greedy_predictions, predictions_len=logits_len
        )
        words, word_ts = text[0].split(), _word_ts[0]
        if len(words) != len(word_ts):
            raise ValueError("words and word_ts length mismatch")
        offset = self.buffer_start - self.onset_delay_in_sec
        words_adj, word_ts_adj = add_timestamp_offset(words, word_ts, offset)
        return words_adj, word_ts_adj

    def update_launcher_timestamps(self):
        """
        Update buffer length, start and end timestamps for frame and buffer.
        """
        new_bufflen_sec = self.n_frame_len / self.sample_rate
        n_buffer_samples = int(len(self.audio_buffer)/self.sample_rate)
        total_buffer_len_sec = len(self.audio_buffer)/self.sample_rate
        self.buffer_end = round(self.buffer_start + total_buffer_len_sec, self.ROUND)
        self.frame_start = round(self.buffer_start + int(self.n_frame_overlap/self.sample_rate), self.ROUND)

    def streaming_step(self, frame):
        loop_start_time = time.time()
        if len(frame) != int(self.sample_rate * self.frame_len):
            raise ValueError(f"frame does not have the expected length.")
        words, timestamps, diar_hyp = self.run_step(frame=frame)
        if diar_hyp != []:
            total_riva_dict = {}
            if len(words) != len(timestamps):
                raise ValueError(f"Mismatched ASR results: `words` has length of {len(words)} but `timestamps` has length of {len(timestamps)}")
            self.update_word_and_word_ts(words, timestamps)
            word_dict_seq_list = self.get_word_dict_seq_list(diar_hyp, self.word_seq, self.word_ts_seq, self.word_ts_seq)
            sentences = self.make_json_output(self.diar.uniq_id, diar_hyp, word_dict_seq_list, total_riva_dict, write_files=False)
            self.string_out = self.print_sentences(sentences, self.params)
            if self.rttm_file_path and len(self.word_seq) > 0:
                self.string_out = self.print_online_DER_info(self.diar.uniq_id, self.string_out, diar_hyp, self.params)
            write_txt(f"{self.diar._out_dir}/print_script.sh", self.string_out.strip())
        self.simulate_delay(loop_start_time) 
    
    def simulate_delay(self, loop_start_time):
        ETA = time.time()-loop_start_time 
        if self._cfg_diarizer.asr.parameters.enforce_real_time and ETA < self.frame_len:
            time.sleep(self.frame_len - ETA)
        comp_ETA = time.time()-loop_start_time 
        logging.info(f"Total ASR and Diarization ETA: {ETA:.3f} comp ETA {comp_ETA:.3f}")

    def audio_queue_launcher(self, Audio, state):
        """
        Pass the audio stream to streaming ASR pipeline. If Audio variable is not provided, the system puts on hold until
        audio stream is resumed.
        """
        if self.launcher_end_time is not None and time.time() - self.launcher_end_time > self.streaming_reset_pause_sec:
            self.reset()
            self.diar.reset()
            logging.info("[Streaming Reset] Resetting ASR and Diarization.")
        stt = time.time()
        logging.info(f"Streaming launcher took {(stt-self.launcher_end_time):.3f}s")
        audio_queue = process_audio_file(Audio)
        self.audio_queue_buffer = np.append(self.audio_queue_buffer, audio_queue)
        while len(self.audio_queue_buffer) > self.CHUNK_SIZE:
            frame = self.audio_queue_buffer[:self.CHUNK_SIZE]
            self.audio_queue_buffer = self.audio_queue_buffer[self.CHUNK_SIZE:]
            self.streaming_step(frame)
        eta = time.time() - stt
        self.launcher_end_time = time.time()
        return f"Audio Queue Length {len(self.audio_queue_buffer)/self.sample_rate:.2f}s", str(self.frame_index)
    
    def transfer_frame_info_to_diarizer(self):
        """
        Transfer timestamps and buffer data to diarizer instance
        """
        self.diar.frame_index = self.frame_index
        self.diar.frame_start = self.frame_start
        self.diar.buffer_start = self.buffer_start
        self.diar.buffer_end = self.buffer_end
        self.diar.total_buffer_in_secs = self.total_buffer_in_secs

    def reset(self):
        '''
        Reset frame_history and decoder's state
        '''
        self.string_out = ""
        self.frame_index = 0
        self.frame_start = 0
        self.buffer_start = None
        self.launcher_end_time = 0.0 
        self.audio_buffer=np.zeros(shape=self.audio_buffer.shape, dtype=np.float32)
        self.prev_char = ''
        self.word_seq = []
        self.word_ts_seq = []

    
    @torch.no_grad()
    def run_step(self, frame=None):
        """
        Launch streaming ASR decoder loop and online speaker diarization module.

        Args:
            frame (Tensor):

        Returns:

        """
        # Save the input frame into audio buffer.
        self.audio_buffer = self.update_audio_frame_input(frame=frame, buffer=self.audio_buffer)
        
        # Run VAD decoder to get VAD-mask and VAD-timestamps
        vad_mask, vad_ts = self.run_VAD_decoder_step(buffer=self.audio_buffer) 
       
        # Run ASR decoder step to obatain word sequence (`words`) and word timestamps (`word_timestamps`)
        words, word_timestamps = self.run_ASR_decoder_step(buffer=self.audio_buffer, frame_mask=vad_mask)
   
        # Use ASR based VAD timestamp if no VAD timestamps are provided
        if vad_ts is None:
            vad_ts = self.get_VAD_from_ASR(word_ts=word_timestamps)
        
        # Sync diarization frame index with ASR frame index
        self.update_launcher_timestamps()
       
        # Update the frame-timing info for diarizer then run diarization step
        self.transfer_frame_info_to_diarizer()
        diar_hyp = self.diar.diarize_step(self.audio_buffer, vad_ts)

        self.frame_index += 1
        return words, word_timestamps, diar_hyp
    
