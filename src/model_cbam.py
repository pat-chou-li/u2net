from typing import Union, List
import torch
import torch.nn as nn
import torch.nn.functional as F

#通道注意力
class ChannelAttentionModule(nn.Module):
    def __init__(self, channel, ratio=16):
        super(ChannelAttentionModule, self).__init__()
        self.avg_pool = nn.AdaptiveAvgPool2d(1)
        self.max_pool = nn.AdaptiveMaxPool2d(1)

        self.shared_MLP = nn.Sequential(
            nn.Conv2d(channel, channel // ratio, 1, bias=False),
            nn.ReLU(),
            nn.Conv2d(channel // ratio, channel, 1, bias=False)
        )
        self.sigmoid = nn.Sigmoid()

    def forward(self, x):
        avgout = self.shared_MLP(self.avg_pool(x))
        maxout = self.shared_MLP(self.max_pool(x))
        return self.sigmoid(avgout + maxout)

#空间注意力
class SpatialAttentionModule(nn.Module):
    def __init__(self):
        super(SpatialAttentionModule, self).__init__()
        self.conv2d = nn.Conv2d(in_channels=2, out_channels=1, kernel_size=7, stride=1, padding=3)
        self.sigmoid = nn.Sigmoid()

    def forward(self, x):
        avgout = torch.mean(x, dim=1, keepdim=True)
        maxout, _ = torch.max(x, dim=1, keepdim=True)
        out = torch.cat([avgout, maxout], dim=1)
        out = self.sigmoid(self.conv2d(out))
        return out


class CBAM(nn.Module):
    def __init__(self, channel):
        super(CBAM, self).__init__()
        self.channel_attention = ChannelAttentionModule(channel)
        self.spatial_attention = SpatialAttentionModule()

    def forward(self, x):
        out = self.channel_attention(x) * x
        out = self.spatial_attention(out) * out
        return out

#SE模块
class Attn(nn.Module):
    def __init__(self, channel, reduction=16):
        super(Attn, self).__init__()
        assert channel % reduction == 0, "channel must be mutil reduction" 
        self.avg_pool = nn.AdaptiveAvgPool2d(1)
        self.fc = nn.Sequential(
            nn.Linear(channel, channel // reduction, bias=False),
            nn.ReLU(inplace=True),
            # nn.ELU(inplace=True),
            nn.Linear(channel // reduction, channel, bias=False),
            nn.Sigmoid()
        )

    def forward(self, x):
        b, c, _, _ = x.size()
        y = self.avg_pool(x).view(b, c)
        y = self.fc(y).view(b, c, 1, 1)
        return x * y.expand_as(x)

class RefineModule(nn.Module):
    def __init__(self, channel, out):
        super(RefineModule, self).__init__()
        self.attn = Attn(256)
        self.conv_up = nn.Conv2d(channel, 256, 3, 1, 1)
        self.conv_dn = nn.Conv2d(256, out, 3, 1, 1)
        self.relu1 = nn.ReLU(inplace=True)
        # self.relu1 = nn.ELU(inplace=True)

    def forward(self, x):
        x = self.conv_up(x)
        x = self.relu1(x)
        x = self.attn(x)
        x = self.conv_dn(x)
        return x 

# 卷积 + batchnormalization + Relu
class ConvBNReLU(nn.Module):
    def __init__(self, in_ch: int, out_ch: int, kernel_size: int = 3, dilation: int = 1):
        super().__init__()

        padding = kernel_size // 2 if dilation == 1 else dilation
        self.conv = nn.Conv2d(in_ch, out_ch, kernel_size, padding=padding, dilation=dilation, bias=False)
        self.bn = nn.BatchNorm2d(out_ch)
        self.relu = nn.ReLU(inplace=True)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.relu(self.bn(self.conv(x)))

# 下采样+卷积+bn+relu，flag表示是否启用下采样，下采样采用maxpooling
class DownConvBNReLU(ConvBNReLU):
    def __init__(self, in_ch: int, out_ch: int, kernel_size: int = 3, dilation: int = 1, flag: bool = True):
        super().__init__(in_ch, out_ch, kernel_size, dilation)
        self.down_flag = flag

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        if self.down_flag:
            x = F.max_pool2d(x, kernel_size=2, stride=2, ceil_mode=True)

        return self.relu(self.bn(self.conv(x)))

# 上采样+卷积+bn+relu，flag表示是否启用上采样，上采样采用双线性插值
class UpConvBNReLU(ConvBNReLU):
    def __init__(self, in_ch: int, out_ch: int, kernel_size: int = 3, dilation: int = 1, flag: bool = True):
        super().__init__(in_ch, out_ch, kernel_size, dilation)
        self.up_flag = flag
# 这里要和下采样过程中的参数拼接，所以传入两个tensor
    def forward(self, x1: torch.Tensor, x2: torch.Tensor) -> torch.Tensor:
        if self.up_flag:
            x1 = F.interpolate(x1, size=x2.shape[2:], mode='bilinear', align_corners=False)
        return self.relu(self.bn(self.conv(torch.cat([x1, x2], dim=1))))

# 一个小的RSU，就是一个unet，传入不同的height得到RSU4,RSU5等,注意这里所写的一切代码都是一个RSU内部的，而不是RSU之间的
class RSU(nn.Module):
    def __init__(self, height: int, in_ch: int, mid_ch: int, out_ch: int):
        super().__init__()

        assert height >= 2
        self.conv_in = ConvBNReLU(in_ch, out_ch)

        encode_list = [DownConvBNReLU(out_ch, mid_ch, flag=False)]
        decode_list = [UpConvBNReLU(mid_ch * 2, mid_ch, flag=False)]
        for i in range(height - 2):
            encode_list.append(DownConvBNReLU(mid_ch, mid_ch))
            decode_list.append(UpConvBNReLU(mid_ch * 2, mid_ch if i < height - 3 else out_ch))

        encode_list.append(ConvBNReLU(mid_ch, mid_ch, dilation=2))
        self.encode_modules = nn.ModuleList(encode_list)
        self.decode_modules = nn.ModuleList(decode_list)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x_in = self.conv_in(x)
        x = x_in
        encode_outputs = []
        #采集下采样层的输出
        for m in self.encode_modules:
            x = m(x)
            encode_outputs.append(x)
# 通过栈操作让上采样层得到同一层的下采样层输入，这里是RSU内部的，不是RSU之间的，首个x是最底下横着的那一层的输出
        x = encode_outputs.pop()
        for m in self.decode_modules:
            x2 = encode_outputs.pop()
            # x2:同层下采样层 x：上一个上采样层的输出，就是非skip-connection的那一条直接路径
            x = m(x, x2)
# x_in是最开始的那个输入
        return x + x_in

# 最下面的3个RSU4F，将上下采样替换为膨胀卷积
class RSU4F(nn.Module):
    def __init__(self, in_ch: int, mid_ch: int, out_ch: int):
        super().__init__()
        self.conv_in = ConvBNReLU(in_ch, out_ch)
        self.encode_modules = nn.ModuleList([ConvBNReLU(out_ch, mid_ch),
                                             ConvBNReLU(mid_ch, mid_ch, dilation=2),
                                             ConvBNReLU(mid_ch, mid_ch, dilation=4),
                                             ConvBNReLU(mid_ch, mid_ch, dilation=8)])

        self.decode_modules = nn.ModuleList([ConvBNReLU(mid_ch * 2, mid_ch, dilation=4),
                                             ConvBNReLU(mid_ch * 2, mid_ch, dilation=2),
                                             ConvBNReLU(mid_ch * 2, out_ch)])
# 同样通过栈实现跳跃链接
    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x_in = self.conv_in(x)

        x = x_in
        encode_outputs = []
        for m in self.encode_modules:
            x = m(x)
            encode_outputs.append(x)

        x = encode_outputs.pop()
        for m in self.decode_modules:
            x2 = encode_outputs.pop()
            x = m(torch.cat([x, x2], dim=1))

        return x + x_in


class U2Net(nn.Module):
    # sod是一个二分类任务，输出通道只需要1
    def __init__(self, cfg: dict, out_ch: int = 1):
        super().__init__()
        assert "encode" in cfg
        assert "decode" in cfg
        self.encode_num = len(cfg["encode"])
        #字面意思，但list中一个item就是一个RSU
        encode_list = []
        #这个是3*3的卷积层，每个都要输出特征图
        side_list = []
        # cbam list
        cbam_list = []
        for c in cfg["encode"]:
            # c: [height, in_ch, mid_ch, out_ch, RSU4F, side]
            assert len(c) == 6
            #构造RSU or RSU4F，解构赋值
            encode_list.append(RSU(*c[:4]) if c[4] is False else RSU4F(*c[1:4]))
            cbam_list.append(ChannelAttentionModule(c[3]))
            #构造side or not
            if c[5] is True:
                #encode层中，只有最底下那层需要采集输出，将此输出输入到一个3*3卷积，并且out_ch为1，生成一个二分类分割图
                #这是6个分割图中最底下的那个
                #注意3*3卷积+padding=1 不改变hw
                side_list.append(nn.Conv2d(c[3], out_ch, kernel_size=3, padding=1))
        self.encode_modules = nn.ModuleList(encode_list)

        decode_list = []
        for c in cfg["decode"]:
            # c: [height, in_ch, mid_ch, out_ch, RSU4F, side]
            assert len(c) == 6
            decode_list.append(RSU(*c[:4]) if c[4] is False else RSU4F(*c[1:4]))
            #对应的，decode层每层都要输出一个分割图
            if c[5] is True:
                side_list.append(nn.Conv2d(c[3], out_ch, kernel_size=3, padding=1))
        self.decode_modules = nn.ModuleList(decode_list)
        self.side_modules = nn.ModuleList(side_list)
        self.cbam_list = nn.ModuleList(cbam_list)
        #最后将6层分割图通过1*1卷积融合成最终结果
        #我将要在这里做注意力机制
        #self.out_conv = nn.Conv2d(self.encode_num * out_ch, out_ch, kernel_size=1)
        #self.out_conv = RefineModule(self.encode_num * out_ch, out_ch)
        self.out_conv = nn.Conv2d(self.encode_num * out_ch, out_ch, kernel_size=1)

    def forward(self, x: torch.Tensor) -> Union[torch.Tensor, List[torch.Tensor]]:
        _, _, h, w = x.shape

        # collect encode outputs
        encode_outputs = []
        for i, m in enumerate(self.encode_modules):
            #遍历每个encode模块并输出到下层
            x = m(x)
            x = self.cbam_list[i](x) + x
            #存储encode输出便于跳跃链接
            encode_outputs.append(x)
            #最后一层没有下采样
            if i != self.encode_num - 1:
                x = F.max_pool2d(x, kernel_size=2, stride=2, ceil_mode=True)

        # collect decode outputs
        x = encode_outputs.pop()
        decode_outputs = [x]
        for m in self.decode_modules:
            x2 = encode_outputs.pop()
            # 通过双线性插值上采样
            x = F.interpolate(x, size=x2.shape[2:], mode='bilinear', align_corners=False)
            x = m(torch.concat([x, x2], dim=1))
            # 前插入，结束后列表中的输出来源于decode1,2...5，encode6
            decode_outputs.insert(0, x)

        # collect side outputs
        side_outputs = []
        for m in self.side_modules:
            x = decode_outputs.pop()
            # 直接将decode的输出通过双线性插值还原成原来的大小
            x = F.interpolate(m(x), size=[h, w], mode='bilinear', align_corners=False)
            # 收集side的输出，最后要融合
            side_outputs.insert(0, x)
        #融合
        x = self.out_conv(torch.concat(side_outputs, dim=1))
        
        if self.training:
            # do not use torch.sigmoid for amp safe
            # [x]: 融合后的特征图， side_outputs 6个融合前的特征图， 这些都要用来计算loss
            return [x] + side_outputs
        else:
            return torch.sigmoid(x)


def u2net_full(out_ch: int = 1):
    cfg = {
        # height, in_ch, mid_ch, out_ch, RSU4F, side
        "encode": [[7, 3, 32, 64, False, False],      # En1
                   [6, 64, 32, 128, False, False],    # En2
                   [5, 128, 64, 256, False, False],   # En3
                   [4, 256, 128, 512, False, False],  # En4
                   [4, 512, 256, 512, True, False],   # En5
                   [4, 512, 256, 512, True, True]],   # En6
        # height, in_ch, mid_ch, out_ch, RSU4F, side
        "decode": [[4, 1024, 256, 512, True, True],   # De5
                   [4, 1024, 128, 256, False, True],  # De4
                   [5, 512, 64, 128, False, True],    # De3
                   [6, 256, 32, 64, False, True],     # De2
                   [7, 128, 16, 64, False, True]]     # De1
    }

    return U2Net(cfg, out_ch)


def u2net_lite(out_ch: int = 1):
    cfg = {
        # height, in_ch, mid_ch, out_ch, RSU4F, side
        "encode": [[7, 3, 16, 64, False, False],  # En1
                   [6, 64, 16, 64, False, False],  # En2
                   [5, 64, 16, 64, False, False],  # En3
                   [4, 64, 16, 64, False, False],  # En4
                   [4, 64, 16, 64, True, False],  # En5
                   [4, 64, 16, 64, True, True]],  # En6
        # height, in_ch, mid_ch, out_ch, RSU4F, side
        "decode": [[4, 128, 16, 64, True, True],  # De5
                   [4, 128, 16, 64, False, True],  # De4
                   [5, 128, 16, 64, False, True],  # De3
                   [6, 128, 16, 64, False, True],  # De2
                   [7, 128, 16, 64, False, True]]  # De1
    }

    return U2Net(cfg, out_ch)


def convert_onnx(m, save_path):
    m.eval()
    x = torch.rand(1, 3, 288, 288, requires_grad=True)

    # export the model
    torch.onnx.export(m,  # model being run
                      x,  # model input (or a tuple for multiple inputs)
                      save_path,  # where to save the model (can be a file or file-like object)
                      export_params=True,
                      opset_version=11)


if __name__ == '__main__':
    # n_m = RSU(height=7, in_ch=3, mid_ch=12, out_ch=3)
    # convert_onnx(n_m, "RSU7.onnx")
    #
    # n_m = RSU4F(in_ch=3, mid_ch=12, out_ch=3)
    # convert_onnx(n_m, "RSU4F.onnx")

    # u2net = u2net_full()
    u2net = u2net_lite()
    convert_onnx(u2net, "u2net_full.onnx")
