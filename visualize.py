#!/usr/bin/env python3
"""
Script to visualize the patterns in a SoftPatterns model based on their
highest-scoring spans in the dev set.
"""
import argparse
from collections import OrderedDict
import sys
import torch
from torch.autograd import Variable
from data import vocab_from_text, read_embeddings, read_docs, read_labels
from soft_patterns import MaxPlusSemiring, fixed_var, Batch, argmax, SoftPatternClassifier, ProbSemiring
from util import chunked

SCORE_IDX = 0
START_IDX_IDX = 1
END_IDX_IDX = 2


def get_nearest_neighbors(w, embeddings, k=1000):
    """
    For every transition in every pattern, gets the word with the highest
    score for that transition.
    Only looks at the first `k` words in the vocab (makes sense, assuming
    they're sorted by descending frequency).
    """
    return argmax(torch.mm(w, embeddings[:k, :]))


def visualize_patterns(model,
                       batch_size,
                       dev_set=None,
                       dev_text=None,
                       k_best=5):
    num_patterns = model.total_num_patterns
    pattern_length = model.max_pattern_length

    scores = get_top_scoring_sequences(model, dev_set, batch_size)

    # 1 above main diagonal
    # TODO: truncate to appropriate lengths, using model.end_states
    diags = model.diags.view(num_patterns, model.num_diags, pattern_length, model.word_dim).data
    biases = model.bias.view(num_patterns, model.num_diags, pattern_length).data
    self_loop_norms = torch.norm(diags[:, 0, :, :], 2, 2)
    self_loop_biases = biases[:, 0, :]
    main_diag_norms = torch.norm(diags[:, 1, :, :], 2, 2)
    main_diag_biases = biases[:, 1, :]
    epsilons = model.get_eps_value().data

    nearest_neighbors = \
        get_nearest_neighbors(
            model.diags.data,
            torch.FloatTensor(model.embeddings).t()
        ).view(
            num_patterns,
            model.num_diags,
            pattern_length
        )[:, 1, :]

    for p in range(num_patterns):
        p_len = model.end_states[p].data[0]
        k_best_doc_idxs = \
            sorted(
                range(len(dev_set)),
                key=lambda doc_idx: scores[p, doc_idx, SCORE_IDX],
                reverse=True  # high-scores first
            )[:k_best]

        def span_text(doc_idx):
            score = round(scores[p, doc_idx, SCORE_IDX], 3)
            start_idx = int(scores[p, doc_idx][START_IDX_IDX])
            end_idx = int(scores[p, doc_idx, END_IDX_IDX])
            return score, " ".join(dev_text[doc_idx][start_idx:end_idx])

        print("Pattern:", p)
        for k, d in enumerate(k_best_doc_idxs):
            score, text = span_text(d)
            print(k, score, text)
        print("self-loop norms: ", [round(x, 3) for x in self_loop_norms[p, :p_len]])
        print("self-loop biases:", [round(x, 3) for x in self_loop_biases[p, :p_len]])
        print("fwd 1 norms: ", [round(x, 3) for x in main_diag_norms[p, :p_len - 1]])
        print("fwd 1 biases:", [round(x, 3) for x in main_diag_biases[p, :p_len - 1]])
        print("fwd 1 nearest neighbors", [model.vocab[x] for x in nearest_neighbors[p, :p_len - 1]])
        print("epsilons:", [round(x, 3) for x in epsilons[p, :p_len]])
        print()


def get_top_scoring_sequences(self, dev_set, max_batch_size):
    """
    Get top scoring sequence in doc for this pattern (for interpretation purposes)
    """
    rig = MaxPlusSemiring
    debug_print = int(100 / max_batch_size) + 1

    # max_scores[pattern_idx, doc_idx, 0] = `score` of best span
    # max_scores[pattern_idx, doc_idx, 1] = `start_token_idx` of best span
    # max_scores[pattern_idx, doc_idx, 2] = `end_token_idx + 1` of best span
    max_scores = rig.zero(self.total_num_patterns, len(dev_set), 3)

    eps_value = self.get_eps_value()

    for batch_idx, chunk in enumerate(chunked(dev_set, max_batch_size)):
        if batch_idx % debug_print == debug_print - 1:
            print(".", end="", flush=True)

        batch = Batch([x for x, y in chunk], self.embeddings, self.to_cuda)

        transition_matrices = self.get_transition_matrices(batch)

        batch_size = batch.size()  # the last batch might be smaller than `max_batch_size`
        num_patterns = self.total_num_patterns

        # will be used for `restart_padding` also
        zero_padding = self.to_cuda(fixed_var(self.semiring.zero(batch_size, num_patterns, 1)))

        batch_end_state_idxs = self.end_states.expand(batch_size, num_patterns, 1)

        for start_token_idx in range(batch.max_doc_len):
            hiddens = self.to_cuda(Variable(self.semiring.zero(batch_size,
                                                               num_patterns,
                                                               self.max_pattern_length)))
            # set start state (0) to 1 for each pattern in each doc
            hiddens[:, :, 0] = self.to_cuda(self.semiring.one(batch_size, num_patterns, 1))
            # iterate over every span starting at `start_token_idx`
            for token_idx_in_span, transition_matrix in enumerate(transition_matrices[start_token_idx:]):
                end_token_idx = start_token_idx + token_idx_in_span
                hiddens = self.transition_once(eps_value,
                                               hiddens,
                                               transition_matrix,
                                               zero_padding,
                                               restart_padding=zero_padding)

                # Score for each pattern is the value at the end state
                scores = torch.gather(hiddens, 2, batch_end_state_idxs).view(batch_size, num_patterns)
                # but only count score when we're not already past the end of the doc
                active_doc_idxs = torch.nonzero(torch.gt(batch.doc_lens, end_token_idx)).squeeze()
                for pattern_idx in range(num_patterns):
                    for doc_idx in active_doc_idxs:
                        score = scores[doc_idx, pattern_idx].data[0]
                        if score >= max_scores[pattern_idx, doc_idx, SCORE_IDX]:
                            max_scores[pattern_idx, doc_idx, SCORE_IDX] = score
                            max_scores[pattern_idx, doc_idx, START_IDX_IDX] = start_token_idx
                            max_scores[pattern_idx, doc_idx, END_IDX_IDX] = end_token_idx + 1

    print()
    return max_scores


# TODO: refactor duplicate code with soft_patterns.py
def main(args):
    print(args)

    pattern_specs = OrderedDict([int(y) for y in x.split(":")] for x in args.patterns.split(","))
    n = args.num_train_instances
    mlp_hidden_dim = args.mlp_hidden_dim
    num_mlp_layers = args.num_mlp_layers

    dev_vocab = vocab_from_text(args.vd)
    print("Dev vocab size:", len(dev_vocab))

    vocab, embeddings, word_dim = \
        read_embeddings(args.embedding_file, dev_vocab)

    dev_input, dev_text = read_docs(args.vd, vocab)
    dev_labels = read_labels(args.vl)
    dev_data = list(zip(dev_input, dev_labels))
    if n is not None:
        dev_data = dev_data[:n]

    num_classes = len(set(dev_labels))
    print("num_classes:", num_classes)

    semiring = MaxPlusSemiring if args.maxplus else ProbSemiring

    model = SoftPatternClassifier(pattern_specs,
                                  mlp_hidden_dim,
                                  num_mlp_layers,
                                  num_classes,
                                  embeddings,
                                  vocab,
                                  semiring,
                                  args.gpu,
                                  False)

    if args.gpu:
        model.to_cuda()

    # Loading model
    state_dict = torch.load(args.input_model)
    model.load_state_dict(state_dict)

    visualize_patterns(model, args.batch_size, dev_data, dev_text)

    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    parser.add_argument("-e", "--embedding_file", help="Word embedding file", required=True)
    parser.add_argument("-p", "--patterns",
                        help="Pattern lengths and numbers: a comma separated list of length:number pairs",
                        default="5:50,4:50,3:50,2:50")
    parser.add_argument("-d", "--mlp_hidden_dim", help="MLP hidden dimension", type=int, default=10)
    parser.add_argument("-b", "--batch_size", help="Batch size", type=int, default=100)
    parser.add_argument("-y", "--num_mlp_layers", help="Number of MLP layers", type=int, default=2)
    parser.add_argument("-n", "--num_train_instances", help="Number of training instances", type=int, default=None)
    parser.add_argument("-g", "--gpu", help="Use GPU", action='store_true')
    parser.add_argument("--input_model", help="Input model (to run test and not train)", required=True)
    parser.add_argument("--vd", help="Validation data file", required=True)
    parser.add_argument("--vl", help="Validation labels file", required=True)
    parser.add_argument("--maxplus",
                        help="Use max-plus semiring instead of plus-times",
                        default=False, action='store_true')

    sys.exit(main(parser.parse_args()))