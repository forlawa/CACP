#
# Copyright (c) 2019 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import torch
import torch.nn as nn
from torchvision.ops.misc import FrozenBatchNorm2d
from collections import OrderedDict
import utils
import modules
from quantization.sim_bn_fold import SimulatedFoldedBatchNorm
import logging
msglogger = logging.getLogger()


__all__ = ["fuse_modules", "fold_batch_norms"]


def fuse_modules(model, types_sequence, fuse_fn, dummy_input=None, adjacency_map=None):
    """
    Scans the module for sequences of modules of the specified types and "fuses" them. As an example, consider the
    following sequence of 3 modules: 'm_1' --> 'm_2' --> 'm_3'. Assuming they match the specified sequence of types,
    they will be fused such that the "fused" module replaces 'm_1', and 'm_2' and 'm_3' are replaced with identity
    operations.

    For a sequence of modules to be fused, it must not contain splits. That is - no module in the sequence can
    have more than a single output. For example, consider the following sequence:

    m_1 --> m_2 --> m_3
        |
        |
        --> m_4

    Even if m_1, m_2 and m_3 match the types sequence, they can't be fused because m_1's output also goes to m_4.

    The fused module is generated by the user specified function 'fuse_fn'.

    To infer the order of modules it is required to perform a forward pass on the model. Hence the need to pass the
    expected input shape.

    Args:
        model (nn.Module): Model instance on which the transformation is performed
        types_sequence (list or tuple): Sequence of module types. Each item in the sequence may itself be a
          list / tuple. For example - to fuse all possible convolution types with ReLU, pass:
          [[nn.Conv1d, nn.Conv2d, nn.Conv3d], nn.ReLU]
        fuse_fn (function): Function that takes a list of models to be fused, and returns a single fused module.
          If the sequence cannot be fused, this function should return None
        dummy_input (torch.Tensor or tuple): Dummy input to the model. Required if summary_graph is None
        adjacency_map (OrderedDict): Pre-computed adjacency map, via SummaryGraph.adjacency_map(). Must be based
          on the passed model, otherwise results are unexpected. If None, then the adjacency map will be created
          internally using the passed dummy_input.
    """
    utils.assign_layer_fq_names(model)
    if adjacency_map is None:
        if dummy_input is None:
            raise ValueError('Must pass either valid adjacency map instance or valid dummy input')
        summary_graph = utils.SummaryGraph(model, dummy_input)
        adjacency_map = summary_graph.adjacency_map(dedicated_modules_only=False)
    named_modules = OrderedDict(model.named_modules())
    in_sequence_idx = 0
    curr_sequence = []

    for node_name, adj_entry in adjacency_map.items():
        module = named_modules.get(node_name, None)
        if module is None:
            reset = True
        else:
            reset = False
            if isinstance(module, types_sequence[in_sequence_idx]):
                curr_sequence.append(module)
                in_sequence_idx += 1
                if in_sequence_idx == len(types_sequence):
                    _fuse_sequence(curr_sequence, named_modules, fuse_fn)
                    reset = True
                elif len(adj_entry.successors) > 1:
                    msglogger.debug(node_name + " is connected to multiple outputs, not fusible")
                    reset = True
            elif isinstance(module, types_sequence[0]):
                # Current module breaks the current sequence, check if it's the start of a new sequence
                in_sequence_idx = 1
                curr_sequence = [module]
            else:
                reset = True
        if reset:
            in_sequence_idx = 0
            curr_sequence = []
    return model


def fold_batch_norms(model, dummy_input=None, adjacency_map=None, inference=True):
    """Scans the model for convolution / linear modules followed by batch-normalization. For each such valid pair,
    folds the parameters of the batch normalization module into the parameters of the parameter module, and replaces
    the batch normalization module with an identity operation.

    To infer the order of modules it is required to perform a forward pass on the model. Hence the need to pass the
    expected input shape.

    Args:
        model (nn.Module): Model instance on which the transformation is performed
        dummy_input (torch.Tensor or tuple): Dummy input to the model. Required if summary_graph is None
        adjacency_map (OrderedDict): Pre-computed adjacency map, via SummaryGraph.adjacency_map(). Must be based
          on the passed model, otherwise results are unexpected. If None, then the adjacency map will be created
          internally using the passed dummy_input.
        inference (bool): an indicator on whether or not the modules are in inference mode.
            This will hard-fuse all BatchNorms into the param-layers.
    """
    def fold_bn(sequence):
        # Re-use this functionality from simulated BN folding implementation
        param_module, bn_module = sequence[0], sequence[1]
        try:
            folded_module = SimulatedFoldedBatchNorm(param_module, bn_module)
        except ValueError:
            msglogger.debug("Can't fold, {} does not track running stats".format(bn_module.cacp_name))
            return None
        if inference:
            folded_module.freeze()
            return folded_module.param_module
        return folded_module

    foldables = (nn.Linear, nn.Conv1d, nn.Conv2d, nn.Conv3d)
    batchnorms = (nn.BatchNorm1d, nn.BatchNorm2d, nn.BatchNorm3d, FrozenBatchNorm2d)
    if any([isinstance(m, batchnorms) for m in model.modules()]):
        return fuse_modules(model, (foldables, batchnorms), fold_bn, dummy_input, adjacency_map)
    return model


def _fuse_sequence(sequence, named_modules, fuse_fn):
    names = [m.cacp_name for m in sequence]
    msglogger.debug('Fusing sequence {}'.format(names))

    # Call fusing function
    fused_module = fuse_fn(sequence)
    if fused_module is None:
        msglogger.error('Sequence {} was not fused'.format(names))
        return

    # Leave a 'mark' in the fused module, indicating which modules were fused. This can come in handy
    # post-fusion, since the identity nodes don't show up in SummaryGraph (they're optimized away).
    setattr(sequence[0], 'fused_modules', names[1:])

    # Replace the first module in the sequence with the fused module
    def split_name(name):
        if '.' in name:
            return name.rsplit('.', 1)
        else:
            return '', name
    container_name, root_module = split_name(names[0])
    container = named_modules[container_name]
    setattr(container, root_module, fused_module)

    # Replace the rest of the modules in the sequence with identity ops
    for container_name, sub_module_name in map(lambda name: split_name(name), names[1:]):
        container = named_modules[container_name]
        setattr(container, sub_module_name, nn.Identity())
