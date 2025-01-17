# Copyright 2018 Dong-Hyun Lee, Kakao Brain.
# (Strongly inspired by original Google BERT code and Hugging Face's code)
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


""" Transformer Model Classes & Config Class """
import pdb
import math
import json
from typing import NamedTuple
import random

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from utils.utils import split_last, merge_last, mixup_op


class Config(NamedTuple):
    "Configuration for BERT model"
    vocab_size: int = None # Size of Vocabulary
    dim: int = 768 # Dimension of Hidden Layer in Transformer Encoder
    n_layers: int = 12 # Numher of Hidden Layers
    n_heads: int = 12 # Numher of Heads in Multi-Headed Attention Layers
    dim_ff: int = 768*4 # Dimension of Intermediate Layers in Positionwise Feedforward Net
    #activ_fn: str = "gelu" # Non-linear Activation Function Type in Hidden Layers
    p_drop_hidden: float = 0.1 # Probability of Dropout of various Hidden Layers
    p_drop_attn: float = 0.1 # Probability of Dropout of Attention Layers
    max_len: int = 512 # Maximum Length for Positional Embeddings
    n_segments: int = 2 # Number of Sentence Segments

    @classmethod
    def from_json(cls, file):
        return cls(**json.load(open(file, "r")))


def gelu(x):
    "Implementation of the gelu activation function by Hugging Face"
    return x * 0.5 * (1.0 + torch.erf(x / math.sqrt(2.0)))


class LayerNorm(nn.Module):
    "A layernorm module in the TF style (epsilon inside the square root)."
    def __init__(self, cfg, variance_epsilon=1e-12):
        super().__init__()
        self.gamma = nn.Parameter(torch.ones(cfg.dim)) # (preload)
        self.beta  = nn.Parameter(torch.zeros(cfg.dim)) # (preload)
        self.variance_epsilon = variance_epsilon

    def forward(self, x):
        u = x.mean(-1, keepdim=True)
        s = (x - u).pow(2).mean(-1, keepdim=True)
        x = (x - u) / torch.sqrt(s + self.variance_epsilon)
        return self.gamma * x + self.beta


class Embeddings(nn.Module):
    "The embedding module from word, position and token_type embeddings."
    def __init__(self, cfg):
        super().__init__()
        self.tok_embed = nn.Embedding(cfg.vocab_size, cfg.dim) # (preload)
        self.pos_embed = nn.Embedding(cfg.max_len, cfg.dim) # (preload)
        self.seg_embed = nn.Embedding(cfg.n_segments, cfg.dim) # (preload)

        self.norm = LayerNorm(cfg) # (preload)
        self.drop = nn.Dropout(cfg.p_drop_hidden)

    def forward(
        self, x, seg, mixup, shuffle_idx, l, clone_ids, mixup_layer, simple_pad, no_grad_clone
        ):
        seq_len = x.size(1)
        pos = torch.arange(seq_len, dtype=torch.long, device=x.device)
        pos = pos.unsqueeze(0).expand_as(x) # (S,) -> (1, S) -> (B, S)  이렇게 외부에서 생성되는 값

        token_e = self.tok_embed(x)
        pos_e = self.pos_embed(pos)
        seg_e = self.seg_embed(seg)

        if mixup and 'word' in mixup:
            if simple_pad:
                if mixup_layer == 0:
                    token_e = mixup_op(token_e, l, shuffle_idx)
            else:
                if no_grad_clone:
                    with torch.no_grad():
                        c_token_e = self.tok_embed(clone_ids)
                else:
                    c_token_e = self.tok_embed(clone_ids)

                if mixup_layer == 0:
                    embeds_a, embeds_b = token_e, c_token_e[shuffle_idx]
                    token_e = l * embeds_a + (1-l) * embeds_b
                else:
                    e = token_e + pos_e + seg_e
                    ec = c_token_e + pos_e + seg_e

                    h = self.drop(self.norm(e))

                    if no_grad_clone:
                        with torch.no_grad():
                            hc = self.drop(self.norm(ec))
                    else:
                        hc = self.drop(self.norm(ec))

                    return h, hc

        e = token_e + pos_e + seg_e
        return self.drop(self.norm(e)), None


class MultiHeadedSelfAttention(nn.Module):
    """ Multi-Headed Dot Product Attention """
    def __init__(self, cfg):
        super().__init__()
        self.proj_q = nn.Linear(cfg.dim, cfg.dim) #(preload)
        self.proj_k = nn.Linear(cfg.dim, cfg.dim) #(preload)
        self.proj_v = nn.Linear(cfg.dim, cfg.dim) #(preload)
        self.drop = nn.Dropout(cfg.p_drop_attn)
        self.scores = None # for visualization
        self.n_heads = cfg.n_heads

    def forward(self, x, mask):
        """
        x, q(query), k(key), v(value) : (B(batch_size), S(seq_len), D(dim))
        mask : (B(batch_size) x S(seq_len))
        * split D(dim) into (H(n_heads), W(width of head)) ; D = H * W
        """
        # (B, S, D) -proj-> (B, S, D) -split-> (B, S, H, W) -trans-> (B, H, S, W)
        q, k, v = self.proj_q(x), self.proj_k(x), self.proj_v(x)
        q, k, v = (split_last(x, (self.n_heads, -1)).transpose(1, 2)
                   for x in [q, k, v])
        # (B, H, S, W) @ (B, H, W, S) -> (B, H, S, S) -softmax-> (B, H, S, S)
        scores = q @ k.transpose(-2, -1) / np.sqrt(k.size(-1))
        if mask is not None:
            mask = mask[:, None, None, :].float()
            scores -= 10000.0 * (1.0 - mask)
        scores = self.drop(F.softmax(scores, dim=-1))
        # (B, H, S, S) @ (B, H, S, W) -> (B, H, S, W) -trans-> (B, S, H, W)
        h = (scores @ v).transpose(1, 2).contiguous()
        # -merge-> (B, S, D)
        h = merge_last(h, 2)
        self.scores = scores
        return h


class PositionWiseFeedForward(nn.Module):
    """ FeedForward Neural Networks for each position """
    def __init__(self, cfg):
        super().__init__()
        self.fc1 = nn.Linear(cfg.dim, cfg.dim_ff) #(preload)
        self.fc2 = nn.Linear(cfg.dim_ff, cfg.dim) #(preload)
        #self.activ = lambda x: activ_fn(cfg.activ_fn, x)

    def forward(self, x):
        # (B, S, D) -> (B, S, D_ff) -> (B, S, D)
        return self.fc2(gelu(self.fc1(x)))


class Block(nn.Module):
    """ Transformer Block """
    def __init__(self, cfg):
        super().__init__()
        self.attn = MultiHeadedSelfAttention(cfg)
        self.proj = nn.Linear(cfg.dim, cfg.dim) # (preload)
        self.norm1 = LayerNorm(cfg) #(preload)
        self.pwff = PositionWiseFeedForward(cfg) #(preload)
        self.norm2 = LayerNorm(cfg) #(preload)
        self.drop = nn.Dropout(cfg.p_drop_hidden)

    def forward(self, x, mask):
        h = self.attn(x, mask)
        h = self.norm1(x + self.drop(self.proj(h)))
        h = self.norm2(h + self.drop(self.pwff(h)))
        return h


class Transformer(nn.Module):
    """ Transformer with Self-Attentive Blocks"""
    def __init__(self, cfg):
        super().__init__()
        self.embed = Embeddings(cfg)
        self.blocks = nn.ModuleList([Block(cfg) for _ in range(cfg.n_layers)])   # h 번 반복

    def forward(
            self, 
            x=None, seg=None, mask=None,
            clone_ids=None, mixup=None, shuffle_idx=None, l=1, 
            mixup_layer=-1, simple_pad=False, no_grad_clone=False
        ):
        h, hc = self.embed(
            x, seg, mixup, shuffle_idx, l, clone_ids, mixup_layer, simple_pad, no_grad_clone
        )

        layer = 1
        for block in self.blocks:
            h = block(h, mask)
            
            if hc is not None:
                if no_grad_clone:
                    with torch.no_grad():
                        hc = block(hc, mask)
                else:
                    hc = block(hc, mask)

            if mixup_layer == layer and (mixup == 'word' or mixup == 'word_cls'):
                if hc is not None:
                    h_a, h_b = h, hc[shuffle_idx]
                    h = l * h_a + (1-l) * h_b
                    hc = None
                else:
                    h = mixup_op(h, l, shuffle_idx)


            layer += 1
        return h


class Classifier(nn.Module):
    """ Classifier with Transformer """
    def __init__(self, cfg, n_labels):
        super().__init__()
        self.transformer = Transformer(cfg)
        self.fc = nn.Linear(cfg.dim, cfg.dim)
        self.activ = nn.Tanh()
        self.drop = nn.Dropout(cfg.p_drop_hidden)
        self.classifier = nn.Linear(cfg.dim, n_labels)
        self.layers = cfg.n_layers

    def forward(
            self,
            input_ids=None, 
            segment_ids=None, 
            input_mask=None, 
            output_h=False, 
            input_h=None,
            mixup=None,
            shuffle_idx=None,
            clone_ids=None,
            l=1,
            manifold_mixup=None,
            simple_pad=False,
            no_grad_clone=False
        ):
        if input_h is None:

            if mixup == 'word':
                mixup_layer = random.randint(0, self.layers) if manifold_mixup else 0
            elif mixup == 'word_cls':
                mixup_layer = random.randint(0, self.layers+1) if manifold_mixup else 0
            elif mixup == 'cls':
                mixup_layer = self.layers + 1
            elif mixup == 'word_cls_only':
                mixup_layer = random.choice([0, self.layers + 1])
            else:
                mixup_layer = -1

            h = self.transformer(
                x=input_ids, seg=segment_ids, mask=input_mask, 
                clone_ids=clone_ids, mixup=mixup, shuffle_idx=shuffle_idx, l=l,
                mixup_layer = mixup_layer, simple_pad=simple_pad, no_grad_clone=no_grad_clone
            )

            # only use the first h in the sequence
            # h shape = [16, 128, 768]
            # h[h:, 0] = [16, 768]

            pooled_h = self.activ(self.fc(h[:, 0]))

            # pooled_h = [16, 768]

            if mixup_layer == self.layers+1:
                pooled_h = mixup_op(pooled_h, l, shuffle_idx)

            if output_h:
                return pooled_h
        else:
            pooled_h = input_h
        logits = self.classifier(self.drop(pooled_h))
        return logits
