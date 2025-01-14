import numpy as np
import tensorflow as tf
from tensorflow.keras import backend as K


def _nan2zero(x):
    return tf.where(tf.math.is_nan(x), tf.zeros_like(x), x)

def _nan2inf(x):
    return tf.where(tf.math.is_nan(x), tf.zeros_like(x)+np.inf, x)

def _nelem(x):
    nelem = tf.reduce_sum(tf.cast(~tf.math.is_nan(x), tf.float32))
    return tf.cast(tf.where(tf.equal(nelem, 0.), 1., nelem), x.dtype)


def _reduce_mean(x):
    nelem = _nelem(x)
    x = _nan2zero(x)
    return tf.divide(tf.reduce_sum(x), nelem)


def mse_loss(y_true, y_pred):
    ret = tf.square(y_pred - y_true)

    return _reduce_mean(ret)


# In the implementations, I try to keep the function signature
# similar to those of Keras objective functions so that
# later on we can use them in Keras smoothly:
# https://github.com/fchollet/keras/blob/master/keras/objectives.py#L7
def poisson_loss(y_true, y_pred):
    y_pred = tf.cast(y_pred, tf.float32)
    y_true = tf.cast(y_true, tf.float32)

    # we can use the Possion PMF from TensorFlow as well
    # dist = tf.contrib.distributions
    # return -tf.reduce_mean(dist.Poisson(y_pred).log_pmf(y_true))

    nelem = _nelem(y_true)
    y_true = _nan2zero(y_true)

    # last term can be avoided since it doesn't depend on y_pred
    # however keeping it gives a nice lower bound to zero
    ret = y_pred - y_true*tf.math.log(y_pred+1e-10) + tf.math.lgamma(y_true+1.0)

    return tf.divide(tf.reduce_sum(ret), nelem)


# We need a class (or closure) here,
# because it's not possible to
# pass extra arguments to Keras loss functions
# See https://github.com/fchollet/keras/issues/2121

# dispersion (theta) parameter is a scalar by default.
# scale_factor scales the nbinom mean before the
# calculation of the loss to balance the
# learning rates of theta and network weights
class NB(object):
    def __init__(self, theta=None, masking=False, scope='nbinom_loss/',
                 scale_factor=1.0, debug=False):

        # for numerical stability
        self.eps = 1e-10
        self.scale_factor = scale_factor
        self.debug = debug
        self.scope = scope
        self.masking = masking
        self.theta = theta

    def loss(self, y_true, y_pred, mean=True, theta=1e6):
        scale_factor = self.scale_factor
        eps = self.eps

        with tf.name_scope(self.scope):
            y_true = tf.cast(y_true, tf.float32)
            y_pred = tf.cast(y_pred, tf.float32) * scale_factor

            if self.masking:
                nelem = _nelem(y_true)
                y_true = _nan2zero(y_true)

            # Clip theta
            theta = tf.minimum(theta, 1e6)

            t1 = tf.math.lgamma(theta+eps) + tf.math.lgamma(y_true+1.0) - tf.math.lgamma(y_true+theta+eps)
            t2 = (theta+y_true) * tf.math.log(1.0 + (y_pred/(theta+eps))) + (y_true * (tf.math.log(theta+eps) - tf.math.log(y_pred+eps)))

            if self.debug:
                assert_ops = [
                        tf.compat.v1.verify_tensor_all_finite(y_pred, 'y_pred has inf/nans'),
                        tf.compat.v1.verify_tensor_all_finite(t1, 't1 has inf/nans'),
                        tf.compat.v1.verify_tensor_all_finite(t2, 't2 has inf/nans')]

                tf.summary.histogram('t1', t1)
                tf.summary.histogram('t2', t2)

                with tf.control_dependencies(assert_ops):
                    final = t1 + t2

            else:
                final = t1 + t2

            final = _nan2inf(final)

            if mean:
                if self.masking:
                    final = tf.divide(tf.reduce_sum(final), nelem)
                else:
                    final = tf.reduce_mean(final)


        return final

class ZINB(NB):
    def __init__(self, pi, ridge_lambda=0.0, scope='zinb_loss/', **kwargs):
        super().__init__(scope=scope, **kwargs)
        self.pi = pi
        self.ridge_lambda = ridge_lambda

    def loss(self, y_true, y_pred, mean=True):
        scale_factor = self.scale_factor
        eps = self.eps

        with tf.name_scope(self.scope):
            # reuse existing NB neg.log.lik.
            # mean is always False here, because everything is calculated
            # element-wise. we take the mean only in the end
            nb_case = super().loss(y_true, y_pred, mean=False) - tf.math.log(1.0-self.pi+eps)

            y_true = tf.cast(y_true, tf.float32)
            y_pred = tf.cast(y_pred, tf.float32) * scale_factor
            theta = tf.minimum(self.theta, 1e6)

            zero_nb = tf.pow(theta/(theta+y_pred+eps), theta)
            zero_case = -tf.math.log(self.pi + ((1.0-self.pi)*zero_nb)+eps)
            result = tf.where(tf.less(y_true, 1e-8), zero_case, nb_case)
            ridge = self.ridge_lambda*tf.square(self.pi)
            result += ridge

            if mean:
                if self.masking:
                    result = _reduce_mean(result)
                else:
                    result = tf.reduce_mean(result)

            result = _nan2inf(result)

            if self.debug:
                tf.summary.histogram('nb_case', nb_case)
                tf.summary.histogram('zero_nb', zero_nb)
                tf.summary.histogram('zero_case', zero_case)
                tf.summary.histogram('ridge', ridge)

        return result

class CombNBLoss(NB):
    def __init__(self, pi, alpha, theta1, theta2, scope='combnb_loss/', **kwargs):
        super().__init__(scope=scope, **kwargs)
        self.pi = pi
        self.alpha = alpha
        self.theta1 = theta1
        self.theta2 = theta2
    
    def loss(self, y_true, y_pred):
        scale_factor = self.scale_factor
        eps = self.eps

        with tf.name_scope(self.scope):
            # reuse existing NB neg.log.lik.
            # mean is always False here, because everything is calculated
            # element-wise. we take the mean only in the end
            mean1 = y_pred
            mean2 = mean1*self.alpha
            if self.debug:
                tf.summary.histogram('mean1', mean1)
#                tf.summary.histogram('mean2', mean2)

            nb_case1 = super().loss(y_true, mean1, mean=False, theta=self.theta1)
            nb_case2 = super().loss(y_true, mean2, mean=False, theta=self.theta2)

            result = tf.math.reduce_logsumexp(tf.stack((nb_case1-self.pi,nb_case2)),axis=0)
            splus = tf.keras.backend.softplus(self.pi)
            result = result + splus
            result = _reduce_mean(result)
            result = _nan2inf(result)


        return result

class CombNBLossSimple(NB):
    def __init__(self, mean1, mean2, theta1, theta2, scope='combnb_loss/', **kwargs):
        super().__init__(scope=scope, **kwargs)
        self.mean1 = mean1
        self.mean2 = mean2
        self.theta1 = theta1
        self.theta2 = theta2
    
    def loss(self, y_true, y_pred):
        scale_factor = self.scale_factor
        eps = self.eps

        with tf.name_scope(self.scope):
            # reuse existing NB neg.log.lik.
            # mean is always False here, because everything is calculated
            # element-wise. we take the mean only in the end
            pi = y_pred
#            if self.debug:
#                tf.summary.histogram('mean1', mean1)
#                tf.summary.histogram('mean2', mean2)

            nb_case1 = super().loss(y_true, self.mean1, mean=False, theta=self.theta1)
            nb_case2 = super().loss(y_true, self.mean2, mean=False, theta=self.theta2)

            result = tf.math.reduce_logsumexp(tf.stack((nb_case1-pi,nb_case2)),axis=0)
            splus = tf.keras.backend.softplus(pi)
            result = result + splus
            result = _reduce_mean(result)
            result = _nan2inf(result)


        return result

class CombNBPoissonLossExtra(NB):
    def __init__(self, enzyme_cells, pi, lambda_poisson, scope='combnbpoisson_loss/', **kwargs):
        super().__init__(scope=scope, **kwargs)
        self.pi = pi
        self.enzyme_cells = enzyme_cells
        self.lambda_poisson = lambda_poisson

    
    def loss(self, y_true, y_pred):
        scale_factor = self.scale_factor
        eps = self.eps

        with tf.name_scope(self.scope):
            # reuse existing NB neg.log.lik.
            # mean is always False here, because everything is calculated
            # element-wise. we take the mean only in the end
            mean1 = y_pred
            if self.debug:
                tf.summary.histogram('mean1', mean1)
#                tf.summary.histogram('mean2', mean2)

            nb_case = super().loss(y_true, mean1, mean=False, theta=self.theta)
#            non_nan_y = _nan2zero(y_true)
#            leni = _nelem(y_true)
#            poiss_case = self.lambda_poisson - non_nan_y*tf.math.log(self.lambda_poisson+eps) + tf.math.lgamma(non_nan_y + 1.0)
#            poiss_case = tf.math.log(tf.divide(poiss_case, leni))
            poiss_case = tf.math.log(poisson_loss(y_true, self.lambda_poisson))

            result = tf.math.reduce_logsumexp(tf.stack((nb_case,poiss_case-self.pi-self.enzyme_cells)),axis=0)
            splus = tf.keras.backend.softplus(self.pi)
            splus2 = tf.keras.backend.softplus(self.enzyme_cells)
            result = result + splus + splus2
            result = _reduce_mean(result)
            result = _nan2inf(result)


        return result

class CombNBLossSimpleExtra(NB):
    def __init__(self, enzyme_cells, mean1, mean2, theta1, theta2, scope='combnb_loss/', **kwargs):
        super().__init__(scope=scope, **kwargs)
        self.enzyme_cells = enzyme_cells
        self.mean1 = mean1
        self.mean2 = mean2
        self.theta1 = theta1
        self.theta2 = theta2
    
    def loss(self, y_true, y_pred):
        scale_factor = self.scale_factor
        eps = self.eps

        with tf.name_scope(self.scope):
            # reuse existing NB neg.log.lik.
            # mean is always False here, because everything is calculated
            # element-wise. we take the mean only in the end
            pi = y_pred
#            if self.debug:
#                tf.summary.histogram('mean1', mean1)
#                tf.summary.histogram('mean2', mean2)

            nb_case1 = super().loss(y_true, self.mean1, mean=False, theta=self.theta1)
            nb_case2 = super().loss(y_true, self.mean2, mean=False, theta=self.theta2)

            result = tf.math.reduce_logsumexp(tf.stack((nb_case1-pi-self.enzyme_cells,nb_case2)),axis=0)
            splus = tf.keras.backend.softplus(pi)
            splus2 = tf.keras.backend.softplus(self.enzyme_cells)
            result = result + splus + splus2
            result = _reduce_mean(result)
            result = _nan2inf(result)


        return result

