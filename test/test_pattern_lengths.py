#!/usr/bin/env python

import unittest

import numpy as np
import torch
from torch import cat, mm, FloatTensor
from torch.autograd import Variable
from util import nub
from itertools import chain, islice



import soft_patterns
from data import read_embeddings, read_docs
from soft_patterns import fixed_var, SoftPatternClassifier
from test.settings import EMBEDDINGS_FILENAME, DATA_FILENAME, MODEL_FILENAME, PATTERN_SPECS, MLP_HIDDEN_DIM, \
    NUM_MLP_LAYERS, NUM_CLASSES, SEMIRING, GPU, DROPOUT, LEGACY


UNK_TOKEN = "*UNK*"

torch.manual_seed(100)
np.random.seed(100)


def forward(model, batch):
    """ old version, for reference """
    transition_matrices = get_transition_matrices(model, batch)
    scores = Variable(model.semiring.zero(batch.size(), model.total_num_patterns).type(model.dtype))

    # to add start state for each word in the document.
    restart_padding = fixed_var(model.semiring.one(model.total_num_patterns, 1), model.gpu)
    zero_padding = fixed_var(model.semiring.zero(model.total_num_patterns, 1), model.gpu)
    eps_value = \
        model.semiring.times(
            model.semiring.from_float(model.epsilon_scale),
            model.semiring.from_float(model.epsilon)
        )
    self_loop_scale = model.get_self_loop_scale()

    all_hiddens = []

    # Different documents in batch
    for doc_index in range(len(transition_matrices)):
        # Start state
        hiddens = Variable(model.semiring.zero(model.total_num_patterns, model.max_pattern_length).type(model.dtype))
        hiddens[:, 0] = model.semiring.one(model.total_num_patterns, 1).type(model.dtype)
        all_hiddens.append(hiddens)
        # For each token in document
        for transition_matrix_val in transition_matrices[doc_index]:
            hiddens = transition_once(model, eps_value,
                                            hiddens,
                                            self_loop_scale,
                                            transition_matrix_val,
                                            zero_padding,
                                            restart_padding)
            all_hiddens.append(hiddens)
            # Score is the final column of hiddens
            start = 0
            for pattern_len, num_patterns in model.pattern_specs.items():
                end_state = -1 - (model.max_pattern_length - pattern_len)
                end_pattern_idx = start + num_patterns
                scores[doc_index, start:end_pattern_idx] = \
                    model.semiring.plus(
                        scores[doc_index, start:end_pattern_idx],
                        hiddens[start:end_pattern_idx, end_state]
                    )  # mm(hidden, self.final)  # TODO: change if learning final state
                start += num_patterns

    return model.mlp.forward(scores), transition_matrices, all_hiddens

def get_transition_matrices(model, batch):
    mm_res = mm(model.diags, batch.embeddings_matrix)
    transition_probs = \
        model.semiring.from_float(mm_res + model.bias.expand(model.bias.size()[0], mm_res.size()[1])).t()

    if model.gpu:
        transition_probs = transition_probs.cuda()

    if model.dropout:
        transition_probs = model.dropout(transition_probs)

    # transition matrix for each document in batch
    transition_matrices = [
        [
            transition_probs[word_index, :].contiguous().view(
                model.total_num_patterns, model.num_diags, model.max_pattern_length
            )
            for word_index in doc
        ]
        for doc in batch.docs
    ]
    return transition_matrices

def transition_once(model,
                    eps_value,
                    hiddens,
                    self_loop_scale,
                    transition_matrix_val,
                    zero_padding,
                    restart_padding):
    # Adding epsilon transitions (don't consume a token, move forward one state)
    # We do this before self-loops and single-steps.
    # We only allow one epsilon transition in a row.
    hiddens = \
        model.semiring.plus(
            hiddens,
            cat((zero_padding,
                 model.semiring.times(
                     hiddens[:, :-1],
                     eps_value  # doesn't depend on token, just state
                 )), 1))
    # single steps forward (consume a token, move forward one state)
    # print(hiddens[:, -1])
    # print("RESTART old: ", restart_padding)

    # print("ef", restart_padding.size(), hiddens[:, -1].size(), transition_matrix_val[:, 1, :-1].size())
    result = \
        cat((restart_padding,  # <- Adding the start state
             model.semiring.times(
                 hiddens[:, :-1],
                 transition_matrix_val[:, 1, :-1])
             ), 1)
    # Adding self loops (consume a token, stay in same state)
    result = \
        model.semiring.plus(
            result,
            model.semiring.times(
                self_loop_scale,
                model.semiring.times(
                    hiddens,
                    transition_matrix_val[:, 0, :]
                )
            )
        )
    return result


class TestPatternLengths(unittest.TestCase):
    def setUp(self):
        vocab, embeddings, word_dim = read_embeddings(EMBEDDINGS_FILENAME)
        self.embeddings = embeddings
        self.data = read_docs(DATA_FILENAME, vocab)[0]
        state_dict = torch.load(MODEL_FILENAME)
        self.model = \
            SoftPatternClassifier(
                PATTERN_SPECS,
                MLP_HIDDEN_DIM,
                NUM_MLP_LAYERS,
                NUM_CLASSES,
                embeddings,
                vocab,
                SEMIRING,
                GPU,
                DROPOUT,
                LEGACY
            )
        self.model.load_state_dict(state_dict)

    def test_pattern_lengths(self):
        """
        Test that using `torch.gather` for collecting end-states works the
        same as doing it manually
        """
        test_data = [self.data[0]]
        batch = Batch(test_data, self.embeddings, GPU)
        batch2 = soft_patterns.Batch(test_data, self.embeddings, GPU)
        expected, transition_expected,all_hiddens_expected = forward(self.model, batch)
        actual, transition_actual,all_hiddens_actual = self.model.forward(batch2, 3)

        for mat_actual, mat_expected in zip(transition_actual, transition_expected):
            for i in range(mat_actual.size()[1]):
                for j in range(mat_actual.size()[2]):
                    for k in range(mat_actual.size()[3]):
                        k1=mat_actual[0,i,j,k].data.numpy()[0]
                        k2=mat_expected[0][i,j,k].data.numpy()[0]
                        # print("tt", i,j,k1, k2, k1==k2)

                        self.assertAlmostEqual(k1, k2, places=4)

        k = 0
        for hiddens_actual, hiddens_expected in zip(all_hiddens_actual, all_hiddens_expected):
            # print(k)
            for i in range(hiddens_expected.size()[0]):
                # print("\t", i)
                for j in range(hiddens_expected.size()[1]):
                    # print("\t\t", j)
                    self.assertAlmostEqual(hiddens_actual[i,j].data.numpy()[0], hiddens_expected[i,j].data.numpy()[0], places=4)
            k += 1

        for expd_doc, act_doc in zip(expected.data, actual.data):
            for expd_y, act_y in zip(expd_doc, act_doc):
                self.assertAlmostEqual(expd_y, act_y, places=4)



class Batch:
    def __init__(self, docs, embeddings, gpu):
        """ Makes a smaller vocab of only words used in the given docs """
        mini_vocab = Vocab.from_docs(docs, default=0)
        self.docs = [mini_vocab.numberize(doc) for doc in docs]
        local_embeddings = [embeddings[i] for i in mini_vocab.names]
        self.embeddings_matrix = fixed_var(FloatTensor(local_embeddings).t(), gpu)

    def size(self):
        return len(self.docs)


class Vocab:
    """
    A bimap from name to index.
    Use `vocab[i]` to lookup name for `i`,
    and `vocab(n)` to lookup index for `n`.
    """
    def __init__(self,
                 names,
                 default=UNK_TOKEN):
        self.default = default
        self.names = list(nub(chain([default], names)))
        self.index = {name: i for i, name in enumerate(self.names)}

    def __getitem__(self, index):
        """ Lookup name given index. """
        return self.names[index] if 0 < index < len(self.names) else self.default

    def __call__(self, name):
        """ Lookup index given name. """
        return self.index.get(name, 0)

    def __contains__(self, item):
        return item in self.index

    def __len__(self):
        return len(self.names)

    def __or__(self, other):
        return Vocab(self.names + other.names)

    def numberize(self, doc):
        """ Replace each name in doc with its index. """
        return [self(token) for token in doc]

    def denumberize(self, doc):
        """ Replace each index in doc with its name. """
        return [self[idx] for idx in doc]

    @staticmethod
    def from_docs(docs, default=UNK_TOKEN):
        return Vocab((i for doc in docs for i in doc), default=default)



if __name__ == "__main__":
    unittest.main()