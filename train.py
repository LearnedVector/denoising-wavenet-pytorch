from pathlib import Path
from typing import Dict, Sequence, Tuple

import numpy as np
import torch
from numpy import ndarray
from torch import nn, Tensor
import torch.optim as optim
from torch.utils.data import DataLoader
from torchsummary import summary
from tqdm import tqdm

from hparams import hp
# from adamwr import AdamW, CosineLRWithRestarts
from dataset import MulchWavDataset, Normalization
from model import DWaveNet
from tbXwriter import CustomWriter
from utils import (arr2str,
                   print_to_file,
                   )


class Trainer:
    def __init__(self, path_state_dict=''):
        module = eval(hp.model_name)

        self.model = module(**getattr(hp, hp.model_name))
        if isinstance(hp.criterion_names, str):
            criterion_names = (hp.criterion_names,)
        else:
            criterion_names = hp.criterion_names
        self.criterions = [
            eval(f'nn.{name}')(reduction='sum') for name in criterion_names
        ]

        self.__init_device(hp.device, hp.out_device)

        self.writer: CustomWriter = None

        self.optimizer = optim.Adam(self.model.parameters(),
                                    lr=hp.learning_rate,
                                    weight_decay=hp.weight_decay,
                                    )

        # Load State Dict
        if path_state_dict:
            st_model, st_optim = torch.load(path_state_dict, self.in_device)
            try:
                if isinstance(self.model, nn.DataParallel):
                    self.model.module.load_state_dict(st_model)
                else:
                    self.model.load_state_dict(st_model)
                self.optimizer.load_state_dict(st_optim)
            except RuntimeError:
                raise Exception('The model is different from the state dict.')

        path_summary = hp.logdir / 'summary.txt'
        if not path_summary.exists():
            print_to_file(
                path_summary,
                summary,
                (self.model, hp.dummy_input_size),
                dict(device=self.str_device[:4])
            )
            with (hp.logdir / 'hparams.txt').open('w') as f:
                f.write(repr(hp))

    def __init_device(self, device, out_device):
        if device == 'cpu':
            self.in_device = torch.device('cpu')
            self.out_device = torch.device('cpu')
            self.str_device = 'cpu'
            return

        # device type: List[int]
        if type(device) == int:
            device = [device]
        elif type(device) == str:
            device = [int(device.replace('cuda:', ''))]
        else:  # sequence of devices
            if type(device[0]) != int:
                device = [int(d.replace('cuda:', '')) for d in device]

        self.in_device = torch.device(f'cuda:{device[0]}')

        if len(device) > 1:
            if type(out_device) == int:
                self.out_device = torch.device(f'cuda:{out_device}')
            else:
                self.out_device = torch.device(out_device)
            self.str_device = ', '.join([f'cuda:{d}' for d in device])

            self.model = nn.DataParallel(self.model,
                                         device_ids=device,
                                         output_device=self.out_device)
        else:
            self.out_device = self.in_device
            self.str_device = str(self.in_device)

        self.model.cuda(self.in_device)
        for criterion in self.criterions:
            criterion.cuda(self.out_device)

        torch.cuda.set_device(self.in_device)

    def _pre(self, data: Dict[str, Tensor], dataset: MulchWavDataset) \
            -> Tuple[Tensor, Tensor]:
        # B, C, T
        x = data['x']
        y = data['y']

        x = x.to(self.in_device, non_blocking=True)
        y = y.to(self.out_device, non_blocking=True)

        x = dataset.norm_in.normalize_(x)
        y = dataset.norm_out.normalize_(y)

        return x, y

    @torch.no_grad()
    def _post_one(self, output: Tensor, Ts: ndarray,
                  idx: int, normalization: Normalization) -> Dict[str, ndarray]:
        one = output[idx, :, :Ts[idx]]  # C, T

        one = normalization.denormalize_(one)
        one = one.cpu().numpy()

        return dict(out=one)

    def _calc_loss(self, y: Tensor, output: Tensor, T_ys: Sequence[int]) -> Tensor:
        loss = torch.zeros(len(self.criterions), device=self.out_device)
        if (T_ys == T_ys[0]).all():
            for i, criterion in enumerate(self.criterions):
                if hp.weight_loss[i] == 0:
                    continue
                loss[i] = hp.weight_loss[i] * criterion(output, y) / T_ys[0]
        else:
            loss = torch.zeros(len(self.criterions), device=self.out_device)
            for i, criterion in enumerate(self.criterions):
                if hp.weight_loss[i] == 0:
                    continue
                for T, item_y, item_out in zip(T_ys, y, output):
                    loss[i] += criterion(item_out[..., :T], item_y[..., :T]) / int(T)
                loss[i] *= hp.weight_loss[i]

        return loss

    def train(self, loader_train: DataLoader, loader_valid: DataLoader, logdir: Path,
              first_epoch=0):

        n_train_data = len(loader_train.dataset)
        # Learning Rates Scheduler
        scheduler = optim.lr_scheduler.CosineAnnealingWarmRestarts(
            self.optimizer,
            last_epoch=first_epoch - 1,
            **hp.scheduler
        )
        avg_loss = torch.zeros(len(self.criterions), device=self.out_device)

        self.writer = CustomWriter(str(logdir), group='valid', purge_step=first_epoch)

        # self.writer.add_graph(
        #     self.model.module if isinstance(self.model, nn.DataParallel) else self.model,
        #     torch.zeros(2, *hp.dummy_input_size),
        #     # operator_export_type='RAW',
        # )

        # Start Training
        for epoch in range(first_epoch, hp.n_epochs):

            print()
            pbar = tqdm(loader_train, desc=f'epoch {epoch:3d}', postfix='[]', dynamic_ncols=True)

            for i_iter, data in enumerate(pbar):
                # get data
                x, y = self._pre(data, loader_train.dataset)  # B, C, T
                T_ys = data['T_ys']

                i_first = 0
                loss_t = None
                for t in range(0, y.shape[-1], hp.l_target):
                    self.optimizer.zero_grad()
                    while i_first < y.shape[0] and t >= T_ys[i_first]:
                        i_first += 1
                    seg_y = y[i_first:, :, t: t + hp.l_target]
                    if seg_y.shape[-1] < 5:
                        break
                    seg_x = x[i_first:, :, t: t + hp.l_input]
                    seg_T_ys = np.clip(T_ys[i_first:] - t,
                                       a_min=None, a_max=hp.l_target)

                    # forward
                    output = self.model(seg_x)[..., :seg_y.shape[-1]]  # B, C, T

                    loss_t = self._calc_loss(seg_y, output, seg_T_ys)
                    loss_t_sum = loss_t.sum()

                    # backward
                    loss_t_sum.backward()
                    self.optimizer.step()

                    # print
                    avg_loss += loss_t.detach_()
                    del loss_t_sum, seg_x, seg_y, output

                scheduler.step(epoch + i_iter * hp.batch_size / n_train_data)

                loss_t_last_np = loss_t.cpu().numpy() / len(T_ys)
                pbar.set_postfix_str(arr2str(loss_t_last_np, ndigits=1))

            avg_loss /= n_train_data
            tag = 'loss/train'
            self.writer.add_scalar(tag, avg_loss.sum().item(), epoch)
            if len(self.criterions) > 1:
                for i, (n, ll) in enumerate(zip(hp.criterion_names, avg_loss)):
                    self.writer.add_scalar(f'{tag}/{i + 1}_{n}', ll.item(), epoch)

            # Validation
            self.validate(loader_valid, logdir, epoch)

            # save loss & model
            if epoch % hp.period_save_state == hp.period_save_state - 1:
                torch.save(
                    (self.model.module.state_dict()
                     if isinstance(self.model, nn.DataParallel)
                     else self.model.state_dict(),
                     self.optimizer.state_dict(),
                     ),
                    logdir / f'{hp.model_name}_{epoch}.pt'
                )
        self.writer.close()

    @torch.no_grad()
    def validate(self, loader: DataLoader, logdir: Path, epoch: int):
        """ Evaluate the performance of the model.

        :param loader: DataLoader to use.
        :param logdir: path of the result files.
        :param epoch:
        """

        self.model.eval()

        avg_loss = torch.zeros(len(self.criterions), device=self.out_device)

        pbar = tqdm(loader, desc='validate ', postfix='[0]', dynamic_ncols=True)
        for i_iter, data in enumerate(pbar):
            # get data
            x, y = self._pre(data, loader.dataset)  # B, C, F, T
            T_ys = data['T_ys']

            # forward
            output = self.model(x)  # [..., :y.shape[-1]]

            # loss
            loss = self._calc_loss(y, output, T_ys)
            avg_loss += loss

            # print
            loss_np = loss.cpu().numpy() / len(T_ys)
            pbar.set_postfix_str(arr2str(loss_np, ndigits=1))

            # write summary
            if i_iter == 0:
                out_one = self._post_one(output, T_ys, 0, loader.dataset.norm_out)

                if not self.writer.reused_sample:
                    one_sample = MulchWavDataset.decollate_padded(data, 0)
                else:
                    one_sample = dict()
                self.writer.write_one(epoch, **out_one, **one_sample)

        avg_loss /= len(loader.dataset)
        tag = 'loss/valid'
        self.writer.add_scalar(tag, avg_loss.sum().item(), epoch)
        if len(self.criterions) > 1:
            for idx, (n, ll) in enumerate(zip(hp.criterion_names, avg_loss)):
                self.writer.add_scalar(f'{tag}/{idx + 1}. {n}', ll.item(), epoch)

        self.model.train()

        return avg_loss

    @torch.no_grad()
    def test(self, loader: DataLoader, logdir: Path):
        group = logdir.name.split('_')[0]

        self.writer = CustomWriter(str(logdir), group=group)

        avg_measure = None
        self.model.eval()

        pbar = tqdm(loader, desc=group, dynamic_ncols=True)
        for i_iter, data in enumerate(pbar):
            # get data
            x, y = self._pre(data, loader.dataset)  # B, C, T
            T_ys = data['T_ys']

            # forward
            output = self.model(x)  # [..., :y.shape[-1]]

            # write summary
            one_sample = MulchWavDataset.decollate_padded(data, 0)  # F, T, C

            out_one = self._post_one(output, T_ys, 0, loader.dataset.norm_out)

            measure = self.writer.write_one(i_iter, **out_one, **one_sample)
            if avg_measure is None:
                avg_measure = measure
            else:
                avg_measure += measure

            # print
            str_measure = arr2str(measure).replace('\n', '; ')
            pbar.write(str_measure)

        self.model.train()

        avg_measure /= len(loader.dataset)

        self.writer.add_text(f'{group}/Average Measure/Proposed', str(avg_measure[0]))
        self.writer.add_text(f'{group}/Average Measure/Reverberant', str(avg_measure[1]))
        self.writer.close()  # Explicitly close

        print()
        str_avg_measure = arr2str(avg_measure).replace('\n', '; ')
        print(f'Average: {str_avg_measure}')
