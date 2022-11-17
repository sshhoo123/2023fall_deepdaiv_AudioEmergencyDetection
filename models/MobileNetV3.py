from functools import partial
from typing import Any, Callable, List, Optional, Sequence, Tuple
from torch import nn, Tensor
import torch
import torch.nn.functional as F
import os
from torchvision.ops.misc import ConvNormActivation

from models.utils import cnn_out_size
from models.block_types import InvertedResidualConfig, InvertedResidual
from models.attention_pooling import MultiHeadAttentionPooling


# Adapted version of MobileNetV3 pytorch implementation
# https://github.com/pytorch/vision/blob/main/torchvision/models/mobilenetv3.py

resources = "resources"

pretrained_models = {
    "mn04_as": os.path.join(resources, "mn04_as_mAP_432.pt"),
    "mn05_as": os.path.join(resources, "mn05_as_mAP_443.pt"),
    "mn10_as": os.path.join(resources, "mn10_as_mAP_471.pt"),
    "mn20_as": os.path.join(resources, "mn20_as_mAP_478.pt"),
    "mn30_as": os.path.join(resources, "mn30_as_mAP_482.pt"),
    "mn40_as": os.path.join(resources, "mn40_as_mAP_484.pt"),
    "mn40_as_ext": os.path.join(resources, "mn40_as_ext_mAP_487.pt"),
}


class MobileNetV3(nn.Module):
    def __init__(
        self,
        inverted_residual_setting: List[InvertedResidualConfig],
        last_channel: int,
        num_classes: int = 1000,
        block: Optional[Callable[..., nn.Module]] = None,
        norm_layer: Optional[Callable[..., nn.Module]] = None,
        dropout: float = 0.2,
        in_conv_kernel: int = 3,
        in_conv_stride: int = 2,
        in_channels: int = 1,
        **kwargs: Any,
    ) -> None:
        """
        MobileNet V3 main class

        Args:
            inverted_residual_setting (List[InvertedResidualConfig]): Network structure
            last_channel (int): The number of channels on the penultimate layer
            num_classes (int): Number of classes
            block (Optional[Callable[..., nn.Module]]): Module specifying inverted residual building block for models
            norm_layer (Optional[Callable[..., nn.Module]]): Module specifying the normalization layer to use
            dropout (float): The droupout probability
            in_conv_kernel (int): Size of kernel for first convolution
            in_conv_stride (int): Size of stride for first convolution
            in_channels (int): Number of input channels
        """
        super(MobileNetV3, self).__init__()

        if not inverted_residual_setting:
            raise ValueError("The inverted_residual_setting should not be empty")
        elif not (
            isinstance(inverted_residual_setting, Sequence)
            and all([isinstance(s, InvertedResidualConfig) for s in inverted_residual_setting])
        ):
            raise TypeError("The inverted_residual_setting should be List[InvertedResidualConfig]")

        if block is None:
            block = InvertedResidual

        depthwise_norm_layer = norm_layer = \
            norm_layer if norm_layer is not None else partial(nn.BatchNorm2d, eps=0.001, momentum=0.01)

        layers: List[nn.Module] = []

        kernel_sizes = [in_conv_kernel]
        strides = [in_conv_stride]

        # building first layer
        firstconv_output_channels = inverted_residual_setting[0].input_channels
        layers.append(
            ConvNormActivation(
                in_channels,
                firstconv_output_channels,
                kernel_size=in_conv_kernel,
                stride=in_conv_stride,
                norm_layer=norm_layer,
                activation_layer=nn.Hardswish,
            )
        )

        # get squeeze excitation config
        se_cnf = kwargs.get('se_conf', None)

        # building inverted residual blocks
        # - keep track of size of frequency and time dimensions for possible application of Squeeze-and-Excitation
        # on the frequency/time dimension
        # - applying Squeeze-and-Excitation on the time dimension is not recommended as this constrains the network to
        # a particular length of the audio clip, whereas Squeeze-and-Excitation on the frequency bands is fine,
        # as the number of frequency bands is usually not changing
        f_dim, t_dim = kwargs.get('input_dims', (128, 1000))
        # take into account first conv layer
        f_dim = cnn_out_size(f_dim, 1, 1, 3, 2)
        t_dim = cnn_out_size(t_dim, 1, 1, 3, 2)
        for cnf in inverted_residual_setting:
            f_dim = cnf.out_size(f_dim)
            t_dim = cnf.out_size(t_dim)
            cnf.f_dim, cnf.t_dim = f_dim, t_dim  # update dimensions in block config
            layers.append(block(cnf, se_cnf, norm_layer, depthwise_norm_layer))
            kernel_sizes.append(cnf.kernel)
            strides.append(cnf.stride)

        # building last several layers
        lastconv_input_channels = inverted_residual_setting[-1].out_channels
        lastconv_output_channels = 6 * lastconv_input_channels
        layers.append(
            ConvNormActivation(
                lastconv_input_channels,
                lastconv_output_channels,
                kernel_size=1,
                norm_layer=norm_layer,
                activation_layer=nn.Hardswish,
            )
        )

        self.features = nn.Sequential(*layers)
        self.head_type = kwargs.get("head_type", False)
        if self.head_type == "multihead_attention_pooling":
            self.classifier = MultiHeadAttentionPooling(lastconv_output_channels, num_classes,
                                                        num_heads=kwargs.get("multihead_attention_heads"))
        elif self.head_type == "fully_convolutional":
            self.classifier = nn.Sequential(
                nn.Conv2d(
                    lastconv_output_channels,
                    num_classes,
                    kernel_size=(1, 1),
                    stride=(1, 1),
                    padding=(0, 0),
                    bias=False),
                nn.BatchNorm2d(num_classes),
                nn.AdaptiveAvgPool2d((1, 1)),
            )
        elif self.head_type == "mlp":
            self.classifier = nn.Sequential(
                nn.AdaptiveAvgPool2d(1),
                nn.Flatten(start_dim=1),
                nn.Linear(lastconv_output_channels, last_channel),
                nn.Hardswish(inplace=True),
                nn.Dropout(p=dropout, inplace=True),
                nn.Linear(last_channel, num_classes),
            )
        else:
            raise NotImplementedError(f"Head '{self.head_type}' unknown. Must be one of: 'mlp', "
                                      f"'fully_convolutional', 'multihead_attention_pooling'")

        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(m.weight, mode="fan_out")
                if m.bias is not None:
                    nn.init.zeros_(m.bias)
            elif isinstance(m, (nn.BatchNorm2d, nn.GroupNorm, nn.LayerNorm)):
                nn.init.ones_(m.weight)
                nn.init.zeros_(m.bias)
            elif isinstance(m, nn.Linear):
                nn.init.normal_(m.weight, 0, 0.01)
                if m.bias is not None:
                    nn.init.zeros_(m.bias)

    def _forward_impl(self, x: Tensor) -> (Tensor, Tensor):
        x = self.features(x)
        features = F.adaptive_avg_pool2d(x, (1, 1)).squeeze()
        x = self.classifier(x).squeeze()
        return x, features

    def forward(self, x: Tensor) -> (Tensor, Tensor):
        return self._forward_impl(x)


def _mobilenet_v3_conf(
        width_mult: float = 1.0,
        reduced_tail: bool = False,
        dilated: bool = False,
        c4_stride: int = 2,
        **kwargs: Any
):
    reduce_divider = 2 if reduced_tail else 1
    dilation = 2 if dilated else 1

    bneck_conf = partial(InvertedResidualConfig, width_mult=width_mult)
    adjust_channels = partial(InvertedResidualConfig.adjust_channels, width_mult=width_mult)

    # InvertedResidualConfig:
    # input_channels, kernel, expanded_channels, out_channels, use_se, activation, stride, dilation, width_mult
    inverted_residual_setting = [
        bneck_conf(16, 3, 16, 16, False, "RE", 1, 1),
        bneck_conf(16, 3, 64, 24, False, "RE", 2, 1),  # C1
        bneck_conf(24, 3, 72, 24, False, "RE", 1, 1),
        bneck_conf(24, 5, 72, 40, True, "RE", 2, 1),  # C2
        bneck_conf(40, 5, 120, 40, True, "RE", 1, 1),
        bneck_conf(40, 5, 120, 40, True, "RE", 1, 1),
        bneck_conf(40, 3, 240, 80, False, "HS", 2, 1),  # C3
        bneck_conf(80, 3, 200, 80, False, "HS", 1, 1),
        bneck_conf(80, 3, 184, 80, False, "HS", 1, 1),
        bneck_conf(80, 3, 184, 80, False, "HS", 1, 1),
        bneck_conf(80, 3, 480, 112, True, "HS", 1, 1),
        bneck_conf(112, 3, 672, 112, True, "HS", 1, 1),
        bneck_conf(112, 5, 672, 160 // reduce_divider, True, "HS", c4_stride, dilation),  # C4
        bneck_conf(160 // reduce_divider, 5, 960 // reduce_divider, 160 // reduce_divider, True, "HS", 1, dilation),
        bneck_conf(160 // reduce_divider, 5, 960 // reduce_divider, 160 // reduce_divider, True, "HS", 1, dilation),
    ]
    last_channel = adjust_channels(1280 // reduce_divider)

    return inverted_residual_setting, last_channel


def _mobilenet_v3(
    inverted_residual_setting: List[InvertedResidualConfig],
    last_channel: int,
    pretrained_name: str,
    **kwargs: Any,
):
    model = MobileNetV3(inverted_residual_setting, last_channel, **kwargs)
    if pretrained_name:
        if pretrained_name in pretrained_models:
            loc = pretrained_models.get(pretrained_name)
            if not os.path.isfile(loc):
                raise FileNotFoundError(f"No model found at specified location: '{loc}'. Download models from the"
                                        f"Github Releases of "
                                        f"https://github.com/fschmid56/EfficientAT and place "
                                        f"them in the folder /resources.")
            state_dict = torch.load(loc)
            model.load_state_dict(state_dict)
        else:
            raise NotImplementedError(f"Model name '{pretrained_name}' unknown.")
    return model


def mobilenet_v3(pretrained_name: str = None, **kwargs: Any) \
        -> MobileNetV3:
    """
    Constructs a MobileNetV3 architecture from
    "Searching for MobileNetV3" <https://arxiv.org/abs/1905.02244>".
    """
    inverted_residual_setting, last_channel = _mobilenet_v3_conf(**kwargs)
    return _mobilenet_v3(inverted_residual_setting, last_channel, pretrained_name, **kwargs)


def get_model(num_classes: int = 527, pretrained_name: str = None, width_mult: float = 1.0,
              reduced_tail: bool = False, dilated: bool = False, c4_stride: int = 2, head_type: str = "mlp",
              multihead_attention_heads: int = 4, input_dim_f: int = 128,
              input_dim_t: int = 1000, se_dims: str = 'c', se_agg: str = "max", se_r: int = 4):
    """
        Arguments to modify the instantiation of a MobileNetv3

        Args:
            num_classes (int): Specifies number of classes to predict
            pretrained_name (str): Specifies name of pre-trained model to load
            width_mult (float): Scales width of network
            reduced_tail (bool): Scales down network tail
            dilated (bool): Applies dilated convolution to network tail
            c4_stride (int): Set to '2' in original implementation;
                might be changed to modify the size of receptive field
            head_type (str): decides which classification head to use
            multihead_attention_heads (int): number of heads in case 'multihead_attention_heads' is used
            input_dim_f (int): number of frequency bands
            input_dim_t (int): number of time frames
            se_dims (Tuple): choose dimension to apply squeeze-excitation on, if multiple dimensions are chosen, then
                squeeze-excitation is applied concurrently and se layer outputs are fused by se_agg operation
            se_agg (str): operation to fuse output of concurrent se layers
            se_r (int): squeeze excitation bottleneck size
            se_dims (str): contains letters corresponding to dimensions 'c' - channel, 'f' - frequency, 't' - time
        """

    dim_map = {'c': 1, 'f': 2, 't': 3}
    assert len(se_dims) <= 3 and all([s in dim_map.keys() for s in se_dims]) or se_dims == 'none'
    input_dims = (input_dim_f, input_dim_t)
    if se_dims == 'none':
        se_dims = None
    else:
        se_dims = [dim_map[s] for s in se_dims]
    se_conf = dict(se_dims=se_dims, se_agg=se_agg, se_r=se_r)
    m = mobilenet_v3(pretrained_name=pretrained_name, num_classes=num_classes,
                     width_mult=width_mult, reduced_tail=reduced_tail, dilated=dilated, c4_stride=c4_stride,
                     head_type=head_type, multihead_attention_heads=multihead_attention_heads,
                     input_dims=input_dims, se_conf=se_conf
                     )
    print(m)
    return m