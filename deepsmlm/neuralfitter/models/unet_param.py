import torch
import torch.nn as nn
import torch.nn.functional as F

# by constantin pape
# from https://github.com/constantinpape/mu-net

def get_activation(activation):
    """ Get activation from str or nn.Module
    """
    if activation is None:
        return None
    elif isinstance(activation, str):
        activation = getattr(nn, activation)()
    else:
        activation = activation()
        assert isinstance(activation, nn.Module)
    return activation


class Upsample(nn.Module):
    """ Upsample the input and change the number of channels
    via 1x1 Convolution if a different number of input/output channels is specified.
    """

    def __init__(self, scale_factor, mode='nearest',
                 in_channels=None, out_channels=None, align_corners=False,
                 ndim=3):
        super().__init__()
        self.mode = mode
        self.scale_factor = scale_factor
        self.align_corners = align_corners
        if in_channels != out_channels:
            if ndim == 2:
                self.conv = nn.Conv2d(in_channels, out_channels, 1)
            elif ndim == 3:
                self.conv = nn.Conv3d(in_channels, out_channels, 1)
            else:
                raise ValueError("Only 2d and 3d supported")
        else:
            self.conv = None

    def forward(self, input):
        x = F.interpolate(input, scale_factor=self.scale_factor,
                          mode=self.mode, align_corners=self.align_corners)
        if self.conv is not None:
            return self.conv(x)
        else:
            return x


# TODO implement side outputs
class UNetBase(nn.Module):
    """ UNet Base class implementation

    Deriving classes must implement
    - _conv_block(in_channels, out_channels, level, part)
        return conv block for a U-Net level
    - _pooler(level)
        return pooling operation used for downsampling in-between encoders
    - _upsampler(in_channels, out_channels, level)
        return upsampling operation used for upsampling in-between decoders
    - _out_conv(in_channels, out_channels)
        return output conv layer

    Arguments:
      in_channels: number of input channels
      out_channels: number of output channels
      depth: depth of the network
      initial_features: number of features after first convolution
      gain: growth factor of features
      pad_convs: whether to use padded convolutions
      norm: whether to use batch-norm, group-norm or None
      p_dropout: dropout probability
      final_activation: activation applied to the network output
    """
    norms = ('BatchNorm', 'GroupNorm')

    def __init__(self, in_channels, out_channels, depth=4,
                 initial_features=64, gain=2, pad_convs=False,
                 norm=None, p_dropout=0.0,
                 final_activation=None,
                 activation=nn.ReLU()):
        super().__init__()

        self.depth = depth
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.pad_convs = pad_convs
        if norm is not None:
            assert norm in self.norms
        self.norm = norm
        assert isinstance(p_dropout, (float, dict))
        self.p_dropout = p_dropout

        # modules of the encoder path
        n_features = [in_channels] + [initial_features * gain ** level
                                      for level in range(self.depth)]
        self.encoder = nn.ModuleList([self._conv_block(n_features[level], n_features[level + 1],
                                                       level, part='encoder', activation=activation)
                                      for level in range(self.depth)])

        # the base convolution block
        self.base = self._conv_block(n_features[-1], gain * n_features[-1], part='base', level=0)

        # modules of the decoder path
        n_features = [initial_features * gain ** level
                      for level in range(self.depth + 1)]
        n_features = n_features[::-1]
        self.decoder = nn.ModuleList([self._conv_block(n_features[level], n_features[level + 1],
                                                       self.depth - level - 1, part='decoder', activation=activation)
                                      for level in range(self.depth)])

        # the pooling layers;
        self.poolers = nn.ModuleList([self._pooler(level) for level in range(self.depth)])
        # the upsampling layers
        self.upsamplers = nn.ModuleList([self._upsampler(n_features[level],
                                                         n_features[level + 1],
                                                         self.depth - level - 1)
                                         for level in range(self.depth)])
        # output conv and activation
        # the output conv is not followed by a non-linearity, because we apply
        # activation afterwards
        self.out_conv = self._out_conv(n_features[-1], out_channels)
        self.activation = get_activation(final_activation)

    @staticmethod
    def _crop_tensor(input_, shape_to_crop):
        input_shape = input_.shape
        # get the difference between the shapes
        shape_diff = tuple((ish - csh) // 2
                           for ish, csh in zip(input_shape, shape_to_crop))
        if all(sd == 0 for sd in shape_diff):
            return input_
        # calculate the crop
        crop = tuple(slice(sd, sh - sd)
                     for sd, sh in zip(shape_diff, input_shape))
        return input_[crop]

    # crop the `from_encoder` tensor and concatenate both
    def _crop_and_concat(self, from_decoder, from_encoder):
        cropped = self._crop_tensor(from_encoder, from_decoder.shape)
        return torch.cat((cropped, from_decoder), dim=1)

    def forward(self, input):
        x = input
        # apply encoder path
        encoder_out = []
        for level in range(self.depth):
            x = self.encoder[level](x)
            encoder_out.append(x)
            x = self.poolers[level](x)

        # apply base
        x = self.base(x)

        # apply decoder path
        encoder_out = encoder_out[::-1]
        for level in range(self.depth):
            x = self.upsamplers[level](x)
            x = self.decoder[level](self._crop_and_concat(x,
                                                          encoder_out[level]))

        # apply output conv and activation (if given)
        x = self.out_conv(x)
        if self.activation is not None:
            x = self.activation(x)
        return x


class UNet2d(UNetBase):
    """ 2d U-Net for segmentation as described in
    https://arxiv.org/abs/1505.04597
    """
    # Convolutional block for single layer of the decoder / encoder
    # we apply to 2d convolutions with relu activation
    def _conv_block(self, in_channels, out_channels, level, part, activation=nn.ReLU()):
        padding = 1 if self.pad_convs else 0
        return nn.Sequential(nn.Conv2d(in_channels, out_channels,
                                       kernel_size=3, padding=padding),
                             activation,
                             nn.Conv2d(out_channels, out_channels,
                                       kernel_size=3, padding=padding),
                             activation)

    # upsampling via transposed 2d convolutions
    def _upsampler(self, in_channels, out_channels, level):
        # use bilinear upsampling + 1x1 convolutions
        return Upsample(in_channels=in_channels,
                        out_channels=out_channels,
                        scale_factor=2, mode='bilinear', ndim=2)

    # pooling via maxpool2d
    def _pooler(self, level):
        return nn.MaxPool2d(2)

    def _out_conv(self, in_channels, out_channels):
        return nn.Conv2d(in_channels, out_channels, 1)


class UNet2dGN(UNet2d):
    """ 2d U-Net with GroupNorm
    """
    # Convolutional block for single layer of the decoder / encoder
    # we apply to 2d convolutions with relu activation
    def _conv_block(self, in_channels, out_channels, level, part, activation=nn.ReLU()):
        num_groups1 = min(in_channels, 32)
        num_groups2 = min(out_channels, 32)
        padding = 1 if self.pad_convs else 0
        return nn.Sequential(nn.GroupNorm(num_groups1, in_channels),
                             nn.Conv2d(in_channels, out_channels,
                                       kernel_size=3, padding=padding),
                             activation,
                             nn.GroupNorm(num_groups2, out_channels),
                             nn.Conv2d(out_channels, out_channels,
                                       kernel_size=3, padding=padding),
                             activation)


def unet_2d(pretrained=None, **kwargs):
    net = UNet2dGN(**kwargs)
    if pretrained is not None:
        assert pretrained in ('isbi',)
        # TODO implement download
    return net


class DoubleMUnet(nn.Module):
    def __init__(self, ch_in, ch_out, depth=3, initial_features=64, inter_features=64, activation=nn.ReLU(),
                 use_last_nl=True, use_gn=True):
        super().__init__()
        if use_gn:
            self.unet_shared = UNet2dGN(1, inter_features, depth=depth, pad_convs=True, initial_features=initial_features,
                                        activation=activation)
            self.unet_union = UNet2dGN(ch_in * inter_features, inter_features, depth=depth, pad_convs=True,
                                       initial_features=initial_features,
                                       activation=activation)
        else:
            self.unet_shared = UNet2d(1, inter_features, depth=depth, pad_convs=True,
                                        initial_features=initial_features,
                                        activation=activation)
            self.unet_union = UNet2d(ch_in * inter_features, inter_features, depth=depth, pad_convs=True,
                                       initial_features=initial_features,
                                       activation=activation)

        assert ch_out in (5, 6)
        self.ch_out = ch_out
        self.mt_p = self._make_mlt_head(inter_features, 1, gn=use_gn)
        self.mt_phot = self._make_mlt_head(inter_features, 1, gn=use_gn)
        self.mt_x = self._make_mlt_head(inter_features, 1, gn=use_gn)
        self.mt_y = self._make_mlt_head(inter_features, 1, gn=use_gn)
        self.mt_z = self._make_mlt_head(inter_features, 1, gn=use_gn)
        self.mt_bg = self._make_mlt_head(inter_features, 1, gn=use_gn)

        self._use_last_nl = use_last_nl

        self.p_nl = torch.sigmoid  # only in inference, during training
        self.phot_nl = torch.sigmoid
        self.xyz_nl = torch.tanh
        self.bg_nl = torch.sigmoid

    @staticmethod
    def parse(param):
        activation = eval(param['HyperParameter']['arch_param']['activation'])
        return DoubleMUnet(
            ch_in=param['HyperParameter']['channels_in'],
            ch_out=param['HyperParameter']['channels_out'],
            depth=param['HyperParameter']['arch_param']['depth'],
            initial_features=param['HyperParameter']['arch_param']['initial_features'],
            inter_features=param['HyperParameter']['arch_param']['inter_features'],
            activation=activation,
            use_last_nl=param['HyperParameter']['arch_param']['use_last_nl'],
            use_gn=param['HyperParameter']['arch_param']['group_normalisation']
        )

    def apply_pnl(self, o):
        """
        Apply nonlinearity (sigmoid) to p channel. This is combined during training in the loss function.
        Only use when not training
        :param o:
        :return:
        """
        o[:, [0]] = self.p_nl(o[:, [0]])
        return o

    def apply_nonlin(self, o):
        """
        Apply non linearity in all the other channels
        :param o: 
        :return: 
        """
        # Apply for phot, xyz
        p = o[:, [0]]  # leave unused
        phot = o[:, [1]]
        xyz = o[:, 2:5]

        if not self.training:
            p = self.p_nl(p)

        phot = self.phot_nl(phot)
        xyz = self.xyz_nl(xyz)

        if self.ch_out == 5:
            o = torch.cat((p, phot, xyz), 1)
            return o
        elif self.ch_out == 6:
            bg = o[:, [5]]
            bg = self.bg_nl(bg)

            o = torch.cat((p, phot, xyz, bg), 1)
            return o

    def _make_mlt_head(self, in_channels, out_channels, activation=nn.ReLU(), last_kernel=1, gn=True):
        num_groups1 = min(in_channels, 32)
        num_groups2 = min(out_channels, 32)
        padding = True
        if gn:
            return nn.Sequential(nn.GroupNorm(num_groups1, in_channels),
                                 nn.Conv2d(in_channels, out_channels,
                                           kernel_size=3, padding=padding),
                                 activation,
                                 nn.GroupNorm(num_groups2, out_channels),
                                 nn.Conv2d(out_channels, out_channels,
                                           kernel_size=last_kernel, padding=False))
        else:
            return nn.Sequential(nn.Conv2d(in_channels, out_channels,
                                           kernel_size=3, padding=padding),
                                 activation,
                                 nn.Conv2d(out_channels, out_channels,
                                           kernel_size=last_kernel, padding=False))

    def forward(self, x):
        o0 = self.unet_shared.forward(x[:, [0]])
        o1 = self.unet_shared.forward(x[:, [1]])
        o2 = self.unet_shared.forward(x[:, [2]])
        o = torch.cat((o0, o1, o2), 1)

        # x_union = torch.cat((x, o), 1)  # cat original frames again
        x_union = o
        o = self.unet_union.forward(x_union)

        # o_p, o_phot, o_x, o_y, o_z = o[:, [0]], o[:, [1]], o[:, [2]], o[:, [3]], o[:, [4]]
        o_p = self.mt_p.forward(o)
        o_phot = self.mt_phot.forward(o)
        o_x = self.mt_x.forward(o)
        o_y = self.mt_y.forward(o)
        o_z = self.mt_z.forward(o)
        o_not_bg = torch.cat((o_p, o_phot, o_x, o_y, o_z), 1)

        if self.ch_out == 5:
            o = o_not_bg

        elif self.ch_out == 6:
            o_bg = self.mt_bg.forward(o)
            o = torch.cat((o_not_bg, o_bg), 1)

        """Apply the final non-linearities"""
        if self._use_last_nl:
            o = self.apply_nonlin(o)

        return o

if __name__ == '__main__':

    model = DoubleMUnet(3, 6, 3, use_last_nl=True)
    x = torch.rand((10, 3, 64, 64))
    y = torch.rand((10, 6, 64, 64))
    criterion = torch.nn.MSELoss()
    out = model.forward(x)
    loss = criterion(out, y)
    loss.backward()

    print('Done')