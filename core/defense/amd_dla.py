"""
@inproceedings{sperl2020dla,
  title={DLA: dense-layer-analysis for adversarial example detection},
  author={Sperl, Philip and Kao, Ching-Yu and Chen, Peng and Lei, Xiao and B{\"o}ttinger, Konstantin},
  booktitle={2020 IEEE European Symposium on Security and Privacy (EuroS\&P)},
  pages={198--215},
  year={2020},
  organization={IEEE}
}

This implementation is not an official version, but adapted from:
https://github.com/v-wangg/OrthogonalPGD/
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import time
import warnings
import os.path as path
from tqdm import tqdm

import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F

import numpy as np
from core.attack.max import Max
from core.attack.stepwise_max import StepwiseMax
from core.defense.md_dnn import DNNMalwareDetector
from core.defense.amd_template import DetectorTemplate
from config import config, logging, ErrorHandler
from tools import utils

logger = logging.getLogger('core.defense.amd_dla')
logger.addHandler(ErrorHandler)


class AdvMalwareDetectorDLA(nn.Module, DetectorTemplate):
    def __init__(self, md_nn_model, input_size, n_classes, ratio=0.95,
                 device='cpu', name='', **kwargs):
        nn.Module.__init__(self)
        DetectorTemplate.__init__(self)
        self.input_size = input_size
        self.n_classes = n_classes
        self.ratio = ratio
        self.device = device
        self.name = name
        self.parse_args(**kwargs)

        # malware detector
        if md_nn_model is not None and isinstance(md_nn_model, nn.Module):
            self.md_nn_model = md_nn_model
            self.is_fitting_md_model = False
        else:
            kwargs['smooth'] = True
            self.md_nn_model = DNNMalwareDetector(self.input_size,
                                                  n_classes,
                                                  self.device,
                                                  name,
                                                  **kwargs)
            self.is_fitting_md_model = True
        self.md_nn_model = self.md_nn_model.to(self.device)
        assert len(self.dense_hidden_units) >= 1, "Expected at least one hidden layer."
        self.alarm_nn_model = TorchAlarm(input_size=sum(self.md_nn_model.dense_hidden_units))

        self.tau = nn.Parameter(torch.zeros([1, ], device=self.device), requires_grad=False)

        self.model_save_path = path.join(config.get('experiments', 'amd_dla') + '_' + self.name,
                                         'model.pth')
        logger.info('========================================DLA model architecture==============================')
        logger.info(self)
        logger.info('===============================================end==========================================')

    def parse_args(self,
                   dense_hidden_units=None,
                   dropout=0.6,
                   alpha_=0.2,
                   **kwargs
                   ):
        if dense_hidden_units is None:
            self.dense_hidden_units = [200, 200]
        elif isinstance(dense_hidden_units, list):
            self.dense_hidden_units = dense_hidden_units
        else:
            raise TypeError("Expect a list of hidden units.")

        self.dropout = dropout
        self.alpha_ = alpha_
        self.proc_number = kwargs['proc_number']
        if len(kwargs) > 0:
            logger.warning("Unknown hyper-parameters {}".format(str(kwargs)))

    def forward_f(self, x):
        return self.md_nn_model(x)

    def forward_g(self, x):
        extra = []
        for dense_layer in self.md_nn_model.dense_layers[:-1]:
            x = self.md_nn_model.activation_func(dense_layer(x))
            extra.append(x)
        extra = torch.cat(extra, dim=-1)
        return self.alarm_nn_model(extra).reshape(-1)

    def forward(self, x):
        raise NotImplementedError("Use forward_f and forward_g instead.")

    def predict(self, test_data_producer, indicator_masking=False):
        """
        predict labels and conduct evaluation on detector & indicator

        Parameters
        --------
        @param test_data_producer, torch.DataLoader
        @param indicator_masking, whether filtering out the examples with low density or masking their values
        """
        y_cent, x_prob, y_true = self.inference(test_data_producer)
        y_pred = y_cent.argmax(1).cpu().numpy()
        y_true = y_true.cpu().numpy()
        indicator_flag = self.indicator(x_prob).cpu().numpy()

        def measurement(_y_true, _y_pred):
            from sklearn.metrics import f1_score, accuracy_score, confusion_matrix, balanced_accuracy_score
            accuracy = accuracy_score(_y_true, _y_pred)
            b_accuracy = balanced_accuracy_score(_y_true, _y_pred)
            MSG = "The accuracy on the test dataset is {:.5f}%"
            logger.info(MSG.format(accuracy * 100))
            MSG = "The balanced accuracy on the test dataset is {:.5f}%"
            logger.info(MSG.format(b_accuracy * 100))

            if np.any([np.all(_y_true == i) for i in range(self.n_classes)]):
                logger.warning("class absent.")
                return

            tn, fp, fn, tp = confusion_matrix(_y_true, _y_pred).ravel()
            fpr = fp / float(tn + fp)
            fnr = fn / float(tp + fn)
            f1 = f1_score(_y_true, _y_pred, average='binary')
            print("Other evaluation metrics we may need:")
            MSG = "False Negative Rate (FNR) is {:.5f}%, False Positive Rate (FPR) is {:.5f}%, F1 score is {:.5f}%"
            logger.info(MSG.format(fnr * 100, fpr * 100, f1 * 100))

        measurement(y_true, y_pred)
        if not indicator_masking:
            # filter out examples with low likelihood
            # y_pred = y_pred[indicator_flag]
            # y_true = y_true[indicator_flag]
            flag_of_retaining = indicator_flag | (y_pred == 1.)  # excluding the examples with ``not sure'' response
            y_pred = y_pred[flag_of_retaining]
            y_true = y_true[flag_of_retaining]
        else:
            # instead filtering out examples, here resets the prediction as 1
            y_pred[~indicator_flag] = 1.
        logger.info('The indicator is turning on...')
        logger.info('The threshold is {:.5}'.format(self.tau.item()))
        measurement(y_true, y_pred)

    def inference(self, test_data_producer):
        y_cent, x_prob = [], []
        gt_labels = []
        self.eval()
        with torch.no_grad():
            for x, y in test_data_producer:
                x, y = utils.to_device(x.double(), y.long(), self.device)
                logits_f = self.forward_f(x)
                y_cent.append(F.softmax(logits_f, dim=-1))
                x_prob.append(self.forward_g(x))
                gt_labels.append(y)

        gt_labels = torch.cat(gt_labels, dim=0)
        y_cent = torch.cat(y_cent, dim=0)
        x_prob = torch.cat(x_prob, dim=0)
        return y_cent, x_prob, gt_labels

    def inference_batch_wise(self, x, y):
        assert isinstance(x, torch.Tensor) and isinstance(y, torch.Tensor)
        self.eval()
        logits_f = self.forward_f(x)
        logits_g = self.forward_g(x)
        return torch.softmax(logits_f, dim=-1).detach().cpu().numpy(), logits_g.detach().cpu().numpy()

    def get_tau_sample_wise(self):
        return self.tau

    def indicator(self, x_prob):
        """
        Return 'True' if a sample is original, and otherwise 'False' is returned.
        """
        if isinstance(x_prob, np.ndarray):
            x_prob = torch.tensor(x_prob, device=self.device)
            return (x_prob <= self.tau).cpu().numpy()
        elif isinstance(x_prob, torch.Tensor):
            return x_prob <= self.tau
        else:
            raise TypeError("Tensor or numpy.ndarray are expected.")

    def get_threshold(self, validation_data_producer, ratio=None):
        """
        get the threshold for adversary detection
        :@param validation_data_producer: Object, an iterator for producing validation dataset
        """
        self.eval()
        if ratio is None:
            ratio = self.ratio
        probabilities = []
        with torch.no_grad():
            for x_val, y_val in validation_data_producer:
                x_val, y_val = utils.to_tensor(x_val.double(), y_val.long(), self.device)
                x_logits = self.forward_g(x_val)
                probabilities.append(x_logits)
            s, _ = torch.sort(torch.cat(probabilities, dim=0))
            i = int((s.shape[0] - 1) * ratio)
            assert i >= 0
            self.tau[0] = s[i]

    def _fit_md(self, train_data_producer, validation_data_producer, epochs=100, lr=0.005, weight_decay=0.,
                verbose=True):
        """
        Train the malware detector, pick the best model according to the validation results

        Parameters
        ----------
        @param train_data_producer: Object, an iterator for producing a batch of training data
        @param validation_data_producer: Object, an iterator for producing validation dataset
        @param epochs, Integer, epochs
        @param lr, Float, learning rate for Adam optimizer
        @param weight_decay, Float, penalty factor
        @param verbose: Boolean, whether to show verbose logs
        """
        optimizer = optim.Adam(self.md_nn_model.parameters(), lr=lr, weight_decay=weight_decay)
        best_avg_acc = 0.
        best_epoch = 0
        total_time = 0.
        nbatches = len(train_data_producer)
        for i in range(epochs):
            self.train()
            losses, accuracies = [], []
            for idx_batch, (x_train, y_train) in enumerate(train_data_producer):
                x_train, y_train = utils.to_device(x_train.double(), y_train.long(), self.device)
                start_time = time.time()
                optimizer.zero_grad()
                logits_f = self.forward_f(x_train)
                loss_train = F.cross_entropy(logits_f, y_train)
                loss_train.backward()
                optimizer.step()
                total_time = total_time + time.time() - start_time
                acc_f_train = (logits_f.argmax(1) == y_train).sum().item()
                acc_f_train /= x_train.size()[0]
                mins, secs = int(total_time / 60), int(total_time % 60)
                losses.append(loss_train.item())
                accuracies.append(acc_f_train)
                if verbose:
                    print(
                        f'Mini batch: {i * nbatches + idx_batch + 1}/{epochs * nbatches} | training time in {mins:.0f} minutes, {secs} seconds.')
                    logger.info(
                        f'Training loss (batch level): {losses[-1]:.4f} | Train accuracy: {acc_f_train * 100:.2f}%.')

            self.eval()
            avg_acc_val = []
            with torch.no_grad():
                for x_val, y_val in validation_data_producer:
                    x_val, y_val = utils.to_device(x_val.double(), y_val.long(), self.device)
                    logits_f = self.forward_f(x_val)
                    acc_val = (logits_f.argmax(1) == y_val).sum().item()
                    acc_val /= x_val.size()[0]
                    avg_acc_val.append(acc_val)
                avg_acc_val = np.mean(avg_acc_val)

            if avg_acc_val >= best_avg_acc:
                best_avg_acc = avg_acc_val
                best_epoch = i
                self.save_to_disk()
                if verbose:
                    print(f'Model saved at path: {self.model_save_path}')

            if verbose:
                logger.info(
                    f'Training loss (epoch level): {np.mean(losses):.4f} | Train accuracy: {np.mean(accuracies) * 100:.2f}')
                logger.info(
                    f'Validation accuracy: {avg_acc_val * 100:.2f} | The best validation accuracy: {best_avg_acc * 100:.2f} at epoch: {best_epoch}')

    def fit(self, train_data_producer, validation_data_producer, attack, attack_param,
            epochs=100, lr=0.005, weight_decay=0., verbose=True):
        """
        Train the alarm, pick the best model according to the validation results

        Parameters
        ----------
        @param train_data_producer: Object, an iterator for producing a batch of training data
        @param validation_data_producer: Object, an iterator for producing validation dataset
        @param attack, attack model, expect Max or Stepwise_Max
        @param attack_param, parameters used by the attack model
        @param epochs, Integer, epochs
        @param lr, Float, learning rate for Adam optimizer
        @param weight_decay, Float, penalty factor
        @param verbose: Boolean, whether to show verbose logs
        """
        # training the malware detector
        if self.is_fitting_md_model:
            self._fit_md(train_data_producer, validation_data_producer, epochs, lr, weight_decay)

        if attack is not None:
            assert isinstance(attack, (Max, StepwiseMax))
            if 'is_attacker' in attack.__dict__.keys():
                assert not attack.is_attacker

        logger.info("Training alarm ...")
        optimizer = optim.Adam(self.parameters(), lr=lr, weight_decay=weight_decay)
        best_avg_acc = 0.
        best_epoch = 0
        total_time = 0.
        nbatches = len(train_data_producer)
        self.md_nn_model.eval()
        for i in range(epochs):
            self.alarm_nn_model.train()
            losses, accuracies = [], []
            for idx_batch, (x_train, y_train) in enumerate(train_data_producer):
                x_train, y_train = utils.to_device(x_train.double(), y_train.long(), self.device)
                batch_size = x_train.shape[0]
                # make anomaly data
                start_time = time.time()
                pertb_x = attack.perturb(self.md_nn_model, x_train, y_train,
                                         **attack_param
                                         )
                pertb_x = utils.round_x(pertb_x, alpha=0.5)
                x_train = torch.cat([x_train, pertb_x], dim=0)
                y_train = torch.zeros((2 * batch_size), device=self.device)
                y_train[batch_size:] = 1
                optimizer.zero_grad()
                logits_g = self.forward_g(x_train)
                loss_train = F.binary_cross_entropy_with_logits(logits_g, y_train)
                loss_train.backward()
                optimizer.step()
                total_time = total_time + time.time() - start_time
                acc_g_train = ((torch.sigmoid(logits_g) >= 0.5) == y_train).sum().item()
                acc_g_train = acc_g_train / (2 * batch_size)
                mins, secs = int(total_time / 60), int(total_time % 60)
                losses.append(loss_train.item())
                accuracies.append(acc_g_train)
                if verbose:
                    print(
                        f'Mini batch: {i * nbatches + idx_batch + 1}/{epochs * nbatches} | training time in {mins:.0f} minutes, {secs} seconds.')
                    logger.info(
                        f'Training loss (batch level): {losses[-1]:.4f} | Train accuracy: {acc_g_train * 100:.2f}%.')

            self.alarm_nn_model.eval()
            avg_acc_val = []
            for x_val, y_val in validation_data_producer:
                x_val, y_val = utils.to_device(x_val.double(), y_val.long(), self.device)
                batch_size_val = x_val.shape[0]
                pertb_x = attack.perturb(self.md_nn_model, x_val, y_val,
                                         **attack_param
                                         )
                pertb_x = utils.round_x(pertb_x, alpha=0.5)
                x_val = torch.cat([x_val, pertb_x], dim=0)
                y_val = torch.zeros((2 * batch_size_val), device=self.device)
                y_val[batch_size_val:] = 1
                logits_g = self.forward_g(x_val)
                acc_val = ((torch.sigmoid(logits_g) >= 0.5) == y_val).sum().item()
                acc_val = acc_val / (2 * batch_size_val)
                avg_acc_val.append(acc_val)
            avg_acc_val = np.mean(avg_acc_val)

            if avg_acc_val >= best_avg_acc:
                best_avg_acc = avg_acc_val
                best_epoch = i
                self.get_threshold(validation_data_producer)
                self.save_to_disk()
                if verbose:
                    print(f'Model saved at path: {self.model_save_path}')

            if verbose:
                logger.info(
                    f'Training loss (epoch level): {np.mean(losses):.4f} | Train accuracy: {np.mean(accuracies) * 100:.2f}')
                logger.info(
                    f'Validation accuracy: {avg_acc_val * 100:.2f} | The best validation accuracy: {best_avg_acc * 100:.2f} at epoch: {best_epoch}')

    def load(self):
        # load model
        assert path.exists(self.model_save_path), 'train model first'
        self.load_state_dict(torch.load(self.model_save_path))

    def save_to_disk(self):
        if not path.exists(self.model_save_path):
            utils.mkdir(path.dirname(self.model_save_path))
        torch.save(self.state_dict(), self.model_save_path)


class TorchAlarm(torch.nn.Module):
    def __init__(self, input_size):
        super().__init__()

        self.layers = torch.nn.ModuleList([
            torch.nn.Linear(input_size, 112),
            torch.nn.ReLU(),
            torch.nn.Linear(112, 100),
            torch.nn.ReLU(),
            torch.nn.Linear(100, 300),
            torch.nn.ReLU(),
            torch.nn.Linear(300, 200),
            torch.nn.ReLU(),
            torch.nn.Linear(200, 77),
            torch.nn.ReLU(),
            torch.nn.Linear(77, 1),
        ])

    def __call__(self, x, training=False):
        if not isinstance(x, torch.Tensor):
            x = torch.tensor(x, dtype=torch.float32)
        for layer in self.layers:
            x = layer(x)
        return x
