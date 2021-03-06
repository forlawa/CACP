
import torch
import logging
msglogger = logging.getLogger()


class SplicingPruner(object):
    """A pruner that both prunes and splices connections.

    The idea of pruning and splicing working in tandem was first proposed in the following
    NIPS paper from Intel Labs China in 2016:
        Dynamic Network Surgery for Efficient DNNs, Yiwen Guo, Anbang Yao, Yurong Chen.
        NIPS 2016, https://arxiv.org/abs/1608.04493.

    A SplicingPruner works best with a Dynamic Network Surgery schedule.
    The original Caffe code from the authors of the paper is available here:
    https://github.com/yiwenguo/Dynamic-Network-Surgery/blob/master/src/caffe/layers/compress_conv_layer.cpp
    """

    def __init__(self, name, sensitivities, low_thresh_mult, hi_thresh_mult, sensitivity_multiplier=0):
        """Arguments:
        """
        self.name = name
        self.sensitivities = sensitivities
        self.low_thresh_mult = low_thresh_mult
        self.hi_thresh_mult = hi_thresh_mult
        self.sensitivity_multiplier = sensitivity_multiplier

    def set_param_mask(self, param, param_name, zeros_mask_dict, meta):
        if param_name not in self.sensitivities:
            if '*' not in self.sensitivities:
                return
            else:
                sensitivity = self.sensitivities['*']
        else:
            sensitivity = self.sensitivities[param_name]

        if self.sensitivity_multiplier > 0:
            # Linearly growing sensitivity - for now this is hard-coded
            starting_epoch = meta['starting_epoch']
            current_epoch = meta['current_epoch']
            sensitivity *= (current_epoch - starting_epoch) * self.sensitivity_multiplier + 1

        if zeros_mask_dict[param_name].mask is None:
            zeros_mask_dict[param_name].mask = torch.ones_like(param)
        zeros_mask_dict[param_name].mask = self.create_mask(param,
                                                            zeros_mask_dict[param_name].mask,
                                                            sensitivity,
                                                            self.low_thresh_mult,
                                                            self.hi_thresh_mult)

    @staticmethod
    def create_mask(param, current_mask, sensitivity, low_thresh_mult, hi_thresh_mult):
        with torch.no_grad():
            if not hasattr(param, '_std'):
                # Compute the mean and standard-deviation once, and cache them.
                param._std = torch.std(param.abs()).item()
                param._mean = torch.mean(param.abs()).item()

            threshold_low = (param._mean + param._std * sensitivity) * low_thresh_mult
            threshold_hi = (param._mean + param._std * sensitivity) * hi_thresh_mult

            # This code performs the code in equation (3) of the "Dynamic Network Surgery" paper:
            #
            #           0    if a  > |W|
            # h(W) =    mask if a <= |W| < b
            #           1    if b <= |W|
            #
            # h(W) is the so-called "network surgery function".
            # mask is the mask used in the previous iteration.
            # a and b are the low and high thresholds, respectively.
            # We followed the example implementation from Yiwen Guo in Caffe, and used the
            # weight tensor's starting mean and std.
            # This is very similar to the initialization performed by cacp.SensitivityPruner.

            zeros, ones = torch.zeros_like(current_mask), torch.ones_like(current_mask)
            weights_abs = param.abs()
            new_mask = torch.where(threshold_low > weights_abs, zeros, current_mask)
            new_mask = torch.where(threshold_hi <= weights_abs, ones, new_mask)
            return new_mask
