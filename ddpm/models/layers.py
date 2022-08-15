import torch.nn as nn
import torch
import math


#############################
#
# Some additional utils
#
#############################


class Swish(nn.Module):
    def forward(self, x):
        return x * torch.sigmoid(x)


class PositionalEncoding(nn.Module):
    """
    Encoding like in "Attention is all you need" paper
    We have the following formula:

    PE_(pos, i) = sin(pos/10000^(i/n_embed))
    PE_(pos, i) = cos(pos/10000^(i/n_embed))

    Here pos - position number, i - dimension, n_embed - dimension of embedding
    i = 0,...,n_embed
    """

    def forward(self, t, n_embed):
        """
        :param t: (Tensor), [b_size x 1], time stamp or position number
        :param n_embed: (Int), dimension of embedding
        :return: (Tensor), [b_size x n_embed]
        """
        h = math.log(10000) / (n_embed // 2)
        dims = torch.arange(n_embed // 2, device=t.device)
        out = torch.exp(dims * -h)

        # [b x 1] -> [b x n_embed // 2]
        out1 = torch.sin(t[:, None] * out[None, :])
        out2 = torch.cos(t[:, None] * out[None, :])

        # [b x n_embed // 2] -> [b x n_embed]
        out = torch.cat((out1, out2), dim=1)

        return out


#############################
#
# Embedding layer for timestamp
#
#############################

class EmbLayer(nn.Module):
    def __init__(self, n_embed=32, scale_fact=4):
        """
        :param n_embed: (Int), dimension of embedded vector
        :param scale_fact: (Int), scaling for embedding layer
        """
        super(EmbLayer, self).__init__()

        self.n_embed_scaled = n_embed // scale_fact

        self.act = Swish()
        self.lin1 = nn.Linear(self.n_embed_scaled, n_embed)
        self.lin2 = nn.Linear(n_embed, n_embed)
        self.emb = PositionalEncoding()

    def forward(self, t):
        """
        :param t: (Tensor), [b_size]
        :return: (Tensor), [b_size x m_embed]
        """

        # [b x 1] -> [b x n_embed_scaled]
        t_embed = self.emb(t, self.n_embed_scaled)

        # [b x n_embed_scaled] -> [b x n_embed]
        t_embed = self.act(self.lin1(t_embed))
        t_embed = self.lin2(t_embed)

        return t_embed


#############################
#
# Residual block
#
#############################

class ResBlock(nn.Module):
    def __init__(self, in_channels, out_channels, n_embed, n_groups=32):
        """
        Residual block from Wide ResNet
        :param in_channels: (Int), number of input channels
        :param out_channels:  (Int), number of output channels
        :param n_embed: (Int), dimension of embedding
        :param n_groups: (Int), number of group for normalization
        """
        super(ResBlock, self).__init__()

        self.act = Swish()

        self.conv1 = nn.Conv2d(in_channels, out_channels, 3, 1, 1)
        self.norm1 = nn.GroupNorm(num_groups=n_groups, num_channels=in_channels)

        self.conv2 = nn.Conv2d(out_channels, out_channels, 3, 1, 1)
        self.norm2 = nn.GroupNorm(num_groups=n_groups, num_channels=out_channels)

        if in_channels != out_channels:
            self.shortcut = nn.Conv2d(in_channels, out_channels, 1, 1)
        else:
            self.shortcut = nn.Identity()

        self.emb = nn.Linear(n_embed, out_channels)

    def forward(self, x, t_emb):
        """
        :param x: (Tensor), [b_size x in_channels x W x H]
        :param t_emb: (Tensor), [b_size x n_embed]
        :return: (Tensor), [b_size x out_channels x W x H]
        """

        # [b x C_int x W x H] -> [b x C_out x W x H]
        out1 = self.conv1(self.act(self.norm1(x)))

        # [b x n_embed] -> [b x C_out x 1 x 1]
        out2 = self.emb(t_emb)[:, :, None, None]

        # [b x C_out x W x H] -> [b x C_out x W x H]
        out = self.conv2(self.act(self.norm2(out1 + out2)))

        return out + self.shortcut(x)


#############################
#
# Attention block
#
#############################

class AttLayer(nn.Module):
    def __init__(self, n_channels, n_heads=1):
        """
        :param n_channels: (Int), input channels
        :param n_heads: (Int), number of heads in attention
        :param n_groups: (Int), groups for normalization
        """
        super(AttLayer, self).__init__()

        self.att_dim = n_channels * 10
        self.scale = self.att_dim ** (-0.5)
        self.n_heads = n_heads

        self.proj = nn.Linear(n_channels, 3 * n_heads * self.att_dim)
        self.out = nn.Linear(n_heads * self.att_dim, n_channels)

    def forward(self, x, t):
        """
        :param x: (Tensor), [b_size x C x W x H]
        :param t: (Tensor), not needed here
        :return: (Tensor), [b_size x C x W x H]
        """
        _ = t

        b_size, c, w, h = x.size()

        # [b_size x C x W x H] -> [b_size x (W * H) x C]
        x = x.view(b_size, c, -1).permute(0, 2, 1)

        # [b_size x (W * H) x C] -> [b_size x (W * H) x n_heads x (3 * att_dim)]
        proj = self.proj(x).view(b_size, -1, self.n_heads, 3 * self.att_dim)

        # Each element with shape [b_size x (W * H) x n_heads x att_dim]
        q, k, v = torch.chunk(proj, 3, dim=-1)

        # Dot product (q * k^T) / scale
        # -> [b_size x (W * H) x (W * H) x n_heads]
        prod = torch.einsum('bihd,bjhd->bijh', q, k) * self.scale

        # Softmax by sequence,
        # -> [b_size x (W * H) x (W * H) x n_heads]
        soft = prod.softmax(dim=1)

        # Dot product V * softmax((q * k^T) / scale)
        # -> [b_size x (W * H) x n_heads x att_dim]
        prod = torch.einsum('bijh,bjhd->bihd', soft, v)

        # [b_size x (W * H) x (n_heads * att_dim)] -> [b_size x (W * H) x C]
        out = self.out(prod.view(b_size, -1, self.n_heads * self.att_dim))
        out += x

        # Back to image
        # [b_size x (W * H) x C] -> [b_size x C x W x H]
        out = out.view(b_size, w, h, c).permute(0, 3, 1, 2)

        return out


#############################
#
# UNet single block
#
#############################

class DownSample(nn.Module):
    def __init__(self, in_channels, out_channels):
        super(DownSample, self).__init__()

        self.conv = nn.Conv2d(out_channels, out_channels, 3, 2, 1)

    def forward(self, x, t):
        _ = t
        out = self.conv(x)

        return out


class UpSample(nn.Module):
    def __init__(self, in_channels, out_channels):
        super(UpSample, self).__init__()

        if in_channels < out_channels:
            out = out_channels // 2
        else:
            out = out_channels
        self.conv = nn.ConvTranspose2d(out_channels, out, 4, 2, 1)

    def forward(self, x, t):
        _ = t
        out = self.conv(x)

        return out


class UnetBlock(nn.Module):
    def __init__(self, in_channels, out_channels, n_embed, num_res=2, sample=None, is_attn=False):
        """
        :param in_channels: (Int)
        :param out_channels: (Int)
        :param n_embed: (Int)
        :param num_res: (Int), number of residual block
        :param sample: (nn.Module), sampling (up or down)
        :param is_attn: (Bool), add attention
        """
        super(UnetBlock, self).__init__()

        self.in_channels = in_channels
        self.out_channels = out_channels
        self.n_embed = n_embed
        self.num_res = num_res
        self.sample = sample
        self.is_attn = is_attn

        # nn.ModuleList()
        self.block = self._make_block()

    def _make_block(self):
        model = nn.ModuleList()

        in_channels = self.in_channels
        out_channels = self.out_channels

        for i in range(self.num_res):
            model.append(ResBlock(in_channels,
                                  out_channels,
                                  self.n_embed))
            if self.is_attn and i != self.num_res - 1:
                model.append(AttLayer(self.out_channels))
            in_channels = out_channels

        if self.sample:
            model.append(self.sample(self.in_channels, self.out_channels))

        return model

    def forward(self, x, t_emb):
        """
        :param x: (Tensor), [b_size x C_in x W x H]
        :param t_emb: (Tensor), [b_size x n_embed]
        :return: (Tensor), [b_size x C_out x W' x H']
        """
        x1, x2 = None, None

        for layer in self.block:

            if isinstance(layer, DownSample):
                x1, x2 = layer(x, t_emb), x
                return x1, x2
            else:
                x = layer(x, t_emb)

        x1 = x
        return x1, x2
