import os
import glob
import re
import numpy as np
from common_definitions import *
from sklearn.utils.class_weight import compute_class_weight
from tqdm import tqdm
from utils._auc import AUC


def pm_W(x, y=None, from_diff=True):
    if from_diff:
        norm = tf.linalg.norm(x[:, None, :] + tf.zeros(tf.shape(x)), ord=1, axis=-1)
    else:
        norm = tf.linalg.norm(x[:, None, :] - y, ord=1, axis=-1)

    return norm / NUM_FEATURES


def allclose(x, y, rtol=1e-5, atol=1e-8):
    return tf.reduce_all(tf.abs(x - y) <= tf.abs(y) * rtol + atol)


def inter_mean(features_mean, features_mean_2=None, distance=True, num_classes=5):
    _a = tf.tile(features_mean, [num_classes, 1])
    _b = tf.reshape(tf.transpose(tf.tile(tf.transpose(features_mean if features_mean_2 is None else features_mean_2), [num_classes, 1])), [num_classes**2, 2048])

    inter_mean_strength = tf.keras.losses.cosine_similarity(_a, _b)
    inter_mean_strength = inter_mean_strength + 1. if distance else inter_mean_strength * -1

    # mask the same classes
    _mask = tf.reshape(1. - tf.eye(num_classes), [-1])
    inter_mean_strength = tf.boolean_mask(inter_mean_strength, _mask)

    # # reduce_mean it to be length of num_classes
    # inter_mean_strength = tf.linalg.norm(tf.reshape(inter_mean_strength, [num_classes, num_classes-1]), ord=2, axis=1) / (num_classes-1)**.5

    return inter_mean_strength


# class BinaryXE_FeLOSS(tf.keras.losses.BinaryCrossentropy):
#     def __init__(self, num_classes=NUM_CLASSES, bs=BATCH_SIZE, *args, **kwargs):
#         super(BinaryXE_FeLOSS, self).__init__(*args, **kwargs)
#         self.num_classes = num_classes
#
#         # for SD.
#         self.indexs = tf.Variable(tf.zeros((bs, num_classes)), dtype=tf.float32)
#
#     def call(self, y_true, y_pred):
#         _bs = tf.shape(y_true)[0]
#
#         _indexs, _indexs_ones = calc_indexs(self.num_classes, y_true)
#
#         self.indexs[0:_bs].assign(_indexs)
#         self.indexs_ones[0:_bs].assign(_indexs_ones)
#
#         return super(BinaryXE_FeLOSS, self).call(y_true, y_pred)


class FeatureStrength:
    """
    Feature strength class for each "data set"
    """

    def __init__(self, num_classes, _indexs: tf.Variable, _kalman_update_alpha=1.):
        self._num_classes = num_classes
        self._indexs = _indexs
        self.features_mean = tf.random.normal((num_classes, NUM_FEATURES))
        self.features_var = tf.random.normal((num_classes, NUM_FEATURES))
        self._kalman_update_alpha = _kalman_update_alpha

        self._first_iter = True

        # constant
        self._mask_imean = tf.cast(1. - tf.linalg.band_part(tf.ones((self._num_classes, self._num_classes)), -1, 0), tf.bool)

    # @tf.function(input_signature=(tf.TensorSpec(shape=[None, NUM_FEATURES], dtype=tf.float32),))
    def __call__(self, _features):
        _num_classes = self._num_classes
        _bs = tf.shape(_features)[0]
        _indexs = tf.transpose(self._indexs[:_bs])  # nc x bs

        # mean formula is E[X]
        _features_mean = _indexs @ _features / (tf.reduce_sum(_indexs, axis=1, keepdims=True) + tf.keras.backend.epsilon())  # nc, NUM_FEATURES

        # variance formula is var
        _features_var = tf.reduce_sum((self._indexs[:_bs][..., None] * _features[:, None, ...] - _features_mean[None, ...])**2, axis=0) / \
                        (tf.reduce_sum(_indexs, axis=1, keepdims=True) + tf.keras.backend.epsilon())  # nc, NUM_FEATURES

        if self._first_iter:
            self.features_mean = _features_mean
            self.features_var = _features_var
            self._first_iter = False
        else:
            self.features_mean = self.features_mean + self._kalman_update_alpha * (
                    _features_mean - self.features_mean)
            self.features_var = self.features_var + self._kalman_update_alpha * (_features_var - self.features_var)

        mean_strength = tf.linalg.norm(self.features_mean, ord=2, axis=-1) / 2048**.5  # the distance to zero
        # mean_strength = tf.linalg.norm(_features, ord=2, axis=-1) / 2048**.5  # the distance to zero
        # var_strength = tf.linalg.norm(tf.math.abs(self.features_mean) - (self.features_var**.5)/(1.960*2), ord=2, axis=-1) / 2048**.5  # the distance to one
        var_strength = tf.linalg.norm(tf.reduce_max(tf.math.abs(self.features_mean), axis=0)/1.960*tf.math.sqrt(tf.reduce_sum(_indexs, axis=1, keepdims=True)) - tf.math.sqrt(self.features_var), ord=2, axis=-1) / 2048**.5
        # var_strength = tf.linalg.norm(1 - tf.math.sqrt(self.features_var), ord=2, axis=-1) / 2048**.5

        # inter_mean_strength = tf.linalg.norm(self.features_mean[None, ...] - self.features_mean[:, None, ...], ord=2,
        #                                      axis=-1) / 2048**.5  # the distance to each other
        # inter_mean_strength = tf.boolean_mask(inter_mean_strength, self._mask_imean)
        # # got the variance as how the algorithm is
        # inter_mean_strength = tf.math.reduce_std(inter_mean_strength) * 2

        inter_mean_strength = inter_mean(self.features_mean, distance=True)

        return mean_strength, var_strength, inter_mean_strength



def _half_tanh(x):
    return 1 - tf.exp(-x)


class FeatureMetric(FeatureStrength):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def __call__(self, *args, **kwargs):
        _epsilon = tf.keras.backend.epsilon()

        raw_mean_s, raw_var_s, raw_imean_s = super().__call__(*args, **kwargs)

        # # scale
        # mean_s, var_s = raw_mean_s / NUM_FEATURES ** .5, raw_var_s / NUM_FEATURES ** .5

        # raw_mean_s = tf.reduce_max(raw_mean_s)
        # # raw_mean_s = tf.reduce_mean(raw_mean_s)
        # raw_var_s = tf.reduce_max(raw_var_s)
        # raw_imean_s = tf.reduce_max(raw_imean_s)

        # loss
        # raw_loss = raw_mean_s + raw_var_s + raw_imean_s
        raw_loss = 0.

        return raw_loss, (raw_mean_s, raw_var_s, raw_imean_s)


class AUC_five_classes(AUC):
    def __init__(self, **kwargs):
        super().__init__(num_classes=5, **kwargs)

    def update_state(self, y_true, y_pred, sample_weight=None):
        super().update_state(tf.gather(y_true, TRAIN_FIVE_CATS_INDEX, axis=-1),
                             tf.gather(y_pred, TRAIN_FIVE_CATS_INDEX, axis=-1), sample_weight)


def f1(y_true, y_pred):  # taken from old keras source code
    # threshold y_pred
    y_pred = tf.cast(tf.math.greater_equal(y_pred, tf.cast(THRESHOLD_SIGMOID, tf.float32)), tf.float32)

    true_positives = tf.math.reduce_sum(tf.round(tf.clip_by_value(y_true * y_pred, 0, 1)), 0)
    possible_positives = tf.math.reduce_sum(tf.round(tf.clip_by_value(y_true, 0, 1)), 0)
    predicted_positives = tf.math.reduce_sum(tf.round(tf.clip_by_value(y_pred, 0, 1)), 0)
    precision = true_positives / (predicted_positives + tf.keras.backend.epsilon())
    recall = true_positives / (possible_positives + tf.keras.backend.epsilon())
    f1_val = 2 * (precision * recall) / (precision + recall + tf.keras.backend.epsilon())
    return f1_val


def custom_sigmoid(x):
    """
	This functions fit SVM case because it is close to max points on {-1,1}
	"""
    return 1 / (1 + tf.math.exp(-2 * tf.math.exp(1.) * x))


def f1_svm(y_true, y_pred):
    y_pred = custom_sigmoid(y_pred)
    return f1(y_true, y_pred)


class AUC_SVM(tf.keras.metrics.AUC):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def update_state(self, y_true, y_pred, sample_weight=None):
        y_pred = custom_sigmoid(y_pred)
        super().update_state(y_true, y_pred, sample_weight)


def f1_mc(y_true, y_pred):
    return f1(y_true[-1, 1::2], y_pred[-1, 1::2])


class AUC_MC(tf.keras.metrics.AUC):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def update_state(self, y_true, y_pred, sample_weight=None):
        super().update_state(y_true[-1, 1::2], y_pred[-1, 1::2], sample_weight)


def get_and_mkdir(path):
    dir_modelckp = os.path.dirname(path)

    if not os.path.exists(dir_modelckp):
        os.makedirs(dir_modelckp)
    return dir_modelckp


def get_max_acc_weight(path):
    dir_modelckp = get_and_mkdir(path)

    model_weight_files = sorted(glob.glob(dir_modelckp + "/*.hdf5"), reverse=True)

    if len(model_weight_files) == 0:
        return False, 0

    max_epoch = None
    max_acc = None
    target_weight_file = None

    # look for target weight
    for mw_file in model_weight_files:
        basename = os.path.basename(mw_file)
        epoch_acc = re.search(r"[.](.*)[-](.*)[.]hdf5", basename)
        epoch = int(epoch_acc.group(1))
        acc = float(epoch_acc.group(2))

        if max_epoch is None:
            max_epoch = epoch
            max_acc = acc
            target_weight_file = mw_file
        else:
            if acc > max_acc:
                max_epoch = epoch
                max_acc = acc
                target_weight_file = mw_file

    return target_weight_file, max_epoch


def calculating_class_weights(y_true):
    number_dim = np.shape(y_true)[1]
    weights = np.empty([number_dim, 2])
    for i in tqdm(range(number_dim)):
        try:
            weights[i] = compute_class_weight('balanced', [0., 1.], y_true[:, i])
        except ValueError:
            weights[i] = np.ones(2)
    return weights


def get_weighted_loss(weights):
    def weighted_loss(y_true, y_pred):
        return tf.keras.backend.mean(
            (weights[:, 0] ** (1 - y_true)) * (weights[:, 1] ** (y_true)) * tf.keras.backend.binary_crossentropy(y_true,
                                                                                                                 y_pred),
            axis=-1)

    return weighted_loss


from tensorflow.python.framework import ops
from tensorflow.python.ops import math_ops
from tensorflow.python.framework import smart_cond


def _maybe_convert_labels(y_true):
    """Converts binary labels into -1/1."""
    are_zeros = math_ops.equal(y_true, 0)
    are_ones = math_ops.equal(y_true, 1)
    is_binary = math_ops.reduce_all(math_ops.logical_or(are_zeros, are_ones))

    def _convert_binary_labels():
        # Convert the binary labels to -1 or 1.
        return 2. * y_true - 1.

    updated_y_true = smart_cond.smart_cond(is_binary,
                                           _convert_binary_labels, lambda: y_true)
    return updated_y_true


def squared_hinge(y_true, y_pred, reduction_bool=True):
    """Computes the squared hinge loss between `y_true` and `y_pred`.
	Args:
	y_true: The ground truth values. `y_true` values are expected to be -1 or 1.
	  If binary (0 or 1) labels are provided we will convert them to -1 or 1.
	y_pred: The predicted values.
	Returns:
	Tensor with one scalar loss entry per sample.
	"""
    y_pred = ops.convert_to_tensor(y_pred)
    y_true = math_ops.cast(y_true, y_pred.dtype)
    y_true = _maybe_convert_labels(y_true)

    if reduction_bool:
        return tf.keras.backend.mean(
            math_ops.square(math_ops.maximum(1. - y_true * y_pred, 0.)), axis=-1)
    else:
        return math_ops.square(math_ops.maximum(1. - y_true * y_pred, 0.))


def get_square_hinge_weighted_loss(weights):
    # Different Error Costs
    def weighted_loss(y_true, y_pred):
        return tf.keras.backend.mean(
            (weights[:, 0] ** (1 - y_true)) * (weights[:, 1] ** y_true) * squared_hinge(y_true, y_pred,
                                                                                        reduction_bool=False), axis=-1)

    return weighted_loss


import math


# learning rate schedule
def step_decay(epoch):
    initial_lrate = CLR_MAXLR
    drop = 0.6
    epochs_drop = CLR_PATIENCE
    lrate = initial_lrate * math.pow(drop, math.floor((1 + epoch) / epochs_drop))
    return lrate


def _np_to_binary(np_array):
    return int("".join(str(int(x)) for x in np_array), 2)


if __name__ == "__main__":
    # img = read_image_and_preprocess("../sample/00002032_012.png")
    a = np.random.randint(0, 2, size=10)
