#!/usr/bin/python3

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import logging

import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F

import numpy as np
from numpy.random import RandomState


from sklearn.metrics import average_precision_score

from torch.utils.data import DataLoader

from dataloader import TestDataset


# regularization terms options
L2 = True
L1 = False
L2_COEFF = 0.00002

PROJECT_CUBE = False
PROJECT_SPHERE = False

class KGEModel(nn.Module):
    def __init__(self, model_name, nentity, nrelation, ntriples, hidden_dim, args):
        super(KGEModel, self).__init__()

        self.model_name = model_name
        self.nentity = nentity
        self.nrelation = nrelation
        self.epsilon = 2.0
        self.lmbda = 0.1
        self.hidden_dim = hidden_dim
        self.idx = 1
        self.ruge_rule_penalty = 1
        self.alpha = 1000

        self.ranks = []

        self.gamma = nn.Parameter(
            torch.Tensor([args.gamma]),
            requires_grad=False
        )

        self.embedding_range = nn.Parameter(
            torch.Tensor([(self.gamma.item() + self.epsilon) / hidden_dim]),
            requires_grad=False
        )

        self.xi = nn.Parameter(torch.zeros(ntriples, 1))
        nn.init.uniform_(
            tensor=self.xi,
            a= -0.1,
            b= 0.1
        )

        self.xi_neg = nn.Parameter(torch.zeros(ntriples, 1))
        nn.init.uniform_(
            tensor=self.xi_neg,
            a= -0.1,
            b= 0.1
        )

        ent_dim_mult, rel_dim_mult = self.compute_multipliers()

        # define entity and relation embeddings
        self.entity_dim = ent_dim_mult*hidden_dim
        self.relation_dim = rel_dim_mult*hidden_dim

        self.entity_embedding = nn.Parameter(torch.zeros(nentity, self.entity_dim))
        self.relation_embedding = nn.Parameter(torch.zeros(self.nrelation, self.relation_dim))
        self.initialize(self.entity_embedding, nentity, hidden_dim)
        self.initialize(self.relation_embedding, nrelation, hidden_dim)
        # setup multipliers
        if self.model_name in ['SpacESS']:
            mult = 4 if 'Quat' in self.model_name else 1
            self.rotator_head = nn.Parameter(torch.zeros(nrelation, mult*hidden_dim))
            self.rotator_tail = nn.Parameter(torch.zeros(nrelation, mult*hidden_dim))
            self.initialize(self.rotator_head, nrelation, hidden_dim)
            self.initialize(self.rotator_tail, nrelation, hidden_dim)

        # set rule info
        self.ruge = args.ruge

        self.epsilon_inv = .1
        self.epsilon_impl = .1
        self.epsilon_eq = 0
        self.epsilon_sym = 0
        self.inject = args.inject

        self.rule_weight = {
            'inverse': 2.0,
            'implication': 1.0,
            'symmetry': .01,
            'equality': .01,
            'ruge': 1
        }

        if model_name == 'pRotatE':
            self.modulus = nn.Parameter(torch.Tensor([[0.5 * self.embedding_range.item()]]))

        self.gamma1 = 50; self.gamma2 = 70


        #Do not forget to modify this line when you add a new model in the "forward" function
        if model_name not in ['TransE', 'DistMult', 'ComplEx', 'RotatE', 'pRotatE', 'SpacESS', 'QuatE', 'TransComplEx']:
            raise ValueError('model %s not supported' % model_name)


    def compute_multipliers(self):
        if self.model_name == 'RotatE':
            return 2, 1
        if self.model_name in ['SpacESS', 'ComplEx', 'DistMult']:
            return 2, 2
        if self.model_name in ['QuatE']:
            return 4, 4
        return 1, 1

    def initialize(self, tensor, in_features, out_features):
        if 'Quat' not in self.model_name:
            nn.init.uniform_(
                tensor=tensor,
                a=-self.embedding_range.item(),
                b=self.embedding_range.item()
            )

        else:  # use quaternion initialization
            fan_in = in_features
            fan_out = out_features

            s = 1. / np.sqrt(2 * in_features)

            rng = torch.random.manual_seed(42)

            # Generating randoms and purely imaginary quaternions :
            kernel_shape = (in_features, out_features)
            nweigths = in_features * out_features

            wi = torch.FloatTensor(nweigths).uniform_()
            wj = torch.FloatTensor(nweigths).uniform_()
            wk = torch.FloatTensor(nweigths).uniform_()

            # Purely imaginary quaternions unitary
            norms = torch.sqrt(wi**2 + wj**2 + wk**2) + 0.0001
            wi /= norms
            wj /= norms
            wk /= norms

            wi = wi.reshape(kernel_shape)
            wj = wj.reshape(kernel_shape)
            wk = wk.reshape(kernel_shape)

            modulus = torch.zeros(kernel_shape).uniform_(-s, s)
            phase = torch.zeros(kernel_shape).uniform_(-np.pi, np.pi)

            weight_r = modulus * torch.cos(phase)
            weight_i = modulus * wi * torch.sin(phase)
            weight_j = modulus * wj * torch.sin(phase)
            weight_k = modulus * wk * torch.sin(phase)

            tensor.data = torch.cat((weight_r, weight_i, weight_j, weight_k), dim = 1)


    def set_loss(self, loss_name):
        self.loss_name = loss_name
        loss_fnc = {
            'rotate': self.rotate_loss,
            'custom': self.custom_loss,
            'slide': self.slide_loss,
            'quate': self.quate_loss,
            'ruge': self.ruge_loss,
            'bce': self.bce_logits_loss,    # ruge loss; to test models with and without ruge rule addition
            'uncertain_loss': self.uncertain_loss,
            'limit_loss':self.Limit_Loss
        }

        if loss_name == 'quate':
            self.criterion = nn.Softplus()

        if loss_name == 'limit_loss':
            self.lda = 0.01
            print(self.lda)

        if loss_name == 'slide':
            self.margin = nn.Parameter(torch.FloatTensor([24.0]), requires_grad = True)

            self.lambda1 = 1.
            self.sigma = 1.01000100

        if loss_name == 'ruge':
            self.criterion = nn.BCEWithLogitsLoss(reduction = 'sum')
            self.ruge_rule_penalty = .01

        if loss_name not in loss_fnc:
            raise ValueError('model %s not supported' % loss_name)

        self.Loss = loss_fnc[loss_name]


    def select_relations(self, indices):
        relation_dict = {}
        if self.model_name in ['SpacESS']:
            relation_head = torch.index_select(
                self.rotator_head,
                dim=0,
                index=indices
            ).unsqueeze(1)

            relation_tail = torch.index_select(
                self.rotator_tail,
                dim=0,
                index=indices
            ).unsqueeze(1)
            relation_dict = {'rotator_head': relation_head, 'rotator_tail': relation_tail}

        if self.model_name != 'biRotatE':
            relation = torch.index_select(
                self.relation_embedding,
                dim=0,
                index=indices
            ).unsqueeze(1)
            relation_dict['translation'] = relation

        return relation_dict


    def entities_select(self, indices_heads, indices_tails):
        head = torch.index_select(
                self.entity_embedding,
                dim=0,
                index=indices_heads)

        tail = torch.index_select(
            self.entity_embedding,
            dim=0,
            index=indices_tails)
        return head, tail


    def forward(self, idx, sample, mode='single'):
        '''
        Forward function that calculate the score of a batch of triples.
        In the 'single' mode, sample is a batch of triple.
        In the 'head-batch' or 'tail-batch' mode, sample consists two part.
        The first part is usually the positive sample.
        And the second part is the entities in the negative samples.
        Because negative samples and positive samples usually share two elements
        in their triple ((head, relation) or (relation, tail)).
        '''
        self.idx = idx

        relation_list = []

        if mode == 'single':
            batch_size, negative_sample_size = sample.size(0), 1
            head, tail = self.entities_select(sample[:, 0], sample[:, 2])
            head = head.unsqueeze(1); tail = tail.unsqueeze(1)

            relation_dict = self.select_relations(sample[:, 1])

        elif mode == 'head-batch':
            tail_part, head_part = sample
            batch_size, negative_sample_size = head_part.size(0), head_part.size(1)

            head, tail = self.entities_select(head_part.view(-1), tail_part[:, 2])
            head = head.view(batch_size, negative_sample_size, -1)
            tail = tail.unsqueeze(1)

            relation_dict = self.select_relations(tail_part[:, 1])

        elif mode == 'tail-batch':
            head_part, tail_part = sample
            batch_size, negative_sample_size = tail_part.size(0), tail_part.size(1)
            head, tail = self.entities_select(head_part[:, 0], tail_part.view(-1))
            head = head.unsqueeze(1)
            tail = tail.view(batch_size, negative_sample_size, -1)

            relation_dict = self.select_relations(head_part[:, 1])

        else:
            raise ValueError('mode %s not supported' % mode)
        arg_dict = {
            'head': head,
            **relation_dict,
            'tail': tail
        
            }

        if self.model_name not in ['SpacESS']:
            arg_dict['mode'] = mode
        score = self.compute_score(arg_dict)

        return score

    def compute_score(self, arg_dict):
        model_func = {
            'TransE': self.TransE,
            'DistMult': self.DistMult,
            'ComplEx': self.ComplEx,
            'RotatE': self.RotatE,
            'pRotatE': self.pRotatE,
            'SpacESS': self.SpacESS,
            'TransComplEx': self.TransComplEx,
            'QuatE': self.QuatE
        }

        score = model_func[self.model_name](**arg_dict)
        return score

    def TransE(self, head, translation, tail, mode):
        if mode == 'head-batch':
            score = head + (translation - tail)
        else:
            score = (head + translation) - tail
        score = torch.norm(score, p = 1, dim = 2)
        return score
        #return self.gamma.item() - score


    def DistMult(self, head, translation, tail, mode):
        if mode == 'head-batch':
            score = head * (translation * tail)
        else:
            score = (head * translation) * tail

        score = score.sum(dim = 2)
        return score


    def ComplEx(self, head, translation, tail, mode):
        re_head, im_head = torch.chunk(head, 2, dim=2)
        re_relation, im_relation = torch.chunk(translation, 2, dim=2)
        re_tail, im_tail = torch.chunk(tail, 2, dim=2)

        if mode == 'head-batch':
            re_score = re_relation * re_tail + im_relation * im_tail
            im_score = re_relation * im_tail - im_relation * re_tail
            score = re_head * re_score + im_head * im_score
        else:
            re_score = re_head * re_relation - im_head * im_relation
            im_score = re_head * im_relation + im_head * re_relation
            score = re_score * re_tail + im_score * im_tail

        score = score.sum(dim = 2)
        return score


    def RotatE(self, head, translation, tail, mode):
        pi = 3.14159265358979323846

        re_head, im_head = torch.chunk(head, 2, dim=2)
        re_tail, im_tail = torch.chunk(tail, 2, dim=2)

        #Make phases of relations uniformly distributed in [-pi, pi]

        '''phase_relation = relation/(self.embedding_range.item()/pi)

            re_relation = torch.cos(phase_relation)
            im_relation = torch.sin(phase_relation)
        '''
        re_relation, im_relation = self.extract_relations(translation)

        if mode == 'head-batch':
            re_score = re_relation * re_tail + im_relation * im_tail
            im_score = re_relation * im_tail - im_relation * re_tail
            re_score = re_score - re_head
            im_score = im_score - im_head
        else:
            re_score = re_head * re_relation - im_head * im_relation
            im_score = re_head * im_relation + im_head * re_relation
            re_score = re_score - re_tail
            im_score = im_score - im_tail

        score = torch.stack([re_score, im_score], dim = 0)
        score = score.norm(dim = 0)
        score = score.sum(dim = 2)
        return score


    def extract_relations(self, *args):
        pi = 3.14159265358979323846

        split_relations = []
        for relation in args:
            phase_relation = relation/(self.embedding_range.item()/pi)
            re_relation = torch.cos(phase_relation)
            im_relation = torch.sin(phase_relation)
            split_relations.extend([re_relation, im_relation])
        return split_relations



    def TransComplEx(self, head, translation, tail, mode):
        re_head, im_head = torch.chunk(head, 2, dim=2)
        re_relation, im_relation = torch.chunk(translation, 2, dim=2)
        re_tail, im_tail = torch.chunk(tail, 2, dim=2)

        if mode == 'head-batch':        
            re_score = re_head + re_relation - re_tail
            im_score = im_head +  im_relation + im_tail
        else:
            re_score = re_head + re_relation - re_tail
            im_score = im_head +  im_relation + im_tail

        normscore_re = torch.norm(re_score, p=1, dim=2)
        normscore_im = torch.norm(im_score, p=1, dim=2)
        norm_score = normscore_re + normscore_im
        score = norm_score

        return score


    def SpacESS(self, head, rotator_head, rotator_tail, translation, tail):
        re_head, im_head = torch.chunk(head, 2, dim=2)
        re_tail, im_tail = torch.chunk(tail, 2, dim=2)

        # multipliers
        re_relation_head, im_relation_head, re_relation_tail, im_relation_tail = self.extract_relations(
            rotator_head, rotator_tail)

        re_translation, im_translation = torch.chunk(translation, 2, dim = 2)

        re_score_head = re_relation_head * re_head - im_relation_head * im_head
        im_score_head = re_relation_head * im_head + im_relation_head * re_head

        re_score_tail = re_relation_tail * re_tail + im_relation_tail * im_tail
        im_score_tail = re_relation_tail * im_tail - im_relation_tail * re_tail

        re_score = re_score_head + re_translation - re_score_tail
        im_score = im_score_head + re_translation - im_score_tail
        score = torch.stack([re_score, im_score], dim = 0)
        score = torch.norm(score, p = 1, dim = 0)
        score = score.sum(dim = 2)
        return score

    def pRotatE(self, head, translation, tail, mode):
        pi = 3.14159262358979323846

        #Make phases of entities and relations uniformly distributed in [-pi, pi]
        phase_head = head/(self.embedding_range.item()/pi)
        phase_relation = translation/(self.embedding_range.item()/pi)
        phase_tail = tail/(self.embedding_range.item()/pi)

        if mode == 'head-batch':
            score = phase_head + (phase_relation - phase_tail)
        else:
            score = (phase_head + phase_relation) - phase_tail

        score = torch.sin(score)
        score = torch.abs(score)

        score = self.gamma.item() - score.sum(dim = 2) * self.modulus
        return score

    def normalize_quaternion(self, tensor):
        t, ti, tj, tk = torch.chunk(tensor, 4, dim = 2)
        denom = torch.sqrt(t**2 + ti**2 + tj**2 + tk**2)
        t = t/denom
        ti = ti/denom
        tj = tj/denom
        tk = tk/denom
        return t, ti, tj, tk

    def QuatE(self, head, translation, tail):
        h, hi, hj, hk = torch.chunk(head, 4, dim=2)
        t, ti, tj, tk = torch.chunk(tail, 4, dim=2)

        r, ri, rj, rk = self.normalize_quaternion(translation)

        rotated_head_real = h*r - hi*ri - hj*rj - hk*rk
        rotated_head_i = h*ri + hi*r + hj*rk - hk*rj
        rotated_head_j = h*rj - hi*rk + hj*r + hk*ri
        rotated_head_k = h*rk + hi*rj - hj*ri + hk*r

        score = rotated_head_real*t + rotated_head_i*ti + rotated_head_j*tj + rotated_head_k*tk
        score = torch.sum(score, -1)
        print(score)
        return score

    def l2_regularizer2(self):
        regul = self.entity_embedding.norm() + self.relation_embedding.norm()
        regul = regul + self.rotator_head.norm()
        return regul

    def l2_regularizer(self):
        ent_re, ent_i, ent_j, ent_k = torch.chunk(self.entity_embedding, 4, dim = 1)
        tr_re, tr_i, tr_j, tr_k = torch.chunk(self.relation_embedding, 4, dim = 1)
        rot_re, rot_i, rot_j, rot_k = torch.chunk(self.rotator_head, 4, dim = 1)
        term1 = torch.mean(ent_re**2) + torch.mean(ent_i**2) + torch.mean(ent_j**2) + torch.mean(ent_k**2)
        temp2 = torch.mean(tr_re**2) + torch.mean(tr_j**2) + torch.mean(tr_k**2) + torch.mean(tr_i**2)
        term3 = torch.mean(rot_re**2) + torch.mean(rot_i**2) + torch.mean(rot_j**2) + torch.mean(rot_k**2)
        l2_reg = term1+temp2 + term3

        return l2_reg

    # ---------------------------------------------------------------------------
    def bce_logits_loss(self, positive_score, negative_score):
        criterion = nn.BCEWithLogitsLoss(reduction = 'sum')
        positive_score = positive_score.squeeze()
        negative_score = negative_score.view(-1)
        total = positive_score.numel() + negative_score.numel()
        positive_loss = criterion(positive_score, torch.ones(positive_score.shape).cuda())
        negative_loss = criterion(negative_score, torch.zeros(negative_score.shape).cuda())
        loss = (positive_loss + negative_loss)/total
        return positive_loss/positive_score.numel(), negative_loss/negative_score.numel(), loss

    def ruge_loss(self, positive_score, negative_score, unlabeled_scores, soft_labels):
        positive_loss = self.criterion(positive_score, torch.ones(positive_score.shape).cuda())
        negative_loss = self.criterion(negative_score, torch.zeros(negative_score.shape).cuda())
        total_labeled = positive_score.numel() + negative_score.numel()

        unlabeled_loss = self.criterion(unlabeled_scores.squeeze(), soft_labels.detach())

        l2 = self.l2_regularizer()
        l2 = self.entity_embedding.norm() + self.relation_embedding.norm()

        loss = labeled_loss/total_labeled + unlabeled_loss.mean() + 0.0002*l2

        loss = (positive_loss + negative_loss)/total_labeled + unlabeled_loss / unlabeled_scores.numel()
        return positive_loss/positive_score.numel(), negative_loss/negative_score.numel(), unlabeled_loss/unlabeled_scores.numel(), loss


    def quate_loss(self, positive_score, negative_score, subsampling_weight, args):
        negative_score = negative_score.view(1, -1).squeeze()
        positive_score = positive_score.squeeze()
        negative_loss = torch.mean(self.criterion(-negative_score))
        positive_loss = torch.mean(self.criterion(positive_score))

        regul1 = self.entity_embedding.norm(p = 1)**2
        loss = positive_loss + negative_loss

        return positive_loss, negative_loss, loss


    def rotate_loss(self, positive_score, negative_score, subsampling_weight, args):
        if self.model_name != 'ComplEx':
            negative_score = self.gamma.item() - negative_score
            positive_score = self.gamma.item() - positive_score

        if args.negative_adversarial_sampling:
            negative_score = (F.softmax(negative_score * args.adversarial_temperature, dim = 1).detach()
                              * F.logsigmoid(-negative_score)).sum(dim = 1)
        else:
            negative_score = F.logsigmoid(-negative_score).mean(dim = 1)


        positive_score = F.logsigmoid(positive_score).squeeze(dim = 1)

        if args.uni_weight:
            positive_sample_loss = - positive_score.mean()
            negative_sample_loss = - negative_score.mean()
        else:
            positive_sample_loss = - (subsampling_weight * positive_score).sum()/subsampling_weight.sum()
            negative_sample_loss = - (subsampling_weight * negative_score).sum()/subsampling_weight.sum()

        loss = (positive_sample_loss + negative_sample_loss)/2

        return positive_sample_loss, negative_sample_loss, loss

    def custom_loss(self, positive_score, negative_score, subsampling_weight, args):
        negative_score = self.gamma2 - negative_score               
        
        if args.negative_adversarial_sampling:
            negative_score = (F.softmax(negative_score * args.adversarial_temperature, dim = 1).detach()
                              * F.relu(negative_score)).sum(dim = 1)
        else:
            negative_score = F.relu(negative_score).mean(dim = 1)

        positive_score = positive_score - self.gamma1
        positive_score = F.relu(positive_score).squeeze(dim = 1)

        if args.uni_weight:
            positive_sample_loss = positive_score.mean()
            negative_sample_loss = negative_score.mean()
        else:
            positive_sample_loss = (subsampling_weight * positive_score).sum()/subsampling_weight.sum()
            negative_sample_loss = (subsampling_weight * negative_score).sum()/subsampling_weight.sum()

        loss = (positive_sample_loss + negative_sample_loss)/2
        return positive_sample_loss, negative_sample_loss, loss

    def uncertain_loss(self, positive_score, negative_score, subsampling_weight, args):

        if self.model_name != 'ComplEx':
            negative_score = self.gamma.item() - negative_score
            positive_score = positive_score - self.gamma.item()
        else:
            positive_score = -positive_score

        xi = self.xi[self.idx].squeeze(2)
        xi_neg = self.xi_neg[self.idx].squeeze(2)
        xi1 = xi_neg.repeat(1, negative_score.size()[1])

        if args.negative_adversarial_sampling:
            # In self-adversarial sampling, we do not apply back-propagation on the sampling weight
            negative_score = (F.softmax(negative_score * args.adversarial_temperature, dim=1).detach()
                              * F.softplus(negative_score + xi1 ** 2)).sum(dim=1)
        else:
            negative_score = F.softplus(negative_score + xi1 ** 2).mean(dim=1)

        positive_score = F.softplus(positive_score + xi ** 2).squeeze(dim=1)

        temp_pos = torch.exp(-self.alpha * xi ** 2)
        temp_neg = torch.exp(-self.alpha * xi1 ** 2)

        exp = torch.tensor([1000.05]).cuda().float() * torch.sum(temp_pos) \
              + torch.tensor([10.05]).cuda().float() * torch.sum(temp_neg)

        if args.uni_weight:
            positive_sample_loss = positive_score.mean()
            negative_sample_loss = negative_score.mean()
        else:
            positive_sample_loss = (subsampling_weight * positive_score).sum() / subsampling_weight.sum()
            negative_sample_loss = (subsampling_weight * negative_score).sum() / subsampling_weight.sum()

        loss = exp + (positive_sample_loss + negative_sample_loss) / 2
        # print(self.half_margin)

        return positive_sample_loss, negative_sample_loss, loss

    def Limit_Loss(self, positive_score, negative_score, subsampling_weight, args):
        temploss = torch.relu(positive_score + self.gamma - negative_score) 
        adv = 1 - F.softmax(negative_score * args.adversarial_temperature, dim=1).detach()
        if args.negative_adversarial_sampling:
            temploss = (adv * temploss).sum(dim=1)
        else:
            temploss = temploss.mean(dim=1)

        if args.uni_weight:                           
            positive_sample_loss = positive_score.mean()
            negative_sample_loss = negative_score.mean()
        else:
            positive_sample_loss = positive_score.mean()                    
            negative_sample_loss = negative_score.mean()

        loss = torch.mean(temploss)
        return positive_sample_loss, negative_sample_loss, loss

    def slide_loss(self, positive_score, negative_score, subsampling_weight, args):
        if self.model_name != 'ComplEx':
            negative_score = self.gamma.item() - negative_score
            positive_score = positive_score - self.gamma.item()
        else:
            positive_score = -positive_score
        if args.negative_adversarial_sampling:
            negative_score = (F.softmax(negative_score * args.adversarial_temperature, dim = 1).detach()
                              * F.relu(negative_score + self.margin**2)).sum(dim = 1)
        else:
            negative_score = F.relu(negative_score + self.margin**2).mean(dim = 1)

        positive_score = F.relu(positive_score + self.margin**2).squeeze(dim = 1)

        if args.uni_weight:
            positive_sample_loss = positive_score.mean()
            negative_sample_loss = negative_score.mean()
        else:
            positive_sample_loss = (subsampling_weight * positive_score).sum()/subsampling_weight.sum()
            negative_sample_loss = (subsampling_weight * negative_score).sum()/subsampling_weight.sum()


        loss = self.lambda1 * torch.exp(-self.sigma * self.margin**2) + (positive_sample_loss + negative_sample_loss)/2
        return positive_sample_loss, negative_sample_loss, loss

    #------------------------------------------
    def predict_soft_labels(self, rules, use_cuda):
        longTensor = torch.cuda.LongTensor if use_cuda else torch.LongTensor
        floatTensor = torch.cuda.FloatTensor if use_cuda else torch.FloatTensor

        labeled = [rule.premise for rule in rules]
        unlabeled = [rule.conclusion for rule in rules]
        conf = [rule.conf for rule in rules]
        labeled = longTensor(labeled)
        unlabeled = longTensor(unlabeled)
        conf = floatTensor(conf)

        labeled_scores = self.forward(labeled)
        labeled_scores = labeled_scores.detach()
        labeled_scores = torch.sigmoid(labeled_scores).squeeze()
        unlabeled_scores = self.forward(unlabeled)
        unlabeled_scores_sigmoid = torch.sigmoid(unlabeled_scores).squeeze()

        soft_labels = unlabeled_scores_sigmoid + self.ruge_rule_penalty*conf*labeled_scores
        soft_labels = torch.clamp(soft_labels, min = 0, max = 1)
        return unlabeled_scores, soft_labels


    # ------------------ LOSS FUNCTIONS FOR GROUNDINGS ----------------------------
    @staticmethod
    def inverse_loss(model, groundings, longTensor):
        sample_premise = torch.index_select(groundings, 1, longTensor([0, 2, 1]))
        sample_conclusion = torch.index_select(groundings, 1, longTensor([1, 3, 0]))
        scores_premise = model(model.idx, sample_premise)
        scores_concl = model(model.idx, sample_conclusion)
        return F.relu(scores_concl - scores_premise + model.epsilon_inv)   

    @staticmethod
    def implication_loss(model, groundings, longTensor):
        sample_premise = torch.index_select(groundings, 1, longTensor([0, 2, 1]))
        sample_conclusion = torch.index_select(groundings, 1, longTensor([0, 3, 1]))
        scores_premise = model(model.idx, sample_premise)
        scores_conclusion = model(model.idx, sample_conclusion)
        score = scores_conclusion - scores_premise + model.epsilon_impl
        score = F.relu(score).mean(dim = 1)
        return score

    @staticmethod
    def equality_loss(model, groundings, longTensor):
        sample_premise = torch.index_select(groundings, 1, longTensor([0, 2, 1]))
        sample_conclusion = torch.index_select(groundings, 1, longTensor([0, 3, 1]))
        scores_premise = model(model.idx, sample_premise)
        scores_conclusion = model(model.idx, sample_conclusion)
        loss = scores_premise - scores_conclusion - model.epsilon_eq
        return torch.norm(loss, p = 1)

    @staticmethod
    def symmetry_loss(model, groundings, longTensor):
        sample_premise = torch.index_select(groundings, 1, longTensor([0, 2, 1]))
        sample_conclusion = torch.index_select(groundings, 1, longTensor([1, 2, 0]))
        scores_premise = model(model.idx,sample_premise)
        scores_conclusion = model(model.idx, sample_conclusion)
        loss = scores_premise - scores_conclusion - model.epsilon_sym
        return torch.norm(loss, p = 1)

    @staticmethod
    def ruge_unlabeled_loss(model, groundings, use_cuda):
        unlabeled_scores, soft_labels = model.predict_soft_labels(groundings, use_cuda)
        unlabeled_scores = unlabeled_scores.squeeze()
        soft_labels = soft_labels.detach()
        softs_notones = soft_labels != 1
        softs_notzeros = soft_labels != 0
        softs = torch.sum(softs_notones == softs_notzeros).item()
        if softs != 0:
            print("FINALLY FOUND {} SOFT LABELS".format(softs))
        criterion = nn.BCEWithLogitsLoss()
        unlabeled_loss = criterion(unlabeled_scores, soft_labels.detach())
        return unlabeled_loss

    @staticmethod
    def adversarial_loss(adv_model, kge_model):
        # compute inconsistnecy loss
        conclusion, premise, conj = adv_model(kge_model, detach = False)  # clause scores
        loss = adv_model.adversarial_loss(conclusion, premise, conj, reverse = True)

        return loss

    # ------------------------------------------------------------------------------
    @staticmethod
    def check_nans(model):
        #print("in check nan")
        nan1 = nan2 = nan3 = nan4 = 0
        nan1 = torch.isnan(model.entity_embedding.grad).sum()
        nan2 = torch.isnan(model.relation_embedding.grad).sum()
        if model.model_name in ['TransRotatE', 'TransQuatE']:
            nan3 = torch.isnan(model.rotator_head.grad).sum()
            nan4 = torch.isnan(model.rotator_tail.grad).sum()
        if model.model_name in ['sTransRotatE', 'sTransQuatE']:
            nan3 = torch.isnan(model.rotator_head.grad).sum()

        if nan1 != 0: print(nan1.item(), ' nan vals in entity grads')
        if nan2 != 0: print(nan2.item(), ' nan vals in relation  grad')
        if nan3 != 0: print(nan3.item(), ' nan vals in rotator')
        if nan4 != 0: print(nan4.item(), ' nan vals in rotator tail')
        if nan1+nan2+nan3+nan4 != 0: exit()

    @staticmethod
    def rule_train_step(model, groundings, use_cuda):
        loss_groundings = {
            'inverse': model.inverse_loss,
            'implication': model.implication_loss,
            'equality': model.equality_loss,
            'symmetry': model.symmetry_loss
            }

        loss_rules = 0
        longTensor = torch.cuda.LongTensor if use_cuda else torch.LongTensor
        rules_log = {}
        for rule_type, grounds in groundings.items():
            if use_cuda:
                grounds = grounds.cuda()
            curr_rule_loss = loss_groundings[rule_type](model, grounds, longTensor)
            curr_rule_loss = curr_rule_loss.mean()
            loss_rules += model.rule_weight[rule_type] * curr_rule_loss
            rules_log[rule_type + ' loss'] = curr_rule_loss.item()

        return loss_rules, rules_log

    @staticmethod
    def ruge_train_step(model, optimizer, train_iterator, args, rules, show_soft):
        ''' A single ruge train step '''

        longTensor = torch.cuda.LongTensor if args.cuda else torch.LongTensor
        floatTensor = torch.cuda.FloatTensor if args.cuda else torch.FloatTensor

        model.train()
        optimizer.zero_grad()

        try:
            rules = rules['ruge']
        except:
            ValueError("No RUGE rules were defined")
        unlabeled_scores, soft_labels = model.predict_soft_labels(rules, args.cuda)
        if show_soft:
            print("Soft labels: ")
            print(soft_labels)

        positive_sample, negative_sample, subsampling_weight, mode = next(train_iterator)
        if args.cuda:
            positive_sample = positive_sample.cuda()
            negative_sample = negative_sample.cuda()
            subsampling_weight = subsampling_weight.cuda()

        negative_score = model((positive_sample, negative_sample), mode = mode)
        positive_score = model(positive_sample)

        loss_args = {
            "positive_score": positive_score,
            'negative_score': negative_score,
            'unlabeled_scores': unlabeled_scores,
            'soft_labels': soft_labels  }

        positive_loss, negative_loss,  unlabeled_loss, loss = model.Loss(**loss_args)

        if args.regularization != 0.0:
            #Use L3 regularization for ComplEx and DistMult
            regularization = args.regularization * (
                model.entity_embedding.norm(p = 3)**3 +
                model.relation_embedding.norm(p = 3).norm(p = 3)**3
            )
            loss = loss + regularization
            regularization_log = {'regularization': regularization.item()}
        else:
            regularization_log = {}

        loss.backward()
        model.check_nans(model)
        optimizer.step()

        log = {
            **regularization_log,
            'positive_sample_loss': positive_loss,
            'negative_sample_loss': negative_loss,
            'unlabeled_sample_loss': unlabeled_loss,
            'loss': loss.item()
            }

        return log


    @staticmethod
    def train_step(model, adv_model, optimizer, train_iterator, args, rules = None):
        '''
        A single train step. Apply back-propation and return the loss
        '''

        if args.project or args.adversarial:
            with torch.no_grad():
                model.relation_embedding.data = F.normalize(model.relation_embedding, p = 2, dim = 1 )
                model.entity_embedding.data.copy_(torch.clamp(model.entity_embedding, min = 0, max = 1))

        model.train()

        optimizer.zero_grad()

        adv_loss = torch.cuda.FloatTensor([0]) if args.cuda else torch.FloatTensor([0])
        adv_log = {}
        if args.adversarial:
            adv_loss = model.adversarial_loss(adv_model, model)
            adv_log['inconsistency loss '] = adv_loss.item()

        idx, positive_sample, negative_sample, subsampling_weight, mode = next(train_iterator)

        if args.cuda:
            positive_sample = positive_sample.cuda()
            negative_sample = negative_sample.cuda()
            subsampling_weight = subsampling_weight.cuda()

        negative_score = model(idx, (positive_sample, negative_sample), mode=mode)
        positive_score = model(idx, positive_sample)

        loss_args = {
                    'positive_score': positive_score,
                    'negative_score': negative_score,
        }
        if model.loss_name != 'bce':
            loss_args = {
                    **loss_args,
                    'subsampling_weight': subsampling_weight,
                    'args': args
            }
        positive_loss, negative_loss, loss = model.Loss(**loss_args)

        regularization_log = {}
        loss = loss + adv_loss

        if args.l2_r != 0: # add L2 regularization term
            if model.model_name == 'sTransRotatE':
                l2_regul = model.l2_regularizer2()
            else:
                l2_regul = model.l2_regularizer()
            loss = loss + args.l2_r*l2_regul
            regularization_log = {
                'l2: ': l2_regul.item()
            }

        if args.regularization != 0.0:
            #Use L3 regularization for ComplEx and DistMult
            regularization = args.regularization * (
                model.entity_embedding.norm(p = 3)**3 +
                model.relation_embedding.norm(p = 3).norm(p = 3)**3
            )
            loss = loss + regularization
            regularization_log['regularization'] = regularization.item()

        if not model.inject and not args.ruge_inject:   # no rule injection; just take step
            loss.backward()
            model.check_nans(model)
            optimizer.step()

        unlabeled_log = {}
        rules_log = {}

        if args.ruge_inject:    # RUGE rule injection
            ruges= rules['ruge']
            ruge_loss = model.ruge_unlabeled_loss(model, rules['ruge'], args.cuda)
            unlabeled_log['unlabeled_loss'] = ruge_loss.item()
            loss = loss + ruge_loss
            loss.backward()
            model.check_nans(model)
            optimizer.step()

        elif rules:   
            rule_loss, rules_log = model.rule_train_step(model, rules, args.cuda)
            loss = loss + rule_loss
            if model.inject:
                loss.backward()
                model.check_nans(model)
                optimizer.step()

        log = {
            **regularization_log,
            'positive_sample_loss': positive_loss,
            'negative_sample_loss': negative_loss,
            **adv_log,
            **unlabeled_log,
            **rules_log,
            'loss': loss.item()
        }
        return log

    @staticmethod
    def test_step(model, test_triples, all_true_triples, args):
        '''
        Evaluate the model on test or valid datasets
        '''

        model.eval()
        model.ranks = []
        if args.countries:
            #Countries S* datasets are evaluated on AUC-PR
            #Process test data for AUC-PR evaluation
            sample = list()
            y_true  = list()
            for head, relation, tail in test_triples:
                for candidate_region in args.regions:
                    y_true.append(1 if candidate_region == tail else 0)
                    sample.append((head, relation, candidate_region))

            sample = torch.LongTensor(sample)
            if args.cuda:
                sample = sample.cuda()

            with torch.no_grad():
                y_score = model(sample).squeeze(1).cpu().numpy()

            y_true = np.array(y_true)

            #average_precision_score is the same as auc_pr
            auc_pr = average_precision_score(y_true, y_score)

            metrics = {'auc_pr': auc_pr}

        else:
            #Otherwise use standard (filtered) MRR, MR, HITS@1, HITS@3, and HITS@10 metrics
            #Prepare dataloader for evaluation
            test_dataloader_head = DataLoader(
                TestDataset(
                    test_triples,
                    all_true_triples,
                    args.nentity,
                    args.nrelation,
                    'head-batch'
                ),
                batch_size=args.test_batch_size,
                num_workers=max(1, args.cpu_num//2),
                collate_fn=TestDataset.collate_fn
            )

            test_dataloader_tail = DataLoader(
                TestDataset(
                    test_triples,
                    all_true_triples,
                    args.nentity,
                    args.nrelation,
                    'tail-batch'
                ),
                batch_size=args.test_batch_size,
                num_workers=max(1, args.cpu_num//2),
                collate_fn=TestDataset.collate_fn
            )

            test_dataset_list = [test_dataloader_head, test_dataloader_tail]

            logs = []

            step = 0
            total_steps = sum([len(dataset) for dataset in test_dataset_list])

            with torch.no_grad():
                for test_dataset in test_dataset_list:
                    for idx, positive_sample, negative_sample, filter_bias, mode in test_dataset:
                        if args.cuda:
                            positive_sample = positive_sample.cuda()
                            negative_sample = negative_sample.cuda()
                            filter_bias = filter_bias.cuda()

                        batch_size = positive_sample.size(0)

                        score = model(idx, (positive_sample, negative_sample), mode)
                        if model.loss_name == 'quate':
                            score = -score
                        elif model.model_name != 'ComplEx':
                            score = model.gamma.item() - score
                        score += filter_bias

                        #Explicitly sort all the entities to ensure that there is no test exposure bias
                        argsort = torch.argsort(score, dim = 1, descending=True)
                        if mode == 'head-batch':
                            positive_arg = positive_sample[:, 0]
                        elif mode == 'tail-batch':
                            positive_arg = positive_sample[:, 2]
                        else:
                            raise ValueError('mode %s not supported' % mode)

                        for i in range(batch_size):
                            #Notice that argsort is not ranking
                            ranking = (argsort[i, :] == positive_arg[i]).nonzero()
                            assert ranking.size(0) == 1

                            #ranking + 1 is the true ranking used in evaluation metrics
                            ranking = 1 + ranking.item()
                            model.ranks.append(ranking)
                            logs.append({
                                'MRR': 1.0/ranking,
                                'MR': float(ranking),
                                'HITS@1': 1.0 if ranking <= 1 else 0.0,
                                'HITS@3': 1.0 if ranking <= 3 else 0.0,
                                'HITS@10': 1.0 if ranking <= 10 else 0.0,
                            })

                        if step % args.test_log_steps == 0:
                            logging.info('Evaluating the model... (%d/%d)' % (step, total_steps))

                        step += 1

            metrics = {}
            for metric in logs[0].keys():
                metrics[metric] = sum([log[metric] for log in logs])/len(logs)

        return metrics


    @staticmethod
    def getScore(model, train_triples, all_true_triples, args):        
        model.eval()
        test_dataloader_head = DataLoader(
                TestDataset(
                    train_triples,
                    all_true_triples,
                    args.nentity,
                    args.nrelation,
                    'head-batch'
                    ),
                batch_size=args.test_batch_size,
                num_workers=max(1, args.cpu_num // 2),
                collate_fn=TestDataset.collate_fn
                )

        test_dataloader_tail = DataLoader(
                TestDataset(
                    train_triples,
                    all_true_triples,
                    args.nentity,
                    args.nrelation,
                    'tail-batch'
                    ),
                batch_size=args.test_batch_size,
                num_workers=max(1, args.cpu_num // 2),
                collate_fn=TestDataset.collate_fn
                )

        test_dataset_list = [test_dataloader_head, test_dataloader_tail]
        logs = []
        step = 0
        scorepositive = list([])
        scorenegative = list([])
        Taopositive = list([])
        Taonegative = list([])
        Total_idx = list([])
        #total_steps = sum([len(dataset) for dataset in test_dataset_list])
        
        with torch.no_grad():
            for test_dataset in test_dataset_list:
                for idx, positive_sample, negative_sample, filter_bias, mode in test_dataset:
                    step = step + 1
                    #print(step)
                    if args.cuda:
                        idx = idx.cuda()
                        positive_sample = positive_sample.cuda()
                        negative_sample = negative_sample.cuda()
                        filter_bias = filter_bias.cuda()

                    batch_size = positive_sample.size(0)
                    rnd = np.random.randint(0, args.nentity, batch_size)
                    rnd = np.random.randint(0, args.nentity, batch_size)
                    #print(negative_sample[:, rnd])
                    #ns = negative_sample[10]
                    negative_sample = positive_sample.clone()
                    negative_sample[:,2] = torch.LongTensor(rnd) 

                    scorep = -model(idx, positive_sample) + model.gamma.item()
                    scoren = -model(idx, negative_sample) + model.gamma.item()

                    #print(positive_sample)
                    score2p = scorep.tolist()
                    score2n = scoren.tolist()
                    # Explicitly sort all the entities to ensure that there is no test exposure bias
                    #argsort = torch.argsort(score, dim=1, descending=True)
                    #score1 = - score1 + model.gamma.item()
                    scorepositive = scorepositive + score2p
                    scorenegative = scorenegative + score2n

                    Total_idx = Total_idx + idx.tolist()

                    #Taopositivetemp = model.xi[idx].tolist()
                    #Taonegativetemp = model.xi_neg[idx].tolist()
                    Taopositivetemp = torch.exp(-model.alpha * model.xi[idx] ** 2)
                    Taonegativetemp = torch.exp(-model.alpha * model.xi_neg[idx] ** 2)
                    Taopositivetemp = Taopositivetemp.squeeze(1)
                    Taonegativetemp = Taonegativetemp.squeeze(1)
                    Taopositivetemp = Taopositivetemp.tolist()
                    Taonegativetemp = Taonegativetemp.tolist()
                    Taopositive = Taopositive + Taopositivetemp
                    Taonegative = Taonegative + Taonegativetemp
                    #Total_idx = Total_idx + myidx
                    #test = model(idx, torch.tensor([[3,2,7],[3,2,8]]))
                    #print(test)

        with open('SP_TransRotatEfb15k.txt', 'w') as filehandle:
            for listitem in scorepositive:
                #print(listitem)
                filehandle.write('%s\n' % listitem)

        with open('ranking_TransRotatEfb15k.txt', 'w') as filehandle:
            for listitem in model.ranks:
                #print(listitem)
                filehandle.write('%s\n' % listitem)

        with open('idxTranseRotatEfb15k.txt', 'w') as filehandle:
            for listitem in Total_idx:
                #print(listitem)
                filehandle.write('%s\n' % listitem)

        with open('TP_0.1_Uncertain_Loss.txt', 'w') as filehandle:
            for listitem in Taopositive:
                #print(listitem)
                filehandle.write('%s\n' % listitem)

