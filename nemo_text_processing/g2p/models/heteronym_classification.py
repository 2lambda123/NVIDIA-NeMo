# Copyright (c) 2022, NVIDIA CORPORATION & AFFILIATES.  All rights reserved.
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


import json
from typing import List, Optional, Tuple

import torch
from hydra.utils import instantiate
from nemo_text_processing.g2p.data.data_utils import read_wordids
from nemo_text_processing.g2p.data.heteronym_classification_data import HeteronymClassificationDataset
from omegaconf import DictConfig
from pytorch_lightning import Trainer
from tqdm import tqdm

from nemo.collections.common.losses import CrossEntropyLoss
from nemo.collections.nlp.metrics.classification_report import ClassificationReport
from nemo.collections.nlp.modules.common import TokenClassifier
from nemo.collections.nlp.parts.utils_funcs import tensor2list
from nemo.core.classes.common import PretrainedModelInfo
from nemo.utils import logging

try:
    from nemo.collections.nlp.models.nlp_model import NLPModel

    NLP_AVAILABLE = True
except (ModuleNotFoundError, ImportError):
    NLP_AVAILABLE = False

__all__ = ['HeteronymClassificationModel']


class HeteronymClassificationModel(NLPModel):
    """
    This is a classification model that selects the best heteronym option out of possible dictionary entries.
    Supports only heteronyms, no OOV.
    """

    def __init__(self, cfg: DictConfig, trainer: Trainer = None):
        self.max_seq_length = cfg.max_seq_length
        self.wordids = cfg.wordids
        self.register_artifact("cfg.wordids", self.wordids)
        self.homograph_dict, self.wordid_to_idx = read_wordids(cfg.wordids)
        self.supported_heteronyms = [h for h in self.homograph_dict.keys()]
        super().__init__(cfg=cfg, trainer=trainer)

        num_classes = len(self.wordid_to_idx)
        self.classifier = TokenClassifier(
            hidden_size=self.hidden_size,
            num_classes=num_classes,
            num_layers=self._cfg.head.num_fc_layers,
            activation=self._cfg.head.activation,
            log_softmax=False,
            dropout=self._cfg.head.fc_dropout,
            use_transformer_init=self._cfg.head.use_transformer_init,
        )

        # Loss Functions
        self.loss = CrossEntropyLoss(logits_ndim=3)

        # setup to track metrics
        self.classification_report = ClassificationReport(
            num_classes=num_classes, mode='macro', dist_sync_on_step=True, label_ids=self.wordid_to_idx
        )

        # Language
        self.lang = cfg.get('lang', None)

        # @typecheck()

    def forward(self, input_ids, attention_mask, token_type_ids):
        hidden_states = self.bert_model(
            input_ids=input_ids, attention_mask=attention_mask, token_type_ids=token_type_ids
        )
        if isinstance(hidden_states, tuple):
            hidden_states = hidden_states[0]
        logits = self.classifier(hidden_states=hidden_states)
        return logits

    def make_step(self, batch):
        logits = self.forward(
            input_ids=batch["input_ids"],
            attention_mask=batch["attention_mask"],
            token_type_ids=torch.zeros_like(batch["input_ids"]),
        )
        # apply mask to mask out irrelevant options (elementwise)
        logits = logits * batch["target_and_negatives_mask"].unsqueeze(1)

        loss = self.loss(logits=logits, labels=batch["targets"])
        return loss, logits

        # Training

    def training_step(self, batch, batch_idx):
        """
		Lightning calls this inside the training loop with the data from the training dataloader
		passed in as `batch`.
		"""

        loss, logits = self.make_step(batch)
        self.log('train_loss', loss)
        return loss

    def training_epoch_end(self, outputs):
        return super().training_epoch_end(outputs)

    # Validation and Testing
    def validation_step(self, batch, batch_idx, split="val"):
        """
        Lightning calls this inside the validation loop with the data from the validation dataloader
        passed in as `batch`.
        """
        val_loss, logits = self.make_step(batch)
        subtokens_mask = batch["subtokens_mask"]
        targets = batch["targets"]
        targets = targets[targets != -100]

        self.log(f"{split}_loss", val_loss)
        tag_preds = torch.argmax(logits, axis=-1)[subtokens_mask > 0]
        tp, fn, fp, _ = self.classification_report(tag_preds, targets)
        return {f'{split}_loss': val_loss, 'tp': tp, 'fn': fn, 'fp': fp}

    def validation_epoch_end(self, outputs, split="val"):
        """
        Args:
            outputs: list of individual outputs of each test step.
            split: dataset split name: "val" or "test"
        """
        avg_loss = torch.stack([x[f'{split}_loss'] for x in outputs]).mean()

        # calculate metrics and classification report
        precision, recall, f1, report = self.classification_report.compute()

        # remove examples with support=0
        report = "\n".join(
            [
                x
                for x in report.split("\n")
                if not x.endswith("          0") and "100.00     100.00     100.00" not in x
            ]
        )
        logging.info(f"{split}_report: {report}")
        logging.info(f"{split}_ACCURACY: {f1:.2f}%")
        self.log(f"{split}_loss", avg_loss, prog_bar=True)
        self.log(f"{split}_precision", precision)
        self.log(f"{split}_f1", f1)
        self.log(f"{split}_recall", recall)

        f1_macro = report[report.index("macro") :].split("\n")[0].replace("macro avg", "").strip().split()[-2]
        f1_micro = report[report.index("micro") :].split("\n")[0].replace("micro avg", "").strip().split()[-2]

        self.log(f"{split}_f1_macro", torch.Tensor([float(f1_macro)]))
        self.log(f"{split}_f1_micro", torch.Tensor([float(f1_micro)]))

        self.classification_report.reset()

    def test_step(self, batch, batch_idx):
        """
        Lightning calls this inside the test loop with the data from the test dataloader passed in as `batch`.
        """
        return self.validation_step(batch, batch_idx, "test")

    def test_epoch_end(self, outputs):
        """
        Called at the end of test to aggregate outputs.

        Args:
            outputs: list of individual outputs of each test step.
        """
        return self.validation_epoch_end(outputs, "test")

    # Functions for inference
    @torch.no_grad()
    def disambiguate(
        self,
        sentences: List[str],
        start_end: List[Tuple[int, int]],
        homographs: List[List[str]],
        batch_size: int,
        num_workers: int = 0,
    ):

        if isinstance(sentences, str):
            sentences = [sentences]

        if len(sentences) != len(start_end) != len(homographs):
            raise ValueError(
                f"Number of sentences should match the lengths of provided start-end indices, {len(sentences)} != {len(start_end)}"
            )

        tmp_manifest = "/tmp/manifest.json"
        with open(tmp_manifest, "w") as f:
            for cur_sentence, cur_start_ends, cur_homographs in zip(sentences, start_end, homographs):
                item = {"text_graphemes": cur_sentence, "start_end": cur_start_ends, "homograph_span": cur_homographs}
                f.write(json.dumps(item, ensure_ascii=False) + '\n')

        # store predictions for all queries in a single list
        all_preds = []
        mode = self.training
        try:
            device = 'cuda' if torch.cuda.is_available() else 'cpu'
            # Switch model to evaluation mode
            self.eval()
            self.to(device)
            infer_datalayer = self._setup_infer_dataloader(
                tmp_manifest, grapheme_field="text_graphemes", batch_size=batch_size, num_workers=num_workers
            )

            for batch in infer_datalayer:
                input_ids, attention_mask, target_and_negatives_mask, subword_mask = batch
                logits = self.forward(
                    input_ids=input_ids.to(device),
                    attention_mask=attention_mask.to(device),
                    target_and_negatives_mask=target_and_negatives_mask.to(device),
                )

                preds = torch.argmax(logits, axis=-1)[subword_mask > 0]
                preds = tensor2list(preds)
                all_preds.extend(preds)
        finally:
            # set mode back to its original value
            self.train(mode=mode)

        # convert indices to wordids
        idx_to_wordid = {v: k for k, v in self.wordid_to_idx.items()}
        all_preds = [idx_to_wordid[p] for p in all_preds]
        return all_preds

    @torch.no_grad()
    def disambiguate_manifest(self, manifest, grapheme_field, batch_size: int, num_workers: int = 0):
        # store predictions for all queries in a single list
        all_preds = []
        mode = self.training
        try:
            device = 'cuda' if torch.cuda.is_available() else 'cpu'
            # Switch model to evaluation mode
            self.eval()
            self.to(device)
            infer_datalayer = self._setup_infer_dataloader(
                manifest, grapheme_field, batch_size=batch_size, num_workers=num_workers
            )

            for batch in tqdm(infer_datalayer):
                input_ids, attention_mask, target_and_negatives_mask, subword_mask = batch
                logits = self.forward(
                    input_ids=input_ids.to(device),
                    attention_mask=attention_mask.to(device),
                    target_and_negatives_mask=target_and_negatives_mask.to(device),
                )

                preds = torch.argmax(logits, axis=-1)[subword_mask > 0]
                preds = tensor2list(preds)
                all_preds.extend(preds)
        finally:
            # set mode back to its original value
            self.train(mode=mode)

        # convert indices to wordids
        idx_to_wordid = {v: k for k, v in self.wordid_to_idx.items()}
        all_preds = [idx_to_wordid[p] for p in all_preds]
        return all_preds

    # Functions for processing data
    def setup_training_data(self, train_data_config: Optional[DictConfig]):
        if not train_data_config or train_data_config.dataset.manifest is None:
            logging.info(
                f"Dataloader config or file_path for the train is missing, so no data loader for train is created!"
            )
            self._train_dl = None
            return
        self._train_dl = self._setup_dataloader_from_config(cfg=train_data_config, data_split="train")

    def setup_validation_data(self, val_data_config: Optional[DictConfig]):
        if not val_data_config or val_data_config.dataset.manifest is None:
            logging.info(
                f"Dataloader config or file_path for the validation is missing, so no data loader for validation is created!"
            )
            self._validation_dl = None
            return
        self._validation_dl = self._setup_dataloader_from_config(cfg=val_data_config, data_split="val")

    def setup_test_data(self, test_data_config: Optional[DictConfig]):
        if not test_data_config or test_data_config.dataset.manifest is None:
            logging.info(
                f"Dataloader config or file_path for the test is missing, so no data loader for test is created!"
            )
            self._test_dl = None
            return
        self._test_dl = self._setup_dataloader_from_config(cfg=test_data_config, data_split="test")

    def _setup_dataloader_from_config(self, cfg: DictConfig, data_split: str):
        if "dataset" not in cfg or not isinstance(cfg.dataset, DictConfig):
            raise ValueError(f"No dataset for {data_split}")
        if "dataloader_params" not in cfg or not isinstance(cfg.dataloader_params, DictConfig):
            raise ValueError(f"No dataloader_params for {data_split}")

        dataset = instantiate(
            cfg.dataset,
            manifest=cfg.dataset.manifest,
            grapheme_field=cfg.dataset.grapheme_field,
            tokenizer=self.tokenizer,
            wordid_to_idx=self.wordid_to_idx,
            wiki_homograph_dict=self.homograph_dict,
            max_seq_len=self.max_seq_length,
            with_labels=True,
        )

        return torch.utils.data.DataLoader(dataset, collate_fn=dataset.collate_fn, **cfg.dataloader_params)

    def _setup_infer_dataloader(
        self, manifest: str, grapheme_field: str, batch_size: int, num_workers: int
    ) -> 'torch.utils.data.DataLoader':

        dataset = HeteronymClassificationDataset(
            manifest=manifest,
            grapheme_field=grapheme_field,
            tokenizer=self.tokenizer,
            wordid_to_idx=self.wordid_to_idx,
            wiki_homograph_dict=self.homograph_dict,
            max_seq_len=self.tokenizer.tokenizer.model_max_length,
            with_labels=False,
        )

        return torch.utils.data.DataLoader(
            dataset,
            collate_fn=dataset.collate_fn,
            batch_size=batch_size,
            shuffle=False,
            num_workers=num_workers,
            drop_last=False,
        )

    @classmethod
    def list_available_models(cls) -> Optional[PretrainedModelInfo]:
        """
        This method returns a list of pre-trained model which can be instantiated directly from NVIDIA's NGC cloud.
        Returns:
            List of available pre-trained models.
        """
        return []
