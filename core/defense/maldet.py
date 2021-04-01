from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import time
from tqdm import tqdm
import os.path as path

import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from torch.utils.checkpoint import checkpoint

import numpy as np

from core.defense.malgat import MalGAT
from config import config, logging, ErrorHandler
from tools import utils

logger = logging.getLogger('core.defense.maldetector')
logger.addHandler(ErrorHandler)


class MalwareDetector(nn.Module):
    def __init__(self, vocab_size, n_classes, n_sample_times=5, device='cpu', name='PRO', **kwargs):
        """
        Construct malware detector

        Parameters
        ----------
        @param vocab_size: Integer, the number of words in the vocabulary
        @param n_classes: Integer, the number of classes, n=2
        @param n_sample_times: Integer, the number of sampling times for predicting
        @param device: String, 'cpu' or 'cuda'
        @param name: String, model name
        """
        super(MalwareDetector, self).__init__()

        self.vocab_size = vocab_size
        self.n_classes = n_classes
        self.n_sample_times = n_sample_times
        self.device = device
        self.name = name
        self.parse_args(**kwargs)

        self.malgat = MalGAT(self.vocab_size,
                             self.embedding_dim,
                             self.hidden_units,
                             self.penultimate_hidden_unit,
                             self.n_heads,
                             self.dropout,
                             self.alpha_,  # slope coefficient of leaky relu
                             self.k,
                             self.use_fusion,
                             self.sparse,
                             self.smooth)

        self.dense = nn.Linear(self.penultimate_hidden_unit, self.n_classes)
        self.model_save_path = path.join(config.get('experiments', 'malware_detector') + '_' + self.name,
                                         'model.pth')

    def parse_args(self,
                   embedding_dim=32,
                   hidden_units=None,
                   penultimate_hidden_unit=64,
                   n_heads=8,
                   dropout=0.6,
                   alpha_=0.2,
                   k=10,
                   use_fusion=True,
                   sparse=True,
                   smooth=False,
                   enable_gd_ckpt=False,
                   **kwargs
                   ):
        self.embedding_dim = embedding_dim
        if hidden_units is None:
            self.hidden_units = [8]
        else:
            self.hidden_units = hidden_units
        self.penultimate_hidden_unit = penultimate_hidden_unit
        self.n_heads = n_heads
        self.dropout = dropout
        self.alpha_ = alpha_
        self.k = k
        self.use_fusion = use_fusion
        self.sparse = sparse
        self.smooth = smooth
        self.enable_gd_ckpt = enable_gd_ckpt
        if len(kwargs) > 0:
            logger.warning("Unknown hyper-parameters {}".format(str(kwargs)))

    def non_adv_eval(self):
        self.malgat.non_adv_eval()

    def forward(self, feature, adj=None):
        if self.enable_gd_ckpt:
            feature.requires_grad = True
            if adj is not None:
                adj.requires_grad = True
            latent_representation = checkpoint(self.malgat, feature, adj)  # saving RAM dramatically
        else:
            latent_representation = self.malgat(feature, adj)
        latent_representation = F.dropout(latent_representation, self.dropout, training=self.training)
        logits = self.dense(latent_representation)
        return latent_representation, logits

    def inference(self, test_data_producer):
        confidences = []
        gt_labels = []
        self.eval()
        with torch.no_grad():
            for ith in tqdm(range(self.n_sample_times)):
                conf_batches = []
                for x, adj, y, _1 in test_data_producer:
                    x, adj, y = utils.to_tensor(x, adj, y, self.device)
                    _2, logits = self.forward(x, adj)
                    conf_batches.append(F.softmax(logits, dim=-1))
                    if ith == 0:
                        gt_labels.append(y)
                conf_batches = torch.vstack(conf_batches)
                confidences.append(conf_batches)
            print(confidences[0])
            print(confidences[1])
        gt_labels = torch.cat(gt_labels, dim=0)
        confidences = torch.mean(torch.stack(confidences).permute([1, 0, 2]), dim=1)
        return confidences, gt_labels

    def inference_batch_wise(self, x, a, y, use_indicator=None):
        assert isinstance(x, torch.Tensor) and isinstance(y, torch.Tensor)
        if a is not None:
            assert isinstance(a, torch.Tensor)
        _, logit = self.forward(x, a)
        return torch.softmax(logit, dim=-1).detach().cpu().numpy(), np.ones((logit.size()[0], ))

    def predict(self, test_data_producer):
        # evaluation
        confidence, y_true = self.inference(test_data_producer)
        y_pred = confidence.argmax(1).cpu().numpy()
        y_true = y_true.cpu().numpy()
        from sklearn.metrics import f1_score, accuracy_score, confusion_matrix, balanced_accuracy_score
        accuracy = accuracy_score(y_true, y_pred)
        b_accuracy = balanced_accuracy_score(y_true, y_pred)
        MSG = "The accuracy on the test dataset is {:.5f}%"
        logger.info(MSG.format(accuracy * 100))
        MSG = "The balanced accuracy on the test dataset is {:.5f}%"
        logger.info(MSG.format(b_accuracy * 100))

        if np.any([np.all(y_true == i) for i in range(self.n_classes)]):
            logger.warning("class absent.")
            return

        tn, fp, fn, tp = confusion_matrix(y_true, y_pred).ravel()
        fpr = fp / float(tn + fp)
        fnr = fn / float(tp + fn)
        f1 = f1_score(y_true, y_pred, average='binary')

        print("Other evaluation metrics we may need:")
        MSG = "False Negative Rate (FNR) is {:.5f}%, False Positive Rate (FPR) is {:.5f}%, F1 score is {:.5f}%"
        logger.info(MSG.format(fnr * 100, fpr * 100, f1 * 100))

    def customize_loss(self, logits, gt_labels, representation, mini_batch_idx):
        return F.cross_entropy(logits, gt_labels)

    def fit(self, train_data_producer, validation_data_producer, epochs=100, lr=0.005, weight_decay=5e-4, verbose=True):
        """
        Train the malware detector, pick the best model according to the cross-entropy loss on validation set

        Parameters
        ----------
        @param train_data_producer: Object, an iterator for producing a batch of training data
        @param validation_data_producer: Object, an iterator for producing validation dataset
        @param epochs, Integer, epochs
        @param lr, Float, learning rate for Adam optimizer
        @param weight_decay, Float, penalty factor, default value 5e-4 in graph attention layer
        @param verbose: Boolean, whether to show verbose logs
        """
        optimizer = optim.Adam(self.param_customizing(weight_decay), lr=lr)
        best_avg_acc = 0.
        best_epoch = 0
        total_time = 0.
        nbatches = len(train_data_producer)
        for i in range(epochs):
            self.train()
            losses, accuracies = [], []
            for idx_batch, res in enumerate(train_data_producer):
                x_batch, adj, y_batch, _1 = res
                x_batch, adj_batch, y_batch = utils.to_tensor(x_batch, adj, y_batch, self.device)
                start_time = time.time()
                optimizer.zero_grad()
                latent_rpst, logits = self.forward(x_batch, adj_batch)
                loss_train = self.customize_loss(logits, y_batch, latent_rpst, idx_batch)
                loss_train.backward()
                optimizer.step()
                total_time = total_time + time.time() - start_time
                acc_train = (logits.argmax(1) == y_batch).sum().item()
                acc_train /= x_batch.size()[0]
                mins, secs = int(total_time / 60), int(total_time % 60)
                losses.append(loss_train.item())
                accuracies.append(acc_train)
                if verbose:
                    print(
                        f'Mini batch: {i * nbatches + idx_batch + 1}/{epochs * nbatches} | training time in {mins:.0f} minutes, {secs} seconds.')
                    logger.info(
                        f'Training loss (batch level): {losses[-1]:.4f} | Train accuracy: {acc_train * 100:.2f}')

            self.eval()
            avg_acc_val = []
            with torch.no_grad():
                for res in validation_data_producer:
                    x_val, adj_val, y_val, _2 = res
                    x_val, adj_val, y_val = utils.to_tensor(x_val, adj_val, y_val, self.device)
                    _, logits = self.forward(x_val, adj_val)
                    acc_val = (logits.argmax(1) == y_val).sum().item()
                    acc_val /= x_val.size()[0]
                    avg_acc_val.append(acc_val)
                avg_acc_val = np.mean(avg_acc_val)

            if avg_acc_val >= best_avg_acc:
                best_avg_acc = avg_acc_val
                best_epoch = i
                if not path.exists(self.model_save_path):
                    utils.mkdir(path.dirname(self.model_save_path))
                torch.save(self.state_dict(), self.model_save_path)
                if verbose:
                    print(f'Model saved at path: {self.model_save_path}')

            if verbose:
                logger.info(
                    f'Training loss (epoch level): {np.mean(losses):.4f} | Train accuracy: {np.mean(accuracies) * 100:.2f}')
                logger.info(
                    f'Validation accuracy: {avg_acc_val * 100:.2f} | The best validation accuracy: {best_avg_acc * 100:.2f} at epoch: {best_epoch}')

    def param_customizing(self, weight_decay):
        customized_params_no_decay = []
        customized_params_decay = []

        for name, param in self.named_parameters():
            if '.mod_frq' in name:
                customized_params_no_decay.append(param)
            elif 'dense.weight' == name or 'dense.bias' == name:
                customized_params_no_decay.append(param)
            else:
                customized_params_decay.append(param)
        return [{'params': customized_params_no_decay, 'weight_decay': 0.},
                {'params': customized_params_decay, 'weight_decay': weight_decay}]

    def load(self):
        """
        load model parameters from disk
        """
        self.load_state_dict(torch.load(self.model_save_path))
