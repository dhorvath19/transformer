import torch.nn as nn
from copy import deepcopy
from transformer.layers import PositionalEncoding, EncoderLayer, Encoder,\
    Decoder, DecoderLayer, Generator, Embeddings
from transformer.sublayers import MultiHeadedAttention, PositionwiseFeedForward
from levenhtein_transformer.layers import LevenshteinEncodeDecoder


def LevenshteinTransformerModel(src_vocab, tgt_vocab,  PAD, BOS, EOS, UNK, d_model=512, N=6, h=8, d_ff=2048, dropout=0.1):
    attn = MultiHeadedAttention(h, d_model)
    ff = PositionwiseFeedForward(d_model, d_ff, dropout)
    position = PositionalEncoding(d_model, dropout)
    model = LevenshteinEncodeDecoder(
        Encoder(EncoderLayer(d_model, deepcopy(attn), deepcopy(ff), dropout), N),
        Decoder(DecoderLayer(d_model, deepcopy(attn), deepcopy(attn), deepcopy(ff), dropout), N),
        nn.Sequential(Embeddings(d_model, src_vocab), deepcopy(position)),
        nn.Sequential(Embeddings(d_model, tgt_vocab), deepcopy(position)),
        Generator(d_model, tgt_vocab),
        PAD, BOS, EOS, UNK
    )
    # This was important from their code.
    # Initialize parameters with Glorot / fan_avg.
    for p in model.parameters():
        if p.dim() > 1:
            nn.init.xavier_uniform_(p)

    return model
