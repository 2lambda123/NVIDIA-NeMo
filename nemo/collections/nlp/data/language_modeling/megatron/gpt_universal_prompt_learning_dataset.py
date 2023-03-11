# Copyright (c) 2022, NVIDIA CORPORATION.  All rights reserved.
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

import functools
import json
import os
import pickle
import re
from typing import Dict, List

import numpy as np
import torch
from tqdm.auto import tqdm

from nemo.collections.common.tokenizers.tokenizer_spec import TokenizerSpec
from nemo.collections.nlp.data.language_modeling.megatron.dataset_utils import get_samples_mapping
from nemo.collections.nlp.data.language_modeling.text_memmap_dataset import JSONLMemMapDataset
from nemo.collections.nlp.modules.common.megatron.utils import build_position_ids
from nemo.core import Dataset
from nemo.utils import AppState, logging

__all__ = ['GPTUniversalPromptLearningDataset']

SHORT_ASSIST_PROMPT = """Assistant: The following is a conversation with an AI assistant. The assistant is helpful, creative, clever, and very friendly.
User: """

LONG_ASSIST_PROMPT = """Assistant: I am Assistant, a large language model trained by NVIDIA.
I am designed to generate human-like text based on the input that I receive. This can include providing responses to questions, generating summaries of text, or even generating entire documents on a given topic. I am able to understand and process natural language, so you can interact with me in the same way that you would with another person.
Feel free to ask me any questions that you have, and I will do my best to provide a helpful and accurate response. You can also provide me with text or a topic, and I can generate text based on that input. Whether you have a specific question that you need answered, or you need help with a language-related task, please let me know how I can assist you today.
User: """

LONG_ASSIST_PROMPT_NV = """Assistant: I am NVLM, a large language model trained by NVIDIA.
I am designed to generate human-like text based on the input that I receive. This can include providing responses to questions, generating summaries of text, or even generating entire documents on a given topic. I am able to understand and process natural language, so you can interact with me in the same way that you would with another person.
Feel free to ask me any questions that you have, and I will do my best to provide a helpful and accurate response. You can also provide me with text or a topic, and I can generate text based on that input. Whether you have a specific question that you need answered, or you need help with a language-related task, please let me know how I can assist you today.
User: """


class GPTUniversalPromptLearningT0Dataset(Dataset):
    """
    The dataset class for prompt-tuning or p-tuning pretrained GPT models.

    Args:
        data (list[strings], list[dicts]): (1) paths to .jsonl or .json files, (2) dict objects corresponding to each input example
        tokenizer (tokenizer): Tokenizer from frozen language model
        task_templates (dict): Dictionary containing all task template information needed to format prompts. Created in the GPTPromptLearningModel class.
        pad_token_id (int): ID of pad token from tokenizer
        max_seq_length (int): maximum sequence length for each dataset examples. Examples will either be truncated to fit this length or dropped if they cannot be truncated.
        min_seq_length (int): min length of each data example in the dataset. Data examples will be dropped if they do not meet the min length requirements. 
        add_bos (bool): Whether to add a beginning of sentence token to each data example
        add_eos (bool): Whether to add an end of sentence token to each data example
        for_train (bool): Whether you're creating a dataset for training or inference
        tokens_to_generate (int): (inference only) Number of tokens to generate during inference
    """

    def __init__(
        self,
        data,
        tokenizer,
        answer_only_loss: bool,
        pad_token_id: int,
        virtual_token_len: int,
        max_seq_length: int,
        add_bos: bool = False,
        add_eos: bool = True,
        max_num_samples: int = None,
        seed: int = 1234,
    ):
        self.tokenizer = tokenizer
        self.virtual_token_len = virtual_token_len
        self.pad_token_id = pad_token_id
        self.max_seq_length = max_seq_length
        self.add_bos = add_bos
        self.add_eos = add_eos
        self.answer_only_loss = answer_only_loss
        self.max_num_samples = max_num_samples
        self.seed = seed
        self.assistant_prompt = 'long_nv'

        assert self.max_seq_length > 0, "Max sequence length should be greater than 0"

        logging.info("Loading and tokenizing dataset ... ")

        self.indexed_dataset = JSONLMemMapDataset(dataset_paths=[data], tokenizer=None, header_lines=0, workers=12)
        # Will be None after this call if `max_num_samples` is None
        self._build_samples_mapping(data)

    def _build_samples_mapping(self, file_path):
        if self.max_num_samples is not None:
            self.samples_mapping = get_samples_mapping(
                indexed_dataset=self.indexed_dataset,
                data_prefix=file_path,
                num_epochs=None,
                max_num_samples=self.max_num_samples,
                max_seq_length=self.max_seq_length - 2,
                short_seq_prob=0,
                seed=self.seed,
                name=file_path.split('/')[-1],
                binary_head=False,
            )
        else:
            self.samples_mapping = None

    def __len__(self):
        if self.max_num_samples is None:
            return len(self.indexed_dataset)
        else:
            return len(self.samples_mapping)

    def __getitem__(self, idx):
        if isinstance(idx, np.int64):
            idx = idx.item()

        if self.samples_mapping is not None:
            assert idx < len(self.samples_mapping)
            idx, _, _ = self.samples_mapping[idx]
            if isinstance(idx, np.uint32):
                idx = idx.item()

        assert idx < len(self.indexed_dataset)
        example = self.indexed_dataset[idx]
        return self._process_example(example)

    def _process_example(self, example):
        """
        Process a single example from the dataset into IDs and other T0-related metadata.
        """
        context = example['input']
        if 'output' in example:
            label = example['output']
        else:
            label = ''

        if self.assistant_prompt == 'short':
            context = SHORT_ASSIST_PROMPT + context + "\n\nAssistant:"
        elif self.assistant_prompt == 'long':
            context = LONG_ASSIST_PROMPT + context + "\n\nAssistant:"
        elif self.assistant_prompt == 'long_nv':
            context = LONG_ASSIST_PROMPT_NV + context + "\n\nAssistant:"

        text = context + ' ' + label

        tokenized_text = self.tokenizer.text_to_ids(text)
        tokenized_input = self.tokenizer.text_to_ids(context)
        answer_ids = tokenized_text[len(tokenized_input) :]
        # add a space between input and output

        offset = self.virtual_token_len
        if self.add_bos:
            offset += 1
        if self.add_eos:
            offset += 1
        if len(tokenized_input) + len(answer_ids) > self.max_seq_length - offset:
            cut_tokens = len(tokenized_input) + len(answer_ids) - self.max_seq_length + offset
            if len(tokenized_input) - cut_tokens > 0:
                # cut the input by default
                tokenized_input = tokenized_input[: len(tokenized_input) - cut_tokens]
            elif len(answer_ids) - cut_tokens > 0:
                # cut the output
                answer_ids = answer_ids[: len(answer_ids) - cut_tokens]
            else:
                # cut both the input and output
                cut_input_tokens = len(tokenized_input) - 1  # retain at least one token
                cut_output_tokens = cut_tokens - cut_input_tokens
                tokenized_input = tokenized_input[: len(tokenized_input) - cut_input_tokens]
                answer_ids = answer_ids[: len(answer_ids) - cut_output_tokens]
        bos_id = self.tokenizer.bos_id
        if self.add_bos:
            tokenized_input = [bos_id] + tokenized_input
        if self.add_eos:
            target = answer_ids + [self.tokenizer.eos_id]
        else:
            target = answer_ids
        answer_start_idx = len(tokenized_input)
        input_ids = tokenized_input + target
        results = (input_ids, answer_start_idx)
        return results

    def collate_fn(self, batch, tp_workers=0):
        """ Prepares input_ids, labels, loss mask, attention_mask, and position ids for global batch """
        input_ids, answer_starts = zip(*batch)

        # Get max sequence length of batch
        batch_max = max(len(ids) for ids in input_ids)

        if tp_workers > 1:
            # more sure the sequence length is multiply of number of tp_workers, needed for sequence parallel.
            resi_padding = (tp_workers - (batch_max - 1) % tp_workers) % tp_workers
        else:
            resi_padding = 0
        batch_max += resi_padding
        input_ids, loss_mask, prompt_input_mask = self.pad_batch_and_build_loss_mask(
            input_ids, batch_max, answer_starts
        )
        # Should be a label for every token in batch, label is the next token
        labels = input_ids[:, 1:].contiguous()
        input_ids = input_ids[:, :-1].contiguous()
        batch_max -= 1

        # Loss mask should align with labels
        loss_mask = loss_mask[:, 1:].contiguous()
        prompt_input_mask = prompt_input_mask[:, 1:].contiguous()

        # Using causal attention mask for whole input
        batch_size = len(input_ids)
        attention_mask = torch.tril(
            torch.ones((batch_size, batch_max + self.virtual_token_len, batch_max + self.virtual_token_len))
        ).view(batch_size, 1, batch_max + self.virtual_token_len, batch_max + self.virtual_token_len)

        # Convert attention mask from float to bool
        attention_mask = attention_mask < 0.5
        position_ids = build_position_ids(input_ids)

        return (
            input_ids,
            labels,
            loss_mask,
            position_ids,
            attention_mask,
            prompt_input_mask,
            torch.tensor(answer_starts),
        )

    def pad_batch_and_build_loss_mask(self, input_ids, batch_max, answer_starts):
        """ Pad input_ids in batch to max batch length while building loss mask """
        batch_loss_masks = []
        prompt_input_masks = []
        for ids, answer_start_idx in zip(input_ids, answer_starts):
            if self.answer_only_loss and answer_start_idx is not None:
                # Loss mask where answer tokens are 1.0 and all other tokens are 0.0
                loss_mask = [float(idx >= answer_start_idx) for idx in range(len(ids))]
            else:
                # Loss mask where virtual tokens are 0.0 and all other tokens are 1.0
                loss_mask = [1.0] * len(ids)
            if answer_start_idx is not None:
                # has ground truth in it, mask it out
                prompt_input_mask = [True] * answer_start_idx + [False] * (batch_max - answer_start_idx)
            else:
                # no ground truth, all the inputs are used for perceiver
                prompt_input_mask = [True] * len(ids) + [False] * (batch_max - len(ids))
            prompt_input_masks.append(prompt_input_mask)
            # Pad to max length
            input_length = len(ids)
            padding_length = batch_max - input_length
            ids.extend([self.pad_token_id] * padding_length)

            # Account for padding in loss mask
            loss_mask.extend([0.0] * padding_length)
            batch_loss_masks.append(torch.tensor(loss_mask, dtype=torch.float))

        # Make into torch tensors
        input_ids = torch.tensor(input_ids, dtype=torch.long)
        batch_loss_masks = torch.stack(batch_loss_masks)
        prompt_input_masks = torch.tensor(prompt_input_masks, dtype=torch.bool)

        return input_ids, batch_loss_masks, prompt_input_masks


class GPTUniversalPromptLearningDataset(Dataset):
    """
    The dataset class for prompt-tuning or p-tuning pretrained GPT models.

    Args:
        data (list[strings], list[dicts]): (1) paths to .jsonl or .json files, (2) dict objects corresponding to each input example
        tokenizer (tokenizer): Tokenizer from frozen language model
        task_templates (dict): Dictionary containing all task template information needed to format prompts. Created in the GPTPromptLearningModel class.
        pad_token_id (int): ID of pad token from tokenizer
        max_seq_length (int): maximum sequence length for each dataset examples. Examples will either be truncated to fit this length or dropped if they cannot be truncated.
        min_seq_length (int): min length of each data example in the dataset. Data examples will be dropped if they do not meet the min length requirements. 
        add_bos (bool): Whether to add a beginning of sentence token to each data example
        add_eos (bool): Whether to add an end of sentence token to each data example
        for_train (bool): Whether you're creating a dataset for training or inference
        tokens_to_generate (int): (inference only) Number of tokens to generate during inference
    """

    def __init__(
        self,
        data,
        tokenizer,
        task_templates: dict,
        pad_token_id: int,
        virtual_token_len: int,
        max_seq_length: int,
        min_seq_length: int = 1,
        add_bos: bool = False,
        add_eos: bool = True,
        for_train: bool = True,
        tokens_to_generate=None,
        cache_data_path: str = None,  # the cache file
        load_cache: bool = True,  # whether to load from the cache if it is available
    ):
        self.tokenizer = tokenizer
        self.virtual_token_len = virtual_token_len
        self.task_templates = task_templates
        self.pad_token_id = pad_token_id
        self.max_seq_length = max_seq_length
        self.min_seq_length = min_seq_length
        self.add_bos = add_bos
        self.add_eos = add_eos
        self.for_train = for_train
        self.examples = []
        self.answer_only_loss = self.task_templates["answer_only_loss"]

        if not self.for_train:
            self.tokens_to_generate = tokens_to_generate

        assert self.min_seq_length <= max_seq_length, "Min sequence length should be less than or equal to max"
        assert self.max_seq_length > 0, "Max sequence length should be greater than 0"

        logging.info("Loading and tokenizing dataset ... ")

        if load_cache and cache_data_path is not None and os.path.exists(cache_data_path):
            # load it from the cache
            logging.info(f'load the data from the cache file {cache_data_path}')
            with open(cache_data_path, 'rb') as f:
                self.examples = pickle.load(f)
            torch.distributed.barrier()
        else:
            # Data is just a list of dicts already loaded from a json file or passed in directly as a dict
            if isinstance(data[0], dict):
                self.load_data(data)

            # Datasets are a list of file path strings to .json or .jsonl files
            elif isinstance(data[0], str):
                for path in data:
                    dataset = open(path, 'r', encoding='utf-8')
                    self.load_data(dataset)
            else:
                raise ValueError("Datasets must be a list of filepath strings or a list of data example dicts")
            if cache_data_path is not None:
                # the first worker save the results into the cache file
                app_state = AppState()
                if app_state._global_rank == 0:
                    with open(cache_data_path, 'wb') as f:
                        pickle.dump(self.examples, f)
                    logging.info(f'save the data to the cache file {cache_data_path}')

    def load_data(self, dataset):
        """
        Loads a dataset by filling in the task templates specified in the config file
        with the information from each training/inference example. Converts all input 
        text into token ids. Also replaces the <|VIRTUAL_PROMPT_#|> placeholders in 
        the task templates with the actual virtual prompt token ids. 

        params:
            dataset: A list of json objects or a dictionary objects each
                     containing the information needed for a training example
        """
        skipped = 0

        prompt_template = self.task_templates["prompt_template"]
        truncation_field = self.task_templates['truncate_field']
        answer_field = self.task_templates["answer_field"]

        # divide the prompt string into pieces according to fields
        pieces = []
        while True:
            result = re.search(r'{\w*}', prompt_template)
            if result is None:
                break
            start = result.end()
            sentence = prompt_template[:start]
            if len(sentence) != 0:
                pieces.append(sentence)
            prompt_template = prompt_template[start:]

        for json_line in tqdm(dataset):

            # Read example dict or load the information for a single example from .json file
            if type(json_line) == dict:
                doc = json_line
            else:
                doc = json.loads(json_line)

            input_ids = self.tokenize_input(
                doc, truncation_field, pieces, self.max_seq_length - self.virtual_token_len, self.tokenizer
            )
            # Skip example if the final length doesn't fit length requirements even after truncation
            if self.min_seq_length <= len(input_ids) <= self.max_seq_length - self.virtual_token_len:
                # Find answer field indices if training and answer_only_loss is True
                answer_start_idx = None
                if self.for_train:
                    answer_start_idx = self._find_answer_start(input_ids, answer_field, doc)
                self.examples.append((input_ids, answer_start_idx))
            else:
                skipped += 1

        logging.info(f'Skipped {skipped} sentences, sequence length too short or too long even after truncation')

    def tokenize_input(
        self, doc: Dict, limit_length_field: str, prompt_pieces: List[str], max_seq_len: int, tokenizer: TokenizerSpec
    ):
        all_ids = []
        limits = []
        for piece in prompt_pieces:
            if isinstance(piece, str):
                # replace variables if any
                variables = re.findall(r'{\w*}', piece)
                limit_length = False
                for var in variables:
                    varname = var[1:-1]
                    if varname == limit_length_field:
                        limit_length = True
                if varname in doc:
                    text = piece.format(**doc)
                    text_ids = tokenizer.text_to_ids(text)
                    all_ids.append(text_ids)
                    limits.append(limit_length)

        # Add BOS/EOS if desired, adds EOS by default
        if self.add_bos:
            all_ids[0] = [self.tokenizer.bos_id] + all_ids[0]
        if self.add_eos:
            all_ids[-1] = all_ids[-1] + [self.tokenizer.eos_id]

        total_num_of_ids = sum([len(i) for i in all_ids])

        if total_num_of_ids > max_seq_len:
            logging.warning("Input sequence is longer than the LM model max seq, will cut it off to fit")
            cut = total_num_of_ids - max_seq_len
            new_ids = []
            for i in range(len(limits)):
                if limits[i]:
                    if len(all_ids[i]) < cut:
                        raise ValueError(
                            f"Some other field length is too long, cutting {self.limit_length_field} is not enough"
                        )
                    new_ids.append(all_ids[i][cut:])
                else:
                    new_ids.append(all_ids[i])
            return functools.reduce(lambda x, y: x + y, new_ids)
        else:
            return functools.reduce(lambda x, y: x + y, all_ids)

    def _find_answer_start(self, input_ids, answer_field, doc):
        """ Find the token ids corresponding to the answer start, for loss masking purposes.
            Assumes the answer is always at the end of the prompt.
        """
        answer_text = doc[answer_field]
        answer_text_ids = self.tokenizer.text_to_ids(answer_text)
        num_answer_text_ids = len(answer_text_ids)

        if self.add_eos:
            num_answer_text_ids += 1

        answer_start_idx = len(input_ids) - num_answer_text_ids
        return answer_start_idx

    def __len__(self):
        return len(self.examples)

    def __getitem__(self, idx):
        return self.examples[idx]

    def collate_fn(self, batch, tp_workers=0):
        """ Prepares input_ids, labels, loss mask, attention_mask, and position ids for global batch """
        input_ids, answer_starts = zip(*batch)

        # Get max sequence length of batch
        batch_max = max(len(ids) for ids in input_ids)

        if tp_workers > 1:
            # more sure the sequence length is multiply of number of tp_workers, needed for sequence parallel.
            resi_padding = (tp_workers - (batch_max - 1) % tp_workers) % tp_workers
        else:
            resi_padding = 0
        batch_max += resi_padding
        input_ids, loss_mask, prompt_input_mask = self.pad_batch_and_build_loss_mask(
            input_ids, batch_max, answer_starts
        )
        # Should be a label for every token in batch, label is the next token
        labels = input_ids[:, 1:].contiguous()
        input_ids = input_ids[:, :-1].contiguous()
        batch_max -= 1

        # Loss mask should align with labels
        loss_mask = loss_mask[:, 1:].contiguous()
        prompt_input_mask = prompt_input_mask[:, 1:].contiguous()

        # Using causal attention mask for whole input
        batch_size = len(input_ids)
        attention_mask = torch.tril(
            torch.ones((batch_size, batch_max + self.virtual_token_len, batch_max + self.virtual_token_len))
        ).view(batch_size, 1, batch_max + self.virtual_token_len, batch_max + self.virtual_token_len)

        # Convert attention mask from float to bool
        attention_mask = attention_mask < 0.5
        position_ids = build_position_ids(input_ids)

        return input_ids, labels, loss_mask, position_ids, attention_mask, prompt_input_mask

    def pad_batch_and_build_loss_mask(self, input_ids, batch_max, answer_starts):
        """ Pad input_ids in batch to max batch length while building loss mask """
        batch_loss_masks = []
        prompt_input_masks = []
        for ids, answer_start_idx in zip(input_ids, answer_starts):
            if self.answer_only_loss and answer_start_idx is not None:
                # Loss mask where answer tokens are 1.0 and all other tokens are 0.0
                loss_mask = [float(idx >= answer_start_idx) for idx in range(len(ids))]
            else:
                # Loss mask where virtual tokens are 0.0 and all other tokens are 1.0
                loss_mask = [1.0] * len(ids)
            if answer_start_idx is not None:
                # has ground truth in it, mask it out
                prompt_input_mask = [True] * answer_start_idx + [False] * (batch_max - answer_start_idx)
            else:
                # no ground truth, all the inputs are used for perceiver
                prompt_input_mask = [True] * len(ids) + [False] * (batch_max - len(ids))
            prompt_input_masks.append(prompt_input_mask)
            # Pad to max length
            input_length = len(ids)
            padding_length = batch_max - input_length
            ids.extend([self.pad_token_id] * padding_length)

            # Account for padding in loss mask
            loss_mask.extend([0.0] * padding_length)
            batch_loss_masks.append(torch.tensor(loss_mask, dtype=torch.float))

        # Make into torch tensors
        input_ids = torch.tensor(input_ids, dtype=torch.long)
        batch_loss_masks = torch.stack(batch_loss_masks)
        prompt_input_masks = torch.tensor(prompt_input_masks, dtype=torch.bool)

        return input_ids, batch_loss_masks, prompt_input_masks

    def inference_collate_fn(self, batch):
        """
        Used for loading inference data. 
        """
        input_ids, answer_starts = zip(*batch)
        input_lengths = torch.cuda.LongTensor([len(inputs) for inputs in input_ids])
        batch_max = input_lengths.max().item()
        batch_max += self.tokens_to_generate

        input_ids, _, _ = self.pad_batch_and_build_loss_mask(input_ids, batch_max, answer_starts)
        input_ids = input_ids.cuda()
        input_ids = torch.cuda.LongTensor(input_ids)

        return input_ids, input_lengths
