# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

from collections.abc import Sequence
from pathlib import Path
from typing import Any, ClassVar
import pandas as pd

import numpy as np
import pytorch_lightning as pl
import torch
from hydra.utils import instantiate
from omegaconf import DictConfig
from torch import nn
from torch.utils.data import ConcatDataset, DataLoader
from torchmetrics import MetricCollection

from emg2qwerty import utils
from emg2qwerty.charset import charset
from emg2qwerty.data import LabelData, WindowedEMGDataset
from emg2qwerty.metrics import CharacterErrorRates
from emg2qwerty.modules import (
    MultiBandRotationInvariantMLP,
    SpectrogramNorm,
    TDSConvEncoder,
    RNNEncoder,
    HybridEncoder
)
from emg2qwerty.transforms import Transform

import pandas as pd

class WindowedEMGDataModule(pl.LightningDataModule):
    def __init__(
        self,
        window_length: int,
        padding: tuple[int, int],
        batch_size: int,
        num_workers: int,
        train_sessions: Sequence[Path],
        val_sessions: Sequence[Path],
        test_sessions: Sequence[Path],
        train_transform: Transform[np.ndarray, torch.Tensor],
        val_transform: Transform[np.ndarray, torch.Tensor],
        test_transform: Transform[np.ndarray, torch.Tensor],
    ) -> None:
        super().__init__()

        self.window_length = window_length
        self.padding = padding

        self.batch_size = batch_size
        self.num_workers = num_workers

        self.train_sessions = train_sessions
        self.val_sessions = val_sessions
        self.test_sessions = test_sessions

        self.train_transform = train_transform
        self.val_transform = val_transform
        self.test_transform = test_transform

    def setup(self, stage: str | None = None) -> None:
        self.train_dataset = ConcatDataset(
            [
                WindowedEMGDataset(
                    hdf5_path,
                    transform=self.train_transform,
                    window_length=self.window_length,
                    padding=self.padding,
                    jitter=True,
                )
                for hdf5_path in self.train_sessions
            ]
        )
        self.val_dataset = ConcatDataset(
            [
                WindowedEMGDataset(
                    hdf5_path,
                    transform=self.val_transform,
                    window_length=self.window_length,
                    padding=self.padding,
                    jitter=False,
                )
                for hdf5_path in self.val_sessions
            ]
        )
        self.test_dataset = ConcatDataset(
            [
                WindowedEMGDataset(
                    hdf5_path,
                    transform=self.test_transform,
                    # Feed the entire session at once without windowing/padding
                    # at test time for more realism
                    window_length=None,
                    padding=(0, 0),
                    jitter=False,
                )
                for hdf5_path in self.test_sessions
            ]
        )

    def train_dataloader(self) -> DataLoader:
        return DataLoader(
            self.train_dataset,
            batch_size=self.batch_size,
            shuffle=True,
            num_workers=self.num_workers,
            collate_fn=WindowedEMGDataset.collate,
            pin_memory=True,
            persistent_workers=True,
        )

    def val_dataloader(self) -> DataLoader:
        return DataLoader(
            self.val_dataset,
            batch_size=self.batch_size,
            shuffle=False,
            num_workers=self.num_workers,
            collate_fn=WindowedEMGDataset.collate,
            pin_memory=True,
            persistent_workers=True,
        )

    def test_dataloader(self) -> DataLoader:
        # Test dataset does not involve windowing and entire sessions are
        # fed at once. Limit batch size to 1 to fit within GPU memory and
        # avoid any influence of padding (while collating multiple batch items)
        # in test scores.
        return DataLoader(
            self.test_dataset,
            batch_size=1,
            shuffle=False,
            num_workers=self.num_workers,
            collate_fn=WindowedEMGDataset.collate,
            pin_memory=True,
            persistent_workers=True,
        )


class TransposedBatchNorm1d(nn.Module):
    def __init__(self, num_features: int):
        super().__init__()
        self.bn = nn.BatchNorm1d(num_features)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x is expected to be (T, N, C)
        x = x.transpose(0, 1).transpose(1, 2)  # now shape is (N, C, T)
        x = self.bn(x)
        x = x.transpose(1, 2).transpose(0, 1)  # revert back to (T, N, C)
        return x


class TDSConvCTCModule(pl.LightningModule):
    NUM_BANDS: ClassVar[int] = 2
    ELECTRODE_CHANNELS: ClassVar[int] = 16

    def __init__(
        self,
        in_features: int,
        mlp_features: Sequence[int],
        block_channels: Sequence[int],
        kernel_width: int,
        optimizer: DictConfig,
        lr_scheduler: DictConfig,
        decoder: DictConfig,
        use_rnn: bool = False,
        use_hybrid: bool = False,
        rnn_hidden_size: int = 256,
        rnn_num_layers: int = 2,
        rnn_bidirectional: bool = True,
        l1_lambda: float = 1e-5,
        l2_lambda: float = 1e-4,
    ) -> None:
        super().__init__()
        self.save_hyperparameters()
        self.l1_lambda = l1_lambda
        self.l2_lambda = l2_lambda

        num_features = self.NUM_BANDS * mlp_features[-1]

        # Model
        # inputs: (T, N, bands=2, electrode_channels=16, freq)
        self.model = nn.Sequential(
            # (T, N, bands=2, C=16, freq)
            SpectrogramNorm(channels=self.NUM_BANDS * self.ELECTRODE_CHANNELS),
            # (T, N, bands=2, mlp_features[-1])
            MultiBandRotationInvariantMLP(
                in_features=in_features,
                mlp_features=mlp_features,
                num_bands=self.NUM_BANDS,
            ),
            # (T, N, num_features)
            nn.Flatten(start_dim=2),
            TDSConvEncoder(
                num_features=num_features,
                block_channels=block_channels,
                kernel_width=kernel_width,
            ),
            # (T, N, num_classes)
            nn.Dropout(p=0.2),
            # nn.Dropout(p=0.3),  # Added dropout layer with 30% probability
            TransposedBatchNorm1d(num_features),
            nn.Linear(num_features, charset().num_classes),
            nn.LogSoftmax(dim=-1),
        )

        if use_hybrid:
            print("Using HybridEncoder (CNN + RNN)")
            self.model.add_module(
                "HybridEncoder",
                HybridEncoder(
                    tds_num_features=num_features,
                    tds_block_channels=block_channels,
                    tds_kernel_width=kernel_width,
                    rnn_hidden_size=rnn_hidden_size,
                    rnn_num_layers=rnn_num_layers,
                    rnn_bidirectional=rnn_bidirectional,
                    rnn_type="LSTM",
                )
            )
            output_size = rnn_hidden_size * (2 if rnn_bidirectional else 1)
        elif use_rnn:
            print("Using RNN Encoder instead of TDSConvEncoder")
            self.model.add_module(
                "RNNEncoder",
                RNNEncoder(
                    input_size=num_features,
                    hidden_size=rnn_hidden_size,
                    num_layers=rnn_num_layers,
                    bidirectional=rnn_bidirectional
                ),
            )
            output_size = rnn_hidden_size * (2 if rnn_bidirectional else 1)
        else:
            print("Using TDSConvEncoder")
            self.model.add_module(
                "TDSConvEncoder",
                TDSConvEncoder(
                    num_features=num_features,
                    block_channels=block_channels,
                    kernel_width=kernel_width,
                ),
            )
            output_size = num_features
        char_set = charset()
        self.model.add_module("Linear", nn.Linear(output_size, char_set.num_classes))
        self.model.add_module("LogSoftmax", nn.LogSoftmax(dim=-1))

        # Criterion
        self.ctc_loss = nn.CTCLoss(blank=charset().null_class)

        # Decoder
        self.decoder = instantiate(decoder)

        # Metrics
        metrics = MetricCollection([CharacterErrorRates()])
        self.metrics = nn.ModuleDict(
            {
                f"{phase}_metrics": metrics.clone(prefix=f"{phase}/")
                for phase in ["train", "val", "test"]
            }
        )
        self.logged_predictions = []

    def forward(self, inputs: torch.Tensor) -> torch.Tensor:
        return self.model(inputs)

    def _step(
        self, phase: str, batch: dict[str, torch.Tensor], *args, **kwargs
    ) -> torch.Tensor:
        inputs = batch["inputs"]
        targets = batch["targets"]
        input_lengths = batch["input_lengths"]
        target_lengths = batch["target_lengths"]
        N = len(input_lengths)  # batch_size

        emissions = self.forward(inputs)

        # Shrink input lengths by an amount equivalent to the conv encoder's
        # temporal receptive field to compute output activation lengths for CTCLoss.
        # NOTE: This assumes the encoder doesn't perform any temporal downsampling
        # such as by striding.
        T_diff = inputs.shape[0] - emissions.shape[0]
        emission_lengths = input_lengths - T_diff

        loss = self.ctc_loss(
            log_probs=emissions,  # (T, N, num_classes)
            targets=targets.transpose(0, 1),  # (T, N) -> (N, T)
            input_lengths=emission_lengths,  # (N,)
            target_lengths=target_lengths,  # (N,)
        )

        l1_reg = 0.0
        l2_reg = 0.0
        for name, param in self.named_parameters():
            if param.requires_grad and "bias" not in name:
                l1_reg += param.abs().sum()  # L1 regularization (Lasso)
                l2_reg += param.pow(2).sum()  # L2 regularization (Ridge)

        loss = loss + self.l1_lambda * l1_reg + self.l2_lambda * l2_reg
        # l2_reg = 0.0
        # for name, param in self.named_parameters():
        #     if param.requires_grad and "bias" not in name:
        #         l2_reg += param.pow(2).sum()
        # loss = loss + self.l2_lambda * l2_reg

        # Decode emissions
        predictions = self.decoder.decode_batch(
            emissions=emissions.detach().cpu().numpy(),
            emission_lengths=emission_lengths.detach().cpu().numpy(),
        )

        for i, pred in enumerate(predictions):
            self.logged_predictions.append({
                "epoch": self.current_epoch,
                "batch_idx": kwargs.get("batch_idx", 0),
                "sample_idx": i,
                "prediction": pred
            })

        # Update metrics
        metrics = self.metrics[f"{phase}_metrics"]
        targets = targets.detach().cpu().numpy()
        target_lengths = target_lengths.detach().cpu().numpy()
        for i in range(N):
            # Unpad targets (T, N) for batch entry
            target = LabelData.from_labels(targets[: target_lengths[i], i])
            metrics.update(prediction=predictions[i], target=target)

        self.log(f"{phase}/loss", loss, batch_size=N, sync_dist=True)

        return loss

    def _epoch_end(self, phase: str) -> None:
        metrics = self.metrics[f"{phase}_metrics"]
        self.log_dict(metrics.compute(), sync_dist=True)
        metrics.reset()
        if self.logged_predictions:
            df = pd.DataFrame(self.logged_predictions)
            df.to_csv(f"{self.logger.log_dir}/{phase}_predictions_epoch_{self.current_epoch}.csv", index=False)
            self.logged_predictions = [] # Clear after saving

        if self.logged_predictions:
            df = pd.DataFrame(self.logged_predictions)
            df.to_csv(f"{self.logger.log_dir}/{phase}_predictions_epoch_{self.current_epoch}.csv", index=False)
            self.logged_predictions = []  # Clear after saving

        if self.logged_predictions:
            df = pd.DataFrame(self.logged_predictions)
            df.to_csv(
                f"{self.logger.log_dir}/{phase}_predictions_epoch_{self.current_epoch}.csv", index=False)
            self.logged_predictions = []  # Clear after saving

    def training_step(self, *args, **kwargs) -> torch.Tensor:
        return self._step("train", *args, **kwargs)

    def validation_step(self, *args, **kwargs) -> torch.Tensor:
        return self._step("val", *args, **kwargs)

    def test_step(self, *args, **kwargs) -> torch.Tensor:
        return self._step("test", *args, **kwargs)

    def on_train_epoch_end(self) -> None:
        self._epoch_end("train")

    def on_validation_epoch_end(self) -> None:
        self._epoch_end("val")

    def on_test_epoch_end(self) -> None:
        self._epoch_end("test")

    def configure_optimizers(self) -> dict[str, Any]:
        return utils.instantiate_optimizer_and_scheduler(
            self.parameters(),
            optimizer_config=self.hparams.optimizer,
            lr_scheduler_config=self.hparams.lr_scheduler,
        )
