from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
import os.path as path
import argparse
import time

import torch
import torch.nn.functional as F

from core.defense import Dataset
from core.defense import MulModMalwareDetector
from tools.utils import save_args, get_group_args, to_tensor, dump_pickle, read_pickle

cmd_md = argparse.ArgumentParser(description='arguments for learning malware detector')

feature_argparse = cmd_md.add_argument_group(title='feature')
feature_argparse.add_argument('--proc_number', type=int, default=2,
                              help='The number of threads for features extraction.')
feature_argparse.add_argument('--number_of_smali_files', type=int, default=1000000,
                              help='The maximum number of smali files to represent each app')
feature_argparse.add_argument('--use_top_disc_features', action='store_true',
                              help='Whether use feature selection or not.')
feature_argparse.add_argument('--max_vocab_size', type=int, default=5000,
                              help='The maximum number of vocabulary size')
feature_argparse.add_argument('--update', action='store_true',
                              help='Whether update the existed features.')

detector_argparse = cmd_md.add_argument_group(title='detector')
detector_argparse.add_argument('--cuda', action='store_true', default=False,
                               help='whether use cuda enable gpu or cpu.')
detector_argparse.add_argument('--seed', type=int, default=0,
                               help='random seed.')
detector_argparse.add_argument('--embedding_dim', type=int, default=64,
                               help='embedding dimension')
detector_argparse.add_argument('--hidden_units', type=lambda s: [int(u) for u in s.split(',')], default='128,128',
                               help='delimited between two integers, e.g., "128,128"', )
detector_argparse.add_argument('--dense_hidden_units', type=lambda s: [int(u) for u in s.split(',')], default='200,200',
                               help='delimited list input, e.g., "32,32"', )
detector_argparse.add_argument('--dropout', type=float, default=0.6,
                               help='dropout rate')
detector_argparse.add_argument('--alpha_', type=float, default=0.2,
                               help='slope coefficient of leaky-relu or elu')
detector_argparse.add_argument('--with_relu', action='store_true', default=False,
                               help='use relu activation elu in the gcn layers.')
detector_argparse.add_argument('--with_bias', action='store_true', default=True,
                               help='append bias in the gcn layers.')
detector_argparse.add_argument('--smooth', action='store_true', default=False,
                               help='use smooth activation elu (rather than leaky-relu) in the nn layers.')

detector_argparse.add_argument('--batch_size', type=int, default=64,
                               help='mini-batch size')
detector_argparse.add_argument('--epochs', type=int, default=100,
                               help='number of epochs to train.')
detector_argparse.add_argument('--lr', type=float, default=0.005,
                               help='initial learning rate.')
detector_argparse.add_argument('--weight_decay', type=float, default=5e-4,
                               help='coefficient of weight decay')

mode_argparse = cmd_md.add_argument_group(title='mode')
mode_argparse.add_argument('--mode', type=str, default='train', choices=['train', 'test'], required=False,
                           help='learn a model or test it.')
mode_argparse.add_argument('--model_name', type=str, default='xxxxxxxx-xxxxxx', required=False,
                           help='suffix date of a tested model name.')


def _main():
    args = cmd_md.parse_args()
    dataset = Dataset(feature_ext_args=get_group_args(args, cmd_md, 'feature'))
    train_x1, train_x2, trainy = dataset.get_numerical_input_batch(*dataset.train_dataset, 'train')
    validation_x1, validation_x2, valy = dataset.get_numerical_input_batch(*dataset.validation_dataset, 'validation')
    test_x1, test_x2, testy = dataset.get_numerical_input_batch(*dataset.test_dataset, 'test')
    train_dataset_producer = dataset.get_input_producer(train_x1, train_x2, trainy, batch_size=args.batch_size,
                                                        name='train')
    val_dataset_producer = dataset.get_input_producer(validation_x1, validation_x2, valy, batch_size=args.batch_size,
                                                      name='val')
    test_dataset_producer = dataset.get_input_producer(test_x1, test_x2, testy, batch_size=args.batch_size, name='test')
    assert dataset.n_classes == 2

    # test: model training
    if not args.cuda:
        dv = 'cpu'
    else:
        dv = 'cuda'

    print(dataset.vocab_size)
    import sys
    sys.exit(1)

    model_name = args.model_name if args.mode == 'test' else time.strftime("%Y%m%d-%H%M%S")
    model = MulModMalwareDetector(dataset.non_api_size,
                                  dataset.vocab_size - dataset.non_api_size,
                                  dataset.n_classes,
                                  device=dv,
                                  name=model_name,
                                  **vars(args)
                                  )
    print('===============================================model architecture=========================================')
    print(model)
    print('===============================================end=========================================')

    model = model.to(dv)

    if args.mode == 'train':
        model.fit(train_dataset_producer,
                  val_dataset_producer,
                  epochs=args.epochs,
                  lr=args.lr,
                  weight_decay=args.weight_decay
                  )
        # human readable
        save_args(path.join(path.dirname(model.model_save_path), "hparam"), vars(args))
        # serialization for building the neural nets
        dump_pickle(vars(args), path.join(path.dirname(model.model_save_path), "hparam.pkl"))

    # test: accuracy
    model.load()
    model.predict(test_dataset_producer)


if __name__ == '__main__':
    _main()