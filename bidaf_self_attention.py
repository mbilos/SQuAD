import tensorflow as tf
import numpy as np
import util

class BiDAF_SelfAttention:
    def __init__(self, config):
        self.config = config

        self.global_step = tf.get_variable('global_step', shape=[], dtype=tf.int32, initializer=tf.constant_initializer(0), trainable=False)
        self.lr = tf.get_variable('learning-rate', shape=[], dtype=tf.float32, initializer=tf.constant_initializer(self.config.learning_rate), trainable=False)
        self.decay_lr = tf.assign(self.lr, tf.maximum(self.lr / 2, 1e-6))

        self.input()
        self.forward()
        self.training()

    def input(self):
        with tf.variable_scope('input') as scope:
            self.c_words = tf.placeholder(tf.float32, [None, self.config.context_len, self.config.word_embed], 'context-words')
            self.c_chars = tf.placeholder(tf.int32, [None, self.config.context_len, self.config.max_char_len], 'context-chars')
            self.c_pos = tf.placeholder(tf.int32, [None, self.config.context_len], 'context-part-of-speech')
            self.c_ner = tf.placeholder(tf.int32, [None, self.config.context_len], 'context-named-entity')

            self.q_words = tf.placeholder(tf.float32, [None, self.config.question_len, self.config.word_embed], 'query-words')
            self.q_chars = tf.placeholder(tf.int32, [None, self.config.question_len, self.config.max_char_len], 'query-chars')
            self.q_pos = tf.placeholder(tf.int32, [None, self.config.question_len], 'query-part-of-speech')
            self.q_ner = tf.placeholder(tf.int32, [None, self.config.question_len], 'query-named-entity')

            self.c_mask = tf.cast(tf.cast(tf.reduce_sum(self.c_words, -1), tf.bool), tf.float32)
            self.q_mask = tf.cast(tf.cast(tf.reduce_sum(self.q_words, -1), tf.bool), tf.float32)

            self.c_len = tf.cast(tf.reduce_sum(self.c_mask, -1), tf.int32)
            self.q_len = tf.cast(tf.reduce_sum(self.q_mask, -1), tf.int32)

            self.start = tf.placeholder(tf.int32, [None], 'start-index')
            self.end = tf.placeholder(tf.int32, [None], 'end-index')

    def forward(self):
        self.c_char_embed, self.q_char_embed = self.char_embedding()
        self.c_encoded, self.q_encoded = self.input_encoder()
        self.attention = self.attention_flow()
        self.modeling = self.model_encoder()
        self.start_linear, self.end_linear, self.pred_start, self.pred_end = self.output()

    def training(self):
        with tf.variable_scope('loss') as scope:
            loss1 = tf.nn.sparse_softmax_cross_entropy_with_logits(logits=self.start_linear, labels=self.start)
            loss2 = tf.nn.sparse_softmax_cross_entropy_with_logits(logits=self.end_linear, labels=self.end)
            loss = tf.reduce_mean(loss1 + loss2)
            lossL2 = tf.add_n([ tf.nn.l2_loss(v) for v in tf.trainable_variables() if 'bias' not in v.name ]) * self.config.l2
            self.loss = loss + lossL2

        with tf.variable_scope('optimizer') as scope:
            optimizer = tf.train.AdamOptimizer(learning_rate=self.lr)
            grads = tf.gradients(self.loss, tf.trainable_variables())
            grads, _ = tf.clip_by_global_norm(grads, self.config.grad_clip)
            grads_and_vars = zip(grads, tf.trainable_variables())
            self.optimize = optimizer.apply_gradients(grads_and_vars, global_step=self.global_step)

        with tf.variable_scope('ema') as scope:
            ema = tf.train.ExponentialMovingAverage(decay=self.config.ema_decay)
            ema_op = ema.apply(tf.trainable_variables())
            with tf.control_dependencies([ema_op]):
                self.loss = tf.identity(self.loss)
                assign_vars = []
                for var in tf.global_variables():
                    v = ema.average(var)
                    if v:
                        assign_vars.append(tf.assign(var,v))
            self.assign_vars = assign_vars

    def output(self):
        with tf.variable_scope('start-index') as scope:
            start_linear = tf.squeeze(tf.layers.dense(self.modeling[-1], 1), -1)
            pred_start = tf.nn.softmax(start_linear)

        with tf.variable_scope('end-index') as scope:
            end_input = tf.concat([tf.expand_dims(start_linear, -1), self.modeling[-1]], -1)
            memory, _ = self.rnn(end_input, self.c_len)

            end_linear = tf.squeeze(tf.layers.dense(memory, 1), -1)
            pred_end = tf.nn.softmax(end_linear)

        return start_linear, end_linear, pred_start, pred_end

    def model_encoder(self):
        with tf.variable_scope('first-memory') as scope:
            memory1, _ = self.rnn(self.attention, self.c_len)

        with tf.variable_scope('self-attention') as scope:
            att = util.trilinear(memory1, memory1) - 1e30 * tf.eye(self.config.context_len)
            att = tf.nn.softmax(att)
            res = tf.matmul(att, memory1)
            res = tf.layers.dense(res, self.config.cell_size * 2, activation=tf.nn.relu)
            res = tf.layers.dropout(res, rate=self.config.dropout, training=self.config.training)

            res += self.attention

        with tf.variable_scope('second-memory') as scope:
            memory2, _ = self.rnn(res, self.c_len)

        return [memory1, memory2]

    def attention_flow(self):
        with tf.variable_scope('attention'):
            c, q = self.c_encoded, self.q_encoded

            similarity = util.trilinear(c, q)

            row_norm = tf.nn.softmax(similarity)
            A = tf.matmul(row_norm, q) # context to query

            column_norm = tf.nn.softmax(similarity, 1)
            B = tf.matmul(tf.matmul(row_norm, column_norm, transpose_b=True), c) # query to context

            attention = tf.concat([c, A, c * A, c * B], -1)
            attention = tf.layers.dense(attention, self.config.cell_size * 2, activation=tf.nn.relu)
            attention = tf.layers.dropout(attention, rate=self.config.dropout, training=self.config.training)

            return attention

    def input_encoder(self):
        with tf.variable_scope('input-embedding'):
            similarity = tf.matmul(tf.nn.l2_normalize(self.c_words, -1), tf.nn.l2_normalize(self.q_words, -1), transpose_b=True)
            c_similarity = tf.reduce_max(similarity, -1, keep_dims=True)
            q_similarity = tf.transpose(tf.reduce_max(similarity, 1, keep_dims=True), [0,2,1])

            c = tf.concat([self.c_words, self.c_char_embed, c_similarity], -1)
            q = tf.concat([self.q_words, self.q_char_embed, q_similarity], -1)

            self.config.embed_size += 1

            if self.config.pos_embed > 0:
                self.pos_emb_matrix = tf.get_variable('pos_emb', [self.config.unique_pos, self.config.pos_embed])
                c_pos_embed = tf.nn.embedding_lookup(self.pos_emb_matrix, self.c_pos)
                q_pos_embed = tf.nn.embedding_lookup(self.pos_emb_matrix, self.q_pos)
                c = tf.concat([c, tf.layers.dropout(c_pos_embed, rate=self.config.dropout*0.5, training=self.config.training)], -1)
                q = tf.concat([q, tf.layers.dropout(q_pos_embed, rate=self.config.dropout*0.5, training=self.config.training)], -1)

            if self.config.ner_embed > 0:
                self.ner_emb_matrix = tf.get_variable('ner_emb', [self.config.unique_ner, self.config.ner_embed])
                c_ner_embed = tf.nn.embedding_lookup(self.ner_emb_matrix, self.c_ner)
                q_ner_embed = tf.nn.embedding_lookup(self.ner_emb_matrix, self.q_ner)
                c = tf.concat([c, tf.layers.dropout(c_ner_embed, rate=self.config.dropout*0.5, training=self.config.training)], -1)
                q = tf.concat([q, tf.layers.dropout(q_ner_embed, rate=self.config.dropout*0.5, training=self.config.training)], -1)

        with tf.variable_scope('contextual-embedding') as scope:
            c_output, _ = self.rnn(c, self.c_len)
            q_output, _ = self.rnn(q, self.q_len, reuse=True)

        return c_output, q_output

    def char_embedding(self):
        with tf.variable_scope('char-embedding'):
            self.char_emb_matrix = tf.get_variable('char_emb', [self.config.unique_chars, self.config.char_embed])

            c = tf.nn.embedding_lookup(self.char_emb_matrix, self.c_chars)
            q = tf.nn.embedding_lookup(self.char_emb_matrix, self.q_chars)

            c = tf.layers.dropout(c, rate=self.config.dropout, training=self.config.training)
            q = tf.layers.dropout(q, rate=self.config.dropout, training=self.config.training)

            c = tf.layers.conv2d(c, self.config.char_embed, kernel_size=[1, 5], activation=tf.nn.relu)
            q = tf.layers.conv2d(q, self.config.char_embed, kernel_size=[1, 5], activation=tf.nn.relu, reuse=True)

            c = tf.reduce_max(c, axis=2)
            q = tf.reduce_max(q, axis=2)

            return c, q

    def rnn(self, inputs, sequence_length, reuse=None):
        return util.bidirectional_dynamic_rnn(
            inputs,
            sequence_length,
            self.config.cell_size,
            dropout=self.config.dropout,
            concat=True,
            reuse=reuse)