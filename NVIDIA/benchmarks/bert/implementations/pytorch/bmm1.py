import torch
import mhalib

if torch.__version__ >= '1.5':
    from torch.utils.cpp_extension import ROCM_HOME
    is_rocm_pytorch = True if ((torch.version.hip is not None) and (ROCM_HOME is not None)) else False

###########################################################################################
class Bmm1Function(torch.autograd.Function):

    @staticmethod
    def forward(ctx, batch1, batch2, seqlen, batch, maxseqlen, heads, embed, scale, stream, sync):
        ctx.save_for_backward(batch1, batch2, seqlen)
        ctx.batch = batch
        ctx.maxseqlen = maxseqlen
        ctx.heads = heads
        ctx.embed = embed
        ctx.scale = scale
        ctx.sync = sync
        ctx.stream = stream
        ntokens = seqlen.sum().item()
        ctx.ntokens = ntokens
        ntokens2 = 0
        for i in range(batch):
            ntokens2 += seqlen[i]*seqlen[i]

        output = torch.empty(ntokens2*heads, device="cuda", dtype=torch.float16)
        mhalib.FastBmm1Fprop(batch2.flatten().contiguous(), batch1.flatten().contiguous(), output.flatten().contiguous(), batch, seqlen, heads, embed, scale, False, stream, sync)

        return output[:ntokens2*heads]

    @staticmethod
    def backward(ctx, grad_output):

        batch1, batch2, seqlen = ctx.saved_tensors
        batch = ctx.batch
        maxseqlen = ctx.maxseqlen
        heads = ctx.heads
        embed = ctx.embed
        ntokens = ctx.ntokens

        grad_batch1 = torch.empty(ntokens,heads*embed, device="cuda", dtype=torch.float16)
        grad_batch2 = torch.empty(ntokens,heads*embed, device="cuda", dtype=torch.float16)

        mhalib.FastBmm1Dgrad2(batch2.flatten().contiguous(), grad_output.flatten().contiguous(), grad_batch1.flatten().contiguous(), batch, seqlen, heads, embed, ctx.scale, False, ctx.stream, ctx.sync)
        mhalib.FastBmm1Dgrad1(batch1.flatten().contiguous(), grad_output.flatten().contiguous(), grad_batch2.flatten().contiguous(), batch, seqlen, heads, embed, ctx.scale, False, ctx.stream, ctx.sync)

        return grad_batch1[:ntokens], grad_batch2[:ntokens], None, None, None, None, None, None, None, None

class Bmm1(torch.nn.Module):
    def __init__(self, batch, seqlen, heads, embed, scale=False, stream=True, sync=True):
        super(Bmm1, self).__init__()

        self.heads = heads
        self.embed = embed
        self.maxseqlen = seqlen
        self.scale = scale
        self.sync = sync
        self.stream = stream

    def forward(self, batch1, batch2, batch, seqlen):
        return Bmm1Function.apply(batch1, batch2, seqlen, batch, self.maxseqlen, self.heads, self.embed, self.scale, self.stream, self.sync)

##########################################################################################

class Bmm1StridedFunction(torch.autograd.Function):

    @staticmethod
    def forward(ctx, mixed, seqlen, batch, maxseqlen, heads, embed, scale, stream, sync, timers):
        ctx.save_for_backward(mixed, seqlen)
        ctx.batch = batch
        ctx.maxseqlen = maxseqlen
        ctx.heads = heads
        ctx.embed = embed
        ctx.scale = scale
        ctx.sync = sync
        ctx.stream = stream
        ctx.timers = timers
        ntokens = seqlen.sum().item()
        ctx.ntokens = ntokens
        ntokens2 = 0
        for i in range(batch):
            ntokens2 += seqlen[i]*seqlen[i]

        output = torch.empty(ntokens2*heads, device="cuda", dtype=torch.float16)

        if timers: timers['start_fprop'].record()
        #print("---------------is_rocm_pytorch----------------",is_rocm_pytorch)
        if is_rocm_pytorch:
            mhalib.FastBmm1Fprop(mixed, mixed, output, output, batch, seqlen, heads, embed, scale, True, stream, sync)
        else:
            mhalib.FastBmm1Fprop(mixed, mixed, output,  batch, seqlen, heads, embed, scale, True, stream, sync)

        if timers: timers['stop_fprop'].record()

        return output[:ntokens2*heads], mixed

    @staticmethod
    #def backward(ctx, grad_output):
    def backward(ctx, grad_output, grad_mixed):

        mixed, seqlen = ctx.saved_tensors
        batch = ctx.batch
        maxseqlen = ctx.maxseqlen
        heads = ctx.heads
        embed = ctx.embed
        ntokens = ctx.ntokens

        #grad_mixed = torch.empty([ntokens,heads*3*embed], device="cuda", dtype=torch.float16)

        if ctx.timers: ctx.timers['start_dgrad'].record()
        if is_rocm_pytorch:
            mhalib.FastBmm1Dgrad2(mixed, grad_output, grad_mixed, grad_mixed, batch, seqlen, heads, embed, ctx.scale, True, ctx.stream, ctx.sync)
        else:
            mhalib.FastBmm1Dgrad2(mixed, grad_output, grad_mixed, batch, seqlen, heads, embed, ctx.scale, True, ctx.stream, ctx.sync)

        if ctx.timers: ctx.timers['stop_dgrad'].record()
        if ctx.timers: ctx.timers['start_wgrad'].record()
        if is_rocm_pytorch:
            mhalib.FastBmm1Dgrad1(mixed, grad_output, grad_mixed, grad_mixed, batch, seqlen, heads, embed, ctx.scale, True, ctx.stream, ctx.sync)
        else:
            mhalib.FastBmm1Dgrad1(mixed, grad_output, grad_mixed, batch, seqlen, heads, embed, ctx.scale, True, ctx.stream, ctx.sync)

        if ctx.timers: ctx.timers['stop_wgrad'].record()
        #return grad_mixed[:ntokens], None, None, None, None, None, None, None, None, None
        return grad_mixed[:ntokens], grad_mixed, None, None, None, None, None, None, None, None, None

class Bmm1Strided(torch.nn.Module):
    def __init__(self, batch, seqlen, heads, embed, scale=True, stream=True, sync=True, timer=False):
        super(Bmm1Strided, self).__init__()

        self.heads = heads
        self.embed = embed
        self.maxseqlen = seqlen
        self.scale = scale
        self.sync = sync
        self.stream = stream
        if timer:
            self.timers = {'start_fprop':torch.cuda.Event(enable_timing=True),
                           'start_dgrad':torch.cuda.Event(enable_timing=True),
                           'start_wgrad':torch.cuda.Event(enable_timing=True),
                           'stop_fprop':torch.cuda.Event(enable_timing=True),
                           'stop_dgrad':torch.cuda.Event(enable_timing=True),
                           'stop_wgrad':torch.cuda.Event(enable_timing=True)}
        else:
            self.timers = None

    def forward(self, mixed, batch, seqlen):
        return Bmm1StridedFunction.apply(mixed, seqlen, batch, self.maxseqlen, self.heads, self.embed, self.scale, self.stream, self.sync, self.timers)

###########################################################################################
