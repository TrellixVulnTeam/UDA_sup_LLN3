# Copyright 2019 SanghunYun, Korea University.
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
import pdb
import argparse

import torch
import torch.nn as nn
import torch.nn.functional as F

import models
import train
from load_data import load_data
from utils.utils import set_seeds, get_device, _get_device, torch_device_one
from utils import optim, configuration
import numpy as np

parser = argparse.ArgumentParser(description='PyTorch UDA Training')

parser.add_argument('--seed', default=42, type=int)
parser.add_argument('--lr', default=0.000025, type=float)
parser.add_argument('--warmup', default=0.1, type=float)
parser.add_argument('--do_lower_case', default=True, type=bool)
parser.add_argument('--mode', default='train_eval', type=str)
parser.add_argument('--model_cfg', default='config/bert_base.json', type=str)

parser.add_argument('--uda_mode', action='store_true')
parser.add_argument('--mixmatch_mode', action='store_true')
parser.add_argument('--uda_test_mode', action='store_true')
parser.add_argument('--sup_mixup', action='store_true')
parser.add_argument('--unsup_mixup', action='store_true')

parser.add_argument('--total_steps', default=10000, type=int)
parser.add_argument('--check_after', default=4999, type=int)
parser.add_argument('--early_stopping', default=10, type=int)
parser.add_argument('--max_seq_length', default=128, type=int)
parser.add_argument('--train_batch_size', default=10, type=int)
parser.add_argument('--eval_batch_size', default=32, type=int)

parser.add_argument('--no_sup_loss', action='store_true')
parser.add_argument('--no_unsup_loss', action='store_true')

#UDA
parser.add_argument('--unsup_ratio', default=1, type=int)
parser.add_argument('--uda_coeff', default=1, type=int)
parser.add_argument('--tsa', default='linear_schedule', type=str)
parser.add_argument('--uda_softmax_temp', default=0.85, type=float)
parser.add_argument('--uda_confidence_thresh', default=0.45, type=float)
parser.add_argument('--unsup_criterion', default='KL', type=str)

#MixMatch
parser.add_argument('--alpha', default=0.75, type=float)
parser.add_argument('--lambda_u', default=75, type=int)
parser.add_argument('--T', default=0.5, type=float)
parser.add_argument('--ema_decay', default=0.999, type=float)

parser.add_argument('--data_parallel', default=True, type=bool)
parser.add_argument('--need_prepro', default=False, type=bool)
parser.add_argument('--sup_data_dir', default='data/imdb_sup_train.txt', type=str)
parser.add_argument('--unsup_data_dir', default="data/imdb_unsup_train.txt", type=str)
parser.add_argument('--eval_data_dir', default="data/imdb_sup_test.txt", type=str)

parser.add_argument('--model_file', default="", type=str)
parser.add_argument('--pretrain_file', default="BERT_Base_Uncased/bert_model.ckpt", type=str)
parser.add_argument('--vocab', default="BERT_Base_Uncased/vocab.txt", type=str)
parser.add_argument('--task', default="imdb", type=str)

parser.add_argument('--save_steps', default=100, type=int)
parser.add_argument('--check_steps', default=250, type=int)
parser.add_argument('--results_dir', default="results", type=str)

parser.add_argument('--is_position', default=False, type=bool)

cfg, unknown = parser.parse_known_args()

def linear_rampup(current, rampup_length):
    if rampup_length == 0:
        return 1.0
    else:
        current = np.clip(current / rampup_length, 0.0, 1.0)
        return float(current)

class SemiLoss(object):
    def __call__(self, outputs_x, targets_x, outputs_u, targets_u, current_step, lambda_u, total_steps):
        probs_u = torch.softmax(outputs_u, dim=1)

        Lx = -torch.mean(torch.sum(F.log_softmax(outputs_x, dim=1) * targets_x, dim=1))
        Lu = torch.mean((probs_u - targets_u)**2)

        return Lx, Lu, lambda_u * linear_rampup(current_step, total_steps)

class WeightEMA(object):
    def __init__(self, cfg, model, ema_model, alpha=0.999):
        self.model = model
        self.ema_model = ema_model
        self.alpha = alpha
        self.cfg = cfg

        params = list(model.state_dict().values())
        ema_params = list(ema_model.state_dict().values())

        self.params = list(map(lambda x: x.float(), params))
        self.ema_params = list(map(lambda x: x.float(), ema_params))
        self.wd = 0.02 * self.cfg.lr

        for param, ema_param in zip(self.params, self.ema_params):
            param.data.copy_(ema_param.data)

    def step(self):
        one_minus_alpha = 1.0 - self.alpha
        for param, ema_param in zip(self.params, self.ema_params):
            ema_param.mul_(self.alpha)
            ema_param.add_(param * one_minus_alpha)
            # customized weight decay
            param.mul_(1 - self.wd)

# TSA
def get_tsa_thresh(schedule, global_step, num_train_steps, start, end):
    training_progress = torch.tensor(float(global_step) / float(num_train_steps))
    print(schedule)
    if schedule == 'linear_schedule':
        threshold = training_progress
    elif schedule == 'exp_schedule':
        scale = 5
        threshold = torch.exp((training_progress - 1) * scale)
    elif schedule == 'log_schedule':
        scale = 5
        threshold = 1 - torch.exp((-training_progress) * scale)
    output = threshold * (end - start) + start
    return output.to(_get_device())

def interleave_offsets(batch, nu):
    groups = [batch // (nu + 1)] * (nu + 1)
    for x in range(batch - sum(groups)):
        groups[-x - 1] += 1
    offsets = [0]
    for g in groups:
        offsets.append(offsets[-1] + g)
    assert offsets[-1] == batch
    return offsets


def interleave(xy, batch):
    nu = len(xy) - 1
    offsets = interleave_offsets(batch, nu)
    xy = [[v[offsets[p]:offsets[p + 1]] for p in range(nu + 1)] for v in xy]
    for i in range(1, nu + 1):
        xy[0][i], xy[i][i] = xy[i][i], xy[0][i]
    return [torch.cat(v, dim=0) for v in xy]


def main():
    # Load Configuration
    model_cfg = configuration.model.from_json(cfg.model_cfg)        # BERT_cfg
    set_seeds(cfg.seed)

    # Load Data & Create Criterion
    data = load_data(cfg)

    if cfg.uda_mode or cfg.mixmatch_mode:
        data_iter = [data.sup_data_iter(), data.unsup_data_iter()] if cfg.mode=='train' \
            else [data.sup_data_iter(), data.unsup_data_iter(), data.eval_data_iter()]  # train_eval
    else:
        data_iter = [data.sup_data_iter()]

    ema_optimizer = None
    ema_model = None
    model = models.Classifier(model_cfg, len(data.TaskDataset.labels))


    if cfg.uda_mode:
        if cfg.unsup_criterion == 'KL':
            unsup_criterion = nn.KLDivLoss(reduction='none')
        else:
            unsup_criterion = nn.MSELoss(reduction='none')
        sup_criterion = nn.CrossEntropyLoss(reduction='none')
        optimizer = optim.optim4GPU(cfg, model)
    elif cfg.mixmatch_mode:
        train_criterion = SemiLoss()
        criterion = nn.CrossEntropyLoss()
        optimizer = torch.optim.Adam(model.parameters(), lr=cfg.lr)
        ema_model = models.Classifier(model_cfg, len(data.TaskDataset.labels))
        for param in ema_model.parameters():
            param.detach_()
        ema_optimizer= WeightEMA(cfg, model, ema_model, alpha=cfg.ema_decay)
    else:
        sup_criterion = nn.CrossEntropyLoss(reduction='none')
        optimizer = optim.optim4GPU(cfg, model)
    
    # Create trainer
    trainer = train.Trainer(cfg, model, data_iter, optimizer, get_device(), ema_model, ema_optimizer)

    # loss functions
    def get_mixmatch_loss_two(model, sup_batch, unsup_batch, global_step):
        input_ids, segment_ids, input_mask, label_ids = sup_batch
        if unsup_batch:
            ori_input_ids, ori_segment_ids, ori_input_mask, \
            aug_input_ids, aug_segment_ids, aug_input_mask  = unsup_batch

        batch_size = input_ids.shape[0]
        sup_size = label_ids.shape[0]

        with torch.no_grad():
            # compute guessed labels of unlabel samples
            outputs_u = model(input_ids=ori_input_ids, segment_ids=ori_segment_ids, input_mask=ori_input_mask)
            outputs_u2 = model(input_ids=aug_input_ids, segment_ids=aug_segment_ids, input_mask=aug_input_mask)
            p = (torch.softmax(outputs_u, dim=1) + torch.softmax(outputs_u2, dim=1)) / 2
            pt = p**(1/cfg.uda_softmax_temp)
            targets_u = pt / pt.sum(dim=1, keepdim=True)
            targets_u = targets_u.detach()
            targets_u = torch.cat((targets_u, targets_u), dim=0)

            # confidence-based masking
            if cfg.uda_confidence_thresh != -1:
                unsup_loss_mask = torch.max(targets_u, dim=-1)[0] > cfg.uda_confidence_thresh
                unsup_loss_mask = unsup_loss_mask.type(torch.float32)
            else:
                unsup_loss_mask = torch.ones(len(logits) - sup_size, dtype=torch.float32)
            unsup_loss_mask = unsup_loss_mask.to(_get_device())

        input_ids = torch.cat((input_ids, ori_input_ids, aug_input_ids), dim=0)
        seg_ids = torch.cat((segment_ids, ori_segment_ids, aug_segment_ids), dim=0)
        input_mask = torch.cat((input_mask, ori_input_mask, aug_input_mask), dim=0)

        logits = model(input_ids, seg_ids, input_mask)

        logits_x = logits[:sup_size]
        logits_u = logits[sup_size:]

        sup_loss = sup_criterion(logits_x, label_ids)
        if cfg.tsa:
            tsa_thresh = get_tsa_thresh(cfg.tsa, global_step, cfg.total_steps, start=1./logits.shape[-1], end=1)
            larger_than_threshold = torch.exp(-sup_loss) > tsa_thresh   # prob = exp(log_prob), prob > tsa_threshold
            # larger_than_threshold = torch.sum(  F.softmax(pred[:sup_size]) * torch.eye(num_labels)[sup_label_ids]  , dim=-1) > tsa_threshold
            loss_mask = torch.ones_like(label_ids, dtype=torch.float32) * (1 - larger_than_threshold.type(torch.float32))
            sup_loss = torch.sum(sup_loss * loss_mask, dim=-1) / torch.max(torch.sum(loss_mask, dim=-1), torch_device_one())
        else:
            sup_loss = torch.mean(sup_loss)

        log_probs_u = F.log_softmax(logits_u, dim=1)
        unsup_loss = torch.sum(unsup_criterion(log_probs_u, targets_u), dim=-1)
        unsup_loss = torch.sum(unsup_loss * unsup_loss_mask, dim=-1) / torch.max(torch.sum(unsup_loss_mask, dim=-1), torch_device_one())

        final_loss = sup_loss + cfg.uda_coeff*unsup_loss
        return final_loss, sup_loss, unsup_loss
        
    def get_mixmatch_loss_short(model, sup_batch, unsup_batch, global_step):
        input_ids, segment_ids, input_mask, label_ids = sup_batch
        if unsup_batch:
            ori_input_ids, ori_segment_ids, ori_input_mask, \
            aug_input_ids, aug_segment_ids, aug_input_mask  = unsup_batch

        batch_size = input_ids.shape[0]
        sup_size = input_ids.size(0)

        # Transform label to one-hot
        label_ids = torch.zeros(batch_size, 2).scatter_(1, label_ids.cpu().view(-1,1), 1).cuda()

        with torch.no_grad():
            # compute guessed labels of unlabel samples
            outputs_u = model(input_ids=ori_input_ids, segment_ids=ori_segment_ids, input_mask=ori_input_mask)
            outputs_u2 = model(input_ids=aug_input_ids, segment_ids=aug_segment_ids, input_mask=aug_input_mask)
            p = (torch.softmax(outputs_u, dim=1) + torch.softmax(outputs_u2, dim=1)) / 2
            pt = p**(1/cfg.uda_softmax_temp)
            targets_u = pt / pt.sum(dim=1, keepdim=True)
            targets_u = targets_u.detach()

        concat_input_ids = torch.cat((input_ids, ori_input_ids, aug_input_ids), dim=0)
        concat_seg_ids = torch.cat((segment_ids, ori_segment_ids, aug_segment_ids), dim=0)
        concat_input_mask = torch.cat((input_mask, ori_input_mask, aug_input_mask), dim=0)
        concat_targets = torch.cat((label_ids, targets_u, targets_u), dim=0)


        hidden = model(
            input_ids=concat_input_ids,
            segment_ids=concat_seg_ids,
            input_mask=concat_input_mask,
            output_h=True
        )


        l = np.random.beta(cfg.alpha, cfg.alpha)
        l = max(l, 1-l)

        idx = torch.randperm(hidden.size(0))

        h_a, h_b = hidden, hidden[idx]
        target_a, target_b = concat_targets, concat_targets[idx]

        mixed_h = l * h_a + (1 - l) * h_b
        mixed_target = l * target_a + (1 - l) * target_b

        logits = model(input_h=mixed_h)

        logits_x = logits[:sup_size]
        logits_u = logits[sup_size:]

        targets_x = mixed_target[:sup_size]
        targets_u = mixed_target[sup_size:]

        #Lx, Lu, w = train_criterion(logits_x, targets_x, logits_u, targets_u, epoch+batch_idx/cfg.val_iteration)
        Lx, Lu, w = train_criterion(logits_x, targets_x, logits_u, targets_u, global_step, cfg.lambda_u, cfg.total_steps)

        final_loss = Lx + w * Lu

        return final_loss, Lx, Lu

    def get_mixmatch_loss(model, sup_batch, unsup_batch, global_step):
        input_ids, segment_ids, input_mask, label_ids = sup_batch
        if unsup_batch:
            ori_input_ids, ori_segment_ids, ori_input_mask, \
            aug_input_ids, aug_segment_ids, aug_input_mask  = unsup_batch

        batch_size = input_ids.shape[0]

        # Transform label to one-hot
        label_ids = torch.zeros(batch_size, 2).scatter_(1, label_ids.cpu().view(-1,1), 1).cuda()

        with torch.no_grad():
            # compute guessed labels of unlabel samples
            outputs_u = model(input_ids=ori_input_ids, segment_ids=ori_segment_ids, input_mask=ori_input_mask)
            outputs_u2 = model(input_ids=aug_input_ids, segment_ids=aug_segment_ids, input_mask=aug_input_mask)
            p = (torch.softmax(outputs_u, dim=1) + torch.softmax(outputs_u2, dim=1)) / 2
            pt = p**(1/cfg.uda_softmax_temp)
            targets_u = pt / pt.sum(dim=1, keepdim=True)
            targets_u = targets_u.detach()

        concat_input_ids = [input_ids, ori_input_ids, aug_input_ids]
        concat_seg_ids = [segment_ids, ori_segment_ids, aug_segment_ids]
        concat_input_mask = [input_mask, ori_input_mask, aug_input_mask]
        concat_targets = [label_ids, targets_u, targets_u]

        # interleave labeled and unlabed samples between batches to get correct batchnorm calculation 
        int_input_ids = interleave(concat_input_ids, batch_size)
        int_seg_ids = interleave(concat_seg_ids, batch_size)
        int_input_mask = interleave(concat_input_mask, batch_size)
        int_targets = interleave(concat_targets, batch_size)

        h_zero = model(
            input_ids=int_input_ids[0],
            segment_ids=int_seg_ids[0],
            input_mask=int_input_mask[0], 
            output_h=True
        )

        h_one = model(
            input_ids=int_input_ids[1],
            segment_ids=int_seg_ids[1],
            input_mask=int_input_mask[1], 
            output_h=True
        )

        h_two = model(
            input_ids=int_input_ids[2],
            segment_ids=int_seg_ids[2],
            input_mask=int_input_mask[2], 
            output_h=True
        )

        int_h = torch.cat([h_zero, h_one, h_two], dim=0)
        int_targets = torch.cat([int_targets[0], int_targets[1], int_targets[2]])

        l = np.random.beta(cfg.alpha, cfg.alpha)
        l = max(l, 1-l)

        idx = torch.randperm(int_h.size(0))

        h_a, h_b = int_h, int_h[idx]
        target_a, target_b = int_targets, int_targets[idx]

        mixed_int_h = l * h_a + (1 - l) * h_b
        mixed_int_target = l * target_a + (1 - l) * target_b

        mixed_int_h = list(torch.split(mixed_int_h, batch_size))
        mixed_int_targets = list(torch.split(mixed_int_target, batch_size))

        logits_one = model(input_h=mixed_int_h[0])
        logits_two = model(input_h=mixed_int_h[1])
        logits_three = model(input_h=mixed_int_h[2])

        logits = [logits_one, logits_two, logits_three]


        # put interleaved samples back
        logits = interleave(logits, batch_size)
        targets = interleave(mixed_int_targets, batch_size)

        logits_x = logits[0]
        logits_u = torch.cat(logits[1:], dim=0)

        targets_x = targets[0]
        targets_u = torch.cat(targets[1:], dim=0)

        #Lx, Lu, w = train_criterion(logits_x, targets_x, logits_u, targets_u, epoch+batch_idx/cfg.val_iteration)
        Lx, Lu, w = train_criterion(logits_x, targets_x, logits_u, targets_u, global_step, cfg.lambda_u, cfg.total_steps)

        final_loss = Lx + w * Lu

        return final_loss, Lx, Lu

    def get_uda_mixup_loss(model, sup_batch, unsup_batch, global_step):
        # batch
        input_ids, segment_ids, input_mask, og_label_ids = sup_batch

        # convert label_ids to hot vector
        sup_size = input_ids.size(0)
        label_ids = torch.zeros(sup_size, 2).scatter_(1, og_label_ids.cpu().view(-1,1), 1).cuda()

        if unsup_batch:
            ori_input_ids, ori_segment_ids, ori_input_mask, \
            aug_input_ids, aug_segment_ids, aug_input_mask  = unsup_batch

            input_ids = torch.cat((input_ids, aug_input_ids), dim=0)
            segment_ids = torch.cat((segment_ids, aug_segment_ids), dim=0)
            input_mask = torch.cat((input_mask, aug_input_mask), dim=0)

        # logits
        hidden = model(
            input_ids=input_ids,
            segment_ids=segment_ids,
            input_mask=input_mask,
            output_h=True
        )

        sup_hidden = hidden[:sup_size]
        unsup_hidden = hidden[sup_size:]

        l = np.random.beta(cfg.alpha, cfg.alpha)

        sup_l = max(l, 1-l) if cfg.sup_mixup else 1
        unsup_l = max(l, 1-l) if cfg.unsup_mixup else 1

        sup_idx = torch.randperm(sup_hidden.size(0))
        sup_h_a, sup_h_b = sup_hidden, sup_hidden[sup_idx]
        sup_label_a, sup_label_b = label_ids, label_ids[sup_idx]
        mixed_sup_h = sup_l * sup_h_a + (1 - sup_l) * sup_h_b
        mixed_sup_label = sup_l * sup_label_a + (1 - sup_l) * sup_label_b

        unsup_idx = torch.randperm(unsup_hidden.size(0))
        unsup_h_a, unsup_h_b = unsup_hidden, unsup_hidden[unsup_idx]
        mixed_unsup_h = unsup_l * unsup_h_a + (1 - unsup_l) * unsup_h_b

        hidden = torch.cat([mixed_sup_h, mixed_unsup_h], dim=0)

        logits = model(input_h=hidden)

        sup_logits = logits[:sup_size]
        unsup_logits = logits[sup_size:]

        # sup loss
        sup_loss = -torch.sum(F.log_softmax(sup_logits, dim=1) * mixed_sup_label, dim=1)

        if cfg.tsa:
            tsa_thresh = get_tsa_thresh(cfg.tsa, global_step, cfg.total_steps, start=1./logits.shape[-1], end=1)
            larger_than_threshold = torch.exp(-sup_loss) > tsa_thresh   # prob = exp(log_prob), prob > tsa_threshold
            # larger_than_threshold = torch.sum(  F.softmax(pred[:sup_size]) * torch.eye(num_labels)[sup_label_ids]  , dim=-1) > tsa_threshold
            loss_mask = torch.ones_like(og_label_ids, dtype=torch.float32) * (1 - larger_than_threshold.type(torch.float32))
            sup_loss = torch.sum(sup_loss * loss_mask, dim=-1) / torch.max(torch.sum(loss_mask, dim=-1), torch_device_one())
        else:
            sup_loss = torch.mean(sup_loss)

        # unsup loss
        if unsup_batch:
            # ori
            with torch.no_grad():
                ori_logits = model(ori_input_ids, ori_segment_ids, ori_input_mask)
                ori_prob   = F.softmax(ori_logits, dim=-1)    # KLdiv target
                # ori_log_prob = F.log_softmax(ori_logits, dim=-1)

                ori_prob_a, ori_prob_b = ori_prob, ori_prob[unsup_idx]
                mixed_ori_prob = unsup_l * ori_prob_a + (1 - unsup_l) * ori_prob_b

                # confidence-based masking
                if cfg.uda_confidence_thresh != -1:
                    unsup_loss_mask = torch.max(mixed_ori_prob, dim=-1)[0] > cfg.uda_confidence_thresh
                    unsup_loss_mask = unsup_loss_mask.type(torch.float32)
                else:
                    unsup_loss_mask = torch.ones(len(logits) - sup_size, dtype=torch.float32)
                unsup_loss_mask = unsup_loss_mask.to(_get_device())

            # aug
            # softmax temperature controlling
            uda_softmax_temp = cfg.uda_softmax_temp if cfg.uda_softmax_temp > 0 else 1.
            aug_log_prob = F.log_softmax(unsup_logits / uda_softmax_temp, dim=-1)

            # KLdiv loss
            """
                nn.KLDivLoss (kl_div)
                input : log_prob (log_softmax)
                target : prob    (softmax)
                https://pytorch.org/docs/stable/nn.html

                unsup_loss is divied by number of unsup_loss_mask
                it is different from the google UDA official
                The official unsup_loss is divided by total
                https://github.com/google-research/uda/blob/master/text/uda.py#L175
            """
            unsup_loss = torch.sum(unsup_criterion(aug_log_prob, mixed_ori_prob), dim=-1)
            unsup_loss = torch.sum(unsup_loss * unsup_loss_mask, dim=-1) / torch.max(torch.sum(unsup_loss_mask, dim=-1), torch_device_one())
            final_loss = sup_loss + cfg.uda_coeff*unsup_loss

            return final_loss, sup_loss, unsup_loss
        return sup_loss, None, None

    def get_loss(model, sup_batch, unsup_batch, global_step):
        # logits -> prob(softmax) -> log_prob(log_softmax)

        # batch
        input_ids, segment_ids, input_mask, label_ids = sup_batch
        if unsup_batch:
            ori_input_ids, ori_segment_ids, ori_input_mask, \
            aug_input_ids, aug_segment_ids, aug_input_mask = unsup_batch

            input_ids = torch.cat((input_ids, aug_input_ids), dim=0)
            segment_ids = torch.cat((segment_ids, aug_segment_ids), dim=0)
            input_mask = torch.cat((input_mask, aug_input_mask), dim=0)
            
        # logits
        hidden = model(
            input_ids=input_ids, 
            segment_ids=segment_ids, 
            input_mask=input_mask,
            output_h=True
        )
        logits = model(input_h=hidden)

        # sup loss
        sup_size = label_ids.shape[0]            
        sup_loss = sup_criterion(logits[:sup_size], label_ids)  # shape : train_batch_size
        if cfg.tsa:
            tsa_thresh = get_tsa_thresh(cfg.tsa, global_step, cfg.total_steps, start=1./logits.shape[-1], end=1)
            larger_than_threshold = torch.exp(-sup_loss) > tsa_thresh   # prob = exp(log_prob), prob > tsa_threshold
            # larger_than_threshold = torch.sum(  F.softmax(pred[:sup_size]) * torch.eye(num_labels)[sup_label_ids]  , dim=-1) > tsa_threshold
            loss_mask = torch.ones_like(label_ids, dtype=torch.float32) * (1 - larger_than_threshold.type(torch.float32))
            sup_loss = torch.sum(sup_loss * loss_mask, dim=-1) / torch.max(torch.sum(loss_mask, dim=-1), torch_device_one())
        else:
            sup_loss = torch.mean(sup_loss)

        # unsup loss
        if unsup_batch:
            # ori
            with torch.no_grad():
                ori_logits = model(ori_input_ids, ori_segment_ids, ori_input_mask)
                ori_prob   = F.softmax(ori_logits, dim=-1)    # KLdiv target
                # temp control
                #ori_prob = ori_prob**(1/cfg.uda_softmax_temp)

                # confidence-based masking
                if cfg.uda_confidence_thresh != -1:
                    unsup_loss_mask = torch.max(ori_prob, dim=-1)[0] > cfg.uda_confidence_thresh
                    unsup_loss_mask = unsup_loss_mask.type(torch.float32)
                else:
                    unsup_loss_mask = torch.ones(len(logits) - sup_size, dtype=torch.float32)
                unsup_loss_mask = unsup_loss_mask.to(_get_device())
                    
            # aug
            uda_softmax_temp = cfg.uda_softmax_temp if cfg.uda_softmax_temp > 0 else 1.
            aug_log_prob = F.log_softmax(logits[sup_size:] / uda_softmax_temp, dim=-1)

            # KLdiv loss
            """
                nn.KLDivLoss (kl_div)
                input : log_prob (log_softmax)
                target : prob    (softmax)
                https://pytorch.org/docs/stable/nn.html

                unsup_loss is divied by number of unsup_loss_mask
                it is different from the google UDA official
                The official unsup_loss is divided by total
                https://github.com/google-research/uda/blob/master/text/uda.py#L175
            """
            unsup_loss = torch.sum(unsup_criterion(aug_log_prob, ori_prob), dim=-1)
            unsup_loss = torch.sum(unsup_loss * unsup_loss_mask, dim=-1) / torch.max(torch.sum(unsup_loss_mask, dim=-1), torch_device_one())

            final_loss = sup_loss + cfg.uda_coeff*unsup_loss

            return final_loss, sup_loss, unsup_loss
        return sup_loss, None, None

    # evaluation
    def get_acc(model, batch):
        # input_ids, segment_ids, input_mask, label_id, sentence = batch
        input_ids, segment_ids, input_mask, label_id = batch
        logits = model(input_ids, segment_ids, input_mask)
        _, label_pred = logits.max(1)

        result = (label_pred == label_id).float()
        accuracy = result.mean()
        # output_dump.logs(sentence, label_pred, label_id)    # output dump

        return accuracy, result

    if cfg.mode == 'train':
        trainer.train(get_loss, None, cfg.model_file, cfg.pretrain_file)

    if cfg.mode == 'train_eval':
        if cfg.mixmatch_mode:
            trainer.train(get_mixmatch_loss_short, get_acc, cfg.model_file, cfg.pretrain_file)
        elif cfg.uda_test_mode:
            trainer.train(get_uda_mixup_loss, get_acc, cfg.model_file, cfg.pretrain_file)
        else:
            trainer.train(get_loss, get_acc, cfg.model_file, cfg.pretrain_file)

    if cfg.mode == 'eval':
        results = trainer.eval(get_acc, cfg.model_file, None)
        total_accuracy = torch.cat(results).mean().item()
        print('Accuracy :' , total_accuracy)


if __name__ == '__main__':
    main()