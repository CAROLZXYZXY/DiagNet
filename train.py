import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.init as init
from torch.autograd import Variable
import numpy as np
import os
import sys
import config
from models.mfb_baseline import mfb_baseline
from models.mfh_baseline import mfh_baseline
from models.mfb_coatt_glove import mfb_coatt_glove
from models.mfh_coatt_glove import mfh_coatt_glove
from models.mfb_coatt_embed_ocr import mfb_coatt_embed_ocr
from models.mfh_coatt_embed_ocr import mfh_coatt_embed_ocr
from models.mfb_coatt_embed_ocr_bin import mfb_coatt_embed_ocr_bin
from models.mfh_coatt_embed_ocr_bin import mfh_coatt_embed_ocr_bin
from models.mfb_coatt_embed_ocr_binonly import mfb_coatt_embed_ocr_binonly
from models.mfh_coatt_embed_ocr_binonly import mfh_coatt_embed_ocr_binonly
from models.mfb_coatt_embed_ocr_binhelp import mfb_coatt_embed_ocr_binhelp
from models.mfh_coatt_embed_ocr_binhelp import mfh_coatt_embed_ocr_binhelp
from utils import data_provider
from utils.data_provider import VQADataProvider
from utils.eval_utils import exec_validation, drawgraph
from utils.commons import cuda_wrapper, get_time, check_mkdir, get_logger
import json
from tensorboardX import SummaryWriter


def train(opt, model, train_Loader, optimizer, lr_scheduler, writer, folder, logger):
    if opt.LATE_FUSION:
        criterion = nn.BCELoss()
        model_prob = model[1]
        model = model[0]
    else:
        criterion = nn.KLDivLoss(reduction='batchmean')
        if opt.BINARY or opt.BIN_HELP:
            criterion2 = nn.BCELoss()
    train_loss = np.zeros(opt.MAX_ITERATIONS)
    # loss for binary predictor
    b_losses = np.zeros(opt.MAX_ITERATIONS)
    # loss for vocab dict part
    voc_losses = np.zeros(opt.MAX_ITERATIONS)
    # loss for ocr tokens part
    ocr_losses = np.zeros(opt.MAX_ITERATIONS)
    results = []
    for iter_idx, (data, word_length, img_feature, label, embed_matrix, ocr_length, ocr_embedding, _, ocr_answer_flags, epoch) in enumerate(train_Loader):
        if iter_idx >= opt.MAX_ITERATIONS:
            break
        model.train()
        epoch = epoch.numpy()
        # TODO: get rid of these weird redundant first dims
        data = torch.squeeze(data, 0)
        word_length = torch.squeeze(word_length, 0)
        img_feature = torch.squeeze(img_feature, 0)
        label = torch.squeeze(label, 0)
        embed_matrix = torch.squeeze(embed_matrix, 0)
        ocr_length = torch.squeeze(ocr_length, 0)
        ocr_embedding = torch.squeeze(ocr_embedding, 0)
        ocr_answer_flags = torch.squeeze(ocr_answer_flags, 0)

        data = cuda_wrapper(Variable(data)).long()
        word_length = cuda_wrapper(word_length)
        img_feature = cuda_wrapper(Variable(img_feature)).float()
        label = cuda_wrapper(Variable(label)).float()
        optimizer.zero_grad()

        if opt.OCR:
            embed_matrix = cuda_wrapper(Variable(embed_matrix)).float()
            ocr_length = cuda_wrapper(ocr_length)
            ocr_embedding = cuda_wrapper(Variable(ocr_embedding)).float()
            if opt.BIN_HELP:
                ocr_answer_flags = cuda_wrapper(ocr_answer_flags)
                binary, pred = model(data, img_feature, embed_matrix, ocr_length, ocr_embedding, 'train')
            elif opt.BINARY:
                ocr_answer_flags = cuda_wrapper(ocr_answer_flags)
                if not opt.LATE_FUSION:
                    binary, pred1, pred2 = model(data, img_feature, embed_matrix, ocr_length, ocr_embedding, 'train')
                else:
                    pred = model(data, img_feature, embed_matrix, ocr_length, ocr_embedding, 'train')
            else:
                pred = model(data, img_feature, embed_matrix, ocr_length, ocr_embedding, 'train')
        elif opt.EMBED:
            embed_matrix = cuda_wrapper(Variable(embed_matrix)).float()

            pred = model(data, img_feature, embed_matrix, 'train')
        else:
            pred = model(data, word_length, img_feature, 'train')

        if opt.LATE_FUSION:
            loss = criterion(pred, ocr_answer_flags.float())
        elif opt.BINARY:
            b_loss = criterion2(binary, ocr_answer_flags.float())
            voc_loss = criterion(pred1, label[:, 0:opt.MAX_ANSWER_VOCAB_SIZE])
            b_losses[iter_idx] = b_loss.data.float()
            voc_losses[iter_idx] = voc_loss.data.float()
            ocr_loss = criterion(pred2, label[:, opt.MAX_ANSWER_VOCAB_SIZE:])
            ocr_losses[iter_idx] = ocr_loss.data.float()
            loss = b_loss * opt.BIN_LOSS_RATE + voc_loss + ocr_loss * opt.BIN_TOKEN_RATE
        elif opt.BIN_HELP:
            b_loss = criterion2(binary, ocr_answer_flags.float())
            b_losses[iter_idx] = b_loss.data.float()
            loss = criterion(pred, label)
            loss += b_loss * opt.BIN_LOSS_RATE
        else:
            loss = criterion(pred, label)
        loss.backward()
        optimizer.step()
        train_loss[iter_idx] = loss.data.float()
        lr_scheduler.step()
        if iter_idx % opt.PRINT_INTERVAL == 0 and iter_idx != 0:
            c_mean_loss = train_loss[iter_idx - opt.PRINT_INTERVAL+1:iter_idx+1].mean()
            mean_b_loss = b_losses[iter_idx - opt.PRINT_INTERVAL+1:iter_idx+1].mean()
            mean_voc_loss = voc_losses[iter_idx - opt.PRINT_INTERVAL+1:iter_idx+1].mean()
            mean_ocr_loss = ocr_losses[iter_idx - opt.PRINT_INTERVAL+1:iter_idx+1].mean()
            writer.add_scalar(opt.ID + '/train_loss', c_mean_loss, iter_idx)
            writer.add_scalar(opt.ID + '/lr', optimizer.param_groups[0]['lr'], iter_idx)
            if opt.BINARY:
                logger.info('Train Epoch: {}\t Iter: {}\t b_loss: {:.4f} voc_loss: {:.4f} ocr_loss: {:.4f}'.format(
                    epoch, iter_idx, mean_b_loss, mean_voc_loss, mean_ocr_loss
                ))
            else:
                logger.info('Train Epoch: {}\tIter: {}\tLoss: {:.4f}'.format(
                    epoch, iter_idx, c_mean_loss
                ))

        if iter_idx % opt.CHECKPOINT_INTERVAL == 0 and iter_idx != 0:
            save_path = os.path.join(config.CACHE_DIR, opt.ID + '_iter_' + str(iter_idx) + '.pth')
            torch.save({
                'model': model.state_dict(),
                'optimizer': optimizer.state_dict(),
                'lr_scheduler': lr_scheduler.state_dict()
            }, save_path)
        if iter_idx % opt.VAL_INTERVAL == 0 and iter_idx != 0:
            if opt.LATE_FUSION:
                test_loss, acc_overall, acc_per_ques, acc_per_ans = exec_validation([model, model_prob], opt, mode='val',
                                                                                    folder=folder, it=iter_idx,
                                                                                    logger=logger)
            else:
                test_loss, acc_overall, acc_per_ques, acc_per_ans = exec_validation(model, opt, mode='val', folder=folder, it=iter_idx, logger=logger)
            writer.add_scalar(opt.ID + '/val_loss', test_loss, iter_idx)
            writer.add_scalar(opt.ID + 'accuracy', acc_overall, iter_idx)
            logger.info('Test loss: {}'.format(test_loss))
            logger.info('Accuracy: {}'.format(acc_overall))
            logger.info('Test per ans: {}'.format(acc_per_ans))
            results.append([iter_idx, c_mean_loss, test_loss, acc_overall, acc_per_ques, acc_per_ans])
            best_result_idx = np.array([x[3] for x in results]).argmax()
            logger.info('Best accuracy of {} was at iteration {}'.format(
                results[best_result_idx][3],
                results[best_result_idx][0]
            ))
            drawgraph(results, folder, opt.MFB_FACTOR_NUM, opt.MFB_OUT_DIM, prefix=opt.ID)
        if iter_idx % opt.TESTDEV_INTERVAL == 0 and iter_idx != 0:
            exec_validation(model, opt, mode='test-dev', folder=folder, it=iter_idx, logger=logger)


def get_model(opt):
    """
    args priority:
    OCR > EMBED > not specified (baseline)
    """
    model = None
    if opt.MODEL == 'mfb':
        if opt.LATE_FUSION:
            model = mfb_coatt_embed_ocr_binonly(opt)
        elif opt.BIN_HELP:
            model = mfb+coatt_embed_ocr_binhelp(opt)
        elif opt.OCR:
            assert opt.EXP_TYPE in ['textvqa','textvqa_butd'], 'dataset not supported'
            if opt.BINARY:
                model = mfb_coatt_embed_ocr_bin(opt)
            else:
                model = mfb_coatt_embed_ocr(opt)
        elif opt.EMBED:
            model = mfb_coatt_glove(opt)
        else:
            model = mfb_baseline(opt)

    elif opt.MODEL == 'mfh':
        if opt.LATE_FUSION:
            model = mfh_coatt_embed_ocr_binonly(opt)
        elif opt.BIN_HELP:
            model = mfh_coatt_embed_ocr_binhelp(opt)
        elif opt.OCR:
            assert opt.EXP_TYPE in ['textvqa','textvqa_butd'], 'dataset not supported'
            if opt.BINARY:
                model = mfh_coatt_embed_ocr_bin(opt)
            else:
                model = mfh_coatt_embed_ocr(opt)
        elif opt.EMBED:
            model = mfh_coatt_glove(opt)
        else:
            model = mfh_baseline(opt)

    return model


def main():
    opt = config.parse_opt()
    # notice that unique id with timestamp is determined here

    # torch.cuda.set_device(opt.TRAIN_GPU_ID)
    # torch.cuda.manual_seed(opt.SEED)
    # print('Using gpu card: ' + torch.cuda.get_device_name(opt.TRAIN_GPU_ID))
    writer = SummaryWriter()

    folder = os.path.join(config.OUTPUT_DIR, opt.ID)
    log_file = os.path.join(config.LOG_DIR, opt.ID)

    logger = get_logger(log_file)

    train_Data = data_provider.VQADataset(opt, config.VOCABCACHE_DIR, logger)
    train_Loader = torch.utils.data.DataLoader(dataset=train_Data, shuffle=True, pin_memory=True, num_workers=2)

    opt.quest_vob_size, opt.ans_vob_size = train_Data.get_vocab_size()

    #model = get_model(opt)
    #optimizer = optim.Adam(model.parameters(), lr=opt.INIT_LERARNING_RATE)
    #lr_scheduler = optim.lr_scheduler.StepLR(optimizer, opt.DECAY_STEPS, opt.DECAY_RATE)
    try:
        if opt.RESUME_PATH:
            logger.info('==> Resuming from checkpoint..')
            checkpoint = torch.load(opt.RESUME_PATH)
            model = get_model(opt)
            model.load_state_dict(checkpoint['model'])
            model = cuda_wrapper(model)
            optimizer = optim.Adam(model.parameters(), lr=opt.INIT_LERARNING_RATE)
            optimizer.load_state_dict(checkpoint['optimizer'])
            lr_scheduler = optim.lr_scheduler.StepLR(optimizer, opt.DECAY_STEPS, opt.DECAY_RATE)
            lr_scheduler.load_state_dict(checkpoint['lr_scheduler'])
        else:
            model = get_model(opt)
            '''init model parameter'''
            for name, param in model.named_parameters():
                if 'bias' in name:  # bias can't init by xavier
                    init.constant_(param, 0.0)
                elif 'weight' in name:
                    init.kaiming_uniform_(param)
                    # init.xavier_uniform(param)  # for mfb_coatt_glove
            model = cuda_wrapper(model)
            optimizer = optim.Adam(model.parameters(), lr=opt.INIT_LERARNING_RATE)
            lr_scheduler = optim.lr_scheduler.StepLR(optimizer, opt.DECAY_STEPS, opt.DECAY_RATE)

        if opt.LATE_FUSION:
            logger.info('==> Load from checkpoint..')
            checkpoint = torch.load(opt.PROB_PATH)
            if opt.MODEL == 'mfb':
                model0 = mfb_coatt_embed_ocr(opt)
            else:
                model0 = mfh_coatt_embed_ocr(opt)
            model0.load_state_dict(checkpoint['model'])
            model0 = cuda_wrapper(model0)
            model = [model, model0]

        train(opt, model, train_Loader, optimizer, lr_scheduler, writer, folder, logger)

    except Exception as e:
        logger.exception(str(e))

    writer.close()


if __name__ == '__main__':
    main()
