import math
import torch
import importlib
import amp_C
from apex.multi_tensor_apply import multi_tensor_applier

import torch.distributed.distributed_c10d as c10d

class DistributedFusedAdam(torch.optim.Optimizer):

    """Implements Adam algorithm. Currently GPU-only.  Requires Apex to be installed via
    ``python setup.py install --cuda_ext --cpp_ext``.
    
    It has been proposed in `Adam: A Method for Stochastic Optimization`_.
    
    Arguments:
        params (iterable): iterable of parameters to optimize or dicts defining
            parameter groups.
        lr (float, optional): learning rate. (default: 1e-3)
        betas (Tuple[float, float], optional): coefficients used for computing
            running averages of gradient and its square. (default: (0.9, 0.999))
        eps (float, optional): term added to the denominator to improve
            numerical stability. (default: 1e-8)
        weight_decay (float, optional): weight decay (L2 penalty) (default: 0)
        amsgrad (boolean, optional): whether to use the AMSGrad variant of this
            algorithm from the paper `On the Convergence of Adam and Beyond`_
            (default: False) NOT SUPPORTED in FusedAdam!
        eps_inside_sqrt (boolean, optional): in the 'update parameters' step,
            adds eps to the bias-corrected second moment estimate before
            evaluating square root instead of adding it to the square root of
            second moment estimate as in the original paper. (default: False)
        use_mt (boolean, optional): use multi tensor apply for lower launch
            latency. (default: False)
        overlap_reductions(boolean, optional): whether to overlap reductions
            with bprop (default: True)
        num_prestats (integer, optional): number of fp64 stats that will be
            reduced during first fp16 gradient reduction block.
        step_supports_amp_scaling(boolean, optional): whether to use customized
            gradient unscaling logic (default: True)
        world: current progress group (default: None, which means the default process group created by init_process_group will be used)

    .. _Adam\: A Method for Stochastic Optimization:
        https://arxiv.org/abs/1412.6980
    .. _On the Convergence of Adam and Beyond:
        https://openreview.net/forum?id=ryQu7f-RZ
    """

    def __init__(self, params,
                 lr=1e-3, bias_correction = True,
                 betas=(0.9, 0.999), eps=1e-8, eps_inside_sqrt = False,
                 weight_decay=0., max_grad_norm=0., amsgrad=False, use_mt=False,
                 amp_scale_adjustment=1.0, overlap_reductions=True,
                 compute_L2_grad_norm=False, distributed_weight_update=0,
                 dwu_group_size=0, dwu_num_blocks=4, dwu_num_rs_pg=1, dwu_num_ar_pg=4,
                 dwu_num_ag_pg=0, revert_method=1, flat_mt=False,
                 dwu_num_chunks=4, predivide=True, e5m2_allgather=False,
                 do_not_flatten_model=False,
                 step_supports_amp_scaling=True,
                 world=None):
        global fused_adam_cuda, distributed_adam_cuda
        fused_adam_cuda = importlib.import_module("fused_adam_cuda")
        distributed_adam_cuda = importlib.import_module("distributed_adam_cuda")
        self.multi_tensor_fused_adam = distributed_adam_cuda.multi_tensor_fused_adam

        self._amp_scale_adjustment = amp_scale_adjustment

        if amsgrad:
            raise RuntimeError('DistributedFusedAdam does not support the AMSGrad variant.')

        defaults = dict(lr=lr, bias_correction=bias_correction,
                        betas=betas, eps=eps, weight_decay=weight_decay,
                        max_grad_norm=max_grad_norm)
        super(DistributedFusedAdam, self).__init__(params, defaults)
        self.eps_mode = 0 if  eps_inside_sqrt else 1

        self._overflow_buf = torch.cuda.IntTensor([0])
        self._has_overflow = False
        self._step_supports_amp_scaling = step_supports_amp_scaling

        # Way to revert a step
        # 3 -> undo kernel + double buffer (debug, print norm of difference)
        # 2 -> double buffer fp32 parameters
        # 1 -> undo kernel
        self._revert_method = revert_method
        if self._revert_method > 1:
            print("revert_method -> double buffer fp32 parameters, will consume more memory")

        self._last_step = False
        self._overlap_reductions = overlap_reductions
        self._global_scale = None
        self._num_blocks = dwu_num_blocks
        self._num_chunks = dwu_num_chunks
        self._predivide = predivide
        self._e5m2_allgather = e5m2_allgather
        self._do_not_flatten_model = do_not_flatten_model
        self._compute_L2_grad_norm = compute_L2_grad_norm
        self._L2_grad_norm = None
        self._group_size = torch.cuda.device_count() if dwu_group_size <= 0 else dwu_group_size
        self._default_group = c10d._get_default_group()
        self._world = world if world is not None else self._default_group
        self._world_size = torch.distributed.get_world_size(group=self._world)
        self._num_groups = self._world_size // self._group_size
        self._global_rank = torch.distributed.get_rank(group=self._default_group)
        self._local_rank = torch.distributed.get_rank(group=self._world)
        self._rank_in_group = self._local_rank % self._group_size

        p_offset = 0
        p_i = 0
        self._param_state = None
        self._model_params = []
        self._grads_info = []
        self._grad_accs = []
        self._group_properties = []
        for group in self.param_groups:
            self._param_group = group
            prev = None
            beta1, beta2 = group['betas']
            bias_correction = 1 if group['bias_correction'] else 0
            eps = group['eps']
            weight_decay = group['weight_decay']
            for p in group['params']:
                torch.distributed.broadcast(p,0)
                if not p.requires_grad:
                    continue
                self._model_params.append(p)
                # Multiple param groups support: 
                # store one hyperparam item per parameter tensor
                self._group_properties.append((
                    beta1,
                    beta2,
                    bias_correction,
                    eps,
                    weight_decay
                    ))
                state = self.state[p]
                if len(state) == 0:
                    state['step'] = 0
                if self._param_state is None:
                    self._param_state = state
                p_grads_size = p.numel()
                def wrapper(param, param_i, param_grads_size, param_offset):
                    param_tmp = param.expand_as(param)
                    grad_acc = param_tmp.grad_fn.next_functions[0][0]
                    def allreduce_hook(*unused):
                        self._do_overlapped_reduction(param_i, param_grads_size, param_offset, param)
                    grad_acc.register_hook(allreduce_hook)
                    self._grad_accs.append(grad_acc)
                self._grads_info.append({"param_grads_size":p_grads_size, "param_offset":p_offset})
                wrapper(p, p_i, p_grads_size, p_offset)
                p_offset += p_grads_size
                # Only enforce 128b alignment (64 * fp16) for non-consecutive parameters
                # RNN is one example of consecutive parameters:
                # (weight_ih, weight_hh, bias_ih, bias_hh)
                if prev is not None and (prev.data_ptr() + prev.numel() * prev.element_size() != p.data_ptr()):
                    p_offset = ((p_offset + 63) // 64) * 64
                prev = p
                p_i += 1
        self._grads_generated = [False]*len(self._grads_info)
        self._flat_mt = flat_mt
        self._grads = []
        if self._overlap_reductions:
            self._current_block = self._num_blocks

        self._net_total_param_size = p_offset
        self._total_param_size = p_offset
        dwu_min_page_size = 256 * self._num_blocks * self._num_chunks * self._group_size
        self._total_param_size = ((self._total_param_size + dwu_min_page_size - 1) // dwu_min_page_size) * dwu_min_page_size
        self._block_size = self._total_param_size // self._num_blocks
        self._chunk_size = self._block_size // self._num_chunks
        self._shard_size = self._chunk_size // self._group_size
        print("self._net_total_param_size=%d, self._total_param_size=%d, dwu_min_page_size=%d, self._block_size=%d, self._chunk_size=%d, self._shard_size=%d" % (self._net_total_param_size, self._total_param_size,dwu_min_page_size,self._block_size,self._chunk_size,self._shard_size))

        self._low_param_i = [0]*self._num_blocks
        for block_id in range(self._num_blocks-1,-1,-1):
            p_i = len(self._grads_info)-1
            while p_i > 0 and self._grads_info[p_i]["param_offset"] > block_id*self._block_size:
                p_i -= 1
            self._low_param_i[block_id] = p_i
        print(self._low_param_i)

        self._flat_grads = torch.zeros([self._total_param_size], dtype=torch.float16, device='cuda')
        self._new_params = torch.zeros([self._total_param_size], dtype=torch.uint8 if self._e5m2_allgather else torch.float16, device='cuda')
        self._mega_shard_size = self._num_blocks * self._num_chunks * self._shard_size
        self._fp32_p = torch.zeros([self._mega_shard_size], dtype=torch.float32, device='cuda')
        self._fp32_m = torch.zeros([self._mega_shard_size], dtype=torch.float32, device='cuda')
        self._fp32_v = torch.zeros([self._mega_shard_size], dtype=torch.float32, device='cuda')
        # FIXME: Rethink fp16 label since it's either uint8 or fp16
        self._fp16_p = torch.zeros([self._mega_shard_size], dtype=torch.uint8 if self._e5m2_allgather else torch.float16, device='cuda')
        self._fp16_g = torch.zeros([self._mega_shard_size], dtype=torch.float16, device='cuda')

        self._individual_flat_grads = []
        for p_i, (grads_info, p) in enumerate(zip(self._grads_info, self._model_params)):
            self._individual_flat_grads.append(self._flat_grads[grads_info["param_offset"]:grads_info["param_offset"]+grads_info["param_grads_size"]].view_as(p))

        def _flat_split(p):
            def __blockify(p):
                return [p[block_id*self._block_size:(block_id+1)*self._block_size] for block_id in range(self._num_blocks)]
            def __chunkify(p):
                return [p[chunk_id*self._chunk_size:(chunk_id+1)*self._chunk_size] for chunk_id in range(self._num_chunks)]
            def __shardify(p):
                return [p[shard_id*self._shard_size:(shard_id+1)*self._shard_size] for shard_id in range(self._group_size)]
            list_of_blocks = __blockify(self._flat_grads)
            list_of_list_of_chunks = [__chunkify(block) for block in list_of_blocks]
            list_of_list_of_list_of_shards = [[__shardify(chunk) for chunk in chunks] for chunks in list_of_list_of_chunks]
            return list_of_blocks, list_of_list_of_chunks, list_of_list_of_list_of_shards
        self._flat_grads_blocks, self._flat_grads_chunks, self._flat_grads_shards = _flat_split(self._flat_grads)
        def _full_packed_split(p):
            def __shardify(p):
                return [p[mega_shard*self._mega_shard_size:(mega_shard+1)*self._mega_shard_size] for mega_shard in range(self._group_size)]
            def __blockify(p):
                return [p[block_id*self._num_chunks*self._shard_size:(block_id+1)*self._num_chunks*self._shard_size] for block_id in range(self._num_blocks)]
            def __chunkify(p):
                return [p[chunk_id*self._shard_size:(chunk_id+1)*self._shard_size] for chunk_id in range(self._num_chunks)]
            list_of_mega_shards = __shardify(p)
            list_of_list_of_mega_blocks = [__blockify(mega_shard) for mega_shard in list_of_mega_shards]
            list_of_list_of_list_of_mega_chunks = [[__chunkify(mega_block) for mega_block in mega_blocks] for mega_blocks in list_of_list_of_mega_blocks]
            return list_of_mega_shards, list_of_list_of_mega_blocks, list_of_list_of_list_of_mega_chunks
        self._new_params_mega_shards, self._new_params_mega_blocks, self._new_params_mega_chunks = _full_packed_split(self._new_params)
        def _packed_split(p):
            def __packed_blockify(p):
                packed_block_size = self._num_chunks*self._shard_size
                return [p[block_id*packed_block_size:(block_id+1)*packed_block_size] for block_id in range(self._num_blocks)]
            def __packed_chunkify(p):
                # in the packed format, each chunk contains one shard, so packed_chunk_size == self._shard_size
                return [p[chunk_id*self._shard_size:(chunk_id+1)*self._shard_size] for chunk_id in range(self._num_chunks)]
            list_of_blocks = __packed_blockify(p)
            list_of_list_of_chunks = [__packed_chunkify(block) for block in list_of_blocks]
            return list_of_blocks, list_of_list_of_chunks
        self._fp32_p_blocks, self._fp32_p_chunks = _packed_split(self._fp32_p)
        self._fp32_m_blocks, self._fp32_m_chunks = _packed_split(self._fp32_m)
        self._fp32_v_blocks, self._fp32_v_chunks = _packed_split(self._fp32_v)
        self._fp16_p_blocks, self._fp16_p_chunks = _packed_split(self._fp16_p)
        self._fp16_g_blocks, self._fp16_g_chunks = _packed_split(self._fp16_g)

        # This paragraph does two things:
        # 1) Copy model parameters into master buffer
        # 2) Create tensor lists for unpacking new parameter tensor after all-gather
        self._packed_flat_to_model_params = []
        self._contrib_tensor_list = []
        self._contrib_group_properties = []
        for shard_id in range(self._group_size):
            for block_id in range(self._num_blocks):
                for chunk_id in range(self._num_chunks):
                    flat_shard_start = (((block_id * self._num_chunks + chunk_id) * self._group_size) + shard_id) * self._shard_size
                    flat_shard_end = flat_shard_start + self._shard_size
                    for (p, grads_info, group_props) in zip(self._model_params, self._grads_info, self._group_properties):
                        flat_grad_start = grads_info["param_offset"]
                        flat_grad_end = flat_grad_start + grads_info["param_grads_size"]
                        clipped_start = (lambda a,b: a if a > b else b)(flat_grad_start, flat_shard_start)
                        clipped_end = (lambda a,b: a if a < b else b)(flat_grad_end, flat_shard_end)
                        if clipped_start < clipped_end:
                            grad_offset = clipped_start - flat_grad_start
                            grad_length = clipped_end - clipped_start
                            shard_offset = clipped_start - flat_shard_start
                            model_param_fragment = p.view(-1)[grad_offset:grad_offset+grad_length]
                            new_param_packed_fragment = self._new_params_mega_chunks[shard_id][block_id][chunk_id][shard_offset:shard_offset+grad_length]
                            self._packed_flat_to_model_params.append( (new_param_packed_fragment, model_param_fragment) )
                            if shard_id == self._rank_in_group:
                                # copy model parameters into master buffer
                                master_param_fragment = self._fp32_p_chunks[block_id][chunk_id][shard_offset:shard_offset+grad_length]
                                opti_state_m_fragment = self._fp32_m_chunks[block_id][chunk_id][shard_offset:shard_offset+grad_length]
                                opti_state_v_fragment = self._fp32_v_chunks[block_id][chunk_id][shard_offset:shard_offset+grad_length]
                                opti_state_g_fragment = self._fp16_g_chunks[block_id][chunk_id][shard_offset:shard_offset+grad_length]
                                opti_state_p_fragment = self._fp16_p_chunks[block_id][chunk_id][shard_offset:shard_offset+grad_length]
                                print("model_param_fragment.size()=%s, new_param_packed_fragment.size()=%s, master_param_fragment.size()=%s" % (str(model_param_fragment.size()), str(new_param_packed_fragment.size()), str(master_param_fragment.size())))
                                master_param_fragment.copy_(model_param_fragment)
                                self._contrib_group_properties.append(group_props)
                                self._contrib_tensor_list.append((master_param_fragment, opti_state_m_fragment, opti_state_v_fragment, opti_state_g_fragment, opti_state_p_fragment)) # p, m, v, g, p_copy

        p, m, v, g, p_copy = list(zip(*self._contrib_tensor_list))
        self._contrib_tensor_list = [p, m, v, g, p_copy]

        math_type = self._fp32_p.dtype
        beta1, beta2, bias_correction, epsilon, decay = list(zip(*self._contrib_group_properties))
        self._contrib_beta1 = torch.tensor(beta1, dtype=math_type, device='cuda')
        self._contrib_beta2 = torch.tensor(beta2, dtype=math_type, device='cuda')
        self._contrib_bias_correction = torch.tensor(bias_correction, dtype=torch.int, device='cuda')
        self._contrib_epsilon = torch.tensor(epsilon, dtype=math_type, device='cuda')
        self._contrib_weight_decay = torch.tensor(decay, dtype=math_type, device='cuda')

        p_in, p_out = zip(*self._packed_flat_to_model_params)
        self._packed_flat_to_model_params = [p_in, p_out]

        # gather global ranks of all members of the current process group
        ranks = list(c10d._pg_group_ranks[world].keys()) if world is not None else list(range(self._world_size))

        self._num_rs_pg = dwu_num_rs_pg
        self._num_ar_pg = dwu_num_ar_pg
        self._num_ag_pg = dwu_num_ag_pg
        if self._num_groups > 1:
            self._ar_pg = []
            for dev_i in range(self._group_size):
                ar_idx = [dev_i+j*self._group_size for j in range(self._num_groups)]
                ar_rank = [ranks[i] for i in ar_idx]
                if self._global_rank in ar_rank:
                    grp = torch.distributed.new_group(ranks=ar_rank)
                    for i in range(self._num_ar_pg):
                        self._ar_pg.append(grp)
            self._ar_st = [torch.cuda.Stream() for _ in range(self._num_ar_pg)]
            for ar_pg in self._ar_pg:
                torch.distributed.all_reduce(self._overflow_buf,group=ar_pg)

        self._rs_pg, rs_ranks = [],[]
        for group_i in range(self._num_groups):
            rs_idx = [group_i*self._group_size+j for j in range(self._group_size)]
            rs_rank = [ranks[i] for i in rs_idx]
            rs_ranks.append(rs_rank)
            if self._global_rank in rs_rank:
                grp = torch.distributed.new_group(ranks=rs_rank)
                for i in range(self._num_rs_pg):
                    self._rs_pg.append(grp)
                if self._compute_L2_grad_norm:
                    self._l2_grad_norm_pg = grp
                    torch.distributed.all_reduce(self._overflow_buf,group=self._l2_grad_norm_pg)
        self._rs_st = [torch.cuda.Stream() for _ in range(self._num_rs_pg)]
        for rs_pg in self._rs_pg:
            torch.distributed.all_reduce(self._overflow_buf,group=rs_pg)

        if self._num_ag_pg == 0:
            self._ag_pg = self._rs_pg
            self._ag_st = self._rs_st
            self._num_ag_pg = self._num_rs_pg
        else:
            self._ag_pg = []
            for group_i in range(self._num_groups):
                ag_rank = rs_ranks[group_i]
                if self._global_rank in ag_rank:
                    grp = torch.distributed.new_group(ranks=ag_rank)
                    for i in range(self._num_ag_pg):
                        self._ag_pg.append(grp)
            self._ag_st = [torch.cuda.Stream() for _ in range(self._num_ag_pg)]
            for ag_pg in self._ag_pg:
                torch.distributed.all_reduce(self._overflow_buf,group=ag_pg)
        self._l2_grad_norm_st = torch.cuda.Stream() if self._compute_L2_grad_norm else None
        self._completion_st = torch.cuda.Stream()

        self._reductions_works = [None]*self._num_blocks
        self._allgather_works = [None]*self._num_blocks

        import inspect
        assert ('no_copy' in inspect.getfullargspec(torch.distributed.reduce_scatter).args), "This version of c10d does not support no_copy option"


    def set_last_step(self, last_step):
        self._last_step = last_step
        
    def _get_flush_block(self):
        flush_block = []
        if self._current_block > 0 and self._grads_generated[self._low_param_i[self._current_block-1]]:
            num_grads = len(self._grads_generated)
            contiguous_idx = num_grads
            while contiguous_idx > 0 and self._grads_generated[contiguous_idx-1]:
                contiguous_idx -= 1

            if contiguous_idx < num_grads and self._grads_info[contiguous_idx]["param_offset"] <= (self._current_block-1)*self._block_size:
                self._current_block -= 1
                start = self._current_block * self._block_size
                end = (self._current_block+1) * self._block_size
                flush_block = [start, end]

        return flush_block

    def _pipeline_block_reductions(self, block_id):
        self._flatten_grad_mt(1.0/self._world_size if self._predivide else 1.0)

        # Reduction within each node
        # Changes gradient format from [block * chunk * shard] to [shard * block * chunk]
        # The output format is the same as the fp32 master parameters
        works = [None]*self._num_chunks
        for chunk_id in range(self._num_chunks):
            glob_chunk_id = block_id * self._num_chunks + chunk_id
            rs_stream = self._rs_st[glob_chunk_id%self._num_rs_pg]
            rs_stream.wait_stream(torch.cuda.current_stream())
            with torch.cuda.stream(rs_stream):
                works[chunk_id] = torch.distributed.reduce_scatter(self._fp16_g_chunks[block_id][chunk_id],self._flat_grads_shards[block_id][chunk_id],group=self._rs_pg[glob_chunk_id%self._num_rs_pg],async_op=True,no_copy=True)

        # Reduction across nodes for each rank
        if self._num_groups > 1:
            for chunk_id in range(self._num_chunks):
                glob_chunk_id = block_id * self._num_chunks + chunk_id
                ar_stream = self._ar_st[glob_chunk_id%self._num_ar_pg]
                with torch.cuda.stream(ar_stream):
                    works[chunk_id].wait()
                    works[chunk_id] = torch.distributed.all_reduce(self._fp16_g_chunks[block_id][chunk_id],group=self._ar_pg[glob_chunk_id%self._num_ar_pg],async_op=True)
        self._reductions_works[block_id] = works

        # Optionally compute L2 grad norm
        if self._compute_L2_grad_norm and block_id == 0:
            with torch.cuda.stream(self._l2_grad_norm_st):
                for block_id in range(self._num_blocks):
                    for chunk_id in range(self._num_chunks):
                        self._reductions_works[block_id][chunk_id].wait()
                # Since the packed format is contiguous after reductions, only one norm is needed
                l2_grad_norm_sq = torch.empty([1], device='cuda')
                l2_grad_norm_sq = self._fp16_g.norm(dtype=torch.float32, p=2)**2
                torch.distributed.all_reduce(l2_grad_norm_sq, group=self._l2_grad_norm_pg)
                self._L2_grad_norm = l2_grad_norm_sq.sqrt().item()

    def __launch_step_kernel(self):
        combined_scale = self._global_scale
        if self._param_group['max_grad_norm'] > 0 and math.isfinite(self.L2_grad_norm):
            combined_scale = self._param_group['max_grad_norm'] / (self.L2_grad_norm / self._global_scale + 1e-6)
            combined_scale = self._global_scale / min(1, combined_scale)
        
        multi_tensor_applier(self.multi_tensor_fused_adam,
                self._overflow_buf,
                self._contrib_tensor_list, # p, m, v, g, p_copy
                self._contrib_beta1,
                self._contrib_beta2,
                self._contrib_bias_correction,
                self._contrib_epsilon,
                self._contrib_weight_decay,
                self._param_group['lr'],
                combined_scale,
                self._param_state['step']+1,
                self.eps_mode)

    def _pipeline_step(self):
        # Call step kernel once per step
        # Call all-gather once per step
        with torch.cuda.stream(self._completion_st):
            for block_id in range(self._num_blocks):
                for chunk_id in range(self._num_chunks):
                    self._reductions_works[block_id][chunk_id].wait()
            self.__launch_step_kernel()
            torch.distributed.all_gather(self._new_params_mega_shards, self._fp16_p, group=self._ag_pg[0], no_copy=True)

    def _flatten_grad_mt(self, scale):
        if self._flat_mt and len(self._grads) > 0:
            self._overflow_buf.zero_()
            multi_tensor_applier(
                    amp_C.multi_tensor_scale,
                    self._overflow_buf,
                    list(zip(*self._grads)),
                    scale)
            self._grads = []

    def _do_overlapped_reduction(self, param_i, param_grads_size, param_offset, param):
        # handle overlapped reductions
        if self._flat_mt:
            self._grads.append( (param.grad, self._individual_flat_grads[param_i]) )
        else:
            torch.div(param.grad, self._world_size if self._predivide else 1.0, out=self._individual_flat_grads[param_i])
        self._grads_generated[param_i]=True
        if not self._last_step:
            if self._overlap_reductions:
                flush_block = self._get_flush_block()
                while flush_block:
                    block_id = flush_block[0] // self._block_size
                    self._pipeline_block_reductions(block_id)
                    flush_block = self._get_flush_block()

    def set_global_scale(self, global_scale):
        """Set global scale.
        """
        self._global_scale = global_scale

    @property
    def global_scale(self):
        return self._global_scale

    @property
    def has_overflow(self):
        """Check if overflows were detected by any call to step(...) method.
        Clears the overflow flag.
        """
        has_overflow = self._has_overflow
        self._has_overflow = False
        return has_overflow

    @property
    def peek_overflow(self):
        """Check if overflows were detected by any call to step(...) method.
        Does not clear overflow flag.
        """
        return self._has_overflow

    def strided_check_finite(self, output_params, stride=1, start=-1, end=-1, clear=True):
        """Strided check for overflow.
        You can get status by calling has_overflow.
        """
        if start >= 0 and start < end:
            out_p = output_params[start:end]
        else:
            out_p = output_params
        fused_adam_cuda.strided_check_finite(self._overflow_buf,
                out_p,
                stride,
                1 if clear else 0)
        self._has_overflow = False if self._overflow_buf.item() == 0 else True
        return self._has_overflow

    @property
    def L2_grad_norm(self):
        if self._compute_L2_grad_norm:
            torch.cuda.current_stream().wait_stream(self._l2_grad_norm_st)
            return self._L2_grad_norm
        else:
            return None

    def complete_reductions(self):
        """Complete reductions if full pipeline is not selected or overlap is not allowed.
        """
        if self._last_step:
            # zero out gradients that have not been completed yet
            for param_i, grad_generated in enumerate(self._grads_generated):
                if not grad_generated:
                    grad_info = self._grads_info[param_i]
                    param_offset = grad_info["param_offset"]
                    param_size = grad_info["param_grads_size"]
                    self._flat_grads[param_offset:param_offset+param_size].zero_()
                    self._grads_generated[param_i] = True

        if self._last_step or not self._overlap_reductions:
            # nothing done so far, run full pipeline after reductions
            for block_id in range(self._num_blocks-1,-1,-1):
                self._pipeline_block_reductions(block_id)

        if self._compute_L2_grad_norm:
            torch.cuda.current_stream().wait_stream(self._l2_grad_norm_st)

        self._current_block = self._num_blocks
        self._grads_generated = [False]*len(self._grads_info)

    def step(self, closure=None):
        loss = None
        if closure is not None:
            loss = closure()

        self._pipeline_step()

        with torch.cuda.stream(self._completion_st):
            # Copy self._new_params to model params
            for p in self._model_params: self.state[p]['step'] += 1
            multi_tensor_applier(
                    fused_adam_cuda.maybe_cast_mt,
                    self._overflow_buf,
                    self._packed_flat_to_model_params)

        torch.cuda.current_stream().wait_stream(self._completion_st)

        self._reductions_works = [None]*self._num_blocks
        self._allgather_works = [None]*self._num_blocks

        return loss
