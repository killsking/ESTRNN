import os
import pickle
import time
from os.path import join, dirname

import cv2
import lmdb
import numpy as np
import torch
import torch.nn as nn

from data.utils import normalize, normalize_reverse
from model import Model
from .metrics import psnr_calculate, ssim_calculate
from .utils import AverageMeter, img2video


def test(para, logger):
    """
    test code
    """
    # load model with checkpoint
    if not para.test_only:
        para.test_checkpoint = join(logger.save_dir, 'model_best.pth.tar')
    if para.test_save_dir is None:
        para.test_save_dir = logger.save_dir
    model = Model(para).cuda()
    checkpoint_path = para.test_checkpoint
    checkpoint = torch.load(checkpoint_path, map_location=lambda storage, loc: storage.cuda())
    model = nn.DataParallel(model)
    model.load_state_dict(checkpoint['state_dict'])

    ds_name = para.dataset
    logger('{} results generating ...'.format(ds_name), prefix='\n')
    if ds_name == 'BSD':
        ds_type = 'test'
        _test_torch(para, logger, model, ds_type)
    elif ds_name == 'gopro_ds_lmdb' or ds_name == 'reds_lmdb':
        ds_type = 'valid'
        _test_lmdb(para, logger, model, ds_type)
    else:
        raise NotImplementedError


def _test_torch(para, logger, model, ds_type):
    PSNR = AverageMeter()
    SSIM = AverageMeter()
    timer = AverageMeter()
    results_register = set()
    H, W = 480, 640
    dataset_path = join(para.data_root, para.dataset, '{}_{}'.format(para.dataset, para.ds_config), ds_type)
    seqs = sorted(os.listdir(dataset_path))
    seq_length = 100
    for seq in seqs:
        logger('seq {} image results generating ...'.format(seq))
        torch.cuda.empty_cache()
        dir_name = '_'.join((para.dataset, para.model, 'test'))
        save_dir = join(para.test_save_dir, dir_name, seq)
        os.makedirs(save_dir, exist_ok=True)
        suffix = 'png' if para.data_format == 'RGB' else 'tiff'
        start = 0
        end = para.test_frames
        while True:
            input_seq = []
            label_seq = []
            for frame_idx in range(start, end):
                blur_img_path = join(dataset_path, seq, 'Blur', para.data_format, '{:08d}.{}'.format(frame_idx, suffix))
                sharp_img_path = join(dataset_path, seq, 'Sharp', 'RGB', '{:08d}.{}'.format(frame_idx, 'png'))
                blur_img = normalize(cv2.imread(blur_img_path).transpose(2, 0, 1)[np.newaxis, ...],
                                     centralize=True, normalize=True)
                gt_img = normalize(cv2.imread(sharp_img_path).transpose(2, 0, 1)[np.newaxis, ...],
                                   centralize=True, normalize=True)
                input_seq.append(blur_img)
                label_seq.append(gt_img)

            input_seq = np.concatenate(input_seq)[np.newaxis, :]
            label_seq = np.concatenate(label_seq)[np.newaxis, :]
            model.eval()
            with torch.no_grad():
                input_seq = torch.from_numpy(input_seq).float().cuda()
                label_seq = torch.from_numpy(label_seq).float().cuda()
                time_start = time.time()
                output_seq = model([input_seq, label_seq]).squeeze(dim=0)
                timer.update(time.time() - time_start, n=len(output_seq))

            for frame_idx in range(para.past_frames, end - start - para.future_frames):
                blur_img = input_seq.squeeze()[frame_idx].squeeze()
                blur_img = blur_img.detach().cpu().numpy().transpose((1, 2, 0))
                blur_img = normalize_reverse(blur_img, centralize=True, normalize=True).astype(np.uint8)
                blur_img_path = join(save_dir, '{:08d}_input.png'.format(frame_idx + start))
                gt_img = label_seq.squeeze()[frame_idx].squeeze()
                gt_img = gt_img.detach().cpu().numpy().transpose((1, 2, 0))
                gt_img = normalize_reverse(gt_img, centralize=True, normalize=True).astype(np.uint8)
                gt_img_path = join(save_dir, '{:08d}_gt.png'.format(frame_idx + start))
                deblur_img = output_seq[frame_idx - para.past_frames]
                deblur_img = deblur_img.detach().cpu().numpy().clip(0, 255).transpose((1, 2, 0))
                deblur_img = normalize_reverse(deblur_img, centralize=True, normalize=True).astype(np.uint8)
                deblur_img_path = join(save_dir, '{:08d}_{}.png'.format(frame_idx + start, para.model.lower()))
                cv2.imwrite(blur_img_path, blur_img)
                cv2.imwrite(gt_img_path, gt_img)
                cv2.imwrite(deblur_img_path, deblur_img)
                if deblur_img_path not in results_register:
                    results_register.add(deblur_img_path)
                    PSNR.update(psnr_calculate(deblur_img, gt_img))
                    SSIM.update(ssim_calculate(deblur_img, gt_img))

            if end == seq_length:
                break
            else:
                start = end - para.future_frames - para.past_frames
                end = start + para.test_frames
                if end > seq_length:
                    end = seq_length
                    start = end - para.test_frames

        if para.video:
            logger('seq {} video result generating ...'.format(seq))
            marks = ['Input', para.model, 'GT']
            path = dirname(save_dir)
            frame_start = para.past_frames
            frame_end = seq_length - para.future_frames
            img2video(path=path, size=(3 * W, 1 * H), seq=seq, frame_start=frame_start, frame_end=frame_end,
                      marks=marks, fps=10)

    logger('Test images : {}'.format(PSNR.count), prefix='\n')
    logger('Test PSNR : {}'.format(PSNR.avg))
    logger('Test SSIM : {}'.format(SSIM.avg))
    logger('Average time per image: {}'.format(timer.avg))


def _test_lmdb(para, logger, model, ds_type):
    PSNR = AverageMeter()
    SSIM = AverageMeter()
    timer = AverageMeter()
    results_register = set()
    if para.dataset == 'gopro_ds_lmdb':
        B, H, W, C = 1, 540, 960, 3
    elif para.dataset == 'reds_ds_lmdb':
        B, H, W, C = 1, 720, 1280, 3
    data_test_path = join(para.data_root, para.dataset, para.dataset[:-4] + ds_type)
    data_test_gt_path = join(para.data_root, para.dataset, para.dataset[:-4] + ds_type + '_gt')
    env_blur = lmdb.open(data_test_path, map_size=1099511627776)
    env_gt = lmdb.open(data_test_gt_path, map_size=1099511627776)
    txn_blur = env_blur.begin()
    txn_gt = env_gt.begin()
    data_test_info_path = join(para.data_root, para.dataset, para.dataset[:-4] + 'info_{}.pkl'.format(ds_type))
    with open(data_test_info_path, 'rb') as f:
        seqs_info = pickle.load(f)
    for seq_idx in range(seqs_info['num']):
        seq_length = seqs_info[seq_idx]['length']
        seq = '{:03d}'.format(seq_idx)
        logger('seq {} image results generating ...'.format(seq))
        torch.cuda.empty_cache()
        dir_name = '_'.join((para.dataset, para.model, 'test'))
        save_dir = join(para.test_save_dir, dir_name, seq)
        os.makedirs(save_dir, exist_ok=True)
        start = 0
        end = para.test_frames
        while (True):
            input_seq = []
            label_seq = []
            for frame_idx in range(start, end):
                code = '%03d_%08d' % (seq_idx, frame_idx)
                code = code.encode()
                img_blur = txn_blur.get(code)
                img_blur = np.frombuffer(img_blur, dtype='uint8')
                img_blur = normalize(img_blur.reshape(H, W, C), centralize=True, normalize=True)
                img_gt = txn_gt.get(code)
                img_gt = np.frombuffer(img_gt, dtype='uint8')
                img_gt = normalize(img_gt.reshape(H, W, C), centralize=True, normalize=True)
                input_seq.append(img_blur.transpose((2, 0, 1))[np.newaxis, :])
                label_seq.append(img_gt.transpose((2, 0, 1))[np.newaxis, :])
            input_seq = np.concatenate(input_seq)[np.newaxis, :]
            label_seq = np.concatenate(label_seq)[np.newaxis, :]
            with torch.no_grad():
                input_seq = torch.from_numpy(input_seq).float().cuda()
                label_seq = torch.from_numpy(label_seq).float().cuda()
                time_start = time.time()
                output_seq = model([input_seq, label_seq]).squeeze(dim=0)
                timer.update(time.time() - time_start, n=len(output_seq))
            for frame_idx in range(para.past_frames, end - start - para.future_frames):
                blur_img = input_seq.squeeze()[frame_idx].squeeze()
                blur_img = blur_img.detach().cpu().numpy().transpose((1, 2, 0))
                blur_img = normalize_reverse(blur_img, centralize=True, normalize=True).astype(np.uint8)
                blur_img_path = join(save_dir, '{:08d}_input.png'.format(frame_idx + start))
                gt_img = label_seq.squeeze()[frame_idx].squeeze()
                gt_img = gt_img.detach().cpu().numpy().transpose((1, 2, 0))
                gt_img = normalize_reverse(gt_img, centralize=True, normalize=True).astype(np.uint8)
                gt_img_path = join(save_dir, '{:08d}_gt.png'.format(frame_idx + start))
                deblur_img = output_seq[frame_idx - para.past_frames]
                deblur_img = deblur_img.detach().cpu().numpy().clip(0, 255).transpose((1, 2, 0))
                deblur_img = normalize_reverse(deblur_img, centralize=True, normalize=True).astype(np.uint8)
                deblur_img_path = join(save_dir, '{:08d}_{}.png'.format(frame_idx + start, para.model.lower()))
                cv2.imwrite(blur_img_path, blur_img)
                cv2.imwrite(gt_img_path, gt_img)
                cv2.imwrite(deblur_img_path, deblur_img)
                if deblur_img_path not in results_register:
                    results_register.add(deblur_img_path)
                    PSNR.update(psnr_calculate(deblur_img, gt_img))
                    SSIM.update(ssim_calculate(deblur_img, gt_img))
            if end == seq_length:
                break
            else:
                start = end - para.future_frames - para.past_frames
                end = start + para.test_frames
                if end > seq_length:
                    end = seq_length
                    start = end - para.test_frames

        if para.video:
            logger('seq {} video result generating ...'.format(seq))
            marks = ['Input', para.model, 'GT']
            path = dirname(save_dir)
            frame_start = para.past_frames
            frame_end = seq_length - para.future_frames
            img2video(path=path, size=(3 * W, 1 * H), seq=seq, frame_start=frame_start, frame_end=frame_end,
                      marks=marks, fps=10)

    logger('Test images : {}'.format(PSNR.count), prefix='\n')
    logger('Test PSNR : {}'.format(PSNR.avg))
    logger('Test SSIM : {}'.format(SSIM.avg))
    logger('Average time per image: {}'.format(timer.avg))