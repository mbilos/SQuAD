import tensorflow as tf
import layers

class BiDAFSelfAttention:
    def __init__(self, config):
        self.config = config

        self.dropout = tf.get_variable('dropout', shape=[], dtype=tf.float32, initializer=tf.constant_initializer(self.config.dropout), trainable=False)
        self.global_step = tf.get_variable('global_step', shape=[], dtype=tf.int32, initializer=tf.constant_initializer(0), trainable=False)

        self.lr = tf.get_variable('learning-rate', shape=[], dtype=tf.float32, initializer=tf.constant_initializer(self.config.learning_rate), trainable=False)
        self.decay_lr = tf.assign(self.lr, tf.maximum(self.lr / 2, 1e-6))

        self.word_embed = tf.get_variable("word-emb-matrix", shape=self.config.embed.shape, initializer=tf.constant_initializer(self.config.embed), trainable=False)
        self.char_embed = tf.get_variable('char-emb-matrix', [self.config.unique_chars, self.config.char_embed])

        self.forward()

    def forward(self):
        self.c_words = tf.placeholder(tf.int32, [None, self.config.context_len], 'context-words')
        self.c_chars = tf.placeholder(tf.int32, [None, self.config.context_len, self.config.max_char_len], 'context-chars')
        self.c_mask  = tf.sign(self.c_words)

        self.q_words = tf.placeholder(tf.int32, [None, self.config.question_len], 'query-words')
        self.q_chars = tf.placeholder(tf.int32, [None, self.config.question_len, self.config.max_char_len], 'query-chars')
        self.q_mask  = tf.sign(self.q_words)

        self.c_len = tf.cast(tf.reduce_sum(self.c_mask, -1), tf.int32)
        self.q_len = tf.cast(tf.reduce_sum(self.q_mask, -1), tf.int32)

        self.start = tf.placeholder(tf.int32, [None], 'start-index')
        self.end = tf.placeholder(tf.int32, [None], 'end-index')

        with tf.variable_scope('input-embedding'):
            c_w = tf.nn.embedding_lookup(self.word_embed, self.c_words)
            q_w = tf.nn.embedding_lookup(self.word_embed, self.q_words)

            c_ch = layers.char_embed(self.c_chars, self.char_embed, dropout=self.dropout)
            q_ch = layers.char_embed(self.q_chars, self.char_embed, dropout=self.dropout, reuse=True)

            c = tf.concat([c_w, c_ch], -1)
            q = tf.concat([q_w, q_ch], -1)

        with tf.variable_scope('rnn'):
            c_rnn = layers.birnn(c, self.c_len, self.config.cell_size, self.config.cell_type, self.dropout)
            q_rnn = layers.birnn(q, self.q_len, self.config.cell_size, self.config.cell_type, self.dropout, reuse=True)

        with tf.variable_scope('attention'):
            attention = layers.bi_attention(c_rnn, q_rnn, layers.trilinear(c_rnn, q_rnn), self.c_mask, self.q_mask)
            attention = tf.layers.conv1d(attention, self.config.cell_size * 2, 1, padding='same')

        with tf.variable_scope('memory1'):
            memory1 = layers.birnn(attention, self.c_len, self.config.cell_size, self.config.cell_type, self.dropout)

        with tf.variable_scope('self-attention') as scope:
            x = memory1
            self_attention = layers.bi_attention(x, x, layers.trilinear(x, x), self.c_mask, self.c_mask, only_c2q=True)
            res = tf.layers.dense(self_attention, self.config.cell_size * 2, activation=tf.nn.relu)
            res = tf.layers.dropout(res, rate=self.config.dropout, training=self.config.training)

            res += attention

        with tf.variable_scope('memory2'):
            memory2 = layers.birnn(res, self.c_len, self.config.cell_size, self.config.cell_type, self.dropout)

        with tf.variable_scope('start-index') as scope:
            self.start_linear = tf.squeeze(tf.layers.dense(memory2, 1), -1)
            self.pred_start = tf.nn.softmax(self.start_linear)

        with tf.variable_scope('end-index') as scope:
            end_input = tf.concat([tf.expand_dims(self.start_linear, -1), memory2], -1)
            memory3 = layers.birnn(end_input, self.c_len, self.config.cell_size, self.config.cell_type, self.dropout)

            self.end_linear = tf.squeeze(tf.layers.dense(memory3, 1), -1)
            self.pred_end = tf.nn.softmax(self.end_linear)

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

        if self.config.ema_decay > 0:
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
