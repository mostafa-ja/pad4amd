"""
A adversarial training framework
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os.path as path
import time

import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from torch.utils.checkpoint import checkpoint
import numpy as np

from config import config, logging, ErrorHandler
from tools import utils

logger = logging.getLogger('core.defense.principled_adv_training')
logger.addHandler(ErrorHandler)


class PrincipledAdvTraining(object):
    """a framework of principled adversarial training for defending against adversarial malware

    Parameters
    ------------------
    @param model, Object,  a model to be protected, e.g., MalwareDetector
    @attack_model: Object, adversary's model for generating adversarial malware on the feature space
    """

    def __init__(self, model, attack_model=None, attack_param=None):
        self.model = model
        self.attack_model = attack_model
        self.attack_param = attack_param

        self.name = self.model.name
        self.model_save_path = path.join(config.get('experiments', 'prip_adv_training') + '_' + self.name,
                                         'model.pth')
        self.model.model_save_path = self.model_save_path

    def fit(self, train_data_producer, validation_data_producer, epochs=100, lr=0.005, weight_decay=5e-4, verbose=True):
        """
        Train the malware detector, pick the best model according to the cross-entropy loss on validation set

        Parameters
        -------
        @param train_data_producer: Object, an iterator for producing a batch of training data
        @param validation_data_producer: Object, an iterator for producing validation dataset
        @param epochs: Integer, epochs
        @param lr: Float, learning rate for Adam optimizer
        @param weight_decay: Float, penalty factor, default value 5e-4 in graph attention layer
        @param verbose: Boolean, whether to show verbose logs
        """
        optimizer = optim.Adam(self.model.parameters(), lr=lr, weight_decay=weight_decay)
        best_avg_acc = 0.
        best_epoch = 0
        total_time = 0.
        nbatchs = len(train_data_producer)
        self.model.sample_weights[0] /= 2.  # owing to the adversarial malware
        for i in range(epochs):
            losses, accuracies = [], []
            for idx_batch, res in enumerate(train_data_producer):
                x_batch, adj, y_batch = res

                # perturb malware feature vectors
                x_batch, adj_batch, y_batch = utils.to_tensor(x_batch, adj, y_batch, self.model.device)
                mal_x_batch, mal_adj_batch, mal_y_batch, null_flag = self.get_mal_data(x_batch, adj_batch, y_batch)
                batch_size = x_batch.shape[0]
                if not null_flag:
                    start_time = time.time()
                    adv_x_batch = self.attack_model.perturb(self.model, mal_x_batch, mal_adj_batch, mal_y_batch,
                                                            self.attack_param['m'],
                                                            self.attack_param['step_length'],
                                                            self.attack_param['verbose']
                                                            )
                    total_time += time.time() - start_time
                    x_batch = torch.vstack([x_batch, adv_x_batch])
                    if adj is not None:
                        adj_batch = torch.vstack([adj_batch, mal_adj_batch])

                # start training
                start_time = time.time()
                self.model.train()
                optimizer.zero_grad()
                latent_rpst, logits = self.model.forward(x_batch, adj_batch)
                loss_train = self.model.customize_loss(logits[:batch_size], y_batch, latent_rpst[:batch_size], idx_batch)
                if not null_flag:
                    loss_train += F.cross_entropy(logits[batch_size:], mal_y_batch) - \
                                  self.model.energy(latent_rpst[batch_size:], logits[batch_size:]) * self.model.beta
                loss_train.backward()
                optimizer.step()
                total_time += time.time() - start_time
                acc_train = (logits.argmax(1) == torch.cat([y_batch, mal_y_batch])).sum().item()
                acc_train /= x_batch.size()[0]
                mins, secs = int(total_time / 60), int(total_time % 60)
                losses.append(loss_train.item())
                accuracies.append(acc_train)
                if verbose:
                    print(
                        f'Mini batch: {i * nbatchs + idx_batch + 1}/{epochs * nbatchs} | training time in {mins:.0f} minutes, {secs} seconds.')
                    logger.info(
                        f'Training loss (batch level): {losses[-1]:.4f} | Train accuracy: {acc_train * 100:.2f}')

            self.model.eval()
            y_pred = []
            pri_x_prob = []
            x_prob = []
            y_gt = []
            for res in validation_data_producer:
                x_val, adj_val, y_val = res
                x_val, adj_val, y_val = utils.to_tensor(x_val, adj_val, y_val, self.model.device)
                mal_x_val, mal_adj_val, mal_y_val, _flag = self.get_mal_data(x_val, adj_val, y_val)
                if not _flag:
                    adv_x_val = self.attack_model.perturb(self.model, mal_x_val, mal_adj_val, mal_y_val)
                    x_val = torch.cat([x_val, adv_x_val])
                    if adj_val is not None:
                        adj_val = torch.vstack([adj_val, mal_adj_val])

                rpst_val, logit_val = self.model.forward(x_val, adj_val)
                y_pred.append(logit_val.argmax(1))
                pri_x_prob.append(self.model.forward_g(rpst_val[:x_val.size()[0]]))
                x_prob.append(self.model.forward_g(rpst_val))
                y_gt.append(torch.cat([y_val, mal_y_val]))

            pri_x_prob = torch.cat(pri_x_prob)
            s, _ = torch.sort(pri_x_prob, descending=True)
            tau_ = s[int((s.shape[0] - 1) * self.model.percentage)]
            x_prob = torch.cat(x_prob)
            acc_val = (torch.cat(y_pred)[x_prob >= tau_] == torch.cat(y_gt)[x_prob >= tau_]).sum().item()
            acc_val /= (x_prob >= tau_).sum().item()

            if acc_val >= best_avg_acc:
                best_avg_acc = acc_val
                self.model.tau = nn.Parameter(tau_, requires_grad=False)
                best_epoch = i
                if not path.exists(self.model_save_path):
                    utils.mkdir(path.dirname(self.model_save_path))
                torch.save(self.model.state_dict(), self.model_save_path)
                if verbose:
                    print(f'Model saved at path: {self.model_save_path}')

            if verbose:
                logger.info(
                    f'Training loss (epoch level): {np.mean(losses):.4f} | Train accuracy: {np.mean(accuracies) * 100:.2f}')
                logger.info(
                    f'Validation accuracy: {acc_val * 100:.2f} | The best validation accuracy: {best_avg_acc * 100:.2f} at epoch: {best_epoch}')

    @staticmethod
    def get_mal_data(x_batch, adj_batch, y_batch):
        mal_x_batch = x_batch[y_batch == 1]
        mal_y_batch = y_batch[y_batch == 1]
        mal_adj_batch = None
        if adj_batch is not None:
            mal_adj_batch = torch.stack([adj for i, adj in enumerate(adj_batch) if y_batch[i] == 1], dim=0)
        null_flag = len(mal_x_batch) <= 0
        return mal_x_batch, mal_adj_batch, mal_y_batch, null_flag
