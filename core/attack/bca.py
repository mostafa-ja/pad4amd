"""
@inproceedings{al2018adversarial,
  title={Adversarial deep learning for robust detection of binary encoded malware},
  author={Al-Dujaili, Abdullah and Huang, Alex and Hemberg, Erik and O’Reilly, Una-May},
  booktitle={2018 IEEE Security and Privacy Workshops (SPW)},
  pages={76--82},
  year={2018},
  organization={IEEE}
}
"""

import torch
import torch.nn.functional as F

from core.attack.base_attack import BaseAttack
from tools.utils import get_x0
from config import logging, ErrorHandler

logger = logging.getLogger('core.attack.bca')
logger.addHandler(ErrorHandler)

EXP_OVER_FLOW = 1e-30


class BCA(BaseAttack):
    """
    Multi-step bit coordinate ascent

    Parameters
    ---------
    @param is_attacker, Boolean, play the role of attacker (note: the defender conducts adversarial training)
    @param oblivion, Boolean, whether know the adversary indicator or not
    @param kappa, attack confidence
    @param manipulation_x, manipulations
    @param omega, the indices of interdependent apis corresponding to each api
    @param device, 'cpu' or 'cuda'
    """

    def __init__(self, is_attacker=True, oblivion=False, kappa=1., manipulation_x=None, omega=None, device=None):
        super(BCA, self).__init__(is_attacker, oblivion, kappa, manipulation_x, omega, device)
        self.omega = None  # no interdependent apis if just api insertion is considered
        self.manipulation_z = None  # all apis are permitted to be insertable
        self.lambda_ = 1.

    def _perturb(self, model, x, label=None,
                 m=10,
                 lambda_=1.,
                 use_sample=False,
                 verbose=False):
        """
        perturb node feature vectors

        Parameters
        -----------
        @param model, a victim model
        @param x: torch.FloatTensor, node feature vectors (each represents the occurrences of apis in a graph) with shape [batch_size, number_of_graphs, vocab_dim]
        @param label: torch.LongTensor, ground truth labels
        @param m: Integer, maximum number of perturbations
        @param lambda_, float, penalty factor
        @param use_sample, Boolean, whether use random start point
        @param verbose, Boolean, whether present attack information or not
        """
        if x is None or x.shape[0] <= 0:
            return []
        adv_x = x
        self.lambda_ = lambda_
        model.eval()
        for t in range(m):
            if use_sample and t == 0:
                adv_x = get_x0(adv_x, rounding_threshold=0.5, is_sample=True)
            var_adv_x = torch.autograd.Variable(adv_x, requires_grad=True)
            loss, done = self.get_loss(model, var_adv_x, label, self.lambda_)
            print("debug: iteration {} accuracy {}".format(t + 1, torch.sum(done).item()/len(done)))
            if torch.all(done):
                break
            grad = torch.autograd.grad(torch.mean(loss), var_adv_x)[0]

            # filtering un-considered graphs & positions
            grad4insertion = (grad > 0) * grad * (adv_x <= 0.5)

            grad4ins_ = grad4insertion.reshape(x.shape[0], -1)
            _, pos = torch.max(grad4ins_, dim=-1)
            perturbation = F.one_hot(pos, num_classes=grad4ins_.shape[-1]).float().reshape(x.shape)
            # avoid to perturb the examples that are successful to evade the victim
            perturbation[done] = 0.
            adv_x = torch.clamp(adv_x + perturbation, min=0., max=1.)
        return adv_x

    def perturb(self, model, x, label=None,
                m=10,
                min_lambda_=1e-5,
                max_lambda_=1e5,
                use_sample=False,
                base=10.,
                verbose=False):
        """
        enhance attack
        """
        assert 0 < min_lambda_ <= max_lambda_
        model.eval()
        if hasattr(model, 'forward_g'):
            self.lambda_ = min_lambda_
        else:
            self.lambda_ = max_lambda_
        adv_x = x.detach().clone().to(torch.double)
        while self.lambda_ <= max_lambda_:
            _, done = self.get_loss(model, adv_x, label, self.lambda_)
            if torch.all(done):
                break
            pert_x = self._perturb(model, adv_x[~done], label[~done],
                                   m,
                                   lambda_=self.lambda_,
                                   use_sample=use_sample,
                                   verbose=False
                                   )
            adv_x[~done] = pert_x
            self.lambda_ *= base
            if not self.check_lambda(model):
                break
        with torch.no_grad():
            _, done = self.get_loss(model, adv_x, label, self.lambda_)
            if verbose:
                logger.info(f"BCA: attack effectiveness {done.sum().item() / x.size()[0] * 100:.3f}%.")
        return adv_x
