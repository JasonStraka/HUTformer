import torch
from torch import nn
from torch import Tensor
import torch.nn.functional as F
from einops import rearrange
import numpy as np
import math
import warnings

def _no_grad_trunc_normal_(tensor, mean, std, a, b):
    # Cut & paste from PyTorch official master until it's in a few official releases - RW
    # Method based on https://people.sc.fsu.edu/~jburkardt/presentations/truncated_normal.pdf
    def norm_cdf(x):
        # Computes standard normal cumulative distribution function
        return (1. + math.erf(x / math.sqrt(2.))) / 2.

    if (mean < a - 2 * std) or (mean > b + 2 * std):
        warnings.warn("mean is more than 2 std from [a, b] in nn.init.trunc_normal_. "
                      "The distribution of values may be incorrect.",
                      stacklevel=2)

    with torch.no_grad():
        # Values are generated by using a truncated uniform distribution and
        # then using the inverse CDF for the normal distribution.
        # Get upper and lower cdf values
        l = norm_cdf((a - mean) / std)
        u = norm_cdf((b - mean) / std)

        # Uniformly fill tensor with values from [l, u], then translate to
        # [2l-1, 2u-1].
        tensor.uniform_(2 * l - 1, 2 * u - 1)

        # Use inverse cdf transform for normal distribution to get truncated
        # standard normal
        tensor.erfinv_()

        # Transform to proper mean, std
        tensor.mul_(std * math.sqrt(2.))
        tensor.add_(mean)

        # Clamp to ensure it's in the proper range
        tensor.clamp_(min=a, max=b)
        return tensor

def trunc_normal_(tensor, mean=0., std=1., a=-2., b=2.):
    # type: (Tensor, float, float, float, float) -> Tensor
    r"""Fills the input Tensor with values drawn from a truncated
    normal distribution. The values are effectively drawn from the
    normal distribution :math:`\mathcal{N}(\text{mean}, \text{std}^2)`
    with values outside :math:`[a, b]` redrawn until they are within
    the bounds. The method used for generating the random values works
    best when :math:`a \leq \text{mean} \leq b`.
    Args:
        tensor: an n-dimensional `torch.Tensor`
        mean: the mean of the normal distribution
        std: the standard deviation of the normal distribution
        a: the minimum cutoff value
        b: the maximum cutoff value
    Examples:
        >>> w = torch.empty(3, 5)
        >>> nn.init.trunc_normal_(w)
    """
    return _no_grad_trunc_normal_(tensor, mean, std, a, b)

class WindowAttention(nn.Module):
    r""" Window based multi-head self attention (W-MSA) module with relative position bias.
    It supports both of shifted and non-shifted window.

    Args:
        dim (int): Number of input channels.
        window_size (tuple[int]): The height and width of the window.
        num_heads (int): Number of attention heads.
        qkv_bias (bool, optional):  If True, add a learnable bias to query, key, value. Default: True
        attn_drop (float, optional): Dropout ratio of attention weight. Default: 0.0
        proj_drop (float, optional): Dropout ratio of output. Default: 0.0
        pretrained_window_size (tuple[int]): The height and width of the window in pre-training.
    """

    def __init__(self, dim, window_size, num_heads, qkv_bias=True, attn_drop=0., proj_drop=0.,
                 pretrained_window_size=[0, 0]):

        super().__init__()
        self.dim = dim
        self.window_size = window_size  # Wh, Ww
        self.pretrained_window_size = pretrained_window_size
        self.num_heads = num_heads

        self.logit_scale = nn.Parameter(torch.log(10 * torch.ones((num_heads, 1, 1))), requires_grad=True)

        # mlp to generate continuous relative position bias
        self.cpb_mlp = nn.Sequential(nn.Linear(2, 512, bias=True),
                                     nn.ReLU(inplace=True),
                                     nn.Linear(512, num_heads, bias=False))

        # get relative_coords_table
        relative_coords_h = torch.arange(-(self.window_size[0] - 1), self.window_size[0], dtype=torch.float32)
        relative_coords_w = torch.arange(-(self.window_size[1] - 1), self.window_size[1], dtype=torch.float32)
        relative_coords_table = torch.stack(
            torch.meshgrid([relative_coords_h,
                            relative_coords_w])).permute(1, 2, 0).contiguous().unsqueeze(0)  # 1, 2*Wh-1, 2*Ww-1, 2
        if pretrained_window_size[0] > 0:
            relative_coords_table[:, :, :, 0] /= (pretrained_window_size[0] - 1)
            relative_coords_table[:, :, :, 1] /= (pretrained_window_size[1] - 1)
        else:
            relative_coords_table[:, :, :, 0] /= (self.window_size[0] - 1)
            relative_coords_table[:, :, :, 1] /= (self.window_size[1] - 1)
        relative_coords_table *= 8  # normalize to -8, 8
        relative_coords_table = torch.sign(relative_coords_table) * torch.log2(
            torch.abs(relative_coords_table) + 1.0) / np.log2(8)

        self.register_buffer("relative_coords_table", relative_coords_table)

        # get pair-wise relative position index for each token inside the window
        coords_h = torch.arange(self.window_size[0])
        coords_w = torch.arange(self.window_size[1])
        coords = torch.stack(torch.meshgrid([coords_h, coords_w]))  # 2, Wh, Ww
        coords_flatten = torch.flatten(coords, 1)  # 2, Wh*Ww
        relative_coords = coords_flatten[:, :, None] - coords_flatten[:, None, :]  # 2, Wh*Ww, Wh*Ww
        relative_coords = relative_coords.permute(1, 2, 0).contiguous()  # Wh*Ww, Wh*Ww, 2
        relative_coords[:, :, 0] += self.window_size[0] - 1  # shift to start from 0
        relative_coords[:, :, 1] += self.window_size[1] - 1
        relative_coords[:, :, 0] *= 2 * self.window_size[1] - 1
        relative_position_index = relative_coords.sum(-1)  # Wh*Ww, Wh*Ww
        self.register_buffer("relative_position_index", relative_position_index)

        self.qkv = nn.Linear(dim, dim * 3, bias=False)
        if qkv_bias:
            self.q_bias = nn.Parameter(torch.zeros(dim))
            self.v_bias = nn.Parameter(torch.zeros(dim))
        else:
            self.q_bias = None
            self.v_bias = None
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)
        self.softmax = nn.Softmax(dim=-1)

    def forward(self, x, mask=None):
        """
        Args:
            x: input features with shape of (num_windows*B, N, C)
            mask: (0/-inf) mask with shape of (num_windows, Wh*Ww, Wh*Ww) or None
        """
        B_, N, C = x.shape
        qkv_bias = None
        if self.q_bias is not None:
            qkv_bias = torch.cat((self.q_bias, torch.zeros_like(self.v_bias, requires_grad=False), self.v_bias))
        qkv = F.linear(input=x, weight=self.qkv.weight, bias=qkv_bias)
        qkv = qkv.reshape(B_, N, 3, self.num_heads, -1).permute(2, 0, 3, 1, 4)
        q, k, v = qkv[0], qkv[1], qkv[2]  # make torchscript happy (cannot use tensor as tuple)

        # cosine attention
        attn = (F.normalize(q, dim=-1) @ F.normalize(k, dim=-1).transpose(-2, -1))
        logit_scale = torch.clamp(self.logit_scale, max=torch.log(torch.tensor(1. / 0.01)).cuda()).exp()
        attn = attn * logit_scale

        relative_position_bias_table = self.cpb_mlp(self.relative_coords_table).view(-1, self.num_heads)
        relative_position_bias = relative_position_bias_table[self.relative_position_index.view(-1)].view(
            self.window_size[0] * self.window_size[1], self.window_size[0] * self.window_size[1], -1)  # Wh*Ww,Wh*Ww,nH
        relative_position_bias = relative_position_bias.permute(2, 0, 1).contiguous()  # nH, Wh*Ww, Wh*Ww
        relative_position_bias = 16 * torch.sigmoid(relative_position_bias)
        attn = attn + relative_position_bias.unsqueeze(0)

        if mask is not None:
            nW = mask.shape[0]
            attn = attn.view(B_ // nW, nW, self.num_heads, N, N) + mask.unsqueeze(1).unsqueeze(0)
            attn = attn.view(-1, self.num_heads, N, N)
            attn = self.softmax(attn)
        else:
            attn = self.softmax(attn)

        attn = self.attn_drop(attn)

        x = (attn @ v).transpose(1, 2).reshape(B_, N, C)
        x = self.proj(x)
        x = self.proj_drop(x)
        return x

    def extra_repr(self) -> str:
        return f'dim={self.dim}, window_size={self.window_size}, ' \
               f'pretrained_window_size={self.pretrained_window_size}, num_heads={self.num_heads}'

    def flops(self, N):
        # calculate flops for 1 window with token length of N
        flops = 0
        # qkv = self.qkv(x)
        flops += N * self.dim * 3 * self.dim
        # attn = (q @ k.transpose(-2, -1))
        flops += self.num_heads * N * (self.dim // self.num_heads) * N
        #  x = (attn @ v)
        flops += self.num_heads * N * N * (self.dim // self.num_heads)
        # x = self.proj(x)
        flops += N * self.dim * self.dim
        return flops
    
class SegmentMerging(nn.Module):
    def __init__(self, dim, norm_layer=nn.LayerNorm):
        super().__init__()
        self.dim = dim
        self.norm = norm_layer(2 * dim)

    def forward(self, x):
        return self.norm(rearrange(x, 'b (p d) c -> b p (d c)', d=2))

class FullAttention(nn.Module):
    '''
    The Attention operation
    '''
    def __init__(self, scale=None, attention_dropout=0.1):
        super(FullAttention, self).__init__()
        self.scale = scale
        self.dropout = nn.Dropout(attention_dropout)
        
    def forward(self, queries, keys, values):
        B, L, H, E = queries.shape
        _, S, _, D = values.shape
        scale = self.scale or 1./torch.sqrt(torch.tensor(E).cuda())

        scores = torch.einsum("blhe,bshe->bhls", queries, keys)
        A = self.dropout(torch.softmax(scale * scores, dim=-1))
        V = torch.einsum("bhls,bshd->blhd", A, values)
        V = V + queries
        return V.contiguous()


class AttentionLayer(nn.Module):
    '''
    The Multi-head Self-Attention (MSA) Layer
    '''
    def __init__(self, d_model, n_heads, d_keys=None, d_values=None, mix=True, dropout = 0.1):
        super(AttentionLayer, self).__init__()

        d_keys = d_keys or (d_model//n_heads)
        d_values = d_values or (d_model//n_heads)

        self.inner_attention = FullAttention(scale=None, attention_dropout = dropout)
        self.query_projection = nn.Linear(d_model, d_keys * n_heads)
        self.key_projection = nn.Linear(d_model, d_keys * n_heads)
        self.value_projection = nn.Linear(d_model, d_values * n_heads)
        self.out_projection = nn.Linear(d_values * n_heads, d_model)
        self.n_heads = n_heads
        self.mix = mix
        self.norm = nn.LayerNorm(d_model)
        self.norm_1 = nn.LayerNorm(d_model)

    def forward(self, x):
        queries, keys, values = x
        B, L, _ = queries.shape
        _, S, _ = keys.shape
        H = self.n_heads

        queries, keys, values = self.norm(queries), self.norm(keys), self.norm(values)
        queries = self.query_projection(queries).view(B, L, H, -1)
        keys = self.key_projection(keys).view(B, S, H, -1)
        values = self.value_projection(values).view(B, S, H, -1)

        out = self.inner_attention(
            queries,
            keys,
            values,
        )
        if self.mix:
            out = out.transpose(2,1).contiguous()
        out = out.view(B, L, -1)

        return self.out_projection(self.norm_1(out)) + out

class HUTformer(nn.Module):
   
    def __init__(self, NUM_NODES=207, len_hist=288, len_pred=288, len_patch=12, mode='encoder', pre_train=None):
        super(HUTformer, self).__init__()
        assert mode in ['encoder', 'decoder']
        self.mode       = mode
        self.NUM_NODES  = NUM_NODES
        self.len_hist   = len_hist
        self.len_pred   = len_pred
        self.len_patch  = len_patch
        self.num_patch  = int(len_hist / len_patch)
        self.num_heads  = 8
        self.drop       = 0.1
        self.embed_dim  = 64
        self.dim_SE     = 16
        self.dim_U      = self.num_patch*self.embed_dim

        self.SE = nn.Parameter(torch.zeros(NUM_NODES, self.dim_SE))
        trunc_normal_(self.SE, std=.02)
        self.embedding = nn.Linear(self.len_patch, self.embed_dim)
        trunc_normal_(self.embedding.weight, std=.02)
        self.STPE = nn.Linear(self.num_patch*self.embed_dim+self.dim_SE+2, self.dim_U)

        self.encoder_1 = WindowAttention(dim=self.embed_dim, window_size=(2, int(self.num_patch/2)), num_heads=self.num_heads, qkv_bias=True, attn_drop=self.drop, proj_drop=self.drop)
        self.encoder_2 = nn.Sequential(SegmentMerging(dim=self.embed_dim, norm_layer=nn.LayerNorm),
                            WindowAttention(dim=int(self.embed_dim*2), window_size=(2, int(self.num_patch/4)), num_heads=self.num_heads, qkv_bias=True, attn_drop=self.drop, proj_drop=self.drop))
        self.encoder_3 = nn.Sequential(SegmentMerging(dim=int(self.embed_dim*2), norm_layer=nn.LayerNorm),
                            WindowAttention(dim=int(self.embed_dim*4), window_size=(2, int(self.num_patch/8)), num_heads=self.num_heads, qkv_bias=True, attn_drop=self.drop, proj_drop=self.drop))
        self.encoder_project = nn.Linear(self.dim_U, self.len_pred)
        if self.mode == 'encoder':
            return
        if pre_train is not None:
            self.load_state_dict(torch.load(pre_train)["model_state_dict"], strict=False)
            print('Load encoder weights.')
        
        self.decoder_1 = WindowAttention(dim=self.embed_dim, window_size=(2, int(self.num_patch/2)), num_heads=self.num_heads, qkv_bias=True, attn_drop=self.drop, proj_drop=self.drop)
        self.decoder_project_0 = nn.Linear(int(self.embed_dim*2), self.embed_dim)
        self.decoder_2 = nn.Sequential(AttentionLayer(d_model=self.embed_dim, n_heads=self.num_heads, mix=True, dropout = self.drop),
                                       WindowAttention(dim=self.embed_dim, window_size=(2, int(self.num_patch/2)), num_heads=self.num_heads, qkv_bias=True, attn_drop=self.drop, proj_drop=self.drop))
        self.decoder_3 = nn.Sequential(AttentionLayer(d_model=self.embed_dim, n_heads=self.num_heads, mix=True, dropout = self.drop),
                                       WindowAttention(dim=self.embed_dim, window_size=(2, int(self.num_patch/2)), num_heads=self.num_heads, qkv_bias=True, attn_drop=self.drop, proj_drop=self.drop))
        self.decoder_project = nn.Linear(self.embed_dim, self.len_patch)
        
        if mode == 'decoder':
            for (name, para) in self.named_parameters():
                if not ('decoder' in name): para.requires_grad = False
            print('Froze encoder parameters.')
        import pandas as pd
        pd.set_option('display.max_rows', 100)
        print(pd.DataFrame(np.array([[name, para.requires_grad] for (name, para) in self.named_parameters()]), columns=['name', 'requires_grad']))

    
    def forward(self, history_data: torch.Tensor, future_data: torch.Tensor, batch_seen: int, epoch: int, train: bool, **kwargs) -> torch.Tensor:
        """

        Args:
            history_data (Tensor): Input data with shape: [B, L1, N, C]
            future_data (Tensor): Future data with shape: [B, L2, N, C]
            mask (Tensor): Mask with shape: [B, N, num_patch]

        Returns:
            torch.Tensor: outputs with shape [B, L2, N, 1]
        """
        B, L, N, C = history_data.shape
        patches = rearrange(history_data[:, :, :, 0].permute(0,2,1), 'b n (p t) -> b n p t', t=self.len_patch) # [B, N, num_patch, len_patch]
        S = self.embedding(patches).reshape((B, N, -1)) # [B, N, num_patch * embed_dim]
        S = torch.cat([
                S, self.SE.repeat(B, 1, 1), # [B, N, num_patch * embed_dim + dim_SE]
                history_data[:, -1, :, 1:], # [B, N, C-1]
            ], dim=-1)
        U = self.STPE(S).reshape((B*N, -1, self.embed_dim)) # [B, N, dim_U] => [BN, self.num_patch, self.embed_dim]
        H_1 = self.encoder_1(U) # [BN, self.num_patch,   self.embed_dim]
        H_2 = self.encoder_2(H_1) # [BN, self.num_patch/2, self.embed_dim*2]
        H_3 = self.encoder_3(H_2) # [BN, self.num_patch/4, self.embed_dim*4]
        predict_encoder = self.encoder_project(rearrange(H_3, '(b n) p d -> b n (p d)', b=B, n=N)) # [B, N, L]

        if self.mode == 'encoder':
            _prediction = predict_encoder.permute(0,2,1).unsqueeze(-1) # [B, L, N, 1]
            return _prediction
        
        patches = rearrange(predict_encoder, 'b n (p t) -> b n p t', t=self.len_patch) # [B, N, num_patch, len_patch]
        S = self.embedding(patches).reshape((B, N, -1)) # [B, N, num_patch * embed_dim]
        S = torch.cat([
                S, self.SE.repeat(B, 1, 1), # [B, N, num_patch * embed_dim + dim_SE]
                future_data[:, -1, :, 1:], # [B, N, C-1]
            ], dim=-1)
        U = self.STPE(S).reshape((B*N, -1, self.embed_dim)) # [B, N, dim_U] => [BN, self.num_patch, self.embed_dim]
        D_1 = self.decoder_1(U)
        D_2 = self.decoder_2((self.decoder_project_0(H_2).repeat(1,2,1), D_1, D_1))
        D_3 = self.decoder_3((H_1, D_2, D_2))
        prediction = self.decoder_project(D_3).reshape((B, N, -1)).permute(0,2,1).unsqueeze(-1) # [B, L, N, 1]
        
        return prediction

