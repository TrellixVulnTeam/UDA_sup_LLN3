def get_loss(model, sup_batch, unsup_batch, global_step): #original get_loss
    # logits -> prob(softmax) -> log_prob(log_softmax)

    # batch
    input_ids, segment_ids, input_mask, label_ids = sup_batch
    if unsup_batch:
        ori_input_ids, ori_segment_ids, ori_input_mask, \
        aug_input_ids, aug_segment_ids, aug_input_mask = unsup_batch

        input_ids = torch.cat((input_ids, aug_input_ids), dim=0)
        segment_ids = torch.cat((segment_ids, aug_segment_ids), dim=0)
        input_mask = torch.cat((input_mask, aug_input_mask), dim=0)
            
    # logits
    hidden = model(
        input_ids=input_ids, 
        segment_ids=segment_ids, 
        input_mask=input_mask,
        output_h=True
    )
    logits = model(input_h=hidden)

    # sup loss
    sup_size = label_ids.shape[0]            
    sup_loss = sup_criterion(logits[:sup_size], label_ids)  # shape : train_batch_size
    if cfg.tsa and cfg.tsa != "none":
        tsa_thresh = get_tsa_thresh(cfg.tsa, global_step, cfg.total_steps, start=1./logits.shape[-1], end=1)
        larger_than_threshold = torch.exp(-sup_loss) > tsa_thresh   # prob = exp(log_prob), prob > tsa_threshold
        # larger_than_threshold = torch.sum(  F.softmax(pred[:sup_size]) * torch.eye(num_labels)[sup_label_ids]  , dim=-1) > tsa_threshold
        loss_mask = torch.ones_like(label_ids, dtype=torch.float32) * (1 - larger_than_threshold.type(torch.float32))
        sup_loss = torch.sum(sup_loss * loss_mask, dim=-1) / torch.max(torch.sum(loss_mask, dim=-1), torch_device_one())
    else:
        sup_loss = torch.mean(sup_loss)

    # unsup loss
    if unsup_batch:
        # ori
        with torch.no_grad():
            ori_logits = model(ori_input_ids, ori_segment_ids, ori_input_mask)
            ori_prob   = F.softmax(ori_logits, dim=-1)    # KLdiv target
            # temp control
            #ori_prob = ori_prob**(1/cfg.uda_softmax_temp)

            # confidence-based masking
            if cfg.uda_confidence_thresh != -1:
                unsup_loss_mask = torch.max(ori_prob, dim=-1)[0] > cfg.uda_confidence_thresh
                unsup_loss_mask = unsup_loss_mask.type(torch.float32)
            else:
                unsup_loss_mask = torch.ones(len(logits) - sup_size, dtype=torch.float32)
            unsup_loss_mask = unsup_loss_mask.to(_get_device())
                    
        # aug
        uda_softmax_temp = cfg.uda_softmax_temp if cfg.uda_softmax_temp > 0 else 1.
        aug_log_prob = F.log_softmax(logits[sup_size:] / uda_softmax_temp, dim=-1)

        # KLdiv loss
        """
            nn.KLDivLoss (kl_div)
            input : log_prob (log_softmax)
            target : prob    (softmax)
            https://pytorch.org/docs/stable/nn.html

            unsup_loss is divied by number of unsup_loss_mask
            it is different from the google UDA official
            The official unsup_loss is divided by total
            https://github.com/google-research/uda/blob/master/text/uda.py#L175
        """
        unsup_loss = torch.sum(unsup_criterion(aug_log_prob, ori_prob), dim=-1)
        unsup_loss = torch.sum(unsup_loss * unsup_loss_mask, dim=-1) / torch.max(torch.sum(unsup_loss_mask, dim=-1), torch_device_one())
        final_loss = sup_loss + cfg.uda_coeff*unsup_loss

        return final_loss, sup_loss, unsup_loss
    return sup_loss, None, None

# original get_loss, restructured
def get_loss_test(model, sup_batch, unsup_batch, global_step):
    # logits -> prob(softmax) -> log_prob(log_softmax)

    # batch
    input_ids, segment_ids, input_mask, label_ids = sup_batch
    ori_input_ids, ori_segment_ids, ori_input_mask, \
    aug_input_ids, aug_segment_ids, aug_input_mask = unsup_batch

    input_ids = torch.cat((input_ids, aug_input_ids), dim=0)
    segment_ids = torch.cat((segment_ids, aug_segment_ids), dim=0)
    input_mask = torch.cat((input_mask, aug_input_mask), dim=0)
            
    # logits
    hidden = model(
        input_ids=input_ids, 
        segment_ids=segment_ids, 
        input_mask=input_mask,
        output_h=True
    )
    logits = model(input_h=hidden)

    # sup loss
    sup_size = label_ids.shape[0]            
    sup_loss = sup_criterion(logits[:sup_size], label_ids)  # shape : train_batch_size
    if cfg.tsa and cfg.tsa != "none":
        tsa_thresh = get_tsa_thresh(cfg.tsa, global_step, cfg.total_steps, start=1./logits.shape[-1], end=1)
        larger_than_threshold = torch.exp(-sup_loss) > tsa_thresh   # prob = exp(log_prob), prob > tsa_threshold
        # larger_than_threshold = torch.sum(  F.softmax(pred[:sup_size]) * torch.eye(num_labels)[sup_label_ids]  , dim=-1) > tsa_threshold
        loss_mask = torch.ones_like(label_ids, dtype=torch.float32) * (1 - larger_than_threshold.type(torch.float32))
        sup_loss = torch.sum(sup_loss * loss_mask, dim=-1) / torch.max(torch.sum(loss_mask, dim=-1), torch_device_one())
    else:
        sup_loss = torch.mean(sup_loss)

    # unsup loss
    # ori
    with torch.no_grad():
        ori_logits = model(ori_input_ids, ori_segment_ids, ori_input_mask)
        ori_prob   = F.softmax(ori_logits, dim=-1)    # KLdiv target
        # temp control
        ori_prob = ori_prob**(1/cfg.uda_softmax_temp)
        ori_prob = ori_prob / ori_prob.sum(dim=1, keepdim=True)
                    
    # aug
    aug_log_prob = F.log_softmax(logits[sup_size:], dim=-1)

    unsup_loss = torch.mean(torch.sum(unsup_criterion(aug_log_prob, ori_prob), dim=-1))
    final_loss = sup_loss + cfg.uda_coeff*unsup_loss

    return final_loss, sup_loss, unsup_loss


def mixmatch_loss_no_mixup(model, sup_batch, unsup_batch, global_step):
    # batch
    input_ids, segment_ids, input_mask, label_ids = sup_batch
    ori_input_ids, ori_segment_ids, ori_input_mask, \
    aug_input_ids, aug_segment_ids, aug_input_mask = unsup_batch


    all_ids = torch.cat([input_ids, ori_input_ids, aug_input_ids], dim=0)
    all_mask = torch.cat([input_mask, ori_input_mask, aug_input_mask], dim=0)
    all_seg = torch.cat([segment_ids, ori_segment_ids, aug_segment_ids], dim=0)

    all_logits = model(all_ids, all_seg, all_mask)
            
    #sup loss
    sup_size = label_ids.shape[0]
    sup_loss = sup_criterion(all_logits[:sup_size], label_ids)
    sup_loss = torch.mean(sup_loss)

    #unsup loss
    with torch.no_grad():
        outputs_u = model(ori_input_ids, ori_segment_ids, ori_input_mask)
        outputs_u2 = model(aug_input_ids, aug_segment_ids, aug_input_mask)
        p = (torch.softmax(outputs_u, dim=1) + torch.softmax(outputs_u2, dim=1)) / 2
        pt = p**(1/cfg.uda_softmax_temp)
        targets_u = pt / pt.sum(dim=1, keepdim=True)
        targets_u = targets_u.detach()

    targets_u = torch.cat([targets_u, targets_u], dim=0)

    # l2
    probs_u = torch.softmax(all_logits[sup_size:], dim=1)
    unsup_loss = torch.mean((probs_u - targets_u)**2)

    w = cfg.lambda_u * linear_rampup(global_step, cfg.total_steps)
    final_loss = sup_loss + w * unsup_loss

    return final_loss, sup_loss, unsup_loss

def get_label_guess_loss(model, sup_batch, unsup_batch, global_step):
    # batch
    input_ids, segment_ids, input_mask, label_ids, num_tokens = sup_batch
    ori_input_ids, ori_segment_ids, ori_input_mask, \
    aug_input_ids, aug_segment_ids, aug_input_mask, \
    ori_num_tokens, aug_num_tokens = unsup_batch= unsup_batch


    all_ids = torch.cat([input_ids, ori_input_ids, aug_input_ids], dim=0)
    all_mask = torch.cat([input_mask, ori_input_mask, aug_input_mask], dim=0)
    all_seg = torch.cat([segment_ids, ori_segment_ids, aug_segment_ids], dim=0)

    all_logits = model(all_ids, all_seg, all_mask)
            
    #sup loss
    sup_size = label_ids.shape[0]
    sup_loss = sup_criterion(all_logits[:sup_size], label_ids)
    if cfg.tsa and cfg.tsa != "none":
        tsa_thresh = get_tsa_thresh(cfg.tsa, global_step, cfg.total_steps, start=1./all_logits.shape[-1], end=1)
        larger_than_threshold = torch.exp(-sup_loss) > tsa_thresh   # prob = exp(log_prob), prob > tsa_threshold
        # larger_than_threshold = torch.sum(  F.softmax(pred[:sup_size]) * torch.eye(num_labels)[sup_label_ids]  , dim=-1) > tsa_threshold
        loss_mask = torch.ones_like(label_ids, dtype=torch.float32) * (1 - larger_than_threshold.type(torch.float32))
        sup_loss = torch.sum(sup_loss * loss_mask, dim=-1) / torch.max(torch.sum(loss_mask, dim=-1), torch_device_one())
    else:
        sup_loss = torch.mean(sup_loss)

    #unsup loss
    with torch.no_grad():
        outputs_u = model(ori_input_ids, ori_segment_ids, ori_input_mask)
        outputs_u2 = model(aug_input_ids, aug_segment_ids, aug_input_mask)
        p = (torch.softmax(outputs_u, dim=1) + torch.softmax(outputs_u2, dim=1)) / 2
        pt = p**(1/cfg.uda_softmax_temp)
        targets_u = pt / pt.sum(dim=1, keepdim=True)
        targets_u = targets_u.detach()

    targets_u = torch.cat([targets_u, targets_u], dim=0)

    # l2
    #probs_u = torch.softmax(all_logits[sup_size:], dim=1)
    #unsup_loss = torch.mean((probs_u - targets_u)**2)

    # kl
    aug_log_prob = F.log_softmax(all_logits[sup_size:], dim=-1)
    unsup_loss = torch.mean(torch.sum(unsup_criterion(aug_log_prob, targets_u), dim=-1))

    final_loss = sup_loss + cfg.uda_coeff*unsup_loss

    return final_loss, sup_loss, unsup_loss


def get_sup_loss(model, sup_batch, unsup_batch, global_step):
    # batch
    input_ids, segment_ids, input_mask, og_label_ids, num_tokens = sup_batch
    ori_input_ids, ori_segment_ids, ori_input_mask, \
    aug_input_ids, aug_segment_ids, aug_input_mask, \
    ori_num_tokens, aug_num_tokens = unsup_batch

    # convert label ids to hot vectors
    sup_size = input_ids.size(0)
    label_ids = torch.zeros(sup_size, 2).scatter_(1, og_label_ids.cpu().view(-1,1), 1)
    label_ids = label_ids.cuda(non_blocking=True)

    # for mixup
    l = np.random.beta(cfg.alpha, cfg.alpha)
    l = max(l, 1-l)
    idx = torch.randperm(input_ids.size(0))

    if cfg.mixup == 'word':
        input_ids, c_input_ids = pad_for_word_mixup(input_ids, input_mask, num_tokens, idx)
    else:
        c_input_ids = None

    # sup loss
    sup_size = input_ids.size(0)
    hidden = model(
        input_ids=input_ids, 
        segment_ids=segment_ids, 
        input_mask=input_mask,
        output_h=True,
        mixup=cfg.mixup,
        shuffle_idx=idx,
        clone_ids=c_input_ids,
        l=l
    )
    logits = model(input_h=hidden)

    if cfg.mixup:
        label_ids = mixup_op(label_ids, l, idx)


    #sup_loss = sup_criterion(logits[:sup_size], label_ids)  # shape : train_batch_size
    sup_loss = -torch.sum(F.log_softmax(logits, dim=1) * label_ids, dim=1)

    if cfg.tsa and cfg.tsa != "none":
        tsa_thresh = get_tsa_thresh(cfg.tsa, global_step, cfg.total_steps, start=1./logits.shape[-1], end=1)
        larger_than_threshold = torch.exp(-sup_loss) > tsa_thresh   # prob = exp(log_prob), prob > tsa_threshold
        # larger_than_threshold = torch.sum(  F.softmax(pred[:sup_size]) * torch.eye(num_labels)[sup_label_ids]  , dim=-1) > tsa_threshold
        loss_mask = torch.ones_like(og_label_ids, dtype=torch.float32) * (1 - larger_than_threshold.type(torch.float32))
        sup_loss = torch.sum(sup_loss * loss_mask, dim=-1) / torch.max(torch.sum(loss_mask, dim=-1), torch_device_one())
    else:
        sup_loss = torch.mean(sup_loss)

    return sup_loss, sup_loss, sup_loss

def get_loss_mixup(model, sup_batch, unsup_batch, global_step):
    # batch
    input_ids, segment_ids, input_mask, og_label_ids, num_tokens = sup_batch
    ori_input_ids, ori_segment_ids, ori_input_mask, \
    aug_input_ids, aug_segment_ids, aug_input_mask, \
    ori_num_tokens, aug_num_tokens = unsup_batch

    # convert label ids to hot vectors
    sup_size = input_ids.size(0)
    label_ids = torch.zeros(sup_size, 2).scatter_(1, og_label_ids.cpu().view(-1,1), 1)
    label_ids = label_ids.cuda(non_blocking=True)

    input_ids = torch.cat((input_ids, aug_input_ids), dim=0)
    segment_ids = torch.cat((segment_ids, aug_segment_ids), dim=0)
    input_mask = torch.cat((input_mask, aug_input_mask), dim=0)
       

    # logits
    hidden = model(
        input_ids=input_ids, 
        segment_ids=segment_ids, 
        input_mask=input_mask,
        output_h=True
    )

    # CLS mixup
    sup_hidden = hidden[:sup_size]
    unsup_hidden = hidden[sup_size:]
    l = np.random.beta(cfg.alpha, cfg.alpha)

    sup_l = max(l, 1-l) if cfg.sup_mixup else 1
    unsup_l = max(l, 1-l) if cfg.unsup_mixup else 1

    sup_idx = torch.randperm(sup_hidden.size(0))
    sup_h_a, sup_h_b = sup_hidden, sup_hidden[sup_idx]
    sup_label_a, sup_label_b = label_ids, label_ids[sup_idx]
    mixed_sup_h = sup_l * sup_h_a + (1 - sup_l) * sup_h_b
    mixed_sup_label = sup_l * sup_label_a + (1 - sup_l) * sup_label_b

    unsup_idx = torch.randperm(unsup_hidden.size(0))
    unsup_h_a, unsup_h_b = unsup_hidden, unsup_hidden[unsup_idx]
    mixed_unsup_h = unsup_l * unsup_h_a + (1 - unsup_l) * unsup_h_b

    hidden = torch.cat([mixed_sup_h, mixed_unsup_h], dim=0)

    # continue forward pass

    logits = model(input_h=hidden)
    sup_logits = logits[:sup_size]
    unsup_logits = logits[sup_size:]

    # sup loss
    sup_loss = -torch.sum(F.log_softmax(sup_logits, dim=1) * mixed_sup_label, dim=1)
    if cfg.tsa and cfg.tsa != "none":
        tsa_thresh = get_tsa_thresh(cfg.tsa, global_step, cfg.total_steps, start=1./logits.shape[-1], end=1)
        larger_than_threshold = torch.exp(-sup_loss) > tsa_thresh   # prob = exp(log_prob), prob > tsa_threshold
        # larger_than_threshold = torch.sum(  F.softmax(pred[:sup_size]) * torch.eye(num_labels)[sup_label_ids]  , dim=-1) > tsa_threshold
        loss_mask = torch.ones_like(og_label_ids, dtype=torch.float32) * (1 - larger_than_threshold.type(torch.float32))
        sup_loss = torch.sum(sup_loss * loss_mask, dim=-1) / torch.max(torch.sum(loss_mask, dim=-1), torch_device_one())
    else:
        sup_loss = torch.mean(sup_loss)


    # unsup loss
    # ori
    with torch.no_grad():
        ori_logits = model(ori_input_ids, ori_segment_ids, ori_input_mask)
        ori_prob   = F.softmax(ori_logits, dim=-1)    # KLdiv target
        # temp control
        ori_prob = ori_prob**(1/cfg.uda_softmax_temp)
        ori_prob = ori_prob / ori_prob.sum(dim=1, keepdim=True)

        ori_prob_a, ori_prob_b = ori_prob, ori_prob[unsup_idx]
        mixed_ori_prob = unsup_l * ori_prob_a + (1-unsup_l) * ori_prob_b
                    
    # aug
    aug_log_prob = F.log_softmax(unsup_logits, dim=-1)

    unsup_loss = torch.mean(torch.sum(unsup_criterion(aug_log_prob, mixed_ori_prob), dim=-1))
    final_loss = sup_loss + cfg.uda_coeff*unsup_loss

    return final_loss, sup_loss, unsup_loss



def get_mixmatch_loss_two(model, sup_batch, unsup_batch, global_step):
    input_ids, segment_ids, input_mask, label_ids = sup_batch
    if unsup_batch:
        ori_input_ids, ori_segment_ids, ori_input_mask, \
        aug_input_ids, aug_segment_ids, aug_input_mask  = unsup_batch

    batch_size = input_ids.shape[0]
    sup_size = label_ids.shape[0]

    with torch.no_grad():
        # compute guessed labels of unlabel samples
        outputs_u = model(input_ids=ori_input_ids, segment_ids=ori_segment_ids, input_mask=ori_input_mask)
        outputs_u2 = model(input_ids=aug_input_ids, segment_ids=aug_segment_ids, input_mask=aug_input_mask)
        p = (torch.softmax(outputs_u, dim=1) + torch.softmax(outputs_u2, dim=1)) / 2
        pt = p**(1/cfg.uda_softmax_temp)
        targets_u = pt / pt.sum(dim=1, keepdim=True)
        targets_u = targets_u.detach()
        targets_u = torch.cat((targets_u, targets_u), dim=0)

        # confidence-based masking
        if cfg.uda_confidence_thresh != -1:
            unsup_loss_mask = torch.max(targets_u, dim=-1)[0] > cfg.uda_confidence_thresh
            unsup_loss_mask = unsup_loss_mask.type(torch.float32)
        else:
            unsup_loss_mask = torch.ones(len(logits) - sup_size, dtype=torch.float32)
        unsup_loss_mask = unsup_loss_mask.to(_get_device())

    input_ids = torch.cat((input_ids, ori_input_ids, aug_input_ids), dim=0)
    seg_ids = torch.cat((segment_ids, ori_segment_ids, aug_segment_ids), dim=0)
    input_mask = torch.cat((input_mask, ori_input_mask, aug_input_mask), dim=0)

    logits = model(input_ids, seg_ids, input_mask)

    logits_x = logits[:sup_size]
    logits_u = logits[sup_size:]

    sup_loss = sup_criterion(logits_x, label_ids)
    if cfg.tsa and cfg.tsa != "none":
        tsa_thresh = get_tsa_thresh(cfg.tsa, global_step, cfg.total_steps, start=1./logits.shape[-1], end=1)
        larger_than_threshold = torch.exp(-sup_loss) > tsa_thresh   # prob = exp(log_prob), prob > tsa_threshold
        # larger_than_threshold = torch.sum(  F.softmax(pred[:sup_size]) * torch.eye(num_labels)[sup_label_ids]  , dim=-1) > tsa_threshold
        loss_mask = torch.ones_like(label_ids, dtype=torch.float32) * (1 - larger_than_threshold.type(torch.float32))
        sup_loss = torch.sum(sup_loss * loss_mask, dim=-1) / torch.max(torch.sum(loss_mask, dim=-1), torch_device_one())
    else:
        sup_loss = torch.mean(sup_loss)

    log_probs_u = F.log_softmax(logits_u, dim=1)
    unsup_loss = torch.sum(unsup_criterion(log_probs_u, targets_u), dim=-1)
    unsup_loss = torch.sum(unsup_loss * unsup_loss_mask, dim=-1) / torch.max(torch.sum(unsup_loss_mask, dim=-1), torch_device_one())

    final_loss = sup_loss + cfg.uda_coeff*unsup_loss
    return final_loss, sup_loss, unsup_loss
        
def get_mixmatch_loss_sep(model, sup_batch, unsup_batch, global_step):
    input_ids, segment_ids, input_mask, label_ids = sup_batch
    if unsup_batch:
        ori_input_ids, ori_segment_ids, ori_input_mask, \
        aug_input_ids, aug_segment_ids, aug_input_mask  = unsup_batch

    batch_size = input_ids.shape[0]
    sup_size = input_ids.size(0)

    # Transform label to one-hot
    label_ids = torch.zeros(batch_size, 2).scatter_(1, label_ids.cpu().view(-1,1), 1).cuda()

    with torch.no_grad():
        # compute guessed labels of unlabel samples
        outputs_u = model(input_ids=ori_input_ids, segment_ids=ori_segment_ids, input_mask=ori_input_mask)
        outputs_u2 = model(input_ids=aug_input_ids, segment_ids=aug_segment_ids, input_mask=aug_input_mask)
        p = (torch.softmax(outputs_u, dim=1) + torch.softmax(outputs_u2, dim=1)) / 2
        pt = p**(1/cfg.uda_softmax_temp)
        targets_u = pt / pt.sum(dim=1, keepdim=True)
        targets_u = targets_u.detach()

    concat_input_ids = torch.cat((input_ids, ori_input_ids, aug_input_ids), dim=0)
    concat_seg_ids = torch.cat((segment_ids, ori_segment_ids, aug_segment_ids), dim=0)
    concat_input_mask = torch.cat((input_mask, ori_input_mask, aug_input_mask), dim=0)
        
    unsup_targets = torch.cat((targets_u, targets_u), dim=0)

    hidden = model(
        input_ids=concat_input_ids,
        segment_ids=concat_seg_ids,
        input_mask=concat_input_mask,
        output_h=True
    )

    sup_hidden = hidden[:sup_size]
    unsup_hidden = hidden[sup_size:]

    l = np.random.beta(cfg.alpha, cfg.alpha)

    sup_l = max(l, 1-l) if cfg.sup_mixup else 1
    unsup_l = max(l, 1-l) if cfg.unsup_mixup else 1

    sup_idx = torch.randperm(sup_hidden.size(0))
    sup_h_a, sup_h_b = sup_hidden, sup_hidden[sup_idx]
    sup_label_a, sup_label_b = label_ids, label_ids[sup_idx]
    mixed_sup_h = sup_l * sup_h_a + (1 - sup_l) * sup_h_b
    mixed_sup_label = sup_l * sup_label_a + (1 - sup_l) * sup_label_b

    unsup_idx = torch.randperm(unsup_hidden.size(0))
    unsup_h_a, unsup_h_b = unsup_hidden, unsup_hidden[unsup_idx]
    unsup_label_a, unsup_label_b = unsup_targets, unsup_targets[unsup_idx]
    mixed_unsup_h = unsup_l * unsup_h_a + (1 - unsup_l) * unsup_h_b
    mixed_unsup_label = unsup_l * unsup_label_a + (1 - unsup_l) * unsup_label_b


    hidden = torch.cat([mixed_sup_h, mixed_unsup_h], dim=0)

    logits = model(input_h=hidden)

    sup_logits = logits[:sup_size]
    unsup_logits = logits[sup_size:]

    #Lx, Lu, w = train_criterion(logits_x, targets_x, logits_u, targets_u, epoch+batch_idx/cfg.val_iteration)
    Lx, Lu, w = train_criterion(
        sup_logits, mixed_sup_label, 
        unsup_logits, mixed_unsup_label, 
        global_step, cfg.lambda_u, cfg.total_steps
    )

    final_loss = Lx + w * Lu

    return final_loss, Lx, Lu

def get_mixmatch_loss_short(model, sup_batch, unsup_batch, global_step):
    input_ids, segment_ids, input_mask, label_ids = sup_batch
    if unsup_batch:
        ori_input_ids, ori_segment_ids, ori_input_mask, \
        aug_input_ids, aug_segment_ids, aug_input_mask  = unsup_batch

    batch_size = input_ids.shape[0]
    sup_size = input_ids.size(0)

    # Transform label to one-hot
    label_ids = torch.zeros(batch_size, 2).scatter_(1, label_ids.cpu().view(-1,1), 1).cuda()

    with torch.no_grad():
        # compute guessed labels of unlabel samples
        outputs_u = model(input_ids=ori_input_ids, segment_ids=ori_segment_ids, input_mask=ori_input_mask)
        outputs_u2 = model(input_ids=aug_input_ids, segment_ids=aug_segment_ids, input_mask=aug_input_mask)
        p = (torch.softmax(outputs_u, dim=1) + torch.softmax(outputs_u2, dim=1)) / 2
        pt = p**(1/cfg.uda_softmax_temp)
        targets_u = pt / pt.sum(dim=1, keepdim=True)
        targets_u = targets_u.detach()

    concat_input_ids = torch.cat((input_ids, ori_input_ids, aug_input_ids), dim=0)
    concat_seg_ids = torch.cat((segment_ids, ori_segment_ids, aug_segment_ids), dim=0)
    concat_input_mask = torch.cat((input_mask, ori_input_mask, aug_input_mask), dim=0)
    concat_targets = torch.cat((label_ids, targets_u, targets_u), dim=0)


    hidden = model(
        input_ids=concat_input_ids,
        segment_ids=concat_seg_ids,
        input_mask=concat_input_mask,
        output_h=True
    )


    l = np.random.beta(cfg.alpha, cfg.alpha)
    l = max(l, 1-l)

    idx = torch.randperm(hidden.size(0))

    h_a, h_b = hidden, hidden[idx]
    target_a, target_b = concat_targets, concat_targets[idx]

    mixed_h = l * h_a + (1 - l) * h_b
    mixed_target = l * target_a + (1 - l) * target_b

    logits = model(input_h=mixed_h)

    logits_x = logits[:sup_size]
    logits_u = logits[sup_size:]

    targets_x = mixed_target[:sup_size]
    targets_u = mixed_target[sup_size:]

    #Lx, Lu, w = train_criterion(logits_x, targets_x, logits_u, targets_u, epoch+batch_idx/cfg.val_iteration)
    Lx, Lu, w = train_criterion(logits_x, targets_x, logits_u, targets_u, global_step, cfg.lambda_u, cfg.total_steps)

    final_loss = Lx + w * Lu

    return final_loss, Lx, Lu

def get_mixmatch_loss(model, sup_batch, unsup_batch, global_step):
    input_ids, segment_ids, input_mask, label_ids = sup_batch
    if unsup_batch:
        ori_input_ids, ori_segment_ids, ori_input_mask, \
        aug_input_ids, aug_segment_ids, aug_input_mask  = unsup_batch

    batch_size = input_ids.shape[0]

    # Transform label to one-hot
    label_ids = torch.zeros(batch_size, 2).scatter_(1, label_ids.cpu().view(-1,1), 1).cuda()

    with torch.no_grad():
        # compute guessed labels of unlabel samples
        outputs_u = model(input_ids=ori_input_ids, segment_ids=ori_segment_ids, input_mask=ori_input_mask)
        outputs_u2 = model(input_ids=aug_input_ids, segment_ids=aug_segment_ids, input_mask=aug_input_mask)
        p = (torch.softmax(outputs_u, dim=1) + torch.softmax(outputs_u2, dim=1)) / 2
        pt = p**(1/cfg.uda_softmax_temp)
        targets_u = pt / pt.sum(dim=1, keepdim=True)
        targets_u = targets_u.detach()

    concat_input_ids = [input_ids, ori_input_ids, aug_input_ids]
    concat_seg_ids = [segment_ids, ori_segment_ids, aug_segment_ids]
    concat_input_mask = [input_mask, ori_input_mask, aug_input_mask]
    concat_targets = [label_ids, targets_u, targets_u]

    # interleave labeled and unlabed samples between batches to get correct batchnorm calculation 
    int_input_ids = interleave(concat_input_ids, batch_size)
    int_seg_ids = interleave(concat_seg_ids, batch_size)
    int_input_mask = interleave(concat_input_mask, batch_size)
    int_targets = interleave(concat_targets, batch_size)

    h_zero = model(
        input_ids=int_input_ids[0],
        segment_ids=int_seg_ids[0],
        input_mask=int_input_mask[0], 
        output_h=True
    )

    h_one = model(
        input_ids=int_input_ids[1],
        segment_ids=int_seg_ids[1],
        input_mask=int_input_mask[1], 
        output_h=True
    )

    h_two = model(
        input_ids=int_input_ids[2],
        segment_ids=int_seg_ids[2],
        input_mask=int_input_mask[2], 
        output_h=True
    )

    int_h = torch.cat([h_zero, h_one, h_two], dim=0)
    int_targets = torch.cat([int_targets[0], int_targets[1], int_targets[2]])

    l = np.random.beta(cfg.alpha, cfg.alpha)
    l = max(l, 1-l)

    idx = torch.randperm(int_h.size(0))

    h_a, h_b = int_h, int_h[idx]
    target_a, target_b = int_targets, int_targets[idx]

    mixed_int_h = l * h_a + (1 - l) * h_b
    mixed_int_target = l * target_a + (1 - l) * target_b

    mixed_int_h = list(torch.split(mixed_int_h, batch_size))
    mixed_int_targets = list(torch.split(mixed_int_target, batch_size))

    logits_one = model(input_h=mixed_int_h[0])
    logits_two = model(input_h=mixed_int_h[1])
    logits_three = model(input_h=mixed_int_h[2])

    logits = [logits_one, logits_two, logits_three]


    # put interleaved samples back
    logits = interleave(logits, batch_size)
    targets = interleave(mixed_int_targets, batch_size)

    logits_x = logits[0]
    logits_u = torch.cat(logits[1:], dim=0)

    targets_x = targets[0]
    targets_u = torch.cat(targets[1:], dim=0)

    #Lx, Lu, w = train_criterion(logits_x, targets_x, logits_u, targets_u, epoch+batch_idx/cfg.val_iteration)
    Lx, Lu, w = train_criterion(logits_x, targets_x, logits_u, targets_u, global_step, cfg.lambda_u, cfg.total_steps)

    final_loss = Lx + w * Lu

    return final_loss, Lx, Lu

def get_uda_mixup_loss(model, sup_batch, unsup_batch, global_step):
    # batch
    input_ids, segment_ids, input_mask, og_label_ids = sup_batch

    # convert label_ids to hot vector
    sup_size = input_ids.size(0)
    label_ids = torch.zeros(sup_size, 2).scatter_(1, og_label_ids.cpu().view(-1,1), 1).cuda()

    if unsup_batch:
        ori_input_ids, ori_segment_ids, ori_input_mask, \
        aug_input_ids, aug_segment_ids, aug_input_mask  = unsup_batch

        input_ids = torch.cat((input_ids, aug_input_ids), dim=0)
        segment_ids = torch.cat((segment_ids, aug_segment_ids), dim=0)
        input_mask = torch.cat((input_mask, aug_input_mask), dim=0)

    # logits
    hidden = model(
        input_ids=input_ids,
        segment_ids=segment_ids,
        input_mask=input_mask,
        output_h=True
    )

    sup_hidden = hidden[:sup_size]
    unsup_hidden = hidden[sup_size:]

    l = np.random.beta(cfg.alpha, cfg.alpha)

    sup_l = max(l, 1-l) if cfg.sup_mixup else 1
    unsup_l = max(l, 1-l) if cfg.unsup_mixup else 1

    sup_idx = torch.randperm(sup_hidden.size(0))
    sup_h_a, sup_h_b = sup_hidden, sup_hidden[sup_idx]
    sup_label_a, sup_label_b = label_ids, label_ids[sup_idx]
    mixed_sup_h = sup_l * sup_h_a + (1 - sup_l) * sup_h_b
    mixed_sup_label = sup_l * sup_label_a + (1 - sup_l) * sup_label_b

    unsup_idx = torch.randperm(unsup_hidden.size(0))
    unsup_h_a, unsup_h_b = unsup_hidden, unsup_hidden[unsup_idx]
    mixed_unsup_h = unsup_l * unsup_h_a + (1 - unsup_l) * unsup_h_b

    hidden = torch.cat([mixed_sup_h, mixed_unsup_h], dim=0)

    logits = model(input_h=hidden)

    sup_logits = logits[:sup_size]
    unsup_logits = logits[sup_size:]

    # sup loss
    sup_loss = -torch.sum(F.log_softmax(sup_logits, dim=1) * mixed_sup_label, dim=1)

    if cfg.tsa and cfg.tsa != "none":
        tsa_thresh = get_tsa_thresh(cfg.tsa, global_step, cfg.total_steps, start=1./logits.shape[-1], end=1)
        larger_than_threshold = torch.exp(-sup_loss) > tsa_thresh   # prob = exp(log_prob), prob > tsa_threshold
        # larger_than_threshold = torch.sum(  F.softmax(pred[:sup_size]) * torch.eye(num_labels)[sup_label_ids]  , dim=-1) > tsa_threshold
        loss_mask = torch.ones_like(og_label_ids, dtype=torch.float32) * (1 - larger_than_threshold.type(torch.float32))
        sup_loss = torch.sum(sup_loss * loss_mask, dim=-1) / torch.max(torch.sum(loss_mask, dim=-1), torch_device_one())
    else:
        sup_loss = torch.mean(sup_loss)

    # unsup loss
    if unsup_batch:
        # ori
        with torch.no_grad():
            ori_logits = model(ori_input_ids, ori_segment_ids, ori_input_mask)
            ori_prob   = F.softmax(ori_logits, dim=-1)    # KLdiv target
            # ori_log_prob = F.log_softmax(ori_logits, dim=-1)

            ori_prob_a, ori_prob_b = ori_prob, ori_prob[unsup_idx]
            mixed_ori_prob = unsup_l * ori_prob_a + (1 - unsup_l) * ori_prob_b

            # confidence-based masking
            if cfg.uda_confidence_thresh != -1:
                unsup_loss_mask = torch.max(mixed_ori_prob, dim=-1)[0] > cfg.uda_confidence_thresh
                unsup_loss_mask = unsup_loss_mask.type(torch.float32)
            else:
                unsup_loss_mask = torch.ones(len(logits) - sup_size, dtype=torch.float32)
            unsup_loss_mask = unsup_loss_mask.to(_get_device())

        # aug
        # softmax temperature controlling
        uda_softmax_temp = cfg.uda_softmax_temp if cfg.uda_softmax_temp > 0 else 1.
        aug_log_prob = F.log_softmax(unsup_logits / uda_softmax_temp, dim=-1)

        # KLdiv loss
        """
            nn.KLDivLoss (kl_div)
            input : log_prob (log_softmax)
            target : prob    (softmax)
            https://pytorch.org/docs/stable/nn.html

            unsup_loss is divied by number of unsup_loss_mask
            it is different from the google UDA official
            The official unsup_loss is divided by total
            https://github.com/google-research/uda/blob/master/text/uda.py#L175
        """
        unsup_loss = torch.sum(unsup_criterion(aug_log_prob, mixed_ori_prob), dim=-1)
        unsup_loss = torch.sum(unsup_loss * unsup_loss_mask, dim=-1) / torch.max(torch.sum(unsup_loss_mask, dim=-1), torch_device_one())
        final_loss = sup_loss + cfg.uda_coeff*unsup_loss

        return final_loss, sup_loss, unsup_loss
    return sup_loss, None, None