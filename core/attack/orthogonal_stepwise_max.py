import torch
import torch.nn.functional as F

from core.attack import StepwiseMax
from tools.utils import get_x0, round_x
from config import logging, ErrorHandler

logger = logging.getLogger('core.attack.orthogonal_stepwise_max')
logger.addHandler(ErrorHandler)
EXP_OVER_FLOW = 1e-120


class OrthogonalStepwiseMax(StepwiseMax):
    """
    Stepwise max attack (mixture of pgd l1, pgd l2, pgd linf)

    Parameters
    ---------
    @param use_random, Boolean,  whether use random start point
    @param rounding_threshold, float, a threshold for rounding real scalars
    @param is_attacker, Boolean, play the role of attacker (note: the defender conducts adversarial training)
    @param oblivion, Boolean, whether know the adversary indicator or not
    @param kappa, attack confidence
    @param manipulation_x, manipulations
    @param omega, the indices of interdependent apis corresponding to each api
    @param device, 'cpu' or 'cuda'
    """

    def __init__(self, project_detector=False, project_classifier=False, k=None,
                 use_random=False, rounding_threshold=0.5,
                 is_attacker=True, manipulation_x=None, omega=None, device=None):
        super(OrthogonalStepwiseMax, self).__init__(use_random,
                                                    rounding_threshold,
                                                    is_attacker,
                                                    oblivion=False,
                                                    kappa=1., manipulation_x=manipulation_x, omega=omega, device=device)
        self.k = k
        self.project_detector = project_detector
        self.project_classifier = project_classifier

    def perturb(self, model, x, label=None,
                steps=100,
                step_check=10,
                sl_l1=1.,
                sl_l2=1.,
                sl_linf=0.01,
                verbose=False):
        """
        enhance attack
        """
        assert steps >= 0 and 1 >= sl_l1 > 0 and sl_l2 >= 0 and sl_linf >= 0
        model.eval()
        mini_steps = [step_check] * (steps // step_check)
        mini_steps = mini_steps + [steps % step_check] if steps % step_check != 0 else mini_steps
        n, red_n = x.size()[0], x.size()[1:]
        red_ind = list(range(2, len(x.size()) + 1))

        adv_x = x.detach().clone().to(torch.double)
        pert_x_cont = None
        prev_done = None
        for i, mini_step in enumerate(mini_steps):
            with torch.no_grad():
                _, done = self.get_loss(model, adv_x, label, self.lambda_)
            if torch.all(done):
                break
            if i == 0:
                adv_x[~done] = x[~done]  # recompute the perturbation under other penalty factors
                prev_done = done
            else:
                adv_x[~done] = pert_x_cont[~done[~prev_done]]
                prev_done = done

            num_sample_red = torch.sum(~done).item()
            pert_x_linf, pert_x_l2, pert_x_l1 = self._perturb(model, adv_x[~done], label[~done],
                                                              mini_step,
                                                              sl_l1,
                                                              sl_l2,
                                                              sl_linf
                                                              )
            with torch.no_grad():
                pertb_x_list = [pert_x_linf, pert_x_l2, pert_x_l1]
                n_attacks = len(pertb_x_list)
                pertbx = torch.vstack(pertb_x_list)
                label_ext = torch.cat([label[~done]] * n_attacks)
                scores, _1 = self.get_scores(model, pertbx, label_ext)
                pertbx = pertbx.reshape(n_attacks, num_sample_red, *red_n).permute([1, 0, *red_ind])
                scores = scores.reshape(n_attacks, num_sample_red).permute(1, 0)
                _, s_idx = scores.max(dim=-1)
                pert_x_cont = pertbx[torch.arange(num_sample_red), s_idx]
                adv_x[~done] = round_x(pert_x_cont, self.round_threshold)
        with torch.no_grad():
            _, done = self.get_loss(model, adv_x, label, self.lambda_)
            if verbose:
                logger.info(f"step-wise max: attack effectiveness {done.sum().item() / done.size()[0] * 100:.3f}%.")
        return adv_x

    def _perturb(self, model, x, label=None,
                 steps=10,
                 step_length_l1=1.,
                 step_length_l2=1.,
                 step_length_linf=0.01,
                 ):
        """
        perturb node feature vectors

        Parameters
        -----------
        @param model, a victim model
        @param x: torch.FloatTensor, node feature vectors (each represents the occurrences of apis in a graph) with shape [batch_size, vocab_dim]
        @param label: torch.LongTensor, ground truth labels
        @param steps: Integer, maximum number of iterations
        @param step_length_l1: float value in [0,1], the step length in each iteration
        @param step_length_l2: float, the step length in each iteration
        @param step_length_linf: float, the step length in each iteration
        """
        if x is None or x.shape[0] <= 0:
            return []
        adv_x = x.clone().detach()
        batch_size = x.shape[0]
        label_adv = torch.ones_like(label).to(model.device).double()

        assert hasattr(model, 'forward_g'), 'Expected an adversary detector'
        model.eval()

        for t in range(steps):
            if t == 0 and self.use_random:
                adv_x = get_x0(adv_x, rounding_threshold=self.round_threshold, is_sample=True)

            var_adv_x = torch.autograd.Variable(adv_x, requires_grad=True)
            # calculating gradient of classifier w.r.t. images
            logits_classifier = model.forward_f(var_adv_x)
            ce = torch.mean(F.cross_entropy(logits_classifier, label, reduction='none'))
            ce.backward()
            grad_classifier = var_adv_x.grad.detach().data  # we do not put it on cpu
            grad_classifier = self.trans_grads(grad_classifier, adv_x)

            var_adv_x.grad = None
            logits_detector = model.forward_g(var_adv_x)
            loss_detector = F.binary_cross_entropy_with_logits(logits_detector, label_adv)
            loss_detector.backward()
            grad_detector = var_adv_x.grad.detach().data
            grad_detector = self.trans_grads(grad_detector, adv_x)

            if self.project_detector:
                # using Orthogonal Projected Gradient Descent
                # projection of gradient of detector on gradient of classifier
                # then grad_d' = grad_d - (project grad_d onto grad_c)
                grad_detector_proj = grad_detector - torch.bmm(
                    (torch.bmm(grad_detector.view(batch_size, 1, -1), grad_classifier.view(batch_size, -1, 1))) / (
                            1e-20 + torch.bmm(grad_classifier.view(batch_size, 1, -1),
                                              grad_classifier.view(batch_size, -1, 1))).view(-1, 1, 1),
                    grad_classifier.view(batch_size, 1, -1)).view(grad_detector.shape)
            else:
                grad_detector_proj = grad_detector

            if self.project_classifier:
                # using Orthogonal Projected Gradient Descent
                # projection of gradient of detector on gradient of classifier
                # then grad_c' = grad_c - (project grad_c onto grad_d)
                grad_classifier_proj = grad_classifier - torch.bmm(
                    (torch.bmm(grad_classifier.view(batch_size, 1, -1), grad_detector.view(batch_size, -1, 1))) / (
                            1e-20 + torch.bmm(grad_detector.view(batch_size, 1, -1),
                                              grad_detector.view(batch_size, -1, 1))).view(-1, 1, 1),
                    grad_detector.view(batch_size, 1, -1)).view(grad_classifier.shape)
            else:
                grad_classifier_proj = grad_classifier

            if self.project_detector:
                logits_classifier[range(batch_size), 0] = logits_classifier[range(batch_size), 0] - 10.
                has_attack_succeeded = (logits_classifier.argmax(1) == 0.)[:, None].float()
            else:
                has_attack_succeeded = (logits_detector <= model.tau)[:, None].float()

            if self.k:
                # take gradients of g onto f every kth step
                if t % self.k == 0:
                    grad = grad_detector_proj
                else:
                    grad = grad_classifier_proj
            else:
                if self.project_detector:
                    grad = grad_classifier_proj * (
                            1. - has_attack_succeeded) + grad_detector_proj * has_attack_succeeded
                else:
                    grad = grad_classifier_proj * has_attack_succeeded + grad_detector_proj * (
                            1. - has_attack_succeeded)

            # if torch.any(torch.isnan(grad)):
            #     print(torch.mean(torch.isnan(grad)))
            #     print("ABORT")
            #     break
            perturbation = torch.sign(grad)
            adv_x_linf = torch.clamp(adv_x_linf + perturbation * step_length_linf, min=0., max=1.)

            l2norm = torch.linalg.norm(grad, dim=-1, keepdim=True)
            perturbation = torch.minimum(
                torch.tensor(1., dtype=x.dtype, device=x.device),
                grad / l2norm
            )
            adv_x_l2 = torch.clamp(adv_x_l2 + perturbation * step_length_l2, min=0., max=1.)

            val, idx = torch.abs(grad).topk(int(1. / step_length), dim=-1)
            perturbation = F.one_hot(idx, num_classes=adv_x.shape[-1]).sum(dim=1).double()
            perturbation = torch.sign(grad) * perturbation
            adv_x_l1 = torch.clamp(adv_x_l1 + perturbation * step_length_l1, min=0., max=1.)
        return adv_x_linf, adv_x_l2, adv_x_l1

    def trans_grads(self, gradients, adv_features):
        # 1. look for allowable position, because only '1--> -' and '0 --> +' are permitted
        #    1.1 api insertion
        pos_insertion = (adv_features <= 0.5) * 1 * (adv_features >= 0.)
        grad4insertion = (gradients >= 0) * pos_insertion * gradients
        # grad4insertion = (gradients > 0) * gradients
        #    2 api removal
        pos_removal = (adv_features > 0.5) * 1
        grad4removal = (gradients < 0) * (pos_removal & self.manipulation_x) * gradients
        return grad4removal + grad4insertion

    def get_scores(self, model, pertb_x, label):
        logits_f = model.forward_f(pertb_x)
        ce = F.cross_entropy(logits_f, label, reduction='none')
        y_pred = logits_f.argmax(1)
        if 'forward_g' in type(model).__dict__.keys() and (not self.oblivion):
            logits_g = model.forward_g(pertb_x)
            loss_no_reduction = ce - F.sigmoid(logits_g)
            done = (y_pred == 0.) & (logits_g <= model.tau)
        else:
            loss_no_reduction = ce
            done = y_pred == 0.
        return loss_no_reduction, done