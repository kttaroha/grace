import argparse
import os.path
import sys
sys.path.append("./")

from core import *
from torch_backend import *

parser = argparse.ArgumentParser()
parser.add_argument('--data_dir', type=str, default='./data')
parser.add_argument('--log_dir', type=str, default='.')
parser.add_argument('--master_address', '-m', type=str, default='127.0.0.1')
parser.add_argument('--rank', '-r', type=int, default=0)
parser.add_argument('--world_size', '-w', type=int, default=2)
parser.add_argument('--compression_method', type=str, default='none')
parser.add_argument('--communication_method', type=str, default='allreduce')
parser.add_argument('--clipping', action='store_true', default=False)
parser.add_argument('--efsgdlr', type=float, default=0.1)
parser.add_argument('--momentum', type=float, default=0.9)
parser.add_argument('--memory', action='store_true', default=False)
parser.add_argument('--backend', type=str, default='nccl')
parser.add_argument('--compress_ratio', type=float, default=0.3)
parser.add_argument('--threshold', type=float, default=0.01)
parser.add_argument('--qstates', type=int, default=256)


# Network definition
def conv_bn(c_in, c_out, bn_weight_init=1.0, **kw):
    return {
        'conv': nn.Conv2d(c_in, c_out, kernel_size=3, stride=1, padding=1, bias=False),
        'bn': batch_norm(c_out, bn_weight_init=bn_weight_init, **kw),
        'relu': nn.ReLU(True)
    }


def residual(c, **kw):
    return {
        'in': Identity(),
        'res1': conv_bn(c, c, **kw),
        'res2': conv_bn(c, c, **kw),
        'add': (Add(), [rel_path('in'), rel_path('res2', 'relu')]),
    }


def basic_net(channels, weight, pool, **kw):
    return {
        'prep': conv_bn(3, channels['prep'], **kw),
        'layer1': dict(conv_bn(channels['prep'], channels['layer1'], **kw), pool=pool),
        'layer2': dict(conv_bn(channels['layer1'], channels['layer2'], **kw), pool=pool),
        'layer3': dict(conv_bn(channels['layer2'], channels['layer3'], **kw), pool=pool),
        'pool': nn.MaxPool2d(4),
        'flatten': Flatten(),
        'linear': nn.Linear(channels['layer3'], 10, bias=False),
        'classifier': Mul(weight),
    }


def net(channels=None, weight=0.125, pool=nn.MaxPool2d(2), extra_layers=(), res_layers=('layer1', 'layer3'), **kw):
    channels = channels or {'prep': 64, 'layer1': 128, 'layer2': 256, 'layer3': 512}
    n = basic_net(channels, weight, pool, **kw)
    for layer in res_layers:
        n[layer]['residual'] = residual(channels[layer], **kw)
    for layer in extra_layers:
        n[layer]['extra'] = conv_bn(channels[layer], channels[layer], **kw)
    return n


losses = {
    'loss': (nn.CrossEntropyLoss(reduction='none'), [('classifier',), ('target',)]),
    'correct': (Correct(), [('classifier',), ('target',)]),
}


class TSVLogger:
    def __init__(self):
        self.log = ['epoch\thours\ttop1Accuracy']

    def append(self, output):
        epoch, hours, acc = output['epoch'], output['total time'] / 3600, output['test acc'] * 100
        self.log.append(f'{epoch}\t{hours:.8f}\t{acc:.2f}')

    def __str__(self):
        return '\n'.join(self.log)


def main():
    args = parser.parse_args()

    print('Downloading datasets')
    dataset = cifar10(args.data_dir)

    epochs = 24
    lr_schedule = PiecewiseLinear([0, 5, epochs], [0, 0.4, 0])
    batch_size = 512
    train_transforms = [Crop(32, 32), FlipLR(), Cutout(8, 8)]

    model = Network(union(net(), losses)).to(device)

    print('Warming up cudnn on random inputs')
    for size in [batch_size, len(dataset['test']['labels']) % batch_size]:
        warmup_cudnn(model, size)

    print('Starting timer')
    timer = Timer(synch=torch.cuda.synchronize)

    print('Preprocessing training data')
    train_set = list(zip(transpose(normalise(pad(dataset['train']['data'], 4))), dataset['train']['labels']))
    print(f'Finished in {timer():.2} seconds')
    print('Preprocessing test data')
    test_set = list(zip(transpose(normalise(dataset['test']['data'])), dataset['test']['labels']))
    print(f'Finished in {timer():.2} seconds')

    TSV = TSVLogger()

    train_batches = Batches(Transform(train_set, train_transforms), batch_size, shuffle=True, set_random_choices=True,
                            drop_last=True)
    test_batches = Batches(test_set, batch_size, shuffle=False, drop_last=False)
    lr = lambda step: lr_schedule(step / len(train_batches)) / batch_size
    opt = SGD(trainable_params(model), lr=lr, momentum=0.9, weight_decay=5e-4 * batch_size, nesterov=True)
    from grace_dl.dist.communicator.allgather import Allgather
    from grace_dl.dist.compressor.topk import TopKCompressor
    from grace_dl.dist.memory.residual import ResidualMemory
    grc = Allgather(TopKCompressor(0.3), ResidualMemory(), args.world_size)

    from grace_dl.dist.helper import grace_from_params
    params = {'compressor': 'none', 'memory': 'none', 'communicator': 'allreduce'}
    params['world_size'] = args.world_size
    grc = grace_from_params(params)

    train(model, opt, train_batches, test_batches, epochs, args.master_address, args.rank, grc,
          loggers=(TableLogger(), TSV), timer=timer, test_time_in_total=False, backend=args.backend)

    with open(os.path.join(os.path.expanduser(args.log_dir), 'logs.tsv'), 'w') as f:
        f.write(str(TSV))


if __name__ == '__main__':
    main()
