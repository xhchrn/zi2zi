# -*- coding: utf-8 -*-
from __future__ import print_function
from __future__ import absolute_import

import tensorflow as tf
import numpy as np
import scipy.misc as misc
import os
import time
from tqdm import trange
from collections import namedtuple
from .ops import conv2d, deconv2d, lrelu, fc, batch_norm, init_embedding, conditional_instance_norm
from .dataset import get_train_dataloader
from .utils import normalize_image, denormalize_image, save_image
from .vgg import VGG_Model

# Auxiliary wrapper classes
# Used to save handles(important nodes in computation graph) for later evaluation
LossHandle = namedtuple("LossHandle",
                        ["d_loss",
                         "g_loss",
                         "const_loss",
                         "l1_loss",
                         "category_loss",
                         "cheat_loss",
                         "vgg_loss"])
EvalHandle = namedtuple("EvalHandle",
                        ["encoder",
                         "fake_s",
                         "fake_c",
                         "source",
                         "embedding"])
InputHandle   = namedtuple("InputHandle",   ["real_data", "embedding_ids"])
SummaryHandle = namedtuple("SummaryHandle", ["d_merged", "g_merged"])


class GEGAN(object):
    def __init__(self, experiment_dir=None, experiment_id=0, batch_size=16, input_width=64, output_width=64,
                 generator_dim=64, discriminator_dim=64, L1_penalty=100, Lconst_penalty=15, Lvgg_penalty=0.1,
                 Lcategory_penalty=1.0, embedding_num=2, embedding_dim=64, input_filters=3, output_filters=3):
        self.experiment_dir     = experiment_dir
        self.experiment_id      = experiment_id
        self.batch_size         = batch_size
        self.input_width        = input_width
        self.output_width       = output_width
        self.generator_dim      = generator_dim
        self.discriminator_dim  = discriminator_dim
        self.L1_penalty         = L1_penalty
        self.Lconst_penalty     = Lconst_penalty
        self.Lvgg_penalty       = Lvgg_penalty
        self.Lcategory_penalty  = Lcategory_penalty
        self.embedding_num      = embedding_num
        self.embedding_dim      = embedding_dim
        self.input_filters      = input_filters
        self.output_filters     = output_filters
        self.train_dataloader   = get_train_dataloader(self.batch_size)
        self.vgg                = VGG_Model()
        # init all the directories
        self.sess = None
        # experiment_dir is needed for training
        if experiment_dir:
            self.data_dir       = os.path.join(self.experiment_dir, "data")
            self.checkpoint_dir = os.path.join(self.experiment_dir, "checkpoint")
            self.sample_dir     = os.path.join(self.experiment_dir, "sample")
            self.log_dir        = os.path.join(self.experiment_dir, "logs")

            if not os.path.exists(self.checkpoint_dir):
                os.makedirs(self.checkpoint_dir)
                print("create checkpoint directory")
            if not os.path.exists(self.log_dir):
                os.makedirs(self.log_dir)
                print("create log directory")
            if not os.path.exists(self.sample_dir):
                os.makedirs(self.sample_dir)
                print("create sample directory")

    def encoder(self, images, is_training, reuse=False):
        with tf.variable_scope("generator"):
            if reuse:
                tf.get_variable_scope().reuse_variables()

            encode_layers = dict()

            def encode_layer(x, output_filters, layer):
                act = lrelu(x)
                conv = conv2d(act, output_filters=output_filters, scope="g_e%d_conv" % layer)
                enc = batch_norm(conv, is_training, scope="g_e%d_bn" % layer)
                encode_layers["e%d" % layer] = enc
                return enc

            e1 = conv2d(images, self.generator_dim, scope="g_e1_conv")
            encode_layers["e1"] = e1
            e2 = encode_layer(e1, self.generator_dim * 2, 2)
            e3 = encode_layer(e2, self.generator_dim * 4, 3)
            e4 = encode_layer(e3, self.generator_dim * 4, 4)
            e5 = encode_layer(e4, self.generator_dim * 8, 5)
            e6 = encode_layer(e5, self.generator_dim * 8, 6)

            return e6, encode_layers

    def decoder(self, encoded, encoding_layers, ids, inst_norm, is_training, reuse=False):
        with tf.variable_scope("generator"):
            if reuse:
                tf.get_variable_scope().reuse_variables()

            s = self.output_width
            s2, s4, s8, s16, s32 = int(s / 2), int(s / 4), int(s / 8), int(s / 16), int(s / 32)


            def decode_layer(x, output_width, output_filters, layer, enc_layer, dropout=False, do_concat=True):
                dec = deconv2d(tf.nn.relu(x), [self.batch_size, output_width,
                                               output_width, output_filters], scope="g_d%d_deconv" % layer)
                if layer != 6:
                    # IMPORTANT: normalization for last layer
                    # Very important, otherwise GAN is unstable
                    # Trying conditional instance normalization to
                    # overcome the fact that batch normalization offers
                    # different train/test statistics
                    if inst_norm:
                        dec = conditional_instance_norm(dec, ids, self.embedding_num, scope="g_d%d_inst_norm" % layer)
                    else:
                        dec = batch_norm(dec, is_training, scope="g_d%d_bn" % layer)
                if dropout:
                    dec = tf.nn.dropout(dec, 0.5)
                if do_concat:
                    dec = tf.concat([dec, enc_layer], 3)
                return dec

            d1 = decode_layer(encoded, s32, self.generator_dim * 8, layer=1, enc_layer=encoding_layers["e5"], dropout=True)
            d2 = decode_layer(d1,      s16, self.generator_dim * 4, layer=2, enc_layer=encoding_layers["e4"])
            d3 = decode_layer(d2,      s8,  self.generator_dim * 4, layer=3, enc_layer=encoding_layers["e3"])
            d4 = decode_layer(d3,      s4,  self.generator_dim * 2, layer=4, enc_layer=encoding_layers["e2"])
            d5 = decode_layer(d4,      s2,  self.generator_dim * 1, layer=5, enc_layer=encoding_layers["e1"])
            d6 = decode_layer(d5,      s,   self.output_filters,    layer=6, enc_layer=None, do_concat=False)

            output = tf.nn.tanh(d6)  # scale to (-1, 1)
            return output

    def generator(self, images, embeddings, embedding_ids, inst_norm, is_training, reuse=False):
        e6, enc_layers = self.encoder(images, is_training=is_training, reuse=reuse)
        local_embeddings = tf.nn.embedding_lookup(embeddings, ids=embedding_ids)
        local_embeddings = tf.reshape(local_embeddings, [self.batch_size, 1, 1, self.embedding_dim])
        embedded = tf.concat([e6, local_embeddings], 3)
        output = self.decoder(embedded, enc_layers, embedding_ids, inst_norm, is_training=is_training, reuse=reuse)
        return output, e6

    def discriminator(self, image, is_training, reuse=False):
        with tf.variable_scope("discriminator"):
            if reuse:
                tf.get_variable_scope().reuse_variables()
            h0 = lrelu(conv2d(image, self.discriminator_dim, scope="d_h0_conv"))
            h1 = lrelu(batch_norm(conv2d(h0, self.discriminator_dim * 2, scope="d_h1_conv"),
                                  is_training, scope="d_bn_1"))
            h2 = lrelu(batch_norm(conv2d(h1, self.discriminator_dim * 4, scope="d_h2_conv"),
                                  is_training, scope="d_bn_2"))
            h3 = lrelu(batch_norm(conv2d(h2, self.discriminator_dim * 8, sh=1, sw=1, scope="d_h3_conv"),
                                  is_training, scope="d_bn_3"))
            # real or fake binary loss
            fc1 = fc(tf.reshape(h3, [self.batch_size, -1]), 1, scope="d_fc1")
            # category loss
            fc2 = fc(tf.reshape(h3, [self.batch_size, -1]), self.embedding_num, scope="d_fc2")

            return tf.nn.sigmoid(fc1), fc1, fc2

    def build_model(self, is_training=True, inst_norm=False, no_target_source=False):
        real_data = tf.placeholder(tf.float32,
                                   [self.batch_size, self.input_width, self.input_width, self.input_filters],
                                   name='real_images')
        embedding_ids   = tf.placeholder(tf.int64, shape=None, name="embedding_ids")
        embedding_ids_c = tf.ones_like(embedding_ids) - embedding_ids # c means complementary

        embedding = init_embedding(self.embedding_num, self.embedding_dim)
        fake_s, encoded_real = self.generator(real_data, embedding, embedding_ids,   is_training=is_training,
                                                inst_norm=inst_norm, reuse=False)
        fake_c, _            = self.generator(real_data, embedding, embedding_ids_c, is_training=is_training,
                                                inst_norm=inst_norm, reuse=True)

        # Note it is not possible to set reuse flag back to False
        # initialize all variables before setting reuse to True
        real_D,   real_D_logits,   real_category_logits   = self.discriminator(real_data, is_training=is_training, reuse=False)
        fake_s_D, fake_s_D_logits, fake_s_category_logits = self.discriminator(fake_s,    is_training=is_training, reuse=True)
        fake_c_D, fake_c_D_logits, fake_c_category_logits = self.discriminator(fake_c,    is_training=is_training, reuse=True)

        # encoding constant loss
        # this loss assume that generated imaged and real image
        # should reside in the same space and close to each other
        encoded_fake_s = self.encoder(fake_s, is_training, reuse=True)[0]
        encoded_fake_c = self.encoder(fake_c, is_training, reuse=True)[0]
        const_loss_s   = tf.reduce_mean(tf.square(encoded_real - encoded_fake_s))
        const_loss_c   = tf.reduce_mean(tf.square(encoded_real - encoded_fake_c))
        const_loss     = (const_loss_s + const_loss_c) * self.Lconst_penalty

        # category loss
        true_labels = tf.reshape(tf.one_hot(indices=embedding_ids,   depth=self.embedding_num),
                                 shape=[self.batch_size, self.embedding_num])
        comp_labels = tf.reshape(tf.one_hot(indices=embedding_ids_c, depth=self.embedding_num),
                                 shape=[self.batch_size, self.embedding_num])
        real_category_loss   = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(logits=real_category_logits,
                                                                                      labels=true_labels))
        fake_s_category_loss = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(logits=fake_s_category_logits,
                                                                                      labels=true_labels))
        fake_c_category_loss = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(logits=fake_c_category_logits,
                                                                                      labels=comp_labels))
        category_loss = self.Lcategory_penalty * (real_category_loss + fake_s_category_loss + fake_c_category_loss)

        # binary real/fake loss
        d_loss_real   = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(logits=real_D_logits,
                                                                               labels=tf.ones_like(real_D)))
        d_loss_fake_s = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(logits=fake_s_D_logits,
                                                                               labels=tf.zeros_like(fake_s_D)))
        d_loss_fake_c = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(logits=fake_c_D_logits,
                                                                               labels=tf.zeros_like(fake_c_D)))

        # L1 loss between real and generated images
        l1_loss  = self.L1_penalty * tf.reduce_mean(tf.abs(fake_s - real_data))

        # vgg loss between real and fake_c
        denorm_real_data = tf.clip_by_value((real_data + 1) * 127.5, 0.0, 255.0)
        denorm_fake_c    = tf.clip_by_value((fake_c    + 1) * 127.5, 0.0, 255.0)
        vgg_loss = self.vgg.vgg_loss(denorm_fake_c, denorm_real_data) * self.Lvgg_penalty

        # maximize the chance generator fool the discriminator
        cheat_loss_s = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(logits=fake_s_D_logits,
                                                                              labels=tf.ones_like(fake_s_D)))
        cheat_loss_c = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(logits=fake_c_D_logits,
                                                                              labels=tf.ones_like(fake_c_D)))
        cheat_loss   = cheat_loss_s + cheat_loss_c

        d_loss = d_loss_real + d_loss_fake_s + d_loss_fake_c + category_loss / 2.0
        g_loss = cheat_loss + l1_loss + vgg_loss + const_loss + \
                 self.Lcategory_penalty * (fake_s_category_loss + fake_c_category_loss)

        # d_loss components
        d_loss_real_summary           = tf.summary.scalar("d_loss_real",          d_loss_real)
        d_loss_fake_s_summary         = tf.summary.scalar("d_loss_fake_s",        d_loss_fake_s)
        d_loss_fake_c_summary         = tf.summary.scalar("d_loss_fake_c",        d_loss_fake_c)
        category_loss_summary         = tf.summary.scalar("category_loss",        category_loss)

        d_loss_summary                = tf.summary.scalar("d_loss",               d_loss)


        # g_loss components
        cheat_loss_summary            = tf.summary.scalar("cheat_loss",           cheat_loss)
        l1_loss_summary               = tf.summary.scalar("l1_loss",              l1_loss)
        vgg_loss_summary              = tf.summary.scalar("vgg_loss",             vgg_loss)
        const_loss_summary            = tf.summary.scalar("const_loss",           const_loss)
        fake_s_category_loss_summary  = tf.summary.scalar("fake_s_category_loss", fake_s_category_loss)
        fake_c_category_loss_summary  = tf.summary.scalar("fake_c_category_loss", fake_c_category_loss)

        g_loss_summary                = tf.summary.scalar("g_loss",               g_loss)


        d_merged_summary = tf.summary.merge([d_loss_real_summary,
                                             d_loss_fake_s_summary,
                                             d_loss_fake_c_summary,
                                             category_loss_summary,
                                             d_loss_summary])
        g_merged_summary = tf.summary.merge([cheat_loss_summary,
                                             l1_loss_summary,
                                             vgg_loss_summary,
                                             const_loss_summary,
                                             fake_s_category_loss_summary,
                                             fake_c_category_loss_summary,
                                             g_loss_summary])

        # expose useful nodes in the graph as handles globally
        input_handle    = InputHandle(real_data     = real_data,
                                      embedding_ids = embedding_ids)

        loss_handle     = LossHandle(d_loss         = d_loss,
                                     g_loss         = g_loss,
                                     l1_loss        = l1_loss,
                                     vgg_loss       = vgg_loss,
                                     const_loss     = const_loss,
                                     category_loss  = category_loss,
                                     cheat_loss     = cheat_loss)

        eval_handle     = EvalHandle(encoder    = encoded_real,
                                     fake_s     = fake_s,
                                     fake_c     = fake_c,
                                     source     = real_data,
                                     embedding  = embedding)

        summary_handle  = SummaryHandle(d_merged = d_merged_summary,
                                        g_merged = g_merged_summary)

        # those operations will be shared, so we need
        # to make them visible globally
        setattr(self, "input_handle", input_handle)
        setattr(self, "loss_handle", loss_handle)
        setattr(self, "eval_handle", eval_handle)
        setattr(self, "summary_handle", summary_handle)

    def register_session(self, sess):
        self.sess = sess

    def retrieve_trainable_vars(self, freeze_encoder=False):
        t_vars = tf.trainable_variables()

        d_vars = [var for var in t_vars if 'd_' in var.name]
        g_vars = [var for var in t_vars if 'g_' in var.name]

        if freeze_encoder:
            # exclude encoder weights
            print("freeze encoder weights")
            g_vars = [var for var in g_vars if not ("g_e" in var.name)]

        return g_vars, d_vars

    def retrieve_generator_vars(self):
        all_vars = tf.global_variables()
        generate_vars = [var for var in all_vars if 'embedding' in var.name or "g_" in var.name]
        return generate_vars

    def retrieve_handles(self):
        input_handle    = getattr(self, "input_handle")
        loss_handle     = getattr(self, "loss_handle")
        eval_handle     = getattr(self, "eval_handle")
        summary_handle  = getattr(self, "summary_handle")

        return input_handle, loss_handle, eval_handle, summary_handle

    def get_model_id_and_dir(self):
        model_id = "experiment_%d_batch_%d" % (self.experiment_id, self.batch_size)
        model_dir = os.path.join(self.checkpoint_dir, model_id)
        return model_id, model_dir

    def checkpoint(self, saver, step):
        model_name = "gegan.model"
        model_id, model_dir = self.get_model_id_and_dir()

        if not os.path.exists(model_dir):
            os.makedirs(model_dir)

        saver.save(self.sess, os.path.join(model_dir, model_name), global_step=step)

    def restore_model(self, saver, model_dir):

        ckpt = tf.train.get_checkpoint_state(model_dir)

        if ckpt:
            saver.restore(self.sess, ckpt.model_checkpoint_path)
            print("restored model %s" % model_dir)
        else:
            print("fail to restore model %s" % model_dir)

    def generate_fake_samples(self, input_images, embedding_ids):
        input_handle, loss_handle, eval_handle, summary_handle = self.retrieve_handles()
        fake_images, real_images, \
        d_loss, g_loss, l1_loss = self.sess.run([eval_handle.generator,
                                                 eval_handle.target,
                                                 loss_handle.d_loss,
                                                 loss_handle.g_loss,
                                                 loss_handle.l1_loss],
                                                feed_dict={
                                                    input_handle.real_data: input_images,
                                                    input_handle.embedding_ids: embedding_ids,
                                                    input_handle.no_target_data: input_images,
                                                    input_handle.no_target_ids: embedding_ids
                                                })
        return fake_images, real_images, d_loss, g_loss, l1_loss

    def validate_model(self, val_iter, epoch, step):
        labels, images = next(val_iter)
        fake_imgs, real_imgs, d_loss, g_loss, l1_loss = self.generate_fake_samples(images, labels)
        print("Sample: d_loss: %.5f, g_loss: %.5f, l1_loss: %.5f" % (d_loss, g_loss, l1_loss))

        merged_fake_images = merge(scale_back(fake_imgs), [self.batch_size, 1])
        merged_real_images = merge(scale_back(real_imgs), [self.batch_size, 1])
        merged_pair = np.concatenate([merged_real_images, merged_fake_images], axis=1)

        model_id, _ = self.get_model_id_and_dir()

        model_sample_dir = os.path.join(self.sample_dir, model_id)
        if not os.path.exists(model_sample_dir):
            os.makedirs(model_sample_dir)

        sample_img_path = os.path.join(model_sample_dir, "sample_%02d_%04d.png" % (epoch, step))
        misc.imsave(sample_img_path, merged_pair)

    def export_generator(self, save_dir, model_dir, model_name="gen_model"):
        saver = tf.train.Saver()
        self.restore_model(saver, model_dir)

        gen_saver = tf.train.Saver(var_list=self.retrieve_generator_vars())
        gen_saver.save(self.sess, os.path.join(save_dir, model_name), global_step=0)

    def infer(self, source_obj, embedding_ids, model_dir, save_dir):
        source_provider = InjectDataProvider(source_obj)

        if isinstance(embedding_ids, int) or len(embedding_ids) == 1:
            embedding_id = embedding_ids if isinstance(embedding_ids, int) else embedding_ids[0]
            source_iter = source_provider.get_single_embedding_iter(self.batch_size, embedding_id)
        else:
            source_iter = source_provider.get_random_embedding_iter(self.batch_size, embedding_ids)

        tf.global_variables_initializer().run()
        saver = tf.train.Saver(var_list=self.retrieve_generator_vars())
        self.restore_model(saver, model_dir)

        def save_imgs(imgs, count):
            p = os.path.join(save_dir, "inferred_%04d.png" % count)
            save_concat_images(imgs, img_path=p)
            print("generated images saved at %s" % p)

        count = 0
        batch_buffer = list()
        for labels, source_imgs in source_iter:
            fake_imgs = self.generate_fake_samples(source_imgs, labels)[0]
            merged_fake_images = merge(scale_back(fake_imgs), [self.batch_size, 1])
            batch_buffer.append(merged_fake_images)
            if len(batch_buffer) == 10:
                save_imgs(batch_buffer, count)
                batch_buffer = list()
            count += 1
        if batch_buffer:
            # last batch
            save_imgs(batch_buffer, count)

    def interpolate(self, source_obj, between, model_dir, save_dir, steps):
        tf.global_variables_initializer().run()
        saver = tf.train.Saver(var_list=self.retrieve_generator_vars())
        self.restore_model(saver, model_dir)
        # new interpolated dimension
        new_x_dim = steps + 1
        alphas = np.linspace(0.0, 1.0, new_x_dim)

        def _interpolate_tensor(_tensor):
            """
            Compute the interpolated tensor here
            """

            x = _tensor[between[0]]
            y = _tensor[between[1]]

            interpolated = list()
            for alpha in alphas:
                interpolated.append(x * (1. - alpha) + alpha * y)

            interpolated = np.asarray(interpolated, dtype=np.float32)
            return interpolated

        def filter_embedding_vars(var):
            var_name = var.name
            if var_name.find("embedding") != -1:
                return True
            if var_name.find("inst_norm/shift") != -1 or var_name.find("inst_norm/scale") != -1:
                return True
            return False

        embedding_vars = filter(filter_embedding_vars, tf.trainable_variables())
        # here comes the hack, we overwrite the original tensor
        # with interpolated ones. Note, the shape might differ

        # this is to restore the embedding at the end
        embedding_snapshot = list()
        for e_var in embedding_vars:
            val = e_var.eval(session=self.sess)
            embedding_snapshot.append((e_var, val))
            t = _interpolate_tensor(val)
            op = tf.assign(e_var, t, validate_shape=False)
            print("overwrite %s tensor" % e_var.name, "old_shape ->", e_var.get_shape(), "new shape ->", t.shape)
            self.sess.run(op)

        source_provider = InjectDataProvider(source_obj)
        input_handle, _, eval_handle, _ = self.retrieve_handles()
        for step_idx in range(len(alphas)):
            alpha = alphas[step_idx]
            print("interpolate %d -> %.4f + %d -> %.4f" % (between[0], 1. - alpha, between[1], alpha))
            source_iter = source_provider.get_single_embedding_iter(self.batch_size, 0)
            batch_buffer = list()
            count = 0
            for _, source_imgs in source_iter:
                count += 1
                labels = [step_idx] * self.batch_size
                generated, = self.sess.run([eval_handle.generator],
                                           feed_dict={
                                               input_handle.real_data: source_imgs,
                                               input_handle.embedding_ids: labels
                                           })
                merged_fake_images = merge(scale_back(generated), [self.batch_size, 1])
                batch_buffer.append(merged_fake_images)
            if len(batch_buffer):
                save_concat_images(batch_buffer,
                                   os.path.join(save_dir, "frame_%02d_%02d_step_%02d.png" % (
                                       between[0], between[1], step_idx)))
        # restore the embedding variables
        print("restore embedding values")
        for var, val in embedding_snapshot:
            op = tf.assign(var, val, validate_shape=False)
            self.sess.run(op)

    def train(self, lr=0.0002, epoch=100, schedule=10, resume=True, flip_labels=False,
              freeze_encoder=False, fine_tune=None, sample_steps=50, checkpoint_steps=1000):
        g_vars, d_vars = self.retrieve_trainable_vars(freeze_encoder=freeze_encoder)
        input_handle, loss_handle, eval_handle, summary_handle = self.retrieve_handles()

        if not self.sess:
            raise Exception("no session registered")

        learning_rate   = tf.placeholder(tf.float32, name="learning_rate")
        d_optimizer     = tf.train.AdamOptimizer(learning_rate, beta1=0.5).minimize(loss_handle.d_loss, var_list=d_vars)
        g_optimizer     = tf.train.AdamOptimizer(learning_rate, beta1=0.5).minimize(loss_handle.g_loss, var_list=g_vars)
        tf.global_variables_initializer().run()
        real_data       = input_handle.real_data
        embedding_ids   = input_handle.embedding_ids

        tf.train.start_queue_runners(sess=self.sess)
        saver = tf.train.Saver(max_to_keep=3)
        summary_writer = tf.summary.FileWriter(self.log_dir, self.sess.graph)

        if resume:
            _, model_dir = self.get_model_id_and_dir()
            self.restore_model(saver, model_dir)

        max_step    = 100000
        current_lr  = 0.0001
        log_step    = 50

        for t in trange(max_step):
            batch_images, labels = self.sess.run(self.train_dataloader)
            batch_images = batch_images / 127.5 - 1.0

            # optimize D
            _, batch_d_loss, d_summary = self.sess.run([d_optimizer,
                                                        loss_handle.d_loss,
                                                        summary_handle.d_merged],
                                                       feed_dict={
                                                           real_data: batch_images,
                                                           embedding_ids: labels,
                                                           learning_rate: current_lr
                                                       })
            # optimize G
            _, batch_g_loss = self.sess.run([g_optimizer, loss_handle.g_loss],
                                            feed_dict={
                                                real_data: batch_images,
                                                embedding_ids: labels,
                                                learning_rate: current_lr
                                            })

            # magic move to train G again
            # according to https://github.com/carpedm20/DCGAN-tensorflow
            # collect all the losses along the way
            _, batch_g_loss, category_loss, cheat_loss, \
            const_loss, l1_loss, vgg_loss, g_summary = self.sess.run([g_optimizer,
                                                                     loss_handle.g_loss,
                                                                     loss_handle.category_loss,
                                                                     loss_handle.cheat_loss,
                                                                     loss_handle.const_loss,
                                                                     loss_handle.l1_loss,
                                                                     loss_handle.vgg_loss,
                                                                     summary_handle.g_merged],
                                                                    feed_dict={
                                                                        real_data: batch_images,
                                                                        embedding_ids: labels,
                                                                        learning_rate: current_lr
                                                                    })

            if t % log_step == 0:
                print("[{}]/[{}] D_loss: {} G_loss: {} vgg_loss: {}".format(t, max_step, batch_d_loss, batch_g_loss, vgg_loss))
                fake_s, fake_c = self.sess.run([eval_handle.fake_s, eval_handle.fake_c],
                                               feed_dict={
                                                   real_data: batch_images,
                                                   embedding_ids: labels
                                               })
                real   = denormalize_image(batch_images)
                fake_s = denormalize_image(fake_s)
                fake_c = denormalize_image(fake_c)
                save_image(real,   os.path.join(self.experiment_dir, "sample", "{}_real.jpg".format(t)))
                save_image(fake_s, os.path.join(self.experiment_dir, "sample", "{}_fake_s.jpg".format(t)))
                save_image(fake_c, os.path.join(self.experiment_dir, "sample", "{}_fake_c.jpg".format(t)))

            if t % checkpoint_steps == 0:
                print("Checkpoint: save checkpoint step: {}".format(t))
                self.checkpoint(saver, t)
