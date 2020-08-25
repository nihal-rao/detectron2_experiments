# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
from typing import List
import torch
from torch import nn
from torch.nn import functional as F

from detectron2.layers import Conv2d, ConvTranspose2d, ShapeSpec, cat, interpolate
from detectron2.structures import Instances, heatmaps_to_keypoints
from detectron2.utils.events import get_event_storage
from detectron2.utils.registry import Registry

_TOTAL_SKIPPED = 0

ROI_KEYPOINT_HEAD_REGISTRY = Registry("ROI_KEYPOINT_HEAD")
ROI_KEYPOINT_HEAD_REGISTRY.__doc__ = """
Registry for keypoint heads, which make keypoint predictions from per-region features.
The registered object will be called with `obj(cfg, input_shape)`.
"""

def build_keypoint_head(cfg, input_shape):
    """
    Build a keypoint head from `cfg.MODEL.ROI_KEYPOINT_HEAD.NAME`.
    """
    name = cfg.MODEL.ROI_KEYPOINT_HEAD.NAME
    return ROI_KEYPOINT_HEAD_REGISTRY.get(name)(cfg, input_shape)


def keypoint_rcnn_loss(pred_keypoint_logits, instances, normalizer):
    """
    @Nihal
    Arguments:
        pred_keypoint_logits (Tensor): A tensor of shape (N, K, S, S) where N is the total number
            of instances in the batch, K is the number of keypoints, and S is the side length
            of the keypoint heatmap. The values are spatial logits.
        instances (list[Instances]): A list of M Instances, where M is the batch size.
            These instances are predictions from the model
            that are in 1:1 correspondence with pred_keypoint_logits.
            Each Instances should contain a `gt_keypoints` field containing a `structures.Keypoint`
            instance.
        normalizer (float): Normalize the loss by this amount.
            If not specified, we normalize by the number of visible keypoints in the minibatch.
    Returns a scalar tensor containing the loss.
    """
    heatmaps = []
    valid = []

    keypoint_side_len = pred_keypoint_logits.shape[2]
    print(keypoint_side_len)
    for instances_per_image in instances:
        if len(instances_per_image) == 0:
            continue
        keypoints = instances_per_image.gt_keypoints
        print(keypoints.tensor)
        print(instances_per_image.proposal_boxes.tensor)
        heatmaps_per_image, valid_per_image = keypoints.to_heatmap(
            instances_per_image.proposal_boxes.tensor, keypoint_side_len
        )
        heatmaps.append(heatmaps_per_image.view(-1))
        #print("number of valid keypoints is {}".format(torch.sum(valid_per_image)))
        valid.append(valid_per_image.view(-1))

    if len(heatmaps):
        keypoint_targets = cat(heatmaps, dim=0)
        valid = cat(valid, dim=0).to(dtype=torch.uint8)
        valid = torch.nonzero(valid).squeeze(1)

    # torch.mean (in binary_cross_entropy_with_logits) doesn't
    # accept empty tensors, so handle it separately
    if len(heatmaps) == 0 or valid.numel() == 0:
        global _TOTAL_SKIPPED
        _TOTAL_SKIPPED += 1
        storage = get_event_storage()
        storage.put_scalar("kpts_num_skipped_batches", _TOTAL_SKIPPED, smoothing_hint=False)
        return pred_keypoint_logits.sum() * 0
    """
    @Nihal
    softargmax_kp is a tensor of shape(N), where N is no. of instances. Each entry in softargmax_kp is the predicted keypoint position in [0,H*W).
    This predicted keypoint position is found using the softargmax function.
    keypoint_targets contains the ground truth keypoint position, belonging to [0,H*W)
    Thus we can now use l1 or l2 loss between the predicted and the target keypoints.
    """

    N, K, H, W = pred_keypoint_logits.shape
    #print("number of instances considered for KP loss {}".format(N))
    pred_keypoint_logits = pred_keypoint_logits.view(N * K, H * W)
    pred_keypoint_softmax = F.softmax(pred_keypoint_logits,dim=1)
    kp_idx = torch.arange(H*W,device='cuda')
    kp_idx = kp_idx.float()
    softargmax_kp = torch.sum(pred_keypoint_softmax*kp_idx,dim=1)
    #print(softargmax_kp.dtype)
    keypoint_targets = keypoint_targets.float()
    print(softargmax_kp[valid])
    print(keypoint_targets[valid])
    keypoint_loss = F.l1_loss(
        softargmax_kp[valid], keypoint_targets[valid], reduction="mean"
    )
    # If a normalizer isn't specified, normalize by the number of visible keypoints in the minibatch
    if normalizer is None:
        normalizer = valid.numel()
    #keypoint_loss /= normalizer
    storage = get_event_storage()
    storage.put_scalar("num_valid_kp_per_instance", normalizer/N)
    return keypoint_loss

def heatmaps_to_rescaled_keypoints(maps: torch.Tensor, rois: torch.Tensor) -> torch.Tensor:
    """
    Extract predicted keypoint locations from heatmaps.
    Args:
        maps (Tensor): (#ROIs, #keypoints, POOL_H, POOL_W). The predicted heatmap of logits for
            each ROI and each keypoint.
        rois (Tensor): (#ROIs, 4). The box of each ROI.
    Returns:
        Tensor of shape (#ROIs, #keypoints, 4) with the last dimension corresponding to
        (x, y, logit, score) for each keypoint.
    When converting discrete pixel indices in an NxN image to a continuous keypoint coordinate,
    we maintain consistency with :meth:`Keypoints.to_heatmap` by using the conversion from
    Heckbert 1990: c = d + 0.5, where d is a discrete coordinate and c is a continuous coordinate.
    """
    offset_x = rois[:, 0]
    offset_y = rois[:, 1]

    offset_x = offset_x.unsqueeze(-1)
    offset_y = offset_y.unsqueeze(-1)

    widths = (rois[:, 2] - rois[:, 0]).clamp(min=1)
    heights = (rois[:, 3] - rois[:, 1]).clamp(min=1)

    widths = widths.unsqueeze(-1)
    heights = heights.unsqueeze(-1)

    N,K,H,W = maps.shape
    print(maps.shape)
    if(H==W):
        print("H and W are same")
        print("widths are {}".format(widths))
        print("heights are {}".format(heights))
        print("offset x are {}".format(offset_x))
        print("offset y are {}".format(offset_y))
    preds = maps.new_zeros(N,K,3)

    pred_keypoint_logits = maps.view(N * K, H * W)
    pred_keypoint_softmax = F.softmax(pred_keypoint_logits,dim=1)
    kp_idx = torch.arange(H*W,device='cuda')
    kp_idx = kp_idx.float()
    softargmax_kp = torch.sum(pred_keypoint_softmax*kp_idx,dim=1)

    softargmax_kp = softargmax_kp.round().int()

    print(softargmax_kp)
    print(softargmax_kp.shape)

    x_int = softargmax_kp % W
    y_int = (softargmax_kp - x_int) // W

    x_int = x_int.view(N,K)
    y_int = y_int.view(N,K)

    x_int = (x_int.float() * widths/W) + offset_x
    y_int = (y_int.float() * heights/H) + offset_y


    preds[:,:,0] = x_int
    preds[:,:,1] = y_int
    preds[:,:,2] = torch.ones(N,K,device='cuda')
    print(preds[:,:,0:2])
    return preds

def keypoint_rcnn_inference(pred_keypoint_logits, pred_instances):
    """
    Post process each predicted keypoint heatmap in `pred_keypoint_logits` into (x, y, score)
        and add it to the `pred_instances` as a `pred_keypoints` field.
    Args:
        pred_keypoint_logits (Tensor): A tensor of shape (R, K, S, S) where R is the total number
           of instances in the batch, K is the number of keypoints, and S is the side length of
           the keypoint heatmap. The values are spatial logits.
        pred_instances (list[Instances]): A list of N Instances, where N is the number of images.
    Returns:
        None. Each element in pred_instances will contain an extra "pred_keypoints" field.
            The field is a tensor of shape (#instance, K, 3) where the last
            dimension corresponds to (x, y, score).
            The scores are larger than 0.
    """
    # flatten all bboxes from all images together (list[Boxes] -> Rx4 tensor)
    bboxes_flat = cat([b.pred_boxes.tensor for b in pred_instances], dim=0)

    keypoint_results = heatmaps_to_rescaled_keypoints(pred_keypoint_logits.detach(), bboxes_flat.detach())
    num_instances_per_image = [len(i) for i in pred_instances]
    keypoint_results = keypoint_results[:, :, [0, 1, 2]].split(num_instances_per_image, dim=0)

    for keypoint_results_per_image, instances_per_image in zip(keypoint_results, pred_instances):
        # keypoint_results_per_image is (num instances)x(num keypoints)x(x, y, score)
        instances_per_image.pred_keypoints = keypoint_results_per_image


class BaseKeypointRCNNHead(nn.Module):
    """
    Implement the basic Keypoint R-CNN losses and inference logic.
    """

    def __init__(self, cfg, input_shape):
        super().__init__()
        # fmt: off
        self.loss_weight                    = cfg.MODEL.ROI_KEYPOINT_HEAD.LOSS_WEIGHT
        self.normalize_by_visible_keypoints = cfg.MODEL.ROI_KEYPOINT_HEAD.NORMALIZE_LOSS_BY_VISIBLE_KEYPOINTS  # noqa
        self.num_keypoints                  = cfg.MODEL.ROI_KEYPOINT_HEAD.NUM_KEYPOINTS
        batch_size_per_image                = cfg.MODEL.ROI_HEADS.BATCH_SIZE_PER_IMAGE
        positive_sample_fraction            = cfg.MODEL.ROI_HEADS.POSITIVE_FRACTION
        # fmt: on
        self.normalizer_per_img = (
            self.num_keypoints * batch_size_per_image * positive_sample_fraction
        )

    def forward(self, x, instances: List[Instances]):
        """
        Args:
            x: input region feature(s) provided by :class:`ROIHeads`.
            instances (list[Instances]): contains the boxes & labels corresponding
                to the input features.
                Exact format is up to its caller to decide.
                Typically, this is the foreground instances in training, with
                "proposal_boxes" field and other gt annotations.
                In inference, it contains boxes that are already predicted.
        Returns:
            A dict of losses if in training. The predicted "instances" if in inference.
        """
        x = self.layers(x)
        if self.training:
            num_images = len(instances)
            normalizer = (
                None
                if self.normalize_by_visible_keypoints
                else num_images * self.normalizer_per_img
            )
            return {
                "loss_keypoint": keypoint_rcnn_loss(x, instances, normalizer=normalizer)
                * self.loss_weight
            }
        else:
            keypoint_rcnn_inference(x, instances)
            return instances

    def layers(self, x):
        """
        Neural network layers that makes predictions from regional input features.
        """
        raise NotImplementedError


@ROI_KEYPOINT_HEAD_REGISTRY.register()
class KRCNNConvDeconvUpsampleHead(BaseKeypointRCNNHead):
    """
    A standard keypoint head containing a series of 3x3 convs, followed by
    a transpose convolution and bilinear interpolation for upsampling.
    """

    def __init__(self, cfg, input_shape: ShapeSpec):
        """
        The following attributes are parsed from config:
            conv_dims: an iterable of output channel counts for each conv in the head
                         e.g. (512, 512, 512) for three convs outputting 512 channels.
            num_keypoints: number of keypoint heatmaps to predicts, determines the number of
                           channels in the final output.
        """
        super().__init__(cfg, input_shape)

        # fmt: off
        # default up_scale to 2 (this can eventually be moved to config)
        up_scale      = 2
        conv_dims     = cfg.MODEL.ROI_KEYPOINT_HEAD.CONV_DIMS
        num_keypoints = cfg.MODEL.ROI_KEYPOINT_HEAD.NUM_KEYPOINTS
        in_channels   = input_shape.channels
        # fmt: on

        self.blocks = []
        for idx, layer_channels in enumerate(conv_dims, 1):
            module = Conv2d(in_channels, layer_channels, 3, stride=1, padding=1)
            self.add_module("conv_fcn{}".format(idx), module)
            self.blocks.append(module)
            in_channels = layer_channels

        deconv_kernel = 4
        self.score_lowres = ConvTranspose2d(
            in_channels, num_keypoints, deconv_kernel, stride=2, padding=deconv_kernel // 2 - 1
        )
        self.up_scale = up_scale

        for name, param in self.named_parameters():
            if "bias" in name:
                nn.init.constant_(param, 0)
            elif "weight" in name:
                # Caffe2 implementation uses MSRAFill, which in fact
                # corresponds to kaiming_normal_ in PyTorch
                nn.init.kaiming_normal_(param, mode="fan_out", nonlinearity="relu")

    def layers(self, x):
        for layer in self.blocks:
            x = F.relu(layer(x))
        x = self.score_lowres(x)
        x = interpolate(x, scale_factor=self.up_scale, mode="bilinear", align_corners=False)
        return x