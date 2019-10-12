# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import torch
import torch.nn.functional as F
import torch.nn as nn

from transformer.layers import Decoder, EncoderDecoder
from levenhtein_transformer.utils import _apply_del_words, _apply_ins_masks, _apply_ins_words, \
    _get_del_targets, _get_ins_targets, fill_tensors as _fill, skip_tensors as _skip


class LevenshteinEncodeDecoder(EncoderDecoder):
    def __init__(self, encoder, decoder, src_embed, tgt_embed, generator, PAD, BOS, EOS, UNK):
        super(LevenshteinEncodeDecoder, self).__init__(encoder, decoder, src_embed, tgt_embed, generator)
        self.pad = PAD
        self.eos = EOS
        self.bos = BOS
        self.unk = UNK

    def forward(self, src: torch.Tensor, x: torch.Tensor, src_mask: torch.Tensor, x_mask: torch.Tensor,
                tgt: torch.Tensor):

        assert tgt is not None, "Forward function only supports training."

        # encoding
        encoder_out = self.encode(src, src_mask)

        # features = self.decode(encoder_out, src_mask, x, x_mask)
        # generate training labels for insertion
        # word_pred_tgt, word_pred_tgt_masks, ins_targets
        word_pred_tgt, word_pred_tgt_masks, ins_targets = _get_ins_targets(x, tgt, self.pad, self.unk)

        ins_targets = ins_targets.clamp(min=0, max=255)  # for safe prediction
        mask_ins_masks = x[:, 1:].ne(self.pad)

        mask_ins_out = self.decoder.forward_mask_ins(encoder_out, src_mask, self.tgt_embed(x), x_mask)

        word_ins_out = self.decoder.forward_word_ins(encoder_out, src_mask, self.tgt_embed(x), x_mask)

        # make online prediction
        word_predictions = F.log_softmax(word_ins_out, dim=-1).max(2)[1]
        word_predictions.masked_scatter_(~word_pred_tgt_masks, tgt[~word_pred_tgt_masks])

        # generate training labels for deletion
        word_del_targets = _get_del_targets(word_predictions, tgt, self.pad)
        word_del_out = self.decoder.forward_word_del(encoder_out, src_mask, self.tgt_embed(word_predictions), x_mask)

        return {
            "mask_ins_out": mask_ins_out,
            "mask_ins_tgt": ins_targets,
            "mask_ins_mask": mask_ins_masks,
            "word_ins_out": word_ins_out,
            "word_ins_tgt": tgt,
            "word_ins_mask": word_pred_tgt_masks,
            "word_del_out": word_del_out,
            "word_del_tgt": word_del_targets,
            "word_del_mask": word_predictions.ne(self.pad),
        }

    def forward_encoder(self, src, src_mask):
        return self.self.encode(src, src_mask)

    def forward_decoder(self, decoder_out, encoder_out, eos_penalty=0.0, max_ratio=None):

        output_tokens = decoder_out["output_tokens"]
        output_scores = decoder_out["output_scores"]
        attn = decoder_out["attn"]

        if max_ratio is None:
            max_lens = output_tokens.new(output_tokens.size(0)).fill_(255)
        else:
            max_lens = ((~encoder_out["encoder_padding_mask"]).sum(1) * max_ratio).clamp(min=10)

        # delete words
        # do not delete tokens if it is <s> </s>
        can_del_word = output_tokens.ne(self.pad).sum(1) > 2
        if can_del_word.sum() != 0:  # we cannot delete, skip
            word_del_out, word_del_attn = self.decoder.forward_word_del(
                _skip(output_tokens, can_del_word), _skip(encoder_out, can_del_word)
            )
            word_del_score = F.log_softmax(word_del_out, 2)
            word_del_pred = word_del_score.max(-1)[1].bool()

            _tokens, _scores, _attn = _apply_del_words(
                output_tokens[can_del_word],
                output_scores[can_del_word],
                word_del_attn,
                word_del_pred,
                self.pad,
                self.bos,
                self.eos,
            )

            output_tokens = _fill(output_tokens, can_del_word, _tokens, self.pad)
            output_scores = _fill(output_scores, can_del_word, _scores, 0)
            attn = _fill(attn, can_del_word, _attn, 0.)

        # insert placeholders
        can_ins_mask = output_tokens.ne(self.pad).sum(1) < max_lens
        if can_ins_mask.sum() != 0:
            mask_ins_out, _ = self.decoder.forward_mask_ins(_skip(output_tokens, can_ins_mask),
                                                            _skip(encoder_out, can_ins_mask))
            mask_ins_score = F.log_softmax(mask_ins_out, 2)
            if eos_penalty > 0.0:
                mask_ins_score[:, :, 0] -= eos_penalty
            mask_ins_pred = mask_ins_score.max(-1)[1]
            mask_ins_pred = torch.min(
                mask_ins_pred, max_lens[:, None].expand_as(mask_ins_pred)
            )

            _tokens, _scores = _apply_ins_masks(
                output_tokens[can_ins_mask],
                output_scores[can_ins_mask],
                mask_ins_pred,
                self.pad,
                self.unk,
                self.eos,
            )
            output_tokens = _fill(output_tokens, can_ins_mask, _tokens, self.pad)
            output_scores = _fill(output_scores, can_ins_mask, _scores, 0)

        # insert words
        can_ins_word = output_tokens.eq(self.unk).sum(1) > 0
        if can_ins_word.sum() != 0:
            word_ins_out, word_ins_attn = self.decoder.forward_word_ins(
                _skip(output_tokens, can_ins_word), _skip(encoder_out, can_ins_word)
            )
            word_ins_score = F.log_softmax(word_ins_out, 2)
            word_ins_pred = word_ins_score.max(-1)[1]

            _tokens, _scores = _apply_ins_words(
                output_tokens[can_ins_word],
                output_scores[can_ins_word],
                word_ins_pred,
                word_ins_score,
                self.unk,
            )

            output_tokens = _fill(output_tokens, can_ins_word, _tokens, self.pad)
            output_scores = _fill(output_scores, can_ins_word, _scores, 0)
            attn = _fill(attn, can_ins_word, word_ins_attn, 0.)

        # delete some unnecessary paddings
        cut_off = output_tokens.ne(self.pad).sum(1).max()
        output_tokens = output_tokens[:, :]
        output_scores = output_scores[:, :cut_off]
        attn = None if attn is None else attn[:, :cut_off, :]
        return {
            "output_tokens": output_tokens,
            "output_scores": output_scores,
            "attn": attn,
        }

    def initialize_output_tokens(self, encoder_out, src_tokens):
        initial_output_tokens = src_tokens.new_zeros(src_tokens.size(0), 2)
        initial_output_tokens[:, 0] = self.bos
        initial_output_tokens[:, 1] = self.eos

        initial_output_scores = initial_output_tokens.new_zeros(
            *initial_output_tokens.size()
        ).type_as(encoder_out["encoder_out"])
        return {
            "output_tokens": initial_output_tokens,
            "output_scores": initial_output_scores,
            "attn": None,
        }


class LevenshteinDecoder(Decoder):
    def __init__(self, layer, n, output_embed_dim):
        super(LevenshteinDecoder, self).__init__(layer, n)

        # embeds the number of tokens to be inserted, max 256
        self.embed_mask_ins = nn.Linear(output_embed_dim * 2, 256)

        # embeds either 0 or 1
        self.embed_word_del = nn.Linear(output_embed_dim, 2)

    def forward_mask_ins(self, encoder_out: torch.Tensor, encoder_out_mask: torch.Tensor, x: torch.Tensor,
                         x_mask: torch.Tensor):
        features = self.forward(x, encoder_out, encoder_out_mask, x_mask)
        # creates pairs of consecutive words, so if the words are marked by their indices (0, 1, ... n):
        # [
        #   [0, 1],
        #   [1, 2],
        #   ...
        #   [n-1, n],
        # ]

        features_cat = torch.cat([features[:, :-1, :], features[:, 1:, :]], 2)
        out = self.embed_mask_ins(features_cat)
        return F.log_softmax(out, dim=2)

    def forward_word_ins(self, encoder_out: torch.Tensor, encoder_out_mask: torch.Tensor, x: torch.Tensor,
                         x_mask: torch.Tensor):
        features = self.forward(x, encoder_out, encoder_out_mask, x_mask)
        return features

    def forward_word_del(self, encoder_out: torch.Tensor, encoder_out_mask: torch.Tensor, x: torch.Tensor,
                         x_mask: torch.Tensor):
        features = self.forward(x, encoder_out, encoder_out_mask, x_mask)
        out = self.embed_word_del(features)
        return F.log_softmax(out, dim=2)

    def forward_word_del_mask_ins(self, encoder_out: torch.Tensor, encoder_out_mask: torch.Tensor, x: torch.Tensor,
                                  x_mask: torch.Tensor):
        # merge the word-deletion and mask insertion into one operation,
        features = self.forward(x, encoder_out, encoder_out_mask, x_mask)
        features_cat = torch.cat([features[:, :-1, :], features[:, 1:, :]], 2)
        f_word_del = F.log_softmax(self.embed_word_del(features), dim=2)
        f_mask_ins = F.log_softmax(self.embed_mask_ins(features_cat), dim=2)
        return f_word_del, f_mask_ins