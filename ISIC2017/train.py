import os
import csv
import random
import argparse
import numpy as np
from tensorboardX import SummaryWriter
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import torch.optim.lr_scheduler as lr_scheduler
import torchvision
import torchvision.utils as utils
import torchvision.transforms as transforms
from model_vgg_grid import AttnVGG
from model_res_1 import AttnResNet
from loss import FocalLoss
from data import preprocess_data, ISIC2017
from utilities import *

os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
os.environ["CUDA_VISIBLE_DEVICES"] = "0,1"

base_seed = 0
torch.backends.cudnn.deterministic = True
torch.manual_seed(base_seed)
torch.cuda.manual_seed_all(base_seed)

parser = argparse.ArgumentParser(description="Attn-Skin-train")

parser.add_argument("--preprocess", action='store_true', help="run preprocess_data")

parser.add_argument("--batch_size", type=int, default=32, help="batch size")
parser.add_argument("--epochs", type=int, default=50, help="number of epochs")
parser.add_argument("--lr", type=float, default=0.01, help="initial learning rate")
parser.add_argument("--outf", type=str, default="logs", help='path of log files')
parser.add_argument("--base_up_factor", type=int, default=8, help="number of epochs")
parser.add_argument("--model", type=str, default="VGGNet", help='VGGNet or ResNet')

parser.add_argument("--normalize_attn", action='store_true', help='if True, attention map is normalized by softmax; otherwise use sigmoid')
parser.add_argument("--focal_loss", action='store_true', help='turn on focal loss (otherwise use cross entropy loss)')
parser.add_argument("--no_attention", action='store_true', help='turn off attention')
parser.add_argument("--over_sample", action='store_true', help='offline oversampling')
parser.add_argument("--log_images", action='store_true', help='log images')

opt = parser.parse_args()

def main():
    # load data
    print('\nloading the dataset ...\n')
    if opt.over_sample:
        print('\ndata is offline oversampled ...\n')
        num_aug = 5
        train_file = 'train_oversample.csv'
    else:
        print('\nno offline oversampling ...\n')
        num_aug = 8
        train_file = 'train.csv'
    im_size = 224
    transform_train = transforms.Compose([
        transforms.Resize((256,256)),
        transforms.RandomCrop(im_size),
        transforms.RandomVerticalFlip(),
        transforms.RandomHorizontalFlip(),
        transforms.ToTensor(),
        transforms.Normalize((0.6916, 0.5459, 0.4865), (0.0834, 0.1164, 0.1322))
    ])
    transform_test = transforms.Compose([
        transforms.Resize((256,256)),
        transforms.CenterCrop(im_size),
        transforms.ToTensor(),
        transforms.Normalize((0.6916, 0.5459, 0.4865), (0.0834, 0.1164, 0.1322))
    ])
    def _init_fn(worker_id):
        random.seed(base_seed + worker_id)
    trainset = ISIC2017(csv_file=train_file, shuffle=True, transform=transform_train)
    trainloader = torch.utils.data.DataLoader(trainset, batch_size=opt.batch_size, shuffle=True, num_workers=8, worker_init_fn=_init_fn)
    testset = ISIC2017(csv_file='test.csv', shuffle=False, rotate=False, transform=transform_test)
    testloader = torch.utils.data.DataLoader(testset, batch_size=64, shuffle=False, num_workers=8)
    # mean & std of the dataset
    '''
    Mean = torch.zeros(3)
    Std = torch.zeros(3)
    for data in trainloader:
        I, L = data
        N, C, __, __ = I.size()
        Mean += I.view(N,C,-1).mean(2).sum(0)
        Std += I.view(N,C,-1).std(2).sum(0)
    Mean /= len(trainset)
    Std  /= len(trainset)
    print('mean: '), print(Mean.numpy())
    print('std: '), print(Std.numpy())
    return
    '''
    print('\ndone\n')

    # load models
    print('\nloading the model ...\n')

    if not opt.no_attention:
        print('\nturn on attention ...\n')
    else:
        print('\nturn off attention ...\n')

    if opt.model == 'VGGNet':
        print('\nbase model: VGGNet ...\n')
        net = AttnVGG(num_classes=2, attention=not opt.no_attention, normalize_attn=opt.normalize_attn)
    elif opt.model == 'ResNet':
        print('\nbase model: ResNet ...\n')
        net = AttnResNet(num_classes=2, attention=not opt.no_attention, normalize_attn=opt.normalize_attn)
    else:
        raise NotImplementedError("Invalid base model name!")

    if opt.focal_loss:
        print('\nuse focal loss ...\n')
        criterion = FocalLoss(gama=2., size_average=True, weight=None)
    else:
        print('\nuse cross entropy loss ...\n')
        criterion = nn.CrossEntropyLoss()
    print('\ndone\n')

    # move to GPU
    print('\nmoving models to GPU ...\n')
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    device_ids = [0,1]
    model = nn.DataParallel(net, device_ids=device_ids).to(device)
    criterion.to(device)
    print('\ndone\n')

    # optimizer
    optimizer = optim.SGD(model.parameters(), lr=opt.lr, momentum=0.9, weight_decay=5e-4)
    lr_lambda = lambda epoch : np.power(0.5, epoch//10)
    scheduler = lr_scheduler.LambdaLR(optimizer, lr_lambda=lr_lambda)

    # training
    print('\nstart training ...\n')
    step = 0
    running_avg_accuracy = 0
    writer = SummaryWriter(opt.outf)
    for epoch in range(opt.epochs):
        images_disp = []
        # adjust learning rate
        scheduler.step()
        current_lr = optimizer.param_groups[0]['lr']
        writer.add_scalar('train/learning_rate', current_lr, epoch)
        print("\nepoch %d learning rate %f\n" % (epoch, current_lr))
        # run for one epoch
        for aug in range(num_aug):
            for i, data in enumerate(trainloader, 0):
                # warm up
                model.train()
                model.zero_grad()
                optimizer.zero_grad()
                inputs, labels = data
                # inputs = (inputs - Mean.view(1,3,1,1)) / Std.view(1,3,1,1)
                inputs, labels = inputs.to(device), labels.to(device)
                if (aug == 0) and (i == 0): # archive images in order to save to logs
                    images_disp.append(inputs[0:16,:,:,:])
                # forward
                pred, __, __, __ = model.forward(inputs)
                # backward
                loss = criterion(pred, labels)
                loss.backward()
                optimizer.step()
                # display results
                if i % 10 == 0:
                    model.eval()
                    pred, __, __, __ = model.forward(inputs)
                    predict = torch.argmax(pred, 1)
                    total = labels.size(0)
                    correct = torch.eq(predict, labels).sum().double().item()
                    accuracy = correct / total
                    running_avg_accuracy = 0.98*running_avg_accuracy + 0.02*accuracy
                    writer.add_scalar('train/loss', loss.item(), step)
                    writer.add_scalar('train/accuracy', accuracy, step)
                    writer.add_scalar('train/running_avg_accuracy', running_avg_accuracy, step)
                    print("[epoch %d][aug %d/%d][%d/%d] loss %.4f accuracy %.2f%% running avg accuracy %.2f%%"
                        % (epoch, aug, num_aug-1, i, len(trainloader)-1, loss.item(), (100*accuracy), (100*running_avg_accuracy)))
                step += 1
        # the end of each epoch: test & log
        model.eval()
        print('\none epoch done, saving checkpoints ...\n')
        torch.save(model.state_dict(), os.path.join(opt.outf, 'net.pth'))
        if epoch == opt.epochs / 2:
            torch.save(model.state_dict(), os.path.join(opt.outf, 'net%d.pth' % epoch))
        total = 0
        correct = 0
        with torch.no_grad():
            with open('test_results.csv', 'wt', newline='') as csv_file:
                csv_writer = csv.writer(csv_file, delimiter=',')
                for i, data in enumerate(testloader, 0):
                    images_test, labels_test = data
                    # images_test = (images_test - Mean.view(1,3,1,1)) / Std.view(1,3,1,1)
                    images_test, labels_test = images_test.to(device), labels_test.to(device)
                    if i == 0: # archive images in order to save to logs
                        images_disp.append(images_test[0:16,:,:,:])
                    pred_test, __, __, __ = model.forward(images_test)
                    predict = torch.argmax(pred_test, 1)
                    total += labels_test.size(0)
                    correct += torch.eq(predict, labels_test).sum().double().item()
                    # record test predicted responses
                    responses = F.softmax(pred_test, dim=1).squeeze().cpu().numpy()
                    responses = [responses[i] for i in range(responses.shape[0])]
                    csv_writer.writerows(responses)
            # log scalars
            precision, recall, precision_mel, recall_mel = compute_mean_pecision_recall('test_results.csv')
            mAP, AUC, ROC = compute_metrics('test_results.csv')
            writer.add_scalar('test/accuracy', correct/total, epoch)
            writer.add_scalar('test/mean_precision', precision, epoch)
            writer.add_scalar('test/mean_recall', recall, epoch)
            writer.add_scalar('test/precision_mel', precision_mel, epoch)
            writer.add_scalar('test/recall_mel', recall_mel, epoch)
            writer.add_scalar('test/mAP', mAP, epoch)
            writer.add_scalar('test/AUC', AUC, epoch)
            writer.add_image('curve/ROC', ROC, epoch)
            print("\n[epoch %d] test result: accuracy %.2f%% \nmean precision %.2f%% mean recall %.2f%% \
                    \nprecision for mel %.2f%% recall for mel %.2f%% \nmAP %.2f%% AUC %.4f\n" %
                    (epoch, 100*correct/total, 100*precision, 100*recall, 100*precision_mel, 100*recall_mel, 100*mAP, AUC))
            # log images
            if opt.log_images:
                print('\nlog images ...\n')
                I_train = utils.make_grid(images_disp[0], nrow=4, normalize=True, scale_each=True)
                writer.add_image('train/image', I_train, epoch)
                if epoch == 0:
                    I_test = utils.make_grid(images_disp[1], nrow=4, normalize=True, scale_each=True)
                    writer.add_image('test/image', I_test, epoch)
            if opt.log_images and (not opt.no_attention):
                if opt.normalize_attn:
                    vis_fun = visualize_attn_softmax
                else:
                    vis_fun = visualize_attn_sigmoid
                print('\nlog attention maps ...\n')
                # training data
                __, c1, c2, c3 = model.forward(images_disp[0])
                if c1 is not None:
                    attn1, stat = vis_fun(I_train, c1, up_factor=opt.base_up_factor, nrow=4)
                    writer.add_image('train/attention_map_1', attn1, epoch)
                    writer.add_scalar('train_c1/max', stat[0], epoch)
                    writer.add_scalar('train_c1/min', stat[1], epoch)
                    writer.add_scalar('train_c1/mean', stat[2], epoch)
                if c2 is not None:
                    attn2, stat = vis_fun(I_train, c2, up_factor=2*opt.base_up_factor, nrow=4)
                    writer.add_image('train/attention_map_2', attn2, epoch)
                    writer.add_scalar('train_c2/max', stat[0], epoch)
                    writer.add_scalar('train_c2/min', stat[1], epoch)
                    writer.add_scalar('train_c2/mean', stat[2], epoch)
                if c3 is not None:
                    attn3, stat = vis_fun(I_train, c3, up_factor=4*opt.base_up_factor, nrow=4)
                    writer.add_image('train/attention_map_3', attn3, epoch)
                    writer.add_scalar('train_c3/max', stat[0], epoch)
                    writer.add_scalar('train_c3/min', stat[1], epoch)
                    writer.add_scalar('train_c3/mean', stat[2], epoch)
                # test data
                __, c1, c2, c3 = model.forward(images_disp[1])
                if c1 is not None:
                    attn1, __ = vis_fun(I_test, c1, up_factor=opt.base_up_factor, nrow=4)
                    writer.add_image('test/attention_map_1', attn1, epoch)
                if c2 is not None:
                    attn2, __ = vis_fun(I_test, c2, up_factor=2*opt.base_up_factor, nrow=4)
                    writer.add_image('test/attention_map_2', attn2, epoch)
                if c3 is not None:
                    attn3, __ = vis_fun(I_test, c3, up_factor=4*opt.base_up_factor, nrow=4)
                    writer.add_image('test/attention_map_3', attn3, epoch)

if __name__ == "__main__":
    if opt.preprocess:
        preprocess_data(root_dir='data_2017')
    main()
