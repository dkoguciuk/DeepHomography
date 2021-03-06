# coding: utf-8
import argparse
import torch
from torch.utils.data import DataLoader
from torch.nn.parallel import DistributedDataParallel
import numpy as np
from numpy import random
import os
import torch.nn as nn
import torch.optim as optim
from tensorboardX import SummaryWriter
import cv2
from torch_homography_model import build_model
from datetime import datetime
from dataset import TrainDataset
from utils import display_using_tensorboard
from utils import synchronize, get_rank
from dist_utils.checkpoint import CheckPointer

# name of log
train_log_dir = 'train_log_Doubleline-FastDLT'

# path of project
exp_name = os.path.abspath(os.path.join(os.path.dirname("__file__"), os.path.pardir))
exp_train_log_dir = os.path.join(exp_name, train_log_dir)

LOG_DIR = os.path.join(exp_train_log_dir, 'logs')

# Where to load model
MODEL_LOAD_DIR = os.path.join(exp_name, 'models')
# Where to save new model
MODEL_SAVE_DIR = os.path.join(exp_train_log_dir, 'real_models')

now_time = datetime.now()


def train(args, writer):

    train_path = os.path.join(exp_name, 'Data/Train_List.txt')
    net = build_model(args.model_name, pretrained=args.pretrained, fix_mask=args.fix_mask)

    if args.distributed:
        torch.nn.SyncBatchNorm.convert_sync_batchnorm(net)
        device = torch.device('cuda:{}'.format(args.local_rank))
        net.to(device)
        net = torch.nn.parallel.DistributedDataParallel(net, device_ids=[args.local_rank],
                                                        output_device=args.local_rank, find_unused_parameters=True)
    elif torch.cuda.is_available():
        device = torch.device('cuda:0')
        net = net.to(device)
    else:
        device = torch.device('cpu:0')
        net = net.to(device)

    train_data = TrainDataset(data_path=train_path, exp_path=exp_name, patch_w=args.patch_size_w,
                              patch_h=args.patch_size_h, rho=16)
    if args.distributed:
        train_sampler = torch.utils.data.distributed.DistributedSampler(train_data, num_replicas=args.gpus,
                                                                        rank=args.local_rank)
    else:
        train_sampler = torch.utils.data.RandomSampler(train_data)
    train_loader = DataLoader(dataset=train_data, batch_size=args.batch_size, num_workers=args.cpus, shuffle=False,
                              drop_last=True, sampler=train_sampler)
    optimizer = optim.Adam(net.parameters(), lr=args.lr, amsgrad=True, weight_decay=1e-4)  # default as 0.0001
    scheduler = optim.lr_scheduler.ExponentialLR(optimizer, gamma=0.8)

    ###########################################################################
    # Checkpoint load
    ###########################################################################

    checkpoint_arguments = {"step": 0}
    checkpointer = CheckPointer(net, optimizer, scheduler, MODEL_SAVE_DIR, True, None, device='cuda')
    extra_checkpoint_data = checkpointer.load()
    checkpoint_arguments.update(extra_checkpoint_data)
    if checkpoint_arguments['step'] != 0:
        print('Checkpoint loaded with step: ', checkpoint_arguments['step'])

    ###########################################################################
    # Start training
    ###########################################################################

    print("######################start training######################")
    print('LEN TRAIN_LOADER: ', len(train_loader))

    score_print_fre = 200
    model_save_fre = 4000
    glob_iter = 0
    start_epoch = 0
    if checkpoint_arguments['step'] != 0:
        glob_iter = checkpoint_arguments['step']
        start_epoch = int(np.rint(glob_iter / len(train_loader)))
        print('Global iter: {} start epoch: {}: ', glob_iter, start_epoch)

    for epoch in range(start_epoch, args.max_epoch):
        net.train()
        loss_sigma = 0.0
        loss_sigma_feature = 0.0

        scheduler.step()  # Note: The initial learning rate should be 1e-4. torch_version==1.0.1 ->init lr == 0.0001; torch_version>=1.2.0 ->init lr == 0.0001*1.25?
        print(epoch, 'lr={:.6f}'.format(scheduler.get_lr()[0]))
        for i, batch_value in enumerate(train_loader):

            org_imges = batch_value[0].float()
            input_tesnors = batch_value[1].float()
            patch_indices = batch_value[2].float()
            h4p = batch_value[3].float()

            I = org_imges[:, 0, ...]
            I = I[:, np.newaxis, ...]
            I2_ori_img = org_imges[:, 1, ...]
            I2_ori_img = I2_ori_img[:, np.newaxis, ...]
            I1 = input_tesnors[:, 0, ...]
            I1 = I1[:, np.newaxis, ...]
            I2 = input_tesnors[:, 1, ...]
            I2 = I2[:, np.newaxis, ...]

            # move to device
            org_imges = org_imges.to(device)
            input_tesnors = input_tesnors.to(device)
            patch_indices = patch_indices.to(device)
            h4p = h4p.to(device)
            I = I.to(device)
            I2_ori_img = I2_ori_img.to(device)
            I2 = I2.to(device)

            # forward, backward, update weights
            optimizer.zero_grad()

            batch_out = net(org_imges, input_tesnors, h4p, patch_indices)
            loss_feature_12 = batch_out['feature_loss_12'].mean()
            loss_feature_21 = batch_out['feature_loss_21'].mean()
            loss_homography = batch_out['homography_loss'].mean()
            pred_I2 = batch_out['pred_I2_d']
            I2_dataMat_CnnFeature = batch_out['patch_2_res_d']
            pred_I2_dataMat_CnnFeature = batch_out['pred_I2_CnnFeature_d']
            triMask = batch_out['mask_ap_I2_d']
            loss_map = batch_out['feature_loss_mat_12_d']

            total_loss = loss_feature_12 + loss_feature_21 + loss_homography
            total_loss.backward()
            optimizer.step()

            loss_sigma += total_loss.item()
            loss_sigma_feature += loss_feature_12.item()

            # print loss etc.
            if i % score_print_fre == 0 and i != 0:
                loss_avg_feature = loss_sigma_feature / score_print_fre
                loss_sigma = 0.0
                loss_sigma_feature = 0.0

                print("Training: Epoch[{:0>3}/{:0>3}] Iteration[{:0>3}]/[{:0>3}] Feature Loss: {:.4f} lr={:.8f}".format(epoch + 1,
                                                                                                       args.max_epoch,
                                                                                                       i + 1, len(train_loader), loss_avg_feature,
                                                                                                       scheduler.get_lr()[0]))

            # using tensorbordX to check the input or output performance during training
            if writer:
                if glob_iter % 200 == 0:
                    display_using_tensorboard(I, I2_ori_img, I2, pred_I2, I2_dataMat_CnnFeature, pred_I2_dataMat_CnnFeature,
                                              triMask, loss_map, writer)
                    writer.add_scalars('Loss_group', {'feature_loss_12': loss_feature_12.item()}, glob_iter)
                    writer.add_scalars('Loss_group', {'feature_loss_21': loss_feature_21.item()}, glob_iter)
                    writer.add_scalars('Loss_group', {'homography_loss': loss_homography.item()}, glob_iter)
                    writer.add_scalars('learning rate', {'value': scheduler.get_last_lr()[0]}, glob_iter)
                    writer.flush()

            # save model
            if (glob_iter % model_save_fre == 0 and glob_iter != start_epoch * len(train_loader) ):

                # Save state
                checkpoint_arguments['step'] = glob_iter
                checkpointer.save("model_{:06d}".format(glob_iter), **checkpoint_arguments)

                for name, layer in net.named_parameters():
                    if layer.requires_grad == True:
                        if writer:
                            writer.add_histogram(name + '_grad', layer.grad.cpu().data.numpy(), glob_iter)
                            writer.add_histogram(name + '_data', layer.cpu().data.numpy(), glob_iter)

            # Another glob iter
            glob_iter += 1

    # Save state
    checkpoint_arguments['step'] = glob_iter - 1
    checkpointer.save("model_{:06d}".format(glob_iter), **checkpoint_arguments)
    print('Finished Training')


if __name__=="__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument('--gpus', type=int, default=2, help='Number of gpus')
    parser.add_argument('--cpus', type=int, default=8, help='Number of cpus')

    parser.add_argument('--img_w', type=int, default=640)
    parser.add_argument('--img_h', type=int, default=360)
    parser.add_argument('--patch_size_h', type=int, default=315)
    parser.add_argument('--patch_size_w', type=int, default=560)

    parser.add_argument('--batch_size', type=int, default=32)
    parser.add_argument('--max_epoch', type=int, default=30)
    parser.add_argument('--lr', type=float, default=1e-4, help='learning rate')

    parser.add_argument('--model_name', type=str, default='resnet34')
    parser.add_argument('--fix_mask', type=bool, default=False, help='Should i fix mask?')
    parser.add_argument('--pretrained', type=bool, default=True, help='Use pretrained waights?')

    # Distributed
    parser.add_argument("--local_rank", type=int, default=0)
    parser.add_argument('--seed',default=0, type=int,
                        help='Random seed for processes. Seed must be fixed for distributed training')
    args = parser.parse_args()

    print('<==================== Loading data ===================>\n')
    print('LOCAL RANK: {}'.format(args.local_rank))

    args.distributed = args.gpus > 1
    if args.distributed:
        torch.manual_seed(args.seed)
        random.seed(args.seed)

        torch.cuda.set_device(args.local_rank)
        torch.distributed.init_process_group(backend="nccl", init_method="env://", world_size=args.gpus,
                                             rank=args.local_rank)
        synchronize()

    ###############################################################################
    # Create summary writer and file structure
    ###############################################################################

    save_to_disk = get_rank() == 0
    print('SAVE TO DISC:', save_to_disk)
    if save_to_disk:
        writer = SummaryWriter(log_dir=LOG_DIR)
        if not os.path.exists(MODEL_SAVE_DIR):
            try:
                os.makedirs(MODEL_SAVE_DIR)
            except OSError as e:
                print(e.args)
        if not os.path.exists(LOG_DIR):
            try:
                os.makedirs(LOG_DIR)
            except OSError as e:
                print(e.args)
    else:
        writer = None

    print(args)
    train(args, writer)


