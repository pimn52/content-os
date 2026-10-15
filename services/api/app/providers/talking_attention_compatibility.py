"""Retained LatentSync explicit-math compatibility; no new kernel.

Original local operator runner: run_latentsync_math.py
Original SHA256: dd3babec3331bb28f6ce93c57e50d7d5e36c579c66665f2194a540ea306fbc30
"""
import torch


def explicit_scaled_dot_product_attention(
    query,
    key,
    value,
    attn_mask=None,
    dropout_p=0.0,
    is_causal=False,
    scale=None,
    enable_gqa=False,
):
    if dropout_p:
        raise RuntimeError("Dropout is not supported by the eval-only attention fallback")
    if enable_gqa:
        key = key.repeat_interleave(query.shape[-3] // key.shape[-3], dim=-3)
        value = value.repeat_interleave(query.shape[-3] // value.shape[-3], dim=-3)
    scale_factor = scale if scale is not None else query.shape[-1] ** -0.5
    scores = query @ key.transpose(-2, -1) * scale_factor
    if is_causal:
        causal_mask = torch.ones(
            query.shape[-2], key.shape[-2], dtype=torch.bool, device=query.device
        ).tril()
        scores = scores.masked_fill(~causal_mask, float("-inf"))
    if attn_mask is not None:
        if attn_mask.dtype == torch.bool:
            scores = scores.masked_fill(~attn_mask, float("-inf"))
        else:
            scores = scores + attn_mask
    return torch.softmax(scores, dim=-1) @ value
