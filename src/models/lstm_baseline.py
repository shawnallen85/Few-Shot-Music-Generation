import numpy as np
import tensorflow as tf

from models.tf_model import TFModel
from models.nn_lib import LSTM, make_cell, get_sentinel_prob, num_stable_log,\
    seq_loss, get_ndcg
from models.base_model import convert_tokens_to_input_and_target


class LSTMBaseline(TFModel):
    """LSTM language model.

    Trained on songs from the meta-training set. During evaluation,
    ignore each episode's support set and evaluate only on query set.
    """

    def __init__(self, config):
        super(LSTMBaseline, self).__init__(config)

    def _define_placeholders(self):
        self._embd_size = self._config['embedding_size']
        self._hidden_size = self._config['hidden_size']
        self._n_layers = self._config['n_layers']
        self._lr = self._config['lr']
        self._max_grad_norm = self._config['max_grad_norm']
        self._embedding_var_name = 'embedding'

        self._batch_size = tf.placeholder(tf.int32, shape=())
        self._seq_length = tf.placeholder(tf.int32, [None])
        self._words = tf.placeholder(
            tf.int32, [None, self._time_steps])
        self._target = tf.placeholder(
            tf.int32, [None, self._time_steps])

        self._is_training = tf.placeholder_with_default(
            True, shape=(), name='is_training')
        # self._max_token_len = tf.placeholder(tf.int32, shape=())

    def _build_lstm(self):
        embedding = tf.get_variable(
            self._embedding_var_name, [self._input_size, self._embd_size])
        self._cell = tf.contrib.rnn.MultiRNNCell(
            [make_cell(0, self._embd_size, self._hidden_size)])
        self._initial_state = self._cell.zero_state(
            self._batch_size, dtype=tf.float32)

        # outputs: [batch_size, time_step, hidden_size]
        # state: [batch_size, hidden_size]
        self._hidden_states_orig, self._final_state = LSTM(
            self._cell, self._words, embedding,
            self._seq_length, self._batch_size, self._initial_state
        )

        # [batch_size * time_step, hidden_size]
        self._hidden_states = tf.reshape(
            self._hidden_states_orig, [-1, self._hidden_size])
        logits = tf.matmul(self._hidden_states, embedding, transpose_b=True)

        # [batch_size, time_step, input_size]
        logits = tf.reshape(
            logits, [self._batch_size, self._time_steps, self._input_size])
        if not self._config['use_sentinel']:
            self._logits = logits
            self._prob = tf.nn.softmax(self._logits)
        else:
            prob_vocab = tf.nn.softmax(logits)
            g, prob_cache = get_sentinel_prob(
                self._target, self._hidden_states, self._batch_size,
                self._time_steps, self._hidden_size, self._input_size)
            self._prob = tf.multiply(g, prob_vocab) + prob_cache
            self._logits = num_stable_log(self._prob)
        """
        max_token_len = tf.tile([self._max_token_len], [self._batch_size])
        self._neg_log = seq_loss(
            self._logits, self._target,
            tf.minimum(max_token_len, self._seq_length), self._time_steps,
            avg_batch=False)
        """
        self._neg_log = seq_loss(
            self._logits, self._target,
            self._seq_length, self._time_steps,
            avg_batch=False)
        self._avg_neg_log = tf.reduce_mean(self._neg_log)

    def _build_graph(self):
        self._build_lstm()
        lr = self._lr
        optimizer = tf.train.AdamOptimizer(lr)
        grads, _ = tf.clip_by_global_norm(tf.gradients(self._avg_neg_log,
                                                       self.get_vars()),
                                          self._max_grad_norm)
        self._train_op = optimizer.apply_gradients(zip(grads, self.get_vars()),
                                                   self._global_step)

    def train(self, episode):
        """Concatenate query and support sets to train."""
        X, Y = convert_tokens_to_input_and_target(
            episode.support, episode.support_seq_len,
            self._start_word, self._end_word)
        X2, Y2 = convert_tokens_to_input_and_target(
            episode.query, episode.query_seq_len,
            self._start_word, self._end_word)
        X = np.concatenate([X, X2])
        Y = np.concatenate([Y, Y2])
        support_seq_len = episode.support_seq_len.flatten()
        query_seq_len = episode.query_seq_len.flatten()
        seq_len = np.concatenate([support_seq_len, query_seq_len])

        feed_dict = {}
        feed_dict[self._words] = X
        feed_dict[self._target] = Y
        feed_dict[self._batch_size] = np.shape(X)[0]
        # adding stop word adds +1 to sequence length
        feed_dict[self._seq_length] = seq_len + 1

        _, loss = self._sess.run([self._train_op, self._avg_neg_log],
                                 feed_dict=feed_dict)

        if self._summary_writer is not None:
            summary = tf.Summary(value=[
                tf.Summary.Value(tag='Train/loss',
                                 simple_value=loss)])
            self._summary_writer.add_summary(summary, self._train_calls)
            self._train_calls += 1

        return loss

    def eval_ndcg(self, episode):
        # Evaluate NDCG ranking metric

        if np.shape(episode.support)[0] > 1:
            episode.support = episode.support[0:1, :, :]
            episode.query = episode.query[0:1, :, :]
            episode.other_query = episode.other_query[0:1, :, :]
            episode.support_seq_len = episode.support_seq_len[0:1, :]
            episode.query_seq_len = episode.query_seq_len[0:1, :]
            episode.other_query_seq_len = episode.other_query_seq_len[0:1, :]

        # Ignore support set and evaluate only on query set.
        X, Y = convert_tokens_to_input_and_target(
            episode.query, episode.query_seq_len,
            self._start_word, self._end_word)
        X_other, Y_other = convert_tokens_to_input_and_target(
            episode.other_query, episode.other_query_seq_len,
            self._start_word, self._end_word)

        query_seq_len = episode.query_seq_len.flatten()
        feed_dict = {}
        feed_dict[self._words] = X
        feed_dict[self._target] = Y
        feed_dict[self._batch_size] = np.shape(X)[0]
        # adding stop word makes sequences longer by +1
        feed_dict[self._seq_length] = query_seq_len + 1
        feed_dict[self._is_training] = False
        # feed_dict[self._max_token_len] = self._config['eval_len']
        nll, avg_nll = self._sess.run(
            [self._neg_log, self._avg_neg_log], feed_dict=feed_dict)

        other_query_seq_len = episode.other_query_seq_len.flatten()
        feed_dict = {}
        feed_dict[self._words] = X_other
        feed_dict[self._target] = Y_other
        feed_dict[self._batch_size] = np.shape(X_other)[0]
        # adding stop word makes sequences longer by +1
        feed_dict[self._seq_length] = other_query_seq_len + 1
        feed_dict[self._is_training] = False
        # feed_dict[self._max_token_len] = self._config['eval_len']
        nll_other, _ = self._sess.run(
            [self._neg_log, self._avg_neg_log], feed_dict=feed_dict)

        rel_scores = np.ones(shape=np.shape(nll))
        rel_scores_other = np.zeros(shape=np.shape(nll_other))

        ndcg = get_ndcg(
            np.concatenate([rel_scores, rel_scores_other]),
            np.concatenate([nll, nll_other]),
            rank_position=np.shape(nll)[0])

        return ndcg

    def eval(self, episode):
        # Ignore support set and evaluate only on query set.

        X, Y = convert_tokens_to_input_and_target(
            episode.query, episode.query_seq_len,
            self._start_word, self._end_word)

        query_seq_len = episode.query_seq_len.flatten()
        feed_dict = {}
        feed_dict[self._words] = X
        feed_dict[self._target] = Y
        feed_dict[self._batch_size] = np.shape(X)[0]
        # adding stop word makes sequences longer by +1
        feed_dict[self._seq_length] = query_seq_len + 1
        feed_dict[self._is_training] = False
        # feed_dict[self._max_token_len] = self._config['max_len']
        avg_neg_log = self._sess.run(
            self._avg_neg_log, feed_dict=feed_dict)

        if self._summary_writer is not None:
            summary = tf.Summary(value=[
                tf.Summary.Value(tag='Eval/Avg_NLL',
                                 simple_value=avg_neg_log)])
            self._summary_writer.add_summary(summary, self._eval_calls)
            self._eval_calls += 1

        return avg_neg_log

    def sample(self, support_set, num):
        """Ignore support set for sampling."""
        pred_words = []
        word = self._start_word

        state = self._sess.run(self._cell.zero_state(1, tf.float32))
        x = np.zeros((1, self._time_steps))
        for i in range(num):
            x[0, 0] = word
            feed_dict = {}
            feed_dict[self._words] = x
            feed_dict[self._batch_size] = 1
            feed_dict[self._seq_length] = [1]
            feed_dict[self._initial_state] = state

            probs, state = self._sess.run([self._prob, self._final_state],
                                          feed_dict=feed_dict)
            p = probs[0][0]
            word = self._sampler.sample(p)
            pred_words.append(word)

        return pred_words
