# Copyright 2016 Goekcen Eraslan
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
# ==============================================================================

import os
import pickle
from abc import ABCMeta, abstractmethod

import numpy as np
import scanpy as sc

import tensorflow.keras
from tensorflow.keras.layers import Input, Dense, Dropout, Activation, BatchNormalization, Lambda
from tensorflow.keras.models import Model
from tensorflow.keras.regularizers import l1_l2
from keras.objectives import mean_squared_error
from tensorflow.keras.initializers import Constant
from tensorflow.keras import backend as K

import tensorflow as tf

from .loss import poisson_loss, NB, ZINB, CombNBLoss, CombNBLossSimple, CombNBLossSimpleExtra, CombNBPoissonLossExtra
from .layers import ConstantDispersionLayer, SliceLayer, ColwiseMultLayer, ElementwiseDense, Linear
from .io import write_text_matrix


MeanAct = lambda x: tf.clip_by_value(K.exp(x), 1e-5, 1e6)
DispAct = lambda x: tf.clip_by_value(tf.nn.softplus(x), 1e-4, 1e4)

advanced_activations = ('PReLU', 'LeakyReLU')

class Autoencoder():
    def __init__(self,
                 input_size,
                 output_size=None,
                 hidden_size=(64, 32, 64),
                 l2_coef=0.,
                 l1_coef=0.,
                 l2_enc_coef=0.,
                 l1_enc_coef=0.,
                 ridge=0.,
                 hidden_dropout=0.,
                 input_dropout=0.,
                 batchnorm=True,
                 activation='relu',
                 init='glorot_uniform',
                 file_path=None,
                 debug=False):

        self.input_size = input_size
        self.output_size = output_size
        self.hidden_size = hidden_size
        self.l2_coef = l2_coef
        self.l1_coef = l1_coef
        self.l2_enc_coef = l2_enc_coef
        self.l1_enc_coef = l1_enc_coef
        self.ridge = ridge
        self.hidden_dropout = hidden_dropout
        self.input_dropout = input_dropout
        self.batchnorm = batchnorm
        self.activation = activation
        self.init = init
        self.loss = None
        self.file_path = file_path
        self.extra_models = {}
        self.model = None
        self.encoder = None
        self.decoder = None
        self.input_layer = None
        self.sf_layer = None
        self.debug = debug

        if self.output_size is None:
            self.output_size = input_size

        if isinstance(self.hidden_dropout, list):
            assert len(self.hidden_dropout) == len(self.hidden_size)
        else:
            self.hidden_dropout = [self.hidden_dropout]*len(self.hidden_size)

    def build(self):

        self.input_layer = Input(shape=(self.input_size,), name='count')
        self.sf_layer = Input(shape=(1,), name='size_factors')
        last_hidden = self.input_layer

        if self.input_dropout > 0.0:
            last_hidden = Dropout(self.input_dropout, name='input_dropout')(last_hidden)

        for i, (hid_size, hid_drop) in enumerate(zip(self.hidden_size, self.hidden_dropout)):
            center_idx = int(np.floor(len(self.hidden_size) / 2.0))
            if i == center_idx:
                layer_name = 'center'
                stage = 'center'  # let downstream know where we are
            elif i < center_idx:
                layer_name = 'enc%s' % i
                stage = 'encoder'
            else:
                layer_name = 'dec%s' % (i-center_idx)
                stage = 'decoder'

            # use encoder-specific l1/l2 reg coefs if given
            if self.l1_enc_coef != 0. and stage in ('center', 'encoder'):
                l1 = self.l1_enc_coef
            else:
                l1 = self.l1_coef

            if self.l2_enc_coef != 0. and stage in ('center', 'encoder'):
                l2 = self.l2_enc_coef
            else:
                l2 = self.l2_coef

            last_hidden = Dense(hid_size, activation=None, kernel_initializer=self.init,
                                kernel_regularizer=l1_l2(l1, l2),
                                name=layer_name)(last_hidden)
            if self.batchnorm:
                last_hidden = BatchNormalization(center=True, scale=False)(last_hidden)

            # Use separate act. layers to give user the option to get pre-activations
            # of layers when requested
            if self.activation in advanced_activations:
                last_hidden = keras.layers.__dict__[self.activation](name='%s_act'%layer_name)(last_hidden)
            else:
                last_hidden = Activation(self.activation, name='%s_act'%layer_name)(last_hidden)

            if hid_drop > 0.0:
                last_hidden = Dropout(hid_drop, name='%s_drop'%layer_name)(last_hidden)

        self.decoder_output = last_hidden
        self.build_output()

    def build_output(self):

        self.loss = mean_squared_error
        mean = Dense(self.output_size, kernel_initializer=self.init,
                     kernel_regularizer=l1_l2(self.l1_coef, self.l2_coef),
                     name='mean')(self.decoder_output)
        output = ColwiseMultLayer([mean, self.sf_layer])

        # keep unscaled output as an extra model
        self.extra_models['mean_norm'] = Model(inputs=self.input_layer, outputs=mean)
        self.extra_models['decoded'] = Model(inputs=self.input_layer, outputs=self.decoder_output)
        self.model = Model(inputs=[self.input_layer, self.sf_layer], outputs=output)

        self.encoder = self.get_encoder()

    def save(self):
        if self.file_path:
            os.makedirs(self.file_path, exist_ok=True)
            with open(os.path.join(self.file_path, 'model.pickle'), 'wb') as f:
                pickle.dump(self, f)

    def load_weights(self, filename):
        self.model.load_weights(filename)
        self.encoder = self.get_encoder()
        self.decoder = None  # get_decoder()

    def get_decoder(self):
        i = 0
        for l in self.model.layers:
            if l.name == 'center_drop':
                break
            i += 1

        return Model(inputs=self.model.get_layer(index=i+1).input,
                     outputs=self.model.output)

    def get_encoder(self, activation=False):
        if activation:
            ret = Model(inputs=self.model.input,
                        outputs=self.model.get_layer('center_act').output)
        else:
            ret = Model(inputs=self.model.input,
                        outputs=self.model.get_layer('center').output)
        return ret

    def predict(self, adata, mode='denoise', return_info=False, copy=False):

        assert mode in ('denoise', 'latent', 'full'), 'Unknown mode'

        adata = adata.copy() if copy else adata

        if mode in ('denoise', 'full'):
            print('dca: Calculating reconstructions...')

            adata.X = self.model.predict({'count': adata.X,
                                          'size_factors': adata.obs.size_factors})

            #adata.uns['dca_loss'] = self.model.test_on_batch({'count': adata.X,
            #                                                  'size_factors': adata.obs.size_factors},
            #                                                 adata.raw.X)
        if mode in ('latent', 'full'):
            print('dca: Calculating low dimensional representations...')

            adata.obsm['X_dca'] = self.encoder.predict({'count': adata.X,
                                                        'size_factors': adata.obs.size_factors})
        if mode == 'latent':
            adata.X = adata.raw.X.copy() #recover normalized expression values

        return adata if copy else None

    def write(self, adata, file_path, mode='denoise', colnames=None):

        colnames = adata.var_names.values if colnames is None else colnames
        rownames = adata.obs_names.values

        print('dca: Saving output(s)...')
        os.makedirs(file_path, exist_ok=True)

        if mode in ('denoise', 'full'):
            print('dca: Saving denoised expression...')
            write_text_matrix(adata.X,
                              os.path.join(file_path, 'mean.tsv'),
                              rownames=rownames, colnames=colnames, transpose=True)

        if mode in ('latent', 'full'):
            print('dca: Saving latent representations...')
            write_text_matrix(adata.obsm['X_dca'],
                              os.path.join(file_path, 'latent.tsv'),
                              rownames=rownames, transpose=False)

class PoissonAutoencoder(Autoencoder):

    def build_output(self):
        mean = Dense(self.output_size, activation=MeanAct, kernel_initializer=self.init,
                     kernel_regularizer=l1_l2(self.l1_coef, self.l2_coef),
                     name='mean')(self.decoder_output)
        output = ColwiseMultLayer([mean, self.sf_layer])
        self.loss = poisson_loss

        self.extra_models['mean_norm'] = Model(inputs=self.input_layer, outputs=mean)
        self.extra_models['decoded'] = Model(inputs=self.input_layer, outputs=self.decoder_output)
        self.model = Model(inputs=[self.input_layer, self.sf_layer], outputs=output)

        self.encoder = self.get_encoder()


class NBConstantDispAutoencoder(Autoencoder):

    def build_output(self):
        mean = Dense(self.output_size, activation=MeanAct, kernel_initializer=self.init,
                     kernel_regularizer=l1_l2(self.l1_coef, self.l2_coef),
                     name='mean')(self.decoder_output)

        # Plug in dispersion parameters via fake dispersion layer
        disp = ConstantDispersionLayer(name='dispersion')
        mean = disp(mean)

        output = ColwiseMultLayer([mean, self.sf_layer])

        nb = NB(disp.theta_exp)
        self.loss = nb.loss
        self.extra_models['dispersion'] = lambda :K.function([], [nb.theta])([])[0].squeeze()
        self.extra_models['mean_norm'] = Model(inputs=self.input_layer, outputs=mean)
        self.extra_models['decoded'] = Model(inputs=self.input_layer, outputs=self.decoder_output)
        self.model = Model(inputs=[self.input_layer, self.sf_layer], outputs=output)

        self.encoder = self.get_encoder()

    def predict(self, adata, mode='denoise', return_info=False, copy=False):
        colnames = adata.var_names.values
        rownames = adata.obs_names.values
        res = super().predict(adata, mode, return_info, copy)
        adata = res if copy else adata

        if return_info:
            adata.var['X_dca_dispersion'] = self.extra_models['dispersion']()

        return adata if copy else None

    def write(self, adata, file_path, mode='denoise', colnames=None):
        colnames = adata.var_names.values if colnames is None else colnames
        rownames = adata.obs_names.values

        super().write(adata, file_path, mode, colnames=colnames)
        if 'X_dca_dispersion' in adata.var_keys():
            write_text_matrix(adata.var['X_dca_dispersion'].reshape(1, -1),
                              os.path.join(file_path, 'dispersion.tsv'),
                              colnames=colnames, transpose=True)


class NBAutoencoder(Autoencoder):

    def build_output(self):
        disp = Dense(self.output_size, activation=DispAct,
                           kernel_initializer=self.init,
                           kernel_regularizer=l1_l2(self.l1_coef,
                               self.l2_coef),
                           name='dispersion')(self.decoder_output)

        mean = Dense(self.output_size, activation=MeanAct, kernel_initializer=self.init,
                       kernel_regularizer=l1_l2(self.l1_coef, self.l2_coef),
                       name='mean')(self.decoder_output)
        output = ColwiseMultLayer([mean, self.sf_layer])
        output = SliceLayer(0, name='slice')([output, disp])

        nb = NB(theta=disp, debug=self.debug)
        self.loss = nb.loss
        self.extra_models['dispersion'] = Model(inputs=self.input_layer, outputs=disp)
        self.extra_models['mean_norm'] = Model(inputs=self.input_layer, outputs=mean)
        self.extra_models['decoded'] = Model(inputs=self.input_layer, outputs=self.decoder_output)

        self.model = Model(inputs=[self.input_layer, self.sf_layer], outputs=output)

        self.encoder = self.get_encoder()

    def predict(self, adata, mode='denoise', return_info=False, copy=False):
        colnames = adata.var_names.values
        rownames = adata.obs_names.values

        res = super().predict(adata, mode, return_info, copy)
        adata = res if copy else adata

        if return_info:
            adata.obsm['X_dca_dispersion'] = self.extra_models['dispersion'].predict(adata.X)

        return adata if copy else None

    def write(self, adata, file_path, mode='denoise', colnames=None):
        colnames = adata.var_names.values if colnames is None else colnames
        rownames = adata.obs_names.values

        super().write(adata, file_path, mode, colnames=colnames)

        if 'X_dca_dispersion' in adata.obsm_keys():
            write_text_matrix(adata.obsm['X_dca_dispersion'],
                              os.path.join(file_path, 'dispersion.tsv'),
                              colnames=colnames, transpose=True)

class NBSharedAutoencoder(NBAutoencoder):

    def build_output(self):
        disp = Dense(1, activation=DispAct,
                     kernel_initializer=self.init,
                     kernel_regularizer=l1_l2(self.l1_coef,
                                              self.l2_coef),
                     name='dispersion')(self.decoder_output)

        mean = Dense(self.output_size, activation=MeanAct, kernel_initializer=self.init,
                       kernel_regularizer=l1_l2(self.l1_coef, self.l2_coef),
                       name='mean')(self.decoder_output)
        output = ColwiseMultLayer([mean, self.sf_layer])
        output = SliceLayer(0, name='slice')([output, disp])

        nb = NB(theta=disp, debug=self.debug)
        self.loss = nb.loss
        self.extra_models['dispersion'] = Model(inputs=self.input_layer, outputs=disp)
        self.extra_models['mean_norm'] = Model(inputs=self.input_layer, outputs=mean)
        self.extra_models['decoded'] = Model(inputs=self.input_layer, outputs=self.decoder_output)

        self.model = Model(inputs=[self.input_layer, self.sf_layer], outputs=output)
        self.encoder = self.get_encoder()


class ZINBAutoencoder(Autoencoder):

    def build_output(self):
        pi = Dense(self.output_size, activation='sigmoid', kernel_initializer=self.init,
                       kernel_regularizer=l1_l2(self.l1_coef, self.l2_coef),
                       name='pi')(self.decoder_output)

        disp = Dense(self.output_size, activation=DispAct,
                           kernel_initializer=self.init,
                           kernel_regularizer=l1_l2(self.l1_coef, self.l2_coef),
                           name='dispersion')(self.decoder_output)

        mean = Dense(self.output_size, activation=MeanAct, kernel_initializer=self.init,
                       kernel_regularizer=l1_l2(self.l1_coef, self.l2_coef),
                       name='mean')(self.decoder_output)
        output = ColwiseMultLayer([mean, self.sf_layer])
        output = SliceLayer(0, name='slice')([output, disp, pi])

        zinb = ZINB(pi, theta=disp, ridge_lambda=self.ridge, debug=self.debug)
        self.loss = zinb.loss
        self.extra_models['pi'] = Model(inputs=self.input_layer, outputs=pi)
        self.extra_models['dispersion'] = Model(inputs=self.input_layer, outputs=disp)
        self.extra_models['mean_norm'] = Model(inputs=self.input_layer, outputs=mean)
        self.extra_models['decoded'] = Model(inputs=self.input_layer, outputs=self.decoder_output)

        self.model = Model(inputs=[self.input_layer, self.sf_layer], outputs=output)

        self.encoder = self.get_encoder()

    def predict(self, adata, mode='denoise', return_info=False, copy=False, colnames=None):

        adata = adata.copy() if copy else adata

        if return_info:
            adata.obsm['X_dca_dispersion'] = self.extra_models['dispersion'].predict(adata.X)
            adata.obsm['X_dca_dropout']    = self.extra_models['pi'].predict(adata.X)

        # warning! this may overwrite adata.X
        super().predict(adata, mode, return_info, copy=False)
        return adata if copy else None

    def write(self, adata, file_path, mode='denoise', colnames=None):
        colnames = adata.var_names.values if colnames is None else colnames
        rownames = adata.obs_names.values

        super().write(adata, file_path, mode, colnames=colnames)

        if 'X_dca_dispersion' in adata.obsm_keys():
            write_text_matrix(adata.obsm['X_dca_dispersion'],
                              os.path.join(file_path, 'dispersion.tsv'),
                              colnames=colnames, transpose=True)

        if 'X_dca_dropout' in adata.obsm_keys():
            write_text_matrix(adata.obsm['X_dca_dropout'],
                              os.path.join(file_path, 'dropout.tsv'),
                              colnames=colnames, transpose=True)


class ZINBAutoencoderElemPi(ZINBAutoencoder):
    def __init__(self, sharedpi=False, **kwds):
        super().__init__(**kwds)
        self.sharedpi = sharedpi

    def build_output(self):
        disp = Dense(self.output_size, activation=DispAct,
                           kernel_initializer=self.init,
                           kernel_regularizer=l1_l2(self.l1_coef, self.l2_coef),
                           name='dispersion')(self.decoder_output)

        mean_no_act = Dense(self.output_size, activation=None, kernel_initializer=self.init,
                       kernel_regularizer=l1_l2(self.l1_coef, self.l2_coef),
                       name='mean_no_act')(self.decoder_output)

        minus = Lambda(lambda x: -x)
        mean_no_act = minus(mean_no_act)
        pidim = self.output_size if not self.sharedpi else 1

        pi = ElementwiseDense(pidim, activation='sigmoid', kernel_initializer=self.init,
                       kernel_regularizer=l1_l2(self.l1_coef, self.l2_coef),
                       name='pi')(mean_no_act)

        mean = Activation(MeanAct, name='mean')(mean_no_act)

        output = ColwiseMultLayer([mean, self.sf_layer])
        output = SliceLayer(0, name='slice')([output, disp, pi])

        zinb = ZINB(pi, theta=disp, ridge_lambda=self.ridge, debug=self.debug)
        self.loss = zinb.loss
        self.extra_models['pi'] = Model(inputs=self.input_layer, outputs=pi)
        self.extra_models['dispersion'] = Model(inputs=self.input_layer, outputs=disp)
        self.extra_models['mean_norm'] = Model(inputs=self.input_layer, outputs=mean)
        self.extra_models['decoded'] = Model(inputs=self.input_layer, outputs=self.decoder_output)

        self.model = Model(inputs=[self.input_layer, self.sf_layer], outputs=output)

        self.encoder = self.get_encoder()



class ZINBSharedAutoencoder(ZINBAutoencoder):

    def build_output(self):
        pi = Dense(1, activation='sigmoid', kernel_initializer=self.init,
                   kernel_regularizer=l1_l2(self.l1_coef, self.l2_coef),
                   name='pi')(self.decoder_output)

        disp = Dense(1, activation=DispAct,
                     kernel_initializer=self.init,
                     kernel_regularizer=l1_l2(self.l1_coef,
                                              self.l2_coef),
                     name='dispersion')(self.decoder_output)

        mean = Dense(self.output_size, activation=MeanAct, kernel_initializer=self.init,
                       kernel_regularizer=l1_l2(self.l1_coef, self.l2_coef),
                       name='mean')(self.decoder_output)
        output = ColwiseMultLayer([mean, self.sf_layer])
        output = SliceLayer(0, name='slice')([output, disp, pi])

        zinb = ZINB(pi, theta=disp, ridge_lambda=self.ridge, debug=self.debug)
        self.loss = zinb.loss
        self.extra_models['pi'] = Model(inputs=self.input_layer, outputs=pi)
        self.extra_models['dispersion'] = Model(inputs=self.input_layer, outputs=disp)
        self.extra_models['mean_norm'] = Model(inputs=self.input_layer, outputs=mean)
        self.extra_models['decoded'] = Model(inputs=self.input_layer, outputs=self.decoder_output)

        self.model = Model(inputs=[self.input_layer, self.sf_layer], outputs=output)

        self.encoder = self.get_encoder()


class ZINBConstantDispAutoencoder(Autoencoder):

    def build_output(self):
        pi = Dense(self.output_size, activation='sigmoid', kernel_initializer=self.init,
                   kernel_regularizer=l1_l2(self.l1_coef, self.l2_coef),
                   name='pi')(self.decoder_output)

        mean = Dense(self.output_size, activation=MeanAct, kernel_initializer=self.init,
                     kernel_regularizer=l1_l2(self.l1_coef, self.l2_coef),
                     name='mean')(self.decoder_output)

        # NB dispersion layer
        disp = ConstantDispersionLayer(name='dispersion')
        mean = disp(mean)

        output = ColwiseMultLayer([mean, self.sf_layer])

        zinb = ZINB(pi, theta=disp.theta_exp, ridge_lambda=self.ridge, debug=self.debug)
        self.loss = zinb.loss
        self.extra_models['pi'] = Model(inputs=self.input_layer, outputs=pi)
        self.extra_models['dispersion'] = lambda :K.function([], [zinb.theta])([])[0].squeeze()
        self.extra_models['mean_norm'] = Model(inputs=self.input_layer, outputs=mean)
        self.extra_models['decoded'] = Model(inputs=self.input_layer, outputs=self.decoder_output)

        self.model = Model(inputs=[self.input_layer, self.sf_layer], outputs=output)

        self.encoder = self.get_encoder()

    def predict(self, adata, mode='denoise', return_info=False, copy=False):
        colnames = adata.var_names.values
        rownames = adata.obs_names.values
        adata = adata.copy() if copy else adata

        if return_info:
            adata.var['X_dca_dispersion'] = self.extra_models['dispersion']()
            adata.obsm['X_dca_dropout']    = self.extra_models['pi'].predict(adata.X)

        super().predict(adata, mode, return_info, copy=False)
        return adata if copy else None

    def write(self, adata, file_path, mode='denoise', colnames=None):
        colnames = adata.var_names.values if colnames is None else colnames
        rownames = adata.obs_names.values

        super().write(adata, file_path, mode)

        if 'X_dca_dispersion' in adata.var_keys():
            write_text_matrix(adata.var['X_dca_dispersion'].values.reshape(1, -1),
                              os.path.join(file_path, 'dispersion.tsv'),
                              colnames=colnames, transpose=True)

        if 'X_dca_dropout' in adata.obsm_keys():
            write_text_matrix(adata.obsm['X_dca_dropout'],
                              os.path.join(file_path, 'dropout.tsv'),
                              colnames=colnames, transpose=True)


class ZINBForkAutoencoder(ZINBAutoencoder):

    def build(self):

        self.input_layer = Input(shape=(self.input_size,), name='count')
        self.sf_layer = Input(shape=(1,), name='size_factors')
        last_hidden = self.input_layer

        if self.input_dropout > 0.0:
            last_hidden = Dropout(self.input_dropout, name='input_dropout')(last_hidden)

        for i, (hid_size, hid_drop) in enumerate(zip(self.hidden_size, self.hidden_dropout)):
            center_idx = int(np.floor(len(self.hidden_size) / 2.0))
            if i == center_idx:
                layer_name = 'center'
                stage = 'center'  # let downstream know where we are
            elif i < center_idx:
                layer_name = 'enc%s' % i
                stage = 'encoder'
            else:
                layer_name = 'dec%s' % (i-center_idx)
                stage = 'decoder'

            # use encoder-specific l1/l2 reg coefs if given
            if self.l1_enc_coef != 0. and stage in ('center', 'encoder'):
                l1 = self.l1_enc_coef
            else:
                l1 = self.l1_coef

            if self.l2_enc_coef != 0. and stage in ('center', 'encoder'):
                l2 = self.l2_enc_coef
            else:
                l2 = self.l2_coef

            if i > center_idx:
                self.last_hidden_mean = Dense(hid_size, activation=None, kernel_initializer=self.init,
                                    kernel_regularizer=l1_l2(l1, l2),
                                    name='%s_last_mean'%layer_name)(last_hidden)
                self.last_hidden_disp = Dense(hid_size, activation=None, kernel_initializer=self.init,
                                    kernel_regularizer=l1_l2(l1, l2),
                                    name='%s_last_disp'%layer_name)(last_hidden)
                self.last_hidden_pi = Dense(hid_size, activation=None, kernel_initializer=self.init,
                                    kernel_regularizer=l1_l2(l1, l2),
                                    name='%s_last_pi'%layer_name)(last_hidden)

                if self.batchnorm:
                    self.last_hidden_mean = BatchNormalization(center=True, scale=False)(self.last_hidden_mean)
                    self.last_hidden_disp = BatchNormalization(center=True, scale=False)(self.last_hidden_disp)
                    self.last_hidden_pi = BatchNormalization(center=True, scale=False)(self.last_hidden_pi)

                # Use separate act. layers to give user the option to get pre-activations
                # of layers when requested
                self.last_hidden_mean = Activation(self.activation, name='%s_mean_act'%layer_name)(self.last_hidden_mean)
                self.last_hidden_disp = Activation(self.activation, name='%s_disp_act'%layer_name)(self.last_hidden_disp)
                self.last_hidden_pi = Activation(self.activation, name='%s_pi_act'%layer_name)(self.last_hidden_pi)

                if hid_drop > 0.0:
                    self.last_hidden_mean = Dropout(hid_drop, name='%s_mean_drop'%layer_name)(self.last_hidden_mean)
                    self.last_hidden_disp = Dropout(hid_drop, name='%s_disp_drop'%layer_name)(self.last_hidden_disp)
                    self.last_hidden_pi = Dropout(hid_drop, name='%s_pi_drop'%layer_name)(self.last_hidden_pi)

            else:
                last_hidden = Dense(hid_size, activation=None, kernel_initializer=self.init,
                                    kernel_regularizer=l1_l2(l1, l2),
                                    name=layer_name)(last_hidden)

                if self.batchnorm:
                    last_hidden = BatchNormalization(center=True, scale=False)(last_hidden)

                # Use separate act. layers to give user the option to get pre-activations
                # of layers when requested
                if self.activation in advanced_activations:
                    last_hidden = keras.layers.__dict__[self.activation](name='%s_act'%layer_name)(last_hidden)
                else:
                    last_hidden = Activation(self.activation, name='%s_act'%layer_name)(last_hidden)

                if hid_drop > 0.0:
                    last_hidden = Dropout(hid_drop, name='%s_drop'%layer_name)(last_hidden)

        self.build_output()


    def build_output(self):
        pi = Dense(self.output_size, activation='sigmoid', kernel_initializer=self.init,
                       kernel_regularizer=l1_l2(self.l1_coef, self.l2_coef),
                       name='pi')(self.last_hidden_pi)

        disp = Dense(self.output_size, activation=DispAct,
                           kernel_initializer=self.init,
                           kernel_regularizer=l1_l2(self.l1_coef, self.l2_coef),
                           name='dispersion')(self.last_hidden_disp)

        mean = Dense(self.output_size, activation=MeanAct, kernel_initializer=self.init,
                       kernel_regularizer=l1_l2(self.l1_coef, self.l2_coef),
                       name='mean')(self.last_hidden_mean)

        output = ColwiseMultLayer([mean, self.sf_layer])
        output = SliceLayer(0, name='slice')([output, disp, pi])

        zinb = ZINB(pi, theta=disp, ridge_lambda=self.ridge, debug=self.debug)
        self.loss = zinb.loss
        self.extra_models['pi'] = Model(inputs=self.input_layer, outputs=pi)
        self.extra_models['dispersion'] = Model(inputs=self.input_layer, outputs=disp)
        self.extra_models['mean_norm'] = Model(inputs=self.input_layer, outputs=mean)

        self.model = Model(inputs=[self.input_layer, self.sf_layer], outputs=output)

        self.encoder = self.get_encoder()


class NBForkAutoencoder(NBAutoencoder):

    def build(self):

        self.input_layer = Input(shape=(self.input_size,), name='count')
        self.sf_layer = Input(shape=(1,), name='size_factors')
        last_hidden = self.input_layer

        if self.input_dropout > 0.0:
            last_hidden = Dropout(self.input_dropout, name='input_dropout')(last_hidden)

        for i, (hid_size, hid_drop) in enumerate(zip(self.hidden_size, self.hidden_dropout)):
            center_idx = int(np.floor(len(self.hidden_size) / 2.0))
            if i == center_idx:
                layer_name = 'center'
                stage = 'center'  # let downstream know where we are
            elif i < center_idx:
                layer_name = 'enc%s' % i
                stage = 'encoder'
            else:
                layer_name = 'dec%s' % (i-center_idx)
                stage = 'decoder'

            # use encoder-specific l1/l2 reg coefs if given
            if self.l1_enc_coef != 0. and stage in ('center', 'encoder'):
                l1 = self.l1_enc_coef
            else:
                l1 = self.l1_coef

            if self.l2_enc_coef != 0. and stage in ('center', 'encoder'):
                l2 = self.l2_enc_coef
            else:
                l2 = self.l2_coef

            if i > center_idx:
                self.last_hidden_mean = Dense(hid_size, activation=None, kernel_initializer=self.init,
                                    kernel_regularizer=l1_l2(l1, l2),
                                    name='%s_last_mean'%layer_name)(last_hidden)
                self.last_hidden_disp = Dense(hid_size, activation=None, kernel_initializer=self.init,
                                    kernel_regularizer=l1_l2(l1, l2),
                                    name='%s_last_disp'%layer_name)(last_hidden)

                if self.batchnorm:
                    self.last_hidden_mean = BatchNormalization(center=True, scale=False)(self.last_hidden_mean)
                    self.last_hidden_disp = BatchNormalization(center=True, scale=False)(self.last_hidden_disp)

                # Use separate act. layers to give user the option to get pre-activations
                # of layers when requested
                self.last_hidden_mean = Activation(self.activation, name='%s_mean_act'%layer_name)(self.last_hidden_mean)
                self.last_hidden_disp = Activation(self.activation, name='%s_disp_act'%layer_name)(self.last_hidden_disp)

                if hid_drop > 0.0:
                    self.last_hidden_mean = Dropout(hid_drop, name='%s_mean_drop'%layer_name)(self.last_hidden_mean)
                    self.last_hidden_disp = Dropout(hid_drop, name='%s_disp_drop'%layer_name)(self.last_hidden_disp)

            else:
                last_hidden = Dense(hid_size, activation=None, kernel_initializer=self.init,
                                    kernel_regularizer=l1_l2(l1, l2),
                                    name=layer_name)(last_hidden)

                if self.batchnorm:
                    last_hidden = BatchNormalization(center=True, scale=False)(last_hidden)

                # Use separate act. layers to give user the option to get pre-activations
                # of layers when requested
                if self.activation in advanced_activations:
                    last_hidden = keras.layers.__dict__[self.activation](name='%s_act'%layer_name)(last_hidden)
                else:
                    last_hidden = Activation(self.activation, name='%s_act'%layer_name)(last_hidden)

                if hid_drop > 0.0:
                    last_hidden = Dropout(hid_drop, name='%s_drop'%layer_name)(last_hidden)

        self.build_output()


    def build_output(self):

        disp = Dense(self.output_size, activation=DispAct,
                           kernel_initializer=self.init,
                           kernel_regularizer=l1_l2(self.l1_coef, self.l2_coef),
                           name='dispersion')(self.last_hidden_disp)

        mean = Dense(self.output_size, activation=MeanAct, kernel_initializer=self.init,
                       kernel_regularizer=l1_l2(self.l1_coef, self.l2_coef),
                       name='mean')(self.last_hidden_mean)

        output = ColwiseMultLayer([mean, self.sf_layer])
        output = SliceLayer(0, name='slice')([output, disp])

        nb = NB(theta=disp, debug=self.debug)
        self.loss = nb.loss
        self.extra_models['dispersion'] = Model(inputs=self.input_layer, outputs=disp)
        self.extra_models['mean_norm'] = Model(inputs=self.input_layer, outputs=mean)

        self.model = Model(inputs=[self.input_layer, self.sf_layer], outputs=output)

        self.encoder = self.get_encoder()

class CombNBAutoencoder(Autoencoder):

    def build_output(self):
        pi = Dense(self.output_size, activation='sigmoid', kernel_initializer=self.init,
                       kernel_regularizer=l1_l2(self.l1_coef, self.l2_coef),
                       name='pi')(self.decoder_output)

        disp1 = Dense(self.output_size, activation=DispAct,
                           kernel_initializer=self.init,
                           kernel_regularizer=l1_l2(self.l1_coef, self.l2_coef),
                           name='dispersion1')(self.decoder_output)

        disp2 = Dense(self.output_size, activation=DispAct,
                           kernel_initializer=self.init,
                           kernel_regularizer=l1_l2(self.l1_coef, self.l2_coef),
                           name='dispersion2')(self.decoder_output)

        mean1 = Dense(self.output_size, activation=MeanAct, kernel_initializer=self.init,
                       kernel_regularizer=l1_l2(self.l1_coef, self.l2_coef),
                       name='mean1')(self.decoder_output)
        alpha = Dense(self.output_size, activation='softmax', kernel_initializer=self.init,
                       kernel_regularizer=l1_l2(self.l1_coef, self.l2_coef),
                       name='alpha')(self.decoder_output)
        enzyme_cells = Linear(input_dim=self.hidden_size[2],name='enzyme_cells',activation=tf.keras.activations.sigmoid)(self.decoder_output)
        mean2 = mean1*alpha
        output = ColwiseMultLayer([pi, enzyme_cells, self.sf_layer])
        output = SliceLayer(0, name='slice')([output, mean1, disp1, disp2, alpha])

        combnb = CombNBLossSimpleExtra(enzyme_cells=enzyme_cells, mean1=mean1, mean2=mean2, theta1=disp1, theta2=disp2, debug=self.debug, scale_factor=self.sf_layer)
        self.loss = combnb.loss
        self.extra_models['pi'] = Model(inputs=self.input_layer, outputs=pi)
        self.extra_models['dispersion1'] = Model(inputs=self.input_layer, outputs=disp1)
        self.extra_models['dispersion2'] = Model(inputs=self.input_layer, outputs=disp2)
        self.extra_models['mean1_norm'] = Model(inputs=self.input_layer, outputs=mean1)
        self.extra_models['alpha'] = Model(inputs=self.input_layer, outputs=alpha)
        self.extra_models['enzyme_cells'] = Model(inputs=self.input_layer, outputs=enzyme_cells)
        self.extra_models['decoded'] = Model(inputs=self.input_layer, outputs=self.decoder_output)

        self.model = Model(inputs=[self.input_layer, self.sf_layer], outputs=output)

        self.encoder = self.get_encoder()

    def predict(self, adata, mode='denoise', return_info=True, copy=False, colnames=None):

        adata = adata.copy() if copy else adata

        if return_info:
            adata.obsm['X_meth_dispersion1'] = self.extra_models['dispersion1'].predict(adata.X)
            adata.obsm['X_meth_dispersion2'] = self.extra_models['dispersion2'].predict(adata.X)
            adata.obsm['X_meth_value']    = self.extra_models['pi'].predict(adata.X)
            adata.obsm['alpha']    = self.extra_models['alpha'].predict(adata.X)
            adata.obsm['mean1_norm']    = self.extra_models['mean1_norm'].predict(adata.X)
            adata.obsm['X_enzyme_activity']    = self.extra_models['enzyme_cells'].predict(adata.X)

        # warning! this may overwrite adata.X
        super().predict(adata, mode, return_info, copy=False)
        return adata if copy else None

    def write(self, adata, file_path, mode='denoise', colnames=None):
        colnames = adata.var_names.values if colnames is None else colnames
        rownames = adata.obs_names.values

        super().write(adata, file_path, mode, colnames=colnames)

        if 'X_meth_dispersion' in adata.obsm_keys():
            write_text_matrix(adata.obsm['X_meth_dispersion1'],
                              os.path.join(file_path, 'dispersion1.tsv'),
                              colnames=colnames, transpose=True)

        if 'X_meth_dispersion2' in adata.obsm_keys():
            write_text_matrix(adata.obsm['X_meth_dispersion2'],
                              os.path.join(file_path, 'dispersion2.tsv'),
                              colnames=colnames, transpose=True)

        if 'X_meth_value' in adata.obsm_keys():
            write_text_matrix(adata.obsm['X_meth_value'],
                              os.path.join(file_path, 'meth_value.tsv'),
                              colnames=colnames, transpose=True)

        if 'X_alpha' in adata.obsm_keys():
            write_text_matrix(adata.obsm['X_alpha'],
                              os.path.join(file_path, 'alpha.tsv'),
                              colnames=colnames, transpose=True)

        if 'X_mean' in adata.obsm_keys():
            write_text_matrix(adata.obsm['X_mean'],
                              os.path.join(file_path, 'mean.tsv'),
                              colnames=colnames, transpose=True)

class CombNBAutoencoderConstantDispersion(CombNBAutoencoder):

    def build_output(self):
        pi = Dense(self.output_size, activation='sigmoid', kernel_initializer=self.init,
                       kernel_regularizer=l1_l2(self.l1_coef, self.l2_coef),
                       name='pi')(self.decoder_output)

        mean1 = Dense(self.output_size, activation=MeanAct, kernel_initializer=self.init,
                       kernel_regularizer=l1_l2(self.l1_coef, self.l2_coef),
                       name='mean1')(self.decoder_output)
        enzyme_cells = Linear(input_dim=self.hidden_size[2],name='enzyme_cells',activation=tf.keras.activations.sigmoid)(self.decoder_output)
        disp1 = ConstantDispersionLayer(name='dispersion1')
        mean1 = disp1(mean1)
        alpha = Dense(self.output_size, activation='softmax', kernel_initializer=self.init,
                       kernel_regularizer=l1_l2(self.l1_coef, self.l2_coef),
                       name='alpha')(self.decoder_output)
        mean2 = mean1*alpha
        disp2 = ConstantDispersionLayer(name='dispersion2')
        mean2 = disp2(mean2)
        output = ColwiseMultLayer([pi, enzyme_cells, self.sf_layer])
        output = SliceLayer(0, name='slice')([output, mean1, alpha, mean2])

        combnb = CombNBLossSimpleExtra(enzyme_cells=enzyme_cells, mean1=mean1, mean2=mean2, theta1=disp1.theta_exp, theta2=disp2.theta_exp, debug=self.debug, scale_factor=self.sf_layer)
        self.loss = combnb.loss
        self.extra_models['pi'] = Model(inputs=self.input_layer, outputs=pi)
        self.extra_models['enzyme_cells'] = Model(inputs=self.input_layer, outputs=enzyme_cells)
        self.extra_models['dispersion1'] = lambda :K.function([], [combnb.theta1])([])[0].squeeze()
        self.extra_models['dispersion2'] = lambda :K.function([], [combnb.theta2])([])[0].squeeze()
        self.extra_models['mean1_norm'] = Model(inputs=self.input_layer, outputs=mean1)
        self.extra_models['alpha'] = Model(inputs=self.input_layer, outputs=alpha)
        self.extra_models['decoded'] = Model(inputs=self.input_layer, outputs=self.decoder_output)

        self.model = Model(inputs=[self.input_layer, self.sf_layer], outputs=output)

        self.encoder = self.get_encoder()

    def predict(self, adata, mode='denoise', return_info=True, copy=False, colnames=None):

        adata = adata.copy() if copy else adata

        if return_info:
            adata.var['meth_dispersion1'] = self.extra_models['dispersion1']()
            adata.var['meth_dispersion2'] = self.extra_models['dispersion2']()
            adata.obsm['X_meth_value']    = self.extra_models['pi'].predict(adata.X)
            adata.obsm['X_enzyme_activity']    = self.extra_models['enzyme_cells'].predict(adata.X)
            adata.obsm['alpha']    = self.extra_models['alpha'].predict(adata.X)
            adata.obsm['mean1_norm']    = self.extra_models['mean1_norm'].predict(adata.X)

        # warning! this may overwrite adata.X
        #super().predict(adata, mode, return_info, copy=False)
        return adata if copy else None

class CombNBPoissonAutoencoder(Autoencoder):

    def build_output(self):
        pi = Dense(self.output_size, activation='sigmoid', kernel_initializer=self.init,
                       kernel_regularizer=l1_l2(self.l1_coef, self.l2_coef),
                       name='pi')(self.decoder_output)

        mean_nb = Dense(self.output_size, activation=MeanAct, kernel_initializer=self.init,
                       kernel_regularizer=l1_l2(self.l1_coef, self.l2_coef),
                       name='mean_nb')(self.decoder_output)
        disp_nb = Dense(self.output_size, activation=DispAct,
                           kernel_initializer=self.init,
                           kernel_regularizer=l1_l2(self.l1_coef, self.l2_coef),
                           name='dispersion_nb')(self.decoder_output)
        #enzyme_cells = Linear(input_dim=self.hidden_size[2],name='enzyme_cells',activation=tf.keras.activations.sigmoid)(self.decoder_output)
        enzyme_cells = Linear(input_dim=self.hidden_size[2],name='enzyme_cells',activation=tf.keras.activations.sigmoid)(self.decoder_output)
        lambda_poisson = Dense(self.output_size, activation=MeanAct, kernel_initializer=self.init,
                       kernel_regularizer=l1_l2(self.l1_coef, self.l2_coef),
                       name='lambda_poisson')(self.decoder_output)
        output = ColwiseMultLayer([mean_nb, enzyme_cells, self.sf_layer])
        output = SliceLayer(0, name='slice')([output, pi, lambda_poisson, disp_nb])

        combnbpoisson = CombNBPoissonLossExtra(enzyme_cells=enzyme_cells, pi=pi, lambda_poisson=lambda_poisson, theta=disp_nb, debug=self.debug, scale_factor=self.sf_layer)
        self.loss = combnbpoisson.loss
        self.extra_models['pi'] = Model(inputs=self.input_layer, outputs=pi)
        self.extra_models['dispersion_nb'] = Model(inputs=self.input_layer, outputs=disp_nb)
        self.extra_models['enzyme_cells'] = Model(inputs=self.input_layer, outputs=enzyme_cells)
        self.extra_models['mean_nb_norm'] = Model(inputs=self.input_layer, outputs=mean_nb)
        self.extra_models['lambda_poisson'] = Model(inputs=self.input_layer, outputs=lambda_poisson)
        self.extra_models['decoded'] = Model(inputs=self.input_layer, outputs=self.decoder_output)

        self.model = Model(inputs=[self.input_layer, self.sf_layer], outputs=output)

        self.encoder = self.get_encoder()

    def predict(self, adata, mode='denoise', return_info=True, copy=False, colnames=None):

        adata = adata.copy() if copy else adata

        if return_info:
            adata.obsm['X_meth_dispersion_nb'] = self.extra_models['dispersion_nb'].predict(adata.X)
            adata.obsm['X_meth_value']    = self.extra_models['pi'].predict(adata.X)
            adata.obsm['lambda_poisson']    = self.extra_models['lambda_poisson'].predict(adata.X)
            adata.obsm['mean_nb_norm']    = self.extra_models['mean_nb_norm'].predict(adata.X)
            adata.obsm['X_enzyme_activity']    = self.extra_models['enzyme_cells'].predict(adata.X)

        # warning! this may overwrite adata.X
        #super().predict(adata, mode, return_info, copy=False)
        return adata if copy else None

class CombNBPoissonAutoencoderConstantDispersion(Autoencoder):

    def build_output(self):
        pi = Dense(self.output_size, activation='sigmoid', kernel_initializer=self.init,
                       kernel_regularizer=l1_l2(self.l1_coef, self.l2_coef),
                       name='pi')(self.decoder_output)

        mean_nb = Dense(self.output_size, activation=MeanAct, kernel_initializer=self.init,
                       kernel_regularizer=l1_l2(self.l1_coef, self.l2_coef),
                       name='mean_nb')(self.decoder_output)
        disp_nb = ConstantDispersionLayer(name='dispersion_nb')
        mean_nb = disp_nb(mean_nb)
        enzyme_cells = Linear(input_dim=self.hidden_size[2],name='enzyme_cells',activation=tf.keras.activations.sigmoid)(self.decoder_output)
        lambda_poisson = Dense(self.output_size, activation=MeanAct, kernel_initializer=self.init,
                       kernel_regularizer=l1_l2(self.l1_coef, self.l2_coef),
                       name='lambda_poisson')(self.decoder_output)
        output = ColwiseMultLayer([mean_nb, enzyme_cells, self.sf_layer])
        output = SliceLayer(0, name='slice')([output, pi, lambda_poisson])

        combnbpoisson = CombNBPoissonLossExtra(enzyme_cells=enzyme_cells, pi=pi, lambda_poisson=lambda_poisson, theta=disp_nb.theta_exp, debug=self.debug, scale_factor=self.sf_layer)
        self.loss = combnbpoisson.loss
        self.extra_models['pi'] = Model(inputs=self.input_layer, outputs=pi)
        self.extra_models['enzyme_cells'] = Model(inputs=self.input_layer, outputs=enzyme_cells)
        self.extra_models['dispersion_nb'] = lambda :K.function([], [combnbpoisson.theta])([])[0].squeeze()
        self.extra_models['mean_nb_norm'] = Model(inputs=self.input_layer, outputs=mean_nb)
        self.extra_models['lambda_poisson'] = Model(inputs=self.input_layer, outputs=lambda_poisson)
        self.extra_models['decoded'] = Model(inputs=self.input_layer, outputs=self.decoder_output)

        self.model = Model(inputs=[self.input_layer, self.sf_layer], outputs=output)

        self.encoder = self.get_encoder()

    def predict(self, adata, mode='denoise', return_info=True, copy=False, colnames=None):

        adata = adata.copy() if copy else adata

        if return_info:
            adata.var['meth_dispersion_nb'] = self.extra_models['dispersion_nb']()
            adata.obsm['X_meth_value']    = self.extra_models['pi'].predict(adata.X)
            adata.obsm['lambda_poisson']    = self.extra_models['lambda_poisson'].predict(adata.X)
            adata.obsm['mean_nb_norm']    = self.extra_models['mean_nb_norm'].predict(adata.X)
            adata.obsm['X_enzyme_activity']    = self.extra_models['enzyme_cells'].predict(adata.X)

        # warning! this may overwrite adata.X
        #super().predict(adata, mode, return_info, copy=False)
        return adata if copy else None

class CombNBSimpleAutoencoder(Autoencoder):

    def build_output(self):
        pi = Dense(self.output_size, activation='sigmoid', kernel_initializer=self.init,
                       kernel_regularizer=l1_l2(self.l1_coef, self.l2_coef),
                       name='pi')(self.decoder_output)

#        alpha = Linear(input_dim=self.output_size,constraint=lambda z: tf.clip_by_value(z, 0, 1),name='alpha')(pi)
#        mean2 = mean1*alpha
        disp1 = Linear(input_dim=self.output_size,name='dispersion1')(pi)
        disp2 = Linear(input_dim=self.output_size,name='dispersion2')(pi)
        mean1 = Linear(input_dim=1,name='mean1')(disp1)
        alpha = Linear(input_dim=1,name='alpha',constraint=lambda z: tf.clip_by_value(z, 0, 1))(disp2)
        mean2 = mean1*alpha
        output = ColwiseMultLayer([pi, self.sf_layer])
        output = SliceLayer(0, name='slice')([output, mean1, mean2, disp1, disp2])

        combnb = CombNBLossSimple(mean1=mean1, mean2=mean2, theta1=disp1, theta2=disp2, debug=self.debug, scale_factor=self.sf_layer)
        self.loss = combnb.loss
        self.extra_models['pi'] = Model(inputs=self.input_layer, outputs=pi)
        self.extra_models['mean1'] = Model(inputs=self.input_layer, outputs=mean1)
        self.extra_models['mean2'] = Model(inputs=self.input_layer, outputs=mean2)
#        self.extra_models['mean1_norm'] = Model(inputs=self.input_layer, outputs=mean1)
#        self.extra_models['alpha'] = Model(inputs=self.input_layer, outputs=alpha)
        self.extra_models['decoded'] = Model(inputs=self.input_layer, outputs=self.decoder_output)

        self.model = Model(inputs=[self.input_layer, self.sf_layer], outputs=output)

        self.encoder = self.get_encoder()

    def predict(self, adata, mode='denoise', return_info=True, copy=False, colnames=None):

        adata = adata.copy() if copy else adata

        if return_info:
#            adata.obsm['X_meth_dispersion1'] = self.extra_models['dispersion1'].predict(adata.X)
#            adata.obsm['X_meth_dispersion2'] = self.extra_models['dispersion2'].predict(adata.X)
            adata.obsm['X_meth_value']    = self.extra_models['pi'].predict(adata.X)
            adata.obsm['mean1_norm']    = self.extra_models['mean1'].predict(adata.X)
            adata.obsm['mean2_norm']    = self.extra_models['mean2'].predict(adata.X)
#            adata.obsm['alpha']    = self.extra_models['alpha'].predict(adata.X)

        # warning! this may overwrite adata.X
        super().predict(adata, mode, return_info, copy=False)
        return adata if copy else None

    def write(self, adata, file_path, mode='denoise', colnames=None):
        colnames = adata.var_names.values if colnames is None else colnames
        rownames = adata.obs_names.values

        super().write(adata, file_path, mode, colnames=colnames)

#        if 'X_meth_dispersion' in adata.obsm_keys():
#            write_text_matrix(adata.obsm['X_meth_dispersion1'],
#                              os.path.join(file_path, 'dispersion1.tsv'),
#                              colnames=colnames, transpose=True)

#        if 'X_meth_dispersion2' in adata.obsm_keys():
#            write_text_matrix(adata.obsm['X_meth_dispersion2'],
#                              os.path.join(file_path, 'dispersion2.tsv'),
#                              colnames=colnames, transpose=True)

        if 'X_meth_value' in adata.obsm_keys():
            write_text_matrix(adata.obsm['X_meth_value'],
                              os.path.join(file_path, 'meth_value.tsv'),
                              colnames=colnames, transpose=True)

#        if 'X_alpha' in adata.obsm_keys():
#            write_text_matrix(adata.obsm['X_alpha'],
#                              os.path.join(file_path, 'alpha.tsv'),
#                              colnames=colnames, transpose=True)

#        if 'X_mean' in adata.obsm_keys():
#            write_text_matrix(adata.obsm['X_mean'],
#                              os.path.join(file_path, 'mean.tsv'),
#                              colnames=colnames, transpose=True)

class CombNBExtraParameters(Autoencoder):

    def build_output(self):
        pi = Dense(self.output_size, activation='sigmoid', kernel_initializer=self.init,
                       kernel_regularizer=l1_l2(self.l1_coef, self.l2_coef),
                       name='pi')(self.decoder_output)

#        alpha = Linear(input_dim=self.output_size,constraint=lambda z: tf.clip_by_value(z, 0, 1),name='alpha')(pi)
#        mean2 = mean1*alpha
        enzyme_cells = Linear(input_dim=self.input_size,constraint=lambda z: tf.clip_by_value(z, 0.0, 1.0),name='enzyme_cells')(self.input_layer)
        mean1 = Linear(input_dim=self.hidden_size[2],name='mean1')(self.decoder_output)
        alpha =  Linear(input_dim=self.hidden_size[2],name='alpha')(self.decoder_output)
        disp1 = ConstantDispersionLayer(name='dispersion1')
        mean1 = disp1(mean1)
        mean2 = mean1*alpha
        disp2 = ConstantDispersionLayer(name='dispersion2')
        mean2 = disp2(mean2)
        output = ColwiseMultLayer([pi, self.sf_layer])
        output = SliceLayer(0, name='slice')([output, enzyme_cells, mean1, mean2])

        combnb = CombNBLossSimpleExtra(enzyme_cells=enzyme_cells, mean1=mean1, mean2=mean2, theta1=disp1.theta_exp, theta2=disp2.theta_exp, debug=self.debug, scale_factor=self.sf_layer)
        self.loss = combnb.loss
        self.extra_models['pi'] = Model(inputs=self.input_layer, outputs=pi)
        #self.extra_models['enzyme_cells'] = Model(inputs=self.input_layer, outputs=enzyme_cells)
#        self.extra_models['mean1'] = Model(inputs=self.input_layer, outputs=mean1)
#        self.extra_models['mean2'] = Model(inputs=self.input_layer, outputs=mean2)
#        self.extra_models['mean1_norm'] = Model(inputs=self.input_layer, outputs=mean1)
#        self.extra_models['alpha'] = Model(inputs=self.input_layer, outputs=alpha)
        self.extra_models['decoded'] = Model(inputs=self.input_layer, outputs=self.decoder_output)

        self.model = Model(inputs=[self.input_layer, self.sf_layer], outputs=output)

        self.encoder = self.get_encoder()

    def predict(self, adata, mode='denoise', return_info=True, copy=False, colnames=None):

        adata = adata.copy() if copy else adata

        if return_info:
#            adata.obsm['X_meth_dispersion1'] = self.extra_models['dispersion1'].predict(adata.X)
#            adata.obsm['X_meth_dispersion2'] = self.extra_models['dispersion2'].predict(adata.X)
            adata.obsm['X_meth_value']    = self.extra_models['pi'].predict(adata.X)
#            adata.obsm['X_enzyme_function']    = self.extra_models['enzyme_cells'].predict(adata.X)
#            adata.obsm['mean1_norm']    = self.extra_models['mean1'].predict(adata.X)
#            adata.obsm['mean2_norm']    = self.extra_models['mean2'].predict(adata.X)
#            adata.obsm['alpha']    = self.extra_models['alpha'].predict(adata.X)

        # warning! this may overwrite adata.X
        super().predict(adata, mode, return_info, copy=False)
        return adata if copy else None

    def write(self, adata, file_path, mode='denoise', colnames=None):
        colnames = adata.var_names.values if colnames is None else colnames
        rownames = adata.obs_names.values

        super().write(adata, file_path, mode, colnames=colnames)

#        if 'X_meth_dispersion' in adata.obsm_keys():
#            write_text_matrix(adata.obsm['X_meth_dispersion1'],
#                              os.path.join(file_path, 'dispersion1.tsv'),
#                              colnames=colnames, transpose=True)

#        if 'X_meth_dispersion2' in adata.obsm_keys():
#            write_text_matrix(adata.obsm['X_meth_dispersion2'],
#                              os.path.join(file_path, 'dispersion2.tsv'),
#                              colnames=colnames, transpose=True)

        if 'X_meth_value' in adata.obsm_keys():
            write_text_matrix(adata.obsm['X_meth_value'],
                              os.path.join(file_path, 'meth_value.tsv'),
                              colnames=colnames, transpose=True)

#        if 'X_alpha' in adata.obsm_keys():
#            write_text_matrix(adata.obsm['X_alpha'],
#                              os.path.join(file_path, 'alpha.tsv'),
#                              colnames=colnames, transpose=True)

#        if 'X_mean' in adata.obsm_keys():
#            write_text_matrix(adata.obsm['X_mean'],
#                              os.path.join(file_path, 'mean.tsv'),
#                              colnames=colnames, transpose=True)

AE_types = {'normal': Autoencoder, 'poisson': PoissonAutoencoder,
            'nb': NBConstantDispAutoencoder, 'nb-conddisp': NBAutoencoder,
            'nb-shared': NBSharedAutoencoder, 'nb-fork': NBForkAutoencoder,
            'zinb': ZINBConstantDispAutoencoder, 'zinb-conddisp': ZINBAutoencoder,
            'zinb-shared': ZINBSharedAutoencoder, 'zinb-fork': ZINBForkAutoencoder,
            'zinb-elempi': ZINBAutoencoderElemPi, 'meth-encoder': CombNBAutoencoder,
            'meth-encoder-constant': CombNBAutoencoderConstantDispersion,
            'meth-encoder-poisson': CombNBPoissonAutoencoder,
            'meth-encoder-poisson-constant': CombNBPoissonAutoencoderConstantDispersion,
            'meth-encoder-extra-params': CombNBExtraParameters,
            'meth-simple-encoder': CombNBSimpleAutoencoder}

