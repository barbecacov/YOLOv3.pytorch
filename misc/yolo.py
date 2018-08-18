import math
import numpy as np
import sys
import time
import torch
import torch.nn as nn
import torch.nn.functional as F
from backbones import backbone_fn
from collections import OrderedDict
from misc.nms.nms_wrapper import nms

import pdb

class YOLOv3(nn.Module):
    '''
    Detection Decoder followed yolo v3.
    '''
    def __init__(self, opt):
        super(YOLOv3, self).__init__()
        self.opt = opt

        self.backbone = backbone_fn(opt)
        _out_filters = self.backbone.layers_out_filters

        final_out_filter0 = 3 * (5 + opt.classes)

        self.embedding0 = self._make_embedding([512, 1024], _out_filters[-1], final_out_filter0)
        #  embedding1
        final_out_filter1 = 3 * (5 + opt.classes)
        self.embedding1_cbl = self._make_cbl(512, 256, 1)
        # self.embedding1_upsample = nn.Upsample(scale_factor=2, mode='nearest')
        self.embedding1 = self._make_embedding([256, 512], _out_filters[-2] + 256, final_out_filter1)
        #  embedding2
        final_out_filter2 = 3 * (5 + opt.classes)
        self.embedding2_cbl = self._make_cbl(256, 128, 1)
        # self.embedding2_upsample = nn.Upsample(scale_factor=2, mode='nearest')
        self.embedding2 = self._make_embedding([128, 256], _out_filters[-3] + 128, final_out_filter2)

        self.anchors = np.array(opt.anchors)
        self.num_layers = len(self.anchors) // 3
        self.num_classes = opt.classes


        # initlize the loss function here.
        self.loss = yolo_loss(opt)

    def _make_cbl(self, _in, _out, ks):
        ''' cbl = conv + batch_norm + leaky_relu
        '''
        pad = (ks - 1) // 2 if ks else 0
        return nn.Sequential(OrderedDict([
            ("conv", nn.Conv2d(_in, _out, kernel_size=ks, stride=1, padding=pad, bias=False)),
            ("bn", nn.BatchNorm2d(_out)),
            ("relu", nn.LeakyReLU(0.1)),
        ]))

    def _make_embedding(self, filters_list, in_filters, out_filter):
        m = nn.ModuleList([
            self._make_cbl(in_filters, filters_list[0], 1),
            self._make_cbl(filters_list[0], filters_list[1], 3),
            self._make_cbl(filters_list[1], filters_list[0], 1),
            self._make_cbl(filters_list[0], filters_list[1], 3),
            self._make_cbl(filters_list[1], filters_list[0], 1),
            self._make_cbl(filters_list[0], filters_list[1], 3)])
        m.add_module("conv_out", nn.Conv2d(filters_list[1], out_filter, kernel_size=1,
                                           stride=1, padding=0, bias=True))
        return m

    def _branch(self, _embedding, _in):
        for i, e in enumerate(_embedding):
            _in = e(_in)
            if i == 4:
                out_branch = _in
        return _in, out_branch

    def forward(self, img, label1, label2, label3):

        if self.opt.backbone_lr == 0:
            with torch.no_grad():
                x2, x1, x0 = self.backbone(img)
        else:
            x2, x1, x0 = self.backbone(img)

        out0, out0_branch = self._branch(self.embedding0, x0)
        #  yolo branch 1
        x1_in = self.embedding1_cbl(out0_branch)
        x1_in = F.interpolate(x1_in, scale_factor=2, mode='nearest')
        x1_in = torch.cat([x1_in, x1], 1)
        out1, out1_branch = self._branch(self.embedding1, x1_in)
        #  yolo branch 2
        x2_in = self.embedding2_cbl(out1_branch)
        # x2_in = self.embedding2_upsample(x2_in)
        x2_in = F.interpolate(x2_in, scale_factor=2, mode='nearest')
        x2_in = torch.cat([x2_in, x2], 1)
        out2, out2_branch = self._branch(self.embedding2, x2_in)

        loss = self.loss((out0, out1, out2), (label1, label2, label3))

        return loss


    def detect(self, img):

        with torch.no_grad():
            x2, x1, x0 = self.backbone(img)
            # forward the decoder block
            out0, out0_branch = self._branch(self.embedding0, x0)
            #  yolo branch 1
            x1_in = self.embedding1_cbl(out0_branch)
            x1_in = F.interpolate(x1_in, scale_factor=2, mode='nearest')
            x1_in = torch.cat([x1_in, x1], 1)
            out1, out1_branch = self._branch(self.embedding1, x1_in)
            #  yolo branch 2
            x2_in = self.embedding2_cbl(out1_branch)
            x2_in = F.interpolate(x2_in, scale_factor=2, mode='nearest')
            x2_in = torch.cat([x2_in, x2], 1)
            out2, out2_branch = self._branch(self.embedding2, x2_in)

        image_shape = torch.Tensor([img.size(2), img.size(3)]).type_as(img)
        boxes_, scores_, classes_  = yolo_eval((out0, out1, out2), self.anchors, self.num_classes, image_shape)

        return boxes_, scores_, classes_

def yolo_boxes_and_scores(feats, anchors, num_classes, input_shape, image_shape):
    '''Process Conv layer output'''

    box_xy, box_wh, box_confidence, box_class_probs = yolo_head(feats,
        anchors, num_classes, input_shape)

    boxes = yolo_correct_boxes(box_xy, box_wh, input_shape, image_shape)
    boxes = boxes.view([-1, 4])

    box_scores = box_confidence * box_class_probs
    box_scores = box_scores.view(-1, num_classes)
    return boxes.view(feats.size(0), -1,4), box_scores.view(feats.size(0), -1,num_classes)

def yolo_correct_boxes(box_xy, box_wh, input_shape, image_shape):
    '''Get corrected boxes'''

    box_yx = torch.stack((box_xy[...,1], box_xy[...,0]), dim=4)
    box_hw = torch.stack((box_wh[...,1], box_wh[...,0]), dim=4)

    new_shape = torch.round(image_shape * torch.min(input_shape/image_shape))
    offset = (input_shape-new_shape)/2./input_shape
    scale = input_shape/new_shape
    box_yx = (box_yx - offset) * scale
    box_hw *= scale

    box_mins = box_yx - (box_hw / 2.)
    box_maxes = box_yx + (box_hw / 2.)
    boxes = torch.stack([
        box_mins[..., 0],  # y_min
        box_mins[..., 1],  # x_min
        box_maxes[..., 0],  # y_max
        box_maxes[..., 1]  # x_max
    ], dim=4)

    # Scale boxes back to original image shape.
    boxes *= torch.cat([image_shape, image_shape]).view(1,1,1,1,4)
    return boxes

def yolo_eval(yolo_outputs,
              anchors,
              num_classes,
              image_shape,
              max_boxes=20,
              score_threshold=.5,
              iou_threshold=.5,
              nms_threshold=.3):
    """Evaluate YOLO model on given input and return filtered boxes."""
    num_layers = len(yolo_outputs)
    anchor_mask = [[6,7,8], [3,4,5], [0,1,2]] if num_layers==3 else [[3,4,5], [1,2,3]] # default setting
    input_shape = torch.Tensor([yolo_outputs[0].shape[2] * 32, yolo_outputs[0].shape[3] * 32]).type_as(yolo_outputs[0]) 
    boxes = []
    box_scores = []        
    for l in range(num_layers):
        _boxes, _box_scores = yolo_boxes_and_scores(yolo_outputs[l],
            anchors[anchor_mask[l]], num_classes, input_shape, image_shape)        
        boxes.append(_boxes)
        box_scores.append(_box_scores)

    boxes = torch.cat(boxes, dim=1)
    box_scores = torch.cat(box_scores, dim=1)

    dets_ = []
    classes_ = []
    images_ = []
    for i in range(boxes.size(0)):
        mask = box_scores[i] >= score_threshold
        for c in range(num_classes):
            class_boxes = boxes[i][mask[:,c]]
            if len(class_boxes) == 0:
                continue

            class_box_scores = box_scores[i][:,c][mask[:,c]]
            _, order = torch.sort(class_box_scores, 0, True)
            # do nms here.
            cls_dets =  torch.cat((class_boxes, class_box_scores.view(-1,1)), 1)
            cls_dets = cls_dets[order]

            keep = nms(cls_dets, nms_threshold)
            cls_dets = cls_dets[keep.view(-1).long()]

            dets_.append(cls_dets)
            classes_.append(torch.ones(class_box_scores.size()) * c)
            images_.append(torch.ones(class_box_scores.size()) * i)

    dets_ = torch.cat(dets_, dim=0)
    images_ = torch.cat(images_, dim=0)
    classes_ = torch.cat(classes_, dim=0)
    
    return dets_, images_, classes_

def box_iou(b1, b2):
    '''Return iou tensor

    Parameters
    ----------
    b1: tensor, shape=(i1,...,iN, 4), xywh
    b2: tensor, shape=(j, 4), xywh

    Returns
    -------
    iou: tensor, shape=(i1,...,iN, j)

    '''

    # Expand dim to apply broadcasting.
    b1 = b1.unsqueeze(3)

    b1_xy = b1[..., :2]
    b1_wh = b1[..., 2:4]
    b1_wh_half = b1_wh/2.
    b1_mins = b1_xy - b1_wh_half
    b1_maxes = b1_xy + b1_wh_half

    # if b2 is an empty tensor: then iou is empty
    if b2.shape[0] == 0:
        iou = torch.zeros(b1.shape[0:4]).type_as(b1)
    else:
        b2 = b2.view(1,1,1,b2.size(0), b2.size(1))
        # Expand dim to apply broadcasting.
        b2_xy = b2[..., :2]
        b2_wh = b2[..., 2:4]
        b2_wh_half = b2_wh/2.
        b2_mins = b2_xy - b2_wh_half
        b2_maxes = b2_xy + b2_wh_half

        intersect_mins = torch.max(b1_mins, b2_mins)
        intersect_maxes = torch.min(b1_maxes, b2_maxes)
        intersect_wh = torch.clamp(intersect_maxes - intersect_mins, min=0)

        intersect_area = intersect_wh[..., 0] * intersect_wh[..., 1]
        b1_area = b1_wh[..., 0] * b1_wh[..., 1]
        b2_area = b2_wh[..., 0] * b2_wh[..., 1]
        iou = intersect_area / (b1_area + b2_area - intersect_area)

    return iou

def yolo_head(feats, anchors, num_classes, input_shape, calc_loss=False):

    num_anchors = len(anchors)

    anchors_tensor = torch.from_numpy(anchors).view(1,1,1,num_anchors, 2).type_as(feats)

    grid_shape = (feats.shape[2:4])

    grid_y = torch.arange(0, grid_shape[0]).view(-1, 1, 1, 1).expand(grid_shape[0], grid_shape[0], 1, 1)
    grid_x = torch.arange(0, grid_shape[1]).view(1, -1, 1, 1).expand(grid_shape[1], grid_shape[1], 1, 1)

    grid = torch.cat([grid_x, grid_y], dim=3).unsqueeze(0).type_as(feats)

    feats = feats.view(-1, num_anchors, num_classes+5, grid_shape[0], \
                grid_shape[1]).permute(0, 3, 4, 1, 2).contiguous()

    # Adjust preditions to each spatial grid point and anchor size.
    box_xy = (torch.sigmoid(feats[...,:2]) + grid) / torch.tensor(grid_shape).view(1,1,1,1,2).type_as(feats)
    box_wh = torch.exp(feats[..., 2:4]) * anchors_tensor / input_shape.view(1,1,1,1,2)

    box_confidence = torch.sigmoid(feats[..., 4:5])
    box_class_probs = torch.sigmoid(feats[..., 5:])

    if calc_loss == True:
        return grid, feats, box_xy, box_wh
    return box_xy, box_wh, box_confidence, box_class_probs

class yolo_loss(nn.Module):
    def __init__(self, opt):
        super(yolo_loss, self).__init__()

        self.opt = opt
        self.anchors = np.array(opt.anchors)
        self.num_layers = len(self.anchors) // 3
        self.anchor_mask = [[6,7,8], [3,4,5], [0,1,2]] if self.num_layers==3 else [[3,4,5], [1,2,3]]
        self.num_classes = opt.classes
        self.ignore_thresh = 0.5

        self.mse_loss = nn.MSELoss(reduction='sum')

    def forward(self, yolo_outputs, y_true):
        
        input_shape = torch.Tensor([yolo_outputs[0].shape[2] * 32, yolo_outputs[0].shape[3] * 32]).type_as(yolo_outputs[0]) 
        grid_shapes = [torch.Tensor([output.shape[2], output.shape[3]]).type_as(yolo_outputs[0]) for output in yolo_outputs] 
        m = yolo_outputs[0].size(0)

        loss_xy = 0
        loss_wh = 0
        loss_conf = 0
        loss_clss = 0
        nRecall = 0
        nRecall75 = 0
        nProposal = 0
        for l in range(self.num_layers):

            object_mask = y_true[l][..., 4:5]
            true_class_probs = y_true[l][..., 5:]
            grid, raw_pred, pred_xy, pred_wh = yolo_head(yolo_outputs[l], 
                                self.anchors[self.anchor_mask[l]], self.num_classes, input_shape, calc_loss=True)            

            pred_box = torch.cat([pred_xy, pred_wh], dim=4)
            # Darknet raw box to calculate loss.
            raw_true_xy = y_true[l][..., :2]*grid_shapes[l].view(1,1,1,1,2) - grid
            raw_true_wh = torch.log(y_true[l][..., 2:4] / torch.Tensor(self.anchors[self.anchor_mask[l]]).type_as(pred_box).view(1,1,1,self.num_layers,2) * input_shape.view(1,1,1,1,2))
            raw_true_wh.masked_fill_(object_mask.expand_as(raw_true_wh)==0, 0)
            box_loss_scale = 2 - y_true[l][...,2:3]*y_true[l][...,3:4]

            # Find ignore mask, iterate over each of batch.
            # ignore_mask = tf.TensorArray(K.dtype(y_true[0]), size=1, dynamic_size=True)
            best_ious = []
            for b in range(m):
                true_box = y_true[l][b,...,0:4][object_mask[b,...,0]==1]
                iou = box_iou(pred_box[b], true_box)
                best_iou, _ = torch.max(iou, dim=3)
                best_ious.append(best_iou)
            
            best_ious = torch.stack(best_ious, dim=0).unsqueeze(4)
            ignore_mask = (best_ious < self.ignore_thresh).float()

            # binary_crossentropy is helpful to avoid exp overflow.
            xy_loss = self.mse_loss(object_mask*box_loss_scale*torch.sigmoid(raw_pred[...,0:2]), object_mask * raw_true_xy)/ m
            wh_loss = torch.sum(object_mask * box_loss_scale * 0.5 * (raw_true_wh-raw_pred[...,2:4])**2)/m

            confidence_loss = (self.mse_loss(torch.sigmoid(raw_pred[...,4:5])[object_mask == 1], object_mask[object_mask==1]) + \
                            self.mse_loss(torch.sigmoid(raw_pred[...,4:5])[((1-object_mask)*ignore_mask) == 1], object_mask[((1-object_mask)*ignore_mask) == 1]))/m

            class_loss = self.mse_loss(torch.sigmoid(raw_pred[...,5:])* object_mask, true_class_probs * object_mask)/m

            loss_xy += xy_loss
            loss_wh += wh_loss
            loss_conf += confidence_loss
            loss_clss += class_loss
            # loss += xy_loss + wh_loss + confidence_loss + class_loss
        
            nRecall += torch.sum(best_ious > 0.5)
            nRecall75 += torch.sum(best_ious > 0.75)
            nProposal += torch.sum(torch.sigmoid(raw_pred[...,4:5]) > 0.25)

        loss = loss_xy + loss_wh + loss_conf + loss_clss
        # print('loss %.3f, xy %.3f, wh %.3f, conf %.3f, class_loss: %.3f, nRecall: %d, nRecall75: %d, nProposal: %d' \
                # %(loss.item(), xy_loss.item(), wh_loss.item(), confidence_loss.item(), class_loss.item(), nRecall.item(), nRecall75.item(), nProposal.item()))

        return loss.unsqueeze(0), loss_xy.unsqueeze(0), loss_wh.unsqueeze(0), loss_conf.unsqueeze(0), \
                loss_clss.unsqueeze(0), nRecall.unsqueeze(0), nRecall75.unsqueeze(0), nProposal.unsqueeze(0)