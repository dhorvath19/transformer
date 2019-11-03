import wandb
import torch
from nltk.translate.bleu_score import corpus_bleu as nltk_corpus_bleu
from sacrebleu import corpus_bleu
from levenhtein_transformer.config import config
from levenhtein_transformer.utils import initialize_output_tokens
from utils import vector_to_sentence


def validate(model, iterator, SRC, TGT, EOS_WORD, eos, bos, max_decode_iter=10, logging=False, is_test=False):
    model.eval()
    print('Validating...')
    # TODO: parallel Bleu calculation

    hypotheses_tokenized = []
    references_tokenized = []

    hypotheses = []
    references = []

    table = wandb.Table(columns=["Text", "Predicted Label", "True Label"])

    for i, batch in enumerate(iterator):
        src = batch.src
        tgt = batch.trg

        src_sentences = [vector_to_sentence(src[i, :], SRC, EOS_WORD, start_from=0) for i in range(src.size(0))]
        tgt_sentences = [vector_to_sentence(tgt[i, :], TGT, EOS_WORD) for i in range(tgt.size(0))]

        decode_iter = 0
        prev_out = initialize_output_tokens(batch.trg, bos=bos, eos=eos)
        encoder_out = model.encode(batch.src, batch.src_mask)
        out = torch.tensor([[]])
        while decode_iter < max_decode_iter:
            out = model.decode(encoder_out, prev_out, batch.src_mask, max_ratio=config['max_decoder_ratio'])

            if out.tolist() == prev_out.tolist():
                break

            decode_iter += 1
            prev_out = out

        out_sentences = [vector_to_sentence(out[i, :], TGT, EOS_WORD) for i in range(out.size(0))]

        hypotheses_tokenized += [out_sentence.split(' ') for out_sentence in out_sentences]
        references_tokenized += [[tgt_sentence.split(' ')] for tgt_sentence in tgt_sentences]

        hypotheses += out_sentences
        references += tgt_sentences

        # sentence_pairs = list(zip(tgt_sentences, out_sentences))
        table.add_data(src_sentences[0], tgt_sentences[0], out_sentences[0])
        if logging:
            print(f"Source: {src_sentences[0]}\nTarget: {tgt_sentences[0]}\nPrediction: {out_sentences[0]}\n")

        # batch_bleu = [sentence_bleu([sentence_pair[0].split(' ')], sentence_pair[1].split(' '))
        #                     for sentence_pair in sentence_pairs]

        # batch_sacrebleu = [corpus_bleu(sentence_pair[1], sentence_pair[0]).score
        #                     for sentence_pair in sentence_pairs]

        # batch_bleus += batch_bleu
        # batch_sacrebleus += batch_sacrebleu

        # print(f'Batch {i} | Bleu: {np.array(batch_bleu).mean() * 100} | Sacrebleu: {np.array(batch_sacrebleu).mean()}')

    # bleu_score = np.array(batch_bleus).mean() * 100
    # sacrebleu_score = np.array(batch_sacrebleus).mean()
    corpus_sacrebleu_score = corpus_bleu(hypotheses, [references])
    corpus_bleu_score = nltk_corpus_bleu(references_tokenized, hypotheses_tokenized)
    wandb.log({"Test samples" if is_test else "Validation samples": table})
    print(f'Corpus bleu: {corpus_bleu_score * 100} | Corpus sacrebleu: {corpus_sacrebleu_score.score}')
    return corpus_sacrebleu_score.score
